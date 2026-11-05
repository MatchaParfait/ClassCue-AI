import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { getStudyProgress, saveStudyProgress, studyNavigationKey, studyProgressKey } from "../api/studyBookmarks";

export function useStudyProgress(libraryId: string | undefined, videoId: number, enabled: boolean) {
  const queryClient = useQueryClient();
  const active = enabled && Boolean(libraryId);
  const progress = useQuery({
    queryKey: studyProgressKey(libraryId, videoId),
    queryFn: () => getStudyProgress(libraryId!, videoId),
    enabled: active,
    staleTime: 0,
  });
  const [saveError, setSaveError] = useState(false);
  const pending = useRef<{ seconds: number; duration: number; observedAt: number } | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  const save = useCallback((seconds: number, duration: number, keepalive = false) => {
    if (!active || !libraryId || !Number.isFinite(seconds) || seconds < 0) return;
    const value = { seconds, duration: Number.isFinite(duration) ? Math.max(0, duration) : 0, observedAt: Date.now() };
    pending.current = value;
    void saveStudyProgress(libraryId, videoId, value.seconds, value.duration, value.observedAt, keepalive).then(result => {
      if (pending.current !== value) return;
      if (alive.current) setSaveError(false);
      queryClient.setQueryData(studyProgressKey(libraryId, videoId), result);
      void queryClient.invalidateQueries({ queryKey: studyNavigationKey(libraryId) });
    }).catch(() => { if (alive.current && pending.current === value) setSaveError(true); });
  }, [active, libraryId, videoId, queryClient]);
  const retry = () => {
    if (pending.current) save(pending.current.seconds, pending.current.duration);
    else void progress.refetch();
  };
  return {
    ready: !active || !progress.isPending,
    position: active ? progress.data?.position_ms : undefined,
    observedAt: active ? progress.data?.observed_at : undefined,
    error: active && (saveError || progress.isError),
    save, retry,
  };
}
