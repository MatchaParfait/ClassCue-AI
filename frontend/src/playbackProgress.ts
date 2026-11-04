export const PLAYBACK_PROGRESS_EVENT = "classroom-playback-progress";

export function playbackProgressKey(libraryId: string, videoId: number): string {
  return `classroom-study-progress:${libraryId}:${videoId}`;
}

export function readPlaybackProgress(key: string, positions?: Map<string, number>): number {
  const session = positions?.get(key);
  if (session !== undefined && Number.isFinite(session) && session >= 0) return session;
  try {
    const seconds = Number(localStorage.getItem(key));
    return Number.isFinite(seconds) && seconds >= 0 ? seconds : 0;
  } catch {
    return 0;
  }
}

export function savePlaybackProgress(key: string, seconds: number, positions?: Map<string, number>): void {
  if (!Number.isFinite(seconds) || seconds < 0) return;
  positions?.set(key, seconds);
  try { localStorage.setItem(key, String(seconds)); } catch { /* Session playback still works without storage. */ }
  window.dispatchEvent(new CustomEvent(PLAYBACK_PROGRESS_EVENT, { detail: { key } }));
}

export function subscribePlaybackProgress(key: string, positions: Map<string, number> | undefined, notify: () => void): () => void {
  const changed = (event: Event) => {
    if ((event as CustomEvent<{ key: string }>).detail?.key === key) notify();
  };
  const stored = (event: StorageEvent) => {
    if (event.storageArea && event.storageArea !== window.localStorage) return;
    if (event.key !== null && event.key !== key) return;
    positions?.delete(key);
    notify();
  };
  window.addEventListener(PLAYBACK_PROGRESS_EVENT, changed);
  window.addEventListener("storage", stored);
  return () => {
    window.removeEventListener(PLAYBACK_PROGRESS_EVENT, changed);
    window.removeEventListener("storage", stored);
  };
}
