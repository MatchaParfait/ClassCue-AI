import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { createManualQuestion, getMarkers, getSubtitles, getVideo, renameManualQuestion, updateMarkerImportance, updateMarkerJumpTime, updateMarkerStatus, updateQuestionEmphasis } from "../api/client";
import { MOCK_MARKERS, MOCK_SUBTITLES, MOCK_VIDEO } from "../data/mockData";
import type { Marker, MarkerImportance, ReviewStatus } from "../types";
import { formatTime } from "../utils";

export const studyKeys = {
  video: (videoId: number) => ["study", "video", videoId] as const,
  subtitles: (videoId: number) => ["study", "subtitles", videoId] as const,
  markers: (videoId: number) => ["study", "markers", videoId] as const,
};

export function useStudyData(videoId: number, demo = false) {
  const queryClient = useQueryClient();
  const video = useQuery({
    queryKey: [...studyKeys.video(videoId), demo ? "demo" : "api"],
    queryFn: () => demo ? { value: { ...MOCK_VIDEO, id: videoId }, source: "mock" as const } : getVideo(videoId),
    staleTime: 60_000,
    refetchOnMount: demo ? false : "always",
    refetchOnWindowFocus: demo ? false : query =>
      query.state.data?.value.status !== "review_ready" ? "always" : false,
    refetchInterval: demo ? false : query =>
      query.state.data && query.state.data.value.status !== "review_ready" ? 2500 : false,
    refetchIntervalInBackground: false,
  });
  const subtitles = useQuery({
    queryKey: [...studyKeys.subtitles(videoId), demo ? "demo" : "api"],
    queryFn: () => demo ? { value: MOCK_SUBTITLES, source: "mock" as const } : getSubtitles(videoId),
    staleTime: 60_000,
  });
  const markers = useQuery({
    queryKey: [...studyKeys.markers(videoId), demo ? "demo" : "api"],
    queryFn: () => demo
      ? { value: MOCK_MARKERS.map((marker) => ({ ...marker, video_id: videoId })), source: "mock" as const }
      : getMarkers(videoId),
    staleTime: 30_000,
  });
  const previousVideo = useRef({ id: videoId, status: video.data?.value.status });
  useEffect(() => {
    const previous = previousVideo.current;
    const status = video.data?.value.status;
    previousVideo.current = { id: videoId, status };
    if (demo || previous.id !== videoId || !previous.status
      || previous.status === "review_ready" || status !== "review_ready") return;
    // Completion may arrive while the sidebar already has markers and no task observer.
    for (const queryKey of [
      studyKeys.subtitles(videoId), studyKeys.markers(videoId),
      ["study", "analysis-runs", videoId], ["videos"], ["library"], ["library-status"],
    ]) void queryClient.invalidateQueries({ queryKey });
  }, [video.data?.value.status, videoId, demo, queryClient]);

  return {
    video: video.data?.value,
    subtitles: subtitles.data?.value ?? [],
    markers: markers.data?.value ?? [],
    source: {
      video: video.data?.source ?? "api",
      subtitles: subtitles.data?.source ?? "api",
      markers: markers.data?.source ?? "api",
    },
    isLoading: video.isLoading || subtitles.isLoading || markers.isLoading,
    isError: video.isError || subtitles.isError || markers.isError,
    errors: {
      video: video.error,
      subtitles: subtitles.error,
      markers: markers.error,
    },
    error: video.error ?? subtitles.error ?? markers.error,
  };
}

export function useMarkerStatusMutation(videoId: number, demo = false) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ marker, status }: { marker: Marker; status: ReviewStatus }) => {
      const saved = demo ? null : await updateMarkerStatus(marker.id, status);
      return { ...marker, ...saved, review_status: status };
    },
    onSuccess: (updated) => {
      queryClient.setQueryData<{ value: Marker[]; source: "api" | "mock" }>(
        [...studyKeys.markers(videoId), demo ? "demo" : "api"],
        (current) => current ? { ...current, value: current.value.map((marker) => marker.id === updated.id ? { ...marker, review_status: updated.review_status } : marker) } : current,
      );
      if (!demo) {
        const cached = queryClient.getQueryData<{ value: Marker[] }>([...studyKeys.markers(videoId), "api"]);
        if (updated.analysis_mode !== "question_extraction"
          && cached?.value.some(marker => marker.analysis_mode === "question_extraction")) {
          void queryClient.invalidateQueries({ queryKey: studyKeys.markers(videoId) });
        }
        void queryClient.invalidateQueries({ queryKey: ["videos"] });
        void queryClient.invalidateQueries({ queryKey: ["library"] });
      }
    },
  });
}

