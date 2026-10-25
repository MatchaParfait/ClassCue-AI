export interface CapturedFrame { blob: Blob; captured_at_ms: number }

export async function captureVideoFrame(video: HTMLVideoElement | null): Promise<CapturedFrame> {
  if (!video || video.readyState < 2 || video.seeking || !video.videoWidth || !video.videoHeight
    || !Number.isFinite(video.currentTime)) {
    throw new Error("当前画面尚未就绪，请等待视频加载或定位完成后再截图");
  }
  if (video.videoWidth * video.videoHeight > 16_000_000) throw new Error("视频画面超过 1600 万像素，暂不能作为题图");
  video.pause();
  const captured_at_ms = Math.round(video.currentTime * 1000);
  const canvas = document.createElement("canvas");
  canvas.width = video.videoWidth; canvas.height = video.videoHeight;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("当前环境无法截取视频画面");
  try {
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(value => value ? resolve(value)
      : reject(new Error("截图生成失败，请重试")), "image/png"));
    if (blob.size > 6 * 1024 * 1024) throw new Error("截图超过 6 MB，暂不能作为题图");
    return { blob, captured_at_ms };
  } catch (error) {
    if (error instanceof DOMException && error.name === "SecurityError") throw new Error("当前视频不允许读取画面，请使用本地课堂视频");
    throw error;
  } finally { canvas.width = 0; canvas.height = 0; }
}
