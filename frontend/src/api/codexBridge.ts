import { request } from "./client";

export interface CodexSettings {
  root: string;
  executable: string;
  home: string;
  version: string;
}
export interface CodexDiscussion {
  request_id: string;
  question_id: string;
  content_version: number;
  thread_id: string | null;
  status: "reserved" | "creating" | "bound" | "injecting" | "context_ready" | "ready";
  source_path: string;
  course_path: string;
  context_file: string;
  title: string;
  image_count?: number;
  uri?: string;
  created_at?: string;
  start_ms?: number;
  end_ms?: number;
  subtitle_file?: string;
  image_transfer?: { status: "paste_required" | "no_images"; image_count: number };
  draft_transfer?: { status: "pending" | "inserting" | "pasted" | "attached" | "blocked" | "unknown" };
  native_transfer?: { status: "inserting" | "pasted" | "attached" | "blocked" | "unknown"; image_count?: number; error?: string };
}
export const getCodexSettings = () => request<CodexSettings | null>("/api/codex-bridge/settings");
export const setCodexSettings = (value: { root: string; executable?: string }) =>
  request<CodexSettings>("/api/codex-bridge/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
  });
export const prepareCodexDiscussion = (questionId: string, newDiscussion = false, copyImages = false) =>
  request<CodexDiscussion>("/api/codex-bridge/discussions", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question_id: questionId, new_discussion: newDiscussion, copy_images: copyImages }),
  });
export const getCodexDiscussions = (questionId: string) =>
  request<CodexDiscussion[]>(`/api/codex-bridge/questions/${encodeURIComponent(questionId)}/discussions`);
export const openCodexDiscussion = (requestId: string) =>
  request<CodexDiscussion>(`/api/codex-bridge/requests/${encodeURIComponent(requestId)}/open`, { method: "POST" });
export const getCodexDiscussion = (requestId: string) =>
  request<CodexDiscussion>(`/api/codex-bridge/requests/${encodeURIComponent(requestId)}`);
export const copyCodexImages = (requestId: string) =>
  request<CodexDiscussion>(`/api/codex-bridge/requests/${encodeURIComponent(requestId)}/copy-images`, { method: "POST" });
export const attachCodexDiscussion = (requestId: string) =>
  request<CodexDiscussion>(`/api/codex-bridge/requests/${encodeURIComponent(requestId)}/attach`, { method: "POST" });

export interface CodexClipRange { video_id: number; start_ms: number; end_ms: number }
export interface CodexClipPreview extends CodexClipRange {
  video_title: string;
  fingerprint: string;
  subtitle_srt: string;
  subtitles: { id: number; start_ms: number; end_ms: number; text: string }[];
  images: { slide_id: number; image_hash: string; image_url: string;
    time_instances: { visible_from_ms: number; visible_until_ms: number; representative_ms: number }[] }[];
}
export const previewCodexClip = (range: CodexClipRange) =>
  request<CodexClipPreview>("/api/codex-bridge/video-clips/preview", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(range),
  });
export const prepareCodexClip = (value: CodexClipRange & {
  fingerprint: string; slide_ids: number[]; new_discussion?: boolean;
}) => request<CodexDiscussion>("/api/codex-bridge/video-clips", {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
});
export const getCodexClipDiscussions = (videoId: number) =>
  request<CodexDiscussion[]>(`/api/codex-bridge/videos/${videoId}/discussions`);
