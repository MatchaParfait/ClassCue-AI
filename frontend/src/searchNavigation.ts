export interface SearchSeek { token: string; seconds: number }

// Scope the URL to its source library so coincidentally identical video IDs
// in a different installation never consume an old search timestamp.
export function parseSearchSeek(search: string, token: string, libraryId: string): SearchSeek | undefined {
  const params = new URLSearchParams(search);
  const raw = params.get("start_ms");
  if (!raw || !/^\d+$/.test(raw) || params.get("library_id") !== libraryId) return;
  const milliseconds = Number(raw);
  if (!Number.isSafeInteger(milliseconds)) return;
  return { token, seconds: milliseconds / 1000 };
}
