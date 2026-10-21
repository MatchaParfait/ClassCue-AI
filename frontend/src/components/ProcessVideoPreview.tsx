import { AlertCircle, RotateCcw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { getStreamUrl, getThumbnailUrl } from "../api/client";
import type { Video } from "../types";

export function ProcessVideoPreview({ video, active }: { video: Video; active: boolean }) {
  const player = useRef<HTMLVideoElement>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!active) player.current?.pause();
  }, [active]);
  useEffect(() => {
    const element = player.current;
    const pauseWhenHidden = () => { if (document.hidden) element?.pause(); };
    document.addEventListener("visibilitychange", pauseWhenHidden);
    return () => {
      document.removeEventListener("visibilitychange", pauseWhenHidden);
      element?.pause();
    };
  }, []);
  return <div className="process-selected-preview">
    <video ref={player} className="process-preview-player" controls playsInline preload="metadata"
      aria-label={`视频预览：${video.filename}`} src={getStreamUrl(video.id)} poster={getThumbnailUrl(video)}
      onError={() => setFailed(true)} onLoadedData={() => setFailed(false)}>
      你的浏览器不支持视频播放。
    </video>
    {failed && <div className="field-error process-preview-error" role="alert">
      <AlertCircle size={15} /><span>视频暂时无法播放，请检查文件或重试。</span>
      <button className="text-button" type="button" onClick={() => {
        setFailed(false);
        player.current?.load();
      }}><RotateCcw size={14} />重新加载视频</button>
    </div>}
  </div>;
}
