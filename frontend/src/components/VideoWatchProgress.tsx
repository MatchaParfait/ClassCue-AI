import { useCallback, useSyncExternalStore, type CSSProperties } from "react";
import { playbackProgressKey, readPlaybackProgress, subscribePlaybackProgress } from "../playbackProgress";
import { formatTime } from "../utils";
import { useReviewTabs } from "./ReviewTabs";

export function VideoWatchProgress({ libraryId, videoId, durationMs }: {
  libraryId: string; videoId: number; durationMs: number;
}) {
  const positions = useReviewTabs()?.positions;
  const key = playbackProgressKey(libraryId, videoId);
  const subscribe = useCallback((notify: () => void) => subscribePlaybackProgress(key, positions, notify), [key, positions]);
  const snapshot = useCallback(() => readPlaybackProgress(key, positions), [key, positions]);
  const seconds = useSyncExternalStore(subscribe, snapshot);
  const duration = Number.isFinite(durationMs) && durationMs > 0 ? durationMs / 1000 : 0;
  const position = duration > 0 ? Math.min(seconds, duration) : seconds;
  const percent = duration > 0 ? position / duration * 100 : 0;
  const label = position > 0
    ? `上次看到 ${formatTime(position * 1000)}${duration > 0 ? ` / ${formatTime(durationMs)}` : "（视频时长未知）"}`
    : "未开始";

  return <div className={`video-watch-progress${percent > 0 ? " has-position" : ""}`}
    role="progressbar" aria-label="上次观看位置" aria-valuemin={0} aria-valuemax={100}
    aria-valuenow={duration > 0 ? Math.round(percent) : undefined} aria-valuetext={label} title={label}
    style={{ "--watch-progress": `${percent}%` } as CSSProperties}>
    <span className="video-watch-track" aria-hidden="true"><span className="video-watch-fill" /></span>
    {percent > 0 && <span className="video-watch-position" aria-hidden="true" />}
  </div>;
}
