import { request } from "./client";
import type { SubtitleSearchResult } from "../types";

export interface SemanticCoverage {
  video_count: number; with_subtitles: number; missing_subtitles: number;
  indexed_videos: number; pending_videos: number; chunk_count: number;
}
export interface SemanticStatus {
  library_id: string; model_id: string; ready: boolean; device: "cpu" | "cuda";
  dimensions: number; coverage: SemanticCoverage; busy: boolean; active_job_id: string | null;
  weights_ready?: boolean;
  runtimes?: { cpu: { ready: boolean }; cuda: { ready: boolean } };
  gpu_detected?: boolean;
  gpu_unavailable_reason?: string;
  download_estimates?: {
    weights_bytes: number;
    runtimes: { cpu: number; cuda: number };
    total_bytes: { cpu: number; cuda: number };
  };
  cache_present?: boolean;
}
export interface SemanticCourse {
  course_id: string; name: string; path: string; video_count: number; with_subtitles: number;
}
export type SemanticCleanupTarget = "cpu" | "cuda" | "weights" | "all";
export interface SemanticCacheItem { present: boolean; ready: boolean; bytes: number; files: number }
export interface SemanticCache {
  weights: SemanticCacheItem;
  runtimes: { cpu: SemanticCacheItem; cuda: SemanticCacheItem };
}
export const getSemanticCache = () => request<SemanticCache>("/api/semantic/cache");
export async function getSemanticCourses() {
  const items: SemanticCourse[] = [];
  let offset = 0;
  while (true) {
    const page = await request<{ items: SemanticCourse[]; has_more: boolean; next_offset: number | null }>(
      `/api/semantic/courses?offset=${offset}`);
    items.push(...page.items);
    if (!page.has_more || page.next_offset === null || page.next_offset <= offset) return items;
    offset = page.next_offset;
  }
}
export function getSemanticStatus(courseId?: string) {
  return request<SemanticStatus>("/api/semantic/status"
    + (courseId ? `?course_id=${encodeURIComponent(courseId)}` : ""));
}
function post<T>(path: string, body: unknown, signal?: AbortSignal) {
  return request<T>(`/api/semantic/${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal,
  });
}
export const prepareSemanticModel = (device: "cpu" | "cuda") => post<{ job_id: string }>("prepare", { device });
export const buildSemanticIndex = (courseId: string) => post<{ job_id: string }>("index", { course_id: courseId });
export const setupSemanticSearch = (courseId: string, device: "cpu" | "cuda") =>
  post<{ job_id: string }>("setup", { course_id: courseId, device });
export const cleanupSemanticSearch = (target: SemanticCleanupTarget = "all", keepDevice?: "cpu" | "cuda") =>
  post<{ job_id: string }>("cleanup", { target, ...(keepDevice ? { keep_device: keepDevice } : {}) });
interface SemanticResult {
  library_id: string; total: number; total_videos: number; has_more: boolean;
  next_offset: number | null; snapshot_id: string; semantic_coverage: SemanticCoverage;
  items: {
    result_id: string; video_id: number; video_name: string; folder_id: number | null; folder_path: string;
    duration_seconds: number;
    start_seconds: number; end_seconds: number; evidence: { record_id: number; text: string }[];
  }[];
}
export async function searchSemanticSubtitles(query: string, offset: number, signal: AbortSignal,
  folderId?: number, snapshotId?: string) {
  const result = await post<SemanticResult>("search", {
    semantic_query: query, sources: ["subtitles"], offset, limit: 20,
    ...(folderId !== undefined ? { course_id: `folder:${folderId}` } : {}),
    ...(snapshotId ? { snapshot_id: snapshotId } : {}),
  }, signal);
  const counts = new Map<number, number>();
  for (const item of result.items) counts.set(item.video_id, (counts.get(item.video_id) ?? 0) + 1);
  const converted: SubtitleSearchResult & { snapshot_id: string; semantic_coverage: SemanticCoverage } = {
    ...result, query, offset, limit: 20,
    items: result.items.map(item => {
      const text = item.evidence.map(evidence => evidence.text).join("\n");
      return {
        result_id: item.result_id, video_id: item.video_id, video_name: item.video_name, filename: item.video_name,
        video_status: "", duration_ms: Math.round(item.duration_seconds * 1000),
        folder_id: item.folder_id, folder_name: item.folder_path || null,
        video_match_count: counts.get(item.video_id) ?? 1, segment_id: item.evidence[0].record_id,
        start_ms: Math.round(item.start_seconds * 1000), end_ms: Math.round(item.end_seconds * 1000),
        text, snippet: text, snippet_parts: [{ text, match: false }],
      };
    }),
  };
  return converted;
}
