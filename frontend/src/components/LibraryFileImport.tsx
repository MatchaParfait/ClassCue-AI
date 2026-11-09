import { AlertCircle, CheckCircle2, LoaderCircle, RotateCcw, Upload, X } from "lucide-react";
import { useEffect, useRef, useState, type DragEvent, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { importVideoPath, uploadVideo } from "../api/client";
import { useDesktopAPI } from "../hooks/useDesktopAPI";

const VIDEO_EXTENSION = /\.(mp4|mkv|mov|avi|webm|m4v|ts|flv)$/i;
const MAX_VIDEO_BYTES = 1024 * 1024 * 1024;
type ImportResult = { file: File; message: string; retryable: boolean };
type ImportFile = File & { localPath?: string };

function hasFiles(event: DragEvent) {
  return Array.from(event.dataTransfer.types ?? []).includes("Files");
}

export function LibraryFileImport({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  const queryClient = useQueryClient();
  const desktop = useDesktopAPI();
  const nativeDrop = useRef<string | null>(null);
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const importRef = useRef<(files: ImportFile[]) => void>();
  const [hovered, setHovered] = useState(false);
  const [progress, setProgress] = useState<{ current: number; total: number; name: string; percent?: number; local?: boolean } | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const [issues, setIssues] = useState<ImportResult[]>([]);
  const busy = useRef(false);
  const mounted = useRef(true);
  const upload = useRef<AbortController | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; upload.current?.abort(); };
  }, []);
  useEffect(() => {
    if (!enabled) setHovered(false);
    const reset = () => setHovered(false);
    window.addEventListener("dragend", reset);
    window.addEventListener("drop", reset);
    window.addEventListener("blur", reset);
    return () => {
      window.removeEventListener("dragend", reset);
      window.removeEventListener("drop", reset);
      window.removeEventListener("blur", reset);
    };
  }, [enabled]);

  useEffect(() => {
    if (!desktop?.bind_video_drop) return;
    let disposed = false;
    let binding: string | null = null;
    const receive = (event: Event) => {
      const detail = (event as CustomEvent<{ token: string; files: { name: string; path?: string }[] }>).detail;
      if (!enabledRef.current || disposed || detail.token !== nativeDrop.current || busy.current) return;
      setHovered(false);
      if (!detail.files.length || detail.files.some(file => !file.path)) {
        setSummary("未导入视频");
        setIssues([{ file: new File([], "拖入的项目"), message: "未取得本机文件路径，请使用本机视频导入入口", retryable: false }]);
        return;
      }
      importRef.current?.(detail.files.map(file => Object.assign(
        new File(["local"], file.name), { localPath: file.path },
      )));
    };
    window.addEventListener("classcue-video-drop", receive);
    void desktop.bind_video_drop().then(token => {
      binding = token;
      if (disposed) void desktop.unbind_video_drop?.(token).catch(() => {});
      else nativeDrop.current = token;
    }).catch(() => { nativeDrop.current = null; });
    return () => {
      disposed = true;
      nativeDrop.current = null;
      window.removeEventListener("classcue-video-drop", receive);
      if (binding) void desktop.unbind_video_drop?.(binding).catch(() => {});
    };
  }, [desktop]);

  const importFiles = async (files: ImportFile[]) => {
    if (busy.current) return;
    busy.current = true;
    setSummary(null);
    setIssues([]);
    const failures: ImportResult[] = [];
    let imported = 0, existing = 0;
    const controller = new AbortController();
    upload.current = controller;
    try {
      for (const [index, file] of files.entries()) {
        if (!mounted.current) break;
        if (controller.signal.aborted) {
          failures.push(...files.slice(index).map(file => ({ file, message: "已停止，尚未开始上传", retryable: true })));
          break;
        }
        let invalid = "";
        if (!VIDEO_EXTENSION.test(file.name)) invalid = "不支持的格式";
        else if (file.size === 0) invalid = "文件为空";
        else if (file.size > MAX_VIDEO_BYTES) invalid = "文件超过 1GB";
        if (invalid) {
          failures.push({ file, message: invalid, retryable: false });
          continue;
        }
        setProgress({ current: index + 1, total: files.length, name: file.name, local: Boolean(file.localPath) });
        try {
          const result = file.localPath ? await importVideoPath(file.localPath, controller.signal) : await uploadVideo(file, {
            signal: controller.signal,
            onProgress: (loaded, total) => {
              if (mounted.current && !controller.signal.aborted) {
                setProgress({ current: index + 1, total: files.length, name: file.name,
                  percent: Math.min(100, Math.floor(loaded / Math.max(1, total) * 100)) });
              }
            },
          });
          if (result.existed) existing++;
          else imported++;
          if (result.video.storage_warning) failures.push({ file, message: result.video.storage_warning, retryable: false });
        } catch (error) {
          failures.push({ file, message: file.localPath && controller.signal.aborted
            ? "已停止等待导入结果；请刷新视频库核对后再重试"
            : error instanceof Error ? error.message : "导入失败", retryable: true });
        }
      }
      if (!mounted.current) return;
      if (imported || existing || failures.length) {
        void queryClient.invalidateQueries({ queryKey: ["videos"] });
        void queryClient.invalidateQueries({ queryKey: ["library"] });
        void queryClient.invalidateQueries({ queryKey: ["library-status"] });
      }
      setSummary([
        imported ? `已导入 ${imported} 个视频` : "",
        existing ? `${existing} 个视频已在库中` : "",
      ].filter(Boolean).join("，") || "未确认导入成功");
      setIssues(failures);
    } finally {
      busy.current = false;
      upload.current = null;
      if (mounted.current) setProgress(null);
    }
  };
  importRef.current = files => { void importFiles(files); };

  const acceptDrag = (event: DragEvent<HTMLDivElement>) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = enabled && !busy.current ? "copy" : "none";
    setHovered(enabled);
  };

  return <div id="classcue-video-import" className={`library-file-import${hovered ? " is-file-hovered" : ""}`}
    onDragEnterCapture={acceptDrag} onDragOverCapture={acceptDrag}
    onDragLeaveCapture={(event) => {
      if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) return;
      setHovered(false);
    }}
    onDropCapture={(event) => {
      if (!hasFiles(event)) return;
      event.preventDefault();
      if (window.pywebview?.api) {
        setHovered(false);
        if (enabled && !busy.current && !nativeDrop.current) {
          setSummary("未导入视频");
          setIssues([{ file: new File([], "拖入的项目"), message: "本机拖入尚未就绪，请稍后重试", retryable: false }]);
        }
        return;
      }
      event.stopPropagation();
      setHovered(false);
      if (!enabled || busy.current) return;
      const files = Array.from(event.dataTransfer.files);
      if (files.length) void importFiles(files);
      else { setSummary("未导入视频"); setIssues([{ file: new File([], "拖入的项目"), message: "请拖入视频文件，不支持文件夹", retryable: false }]); }
    }}>
    {enabled && (progress || summary) && <div className={`library-import-feedback${issues.length ? " has-issues" : ""}`} role={issues.length ? "alert" : "status"} aria-live="polite" aria-busy={Boolean(progress)}>
      <div className="library-import-status">
        {progress ? <LoaderCircle className="spin" size={18} /> : issues.length ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}
        <span>{progress ? `正在导入 ${progress.current}/${progress.total}：${progress.name}${
          progress.local ? " · 正在校验入库…" : progress.percent === 100 ? " · 已上传，正在校验入库…" : progress.percent !== undefined ? ` · 上传 ${progress.percent}%` : " · 正在连接…"
        }` : summary}</span>
        {progress && <button className="secondary-button compact" type="button" onClick={() => upload.current?.abort()}>
          <X size={14} />停止等待</button>}
        {!progress && issues.some((issue) => issue.retryable) && <button className="secondary-button compact" type="button"
          onClick={() => void importFiles(issues.filter((issue) => issue.retryable).map((issue) => issue.file))}><RotateCcw size={14} />重试失败项</button>}
        {!progress && <button className="icon-button" type="button" title="关闭导入结果" aria-label="关闭导入结果"
          onClick={() => { setSummary(null); setIssues([]); }}><X size={16} /></button>}
      </div>
      {issues.length > 0 && <ul className="library-import-issues">{issues.map((issue, index) => <li key={index}>{issue.file.name}：{issue.message}</li>)}</ul>}
    </div>}
    {children}
    {enabled && hovered && <div className="library-file-drop-overlay" role="status">
      {progress ? <LoaderCircle className="spin" size={36} /> : <Upload size={36} />}
      <strong>{progress ? "正在导入，请稍候" : "松开即可导入视频"}</strong>
    </div>}
  </div>;
}