export function useQuestionEmphasisMutation(videoId: number, demo = false) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ marker, emphasized }: { marker: Marker; emphasized: boolean | null }) => {
      if (!demo) return updateQuestionEmphasis(marker.id, emphasized);
      const cached = queryClient.getQueryData<{ value: Marker[] }>([...studyKeys.markers(videoId), "demo"]);
      return { ...marker, question_emphasis_override: emphasized,
        question_emphasized: emphasized ?? (cached?.value ?? []).some(point =>
          point.analysis_mode !== "question_extraction" && point.review_status !== "excluded"
          && point.start_ms <= marker.end_ms + 60_000 && point.end_ms >= marker.start_ms - 60_000) };
    },
    onSuccess: (updated) => {
      queryClient.setQueryData<{ value: Marker[]; source: "api" | "mock" }>(
        [...studyKeys.markers(videoId), demo ? "demo" : "api"],
        (current) => current ? {
          ...current,
          value: current.value.map((marker) => marker.id === updated.id ? {
            ...marker, question_emphasized: updated.question_emphasized,
            question_emphasis_override: updated.question_emphasis_override == null ? null : Boolean(updated.question_emphasis_override),
          } : marker),
        } : current,
      );
    },
  });
}

export function useCreateManualQuestionMutation(videoId: number, demo = false) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (timeMs: number): Promise<Marker> => {
      if (!demo) return createManualQuestion(videoId, timeMs);
      const cached = queryClient.getQueryData<{ value: Marker[] }>([...studyKeys.markers(videoId), "demo"]);
      return {
        id: Math.max(0, ...(cached?.value ?? []).map(marker => marker.id)) + 1,
        video_id: videoId, start_ms: timeMs, end_ms: timeMs + 1,
        title: `手动题目 · ${formatTime(timeMs)}`, note: "", source_quote: "",
        trigger_phrase: `MANUAL-question:${crypto.randomUUID()}`,
        analysis_mode: "question_extraction", review_status: "active",
        question_emphasized: (cached?.value ?? []).some(marker => marker.analysis_mode !== "question_extraction"
          && marker.review_status !== "excluded" && marker.start_ms <= timeMs + 60_001 && marker.end_ms >= timeMs - 60_000),
      };
    },
    onSuccess: (created) => {
      queryClient.setQueryData<{ value: Marker[]; source: "api" | "mock" }>(
        [...studyKeys.markers(videoId), demo ? "demo" : "api"],
        current => ({
          value: [...(current?.value ?? []).filter(marker => marker.id !== created.id), created],
          source: current?.source ?? (demo ? "mock" : "api"),
        }),
      );
      if (!demo) {
        void queryClient.invalidateQueries({ queryKey: ["videos"] });
        void queryClient.invalidateQueries({ queryKey: ["library"] });
      }
    },
  });
}

export function useRenameManualQuestionMutation(videoId: number, demo = false) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ marker, title }: { marker: Marker; title: string }) =>
      demo ? { ...marker, title } : renameManualQuestion(marker.id, title),
    onSuccess: (updated) => {
      queryClient.setQueryData<{ value: Marker[]; source: "api" | "mock" }>(
        [...studyKeys.markers(videoId), demo ? "demo" : "api"],
        current => current ? {
          ...current, value: current.value.map(marker => marker.id === updated.id ? { ...marker, title: updated.title } : marker),
        } : current,
      );
    },
  });
}

export function useMarkerImportanceMutation(videoId: number, demo = false) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ marker, importance }: { marker: Marker; importance: MarkerImportance }) =>
      demo ? { ...marker, importance } : updateMarkerImportance(marker.id, importance),
    onSuccess: (updated) => {
      queryClient.setQueryData<{ value: Marker[]; source: "api" | "mock" }>(
        [...studyKeys.markers(videoId), demo ? "demo" : "api"],
        (current) => current ? {
          ...current,
          value: current.value.map((marker) => marker.id === updated.id ? { ...marker, importance: updated.importance } : marker),
        } : current,
      );
      if (!demo) {
        void queryClient.invalidateQueries({ queryKey: ["videos"] });
        void queryClient.invalidateQueries({ queryKey: ["library"] });
      }
    },
  });
}

export function useMarkerJumpTimeMutation(videoId: number, demo = false) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ marker, userStartMs }: { marker: Marker; userStartMs: number | null }) =>
      demo ? { ...marker, user_start_ms: userStartMs, user_start_updated_at: userStartMs === null ? null : new Date().toISOString() }
        : updateMarkerJumpTime(marker.id, userStartMs),
    onSuccess: (updated) => {
      queryClient.setQueryData<{ value: Marker[]; source: "api" | "mock" }>(
        [...studyKeys.markers(videoId), demo ? "demo" : "api"],
        (current) => current ? {
          ...current,
          value: current.value.map((marker) => marker.id === updated.id ? {
            ...marker, user_start_ms: updated.user_start_ms,
            user_start_updated_at: updated.user_start_updated_at,
          } : marker),
        } : current,
      );
    },
  });
}
