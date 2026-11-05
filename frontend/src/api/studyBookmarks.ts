import { request } from "./client";

export interface StudyPath { id: number; name: string }
export interface StudyProgress {
  video_id: number;
  position_ms: number;
  duration_ms: number;
  observed_at: number;
}
export interface StudyVideo {
  video_id: number;
  display_name: string;
  filename: string;
  folder_id: number | null;
  position_ms: number | null;
  duration_ms: number;
  observed_at: number | null;
  bookmark_count: number;
  bookmark_note: string | null;
  completed: boolean;
  path: StudyPath[];
}
export interface StudyFolder {
  folder_id: number;
  bookmark_count: number;
  latest: StudyVideo | null;
}
export interface StudyNavigation {
  library_id: string;
  videos: StudyVideo[];
  folders: StudyFolder[];
  latest: StudyVideo | null;
}
export interface StudyBookmark {
  id: number;
  video_id: number;
  position_ms: number | null;
  note: string;
  display_name: string;
  filename: string;
  path: StudyPath[];
  status?: string;
  has_transcript?: boolean;
}

export const studyNavigationKey = (libraryId?: string) => ["study-navigation", libraryId] as const;
export const studyBookmarksKey = (libraryId: string) => ["study-bookmarks", libraryId] as const;
export const studyProgressKey = (libraryId: string | undefined, videoId: number) => ["study-progress", libraryId, videoId] as const;
const json = (value: unknown) => ({ headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });

export function getStudyNavigation(): Promise<StudyNavigation> {
  return request("/api/study-navigation");
}
export function getStudyProgress(libraryId: string, videoId: number): Promise<StudyProgress | null> {
  return request(`/api/videos/${videoId}/study-progress?${new URLSearchParams({ library_id: libraryId })}`);
}
export function saveStudyProgress(libraryId: string, videoId: number, seconds: number, duration: number, observedAt: number, keepalive = false): Promise<StudyProgress> {
  return request(`/api/videos/${videoId}/study-progress`, {
    method: "PUT", keepalive, ...json({ library_id: libraryId, position_ms: Math.round(seconds * 1000),
      duration_ms: Math.round(duration * 1000), observed_at: observedAt }),
  });
}
export function getStudyBookmarks(libraryId: string, scope: { folderId?: number; videoId?: number }): Promise<StudyBookmark[]> {
  const params = new URLSearchParams({ library_id: libraryId });
  if (scope.folderId !== undefined) params.set("folder_id", String(scope.folderId));
  if (scope.videoId !== undefined) params.set("video_id", String(scope.videoId));
  return request(`/api/study-bookmarks?${params}`);
}
export function createStudyBookmark(libraryId: string, videoId: number, position: number | null, note: string): Promise<StudyBookmark> {
  return request(`/api/videos/${videoId}/bookmarks`, { method: "POST", ...json({ library_id: libraryId, position_ms: position, note }) });
}
export function editStudyBookmark(libraryId: string, id: number, note: string): Promise<StudyBookmark> {
  return request(`/api/study-bookmarks/${id}`, { method: "PATCH", ...json({ library_id: libraryId, note }) });
}
export function deleteStudyBookmark(libraryId: string, id: number): Promise<{ deleted: boolean }> {
  return request(`/api/study-bookmarks/${id}`, { method: "DELETE", ...json({ library_id: libraryId }) });
}
export function studyVideoUrl(libraryId: string, videoId: number, positionMs: number | null) {
  const params = new URLSearchParams({ library_id: libraryId });
  if (positionMs !== null) params.set("start_ms", String(positionMs));
  return `/review/${videoId}?${params}`;
}
