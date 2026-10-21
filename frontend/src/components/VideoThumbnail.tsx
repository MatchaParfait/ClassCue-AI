import { FileVideo } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { getThumbnailUrl } from "../api/client";
import { getStartupThumbnail } from "../startupTransition";
import type { Video } from "../types";

export function VideoThumbnail({ video }: { video: Video }) {
  const url = getThumbnailUrl(video);
  return <ThumbnailImage key={url} video={video} url={url} />;
}

function ThumbnailImage({ video, url }: { video: Video; url: string }) {
  const [preview, setPreview] = useState(() => getStartupThumbnail(video.library_id, video.id));
  const [failed, setFailed] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const loaded = async (image: HTMLImageElement) => {
    await image.decode?.().catch(() => {});
    if (mounted.current && image.isConnected) setPreview(null);
  };
  return (
    <div className="library-card-icon video-icon video-thumbnail">
      <FileVideo size={21} aria-hidden="true" />
      {preview && (
        <img src={preview} alt="" aria-hidden="true" decoding="async" draggable={false}
          onError={() => setPreview(null)} />
      )}
      {!failed && (
        <img src={url} alt={`${video.display_name}的第 24 帧截图`} loading="lazy" decoding="async"
          draggable={false} style={preview ? { opacity: 0 } : undefined}
          onLoad={event => { if (preview) void loaded(event.currentTarget); }} onError={() => setFailed(true)} />
      )}
    </div>
  );
}
