import type { Marker, ReviewFilter } from "./types";

export function canOpenReview(status: string, hasTranscript: boolean): boolean {
  return status === "review_ready" || (hasTranscript && status !== "processing");
}

export function formatTime(milliseconds: number): string {
  const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
    : `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function markerJumpTime(marker: Marker): number {
  return marker.user_start_ms ?? marker.start_ms;
}

export function captureJumpTime(milliseconds: number, durationMs: number): number {
  const clamped = Math.max(0, durationMs > 0 ? Math.min(durationMs, milliseconds) : milliseconds);
  return Math.floor(clamped / 1000) * 1000;
}

export function parseTimeInput(value: string): number | null {
  const text = value.trim();
  if (!/^\d+:[0-5]\d(?::[0-5]\d)?$/.test(text)) return null;
  const parts = text.split(":").map(Number);
  const seconds = parts.length === 3
    ? parts[0] * 3600 + parts[1] * 60 + parts[2]
    : parts[0] * 60 + parts[1];
  const milliseconds = seconds * 1000;
  return Number.isSafeInteger(milliseconds) ? milliseconds : null;
}

export function filterMarkers(
  markers: Marker[], mode: Marker["analysis_mode"], status: ReviewFilter,
  viewedMarkerIds: ReadonlySet<number>,
): Marker[] {
  return markers
    .filter((marker) => marker.analysis_mode === mode)
    .filter((marker) => marker.review_status !== "excluded")
    .filter((marker) => status === "all" || viewedMarkerIds.has(marker.id) === (status === "viewed"))
    .sort((left, right) => left.start_ms - right.start_ms || left.id - right.id);
}

export function activeSubtitleId(subtitles: { id: number; start_ms: number; end_ms: number }[], currentTimeMs: number): number | null {
  return subtitles.find((subtitle) => currentTimeMs >= subtitle.start_ms && currentTimeMs < subtitle.end_ms)?.id ?? null;
}
