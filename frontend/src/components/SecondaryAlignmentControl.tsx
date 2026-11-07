import { useEffect, useRef, useState } from "react";
import { Ban, Check, LoaderCircle, Undo2, WandSparkles } from "lucide-react";
import { cancelJob, changeSecondaryAlignment, getJob, getSecondaryView, startSecondaryAlignment } from "../api/client";
import type { Job, SecondaryView } from "../types";

export function useSecondaryAlignment({ videoId, secondary, onChange }: {
  videoId: number; secondary: SecondaryView | null; onChange: (view: SecondaryView) => void;
}) {
  const [jobId, setJobId] = useState(secondary?.alignment_job_id ?? null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  const scope = useRef(0);
  const secondaryId = secondary?.video_id;
  const current = useRef({ secondary, onChange });
  current.current = { secondary, onChange };
  const running = Boolean(jobId && (!job || ["queued", "running", "resource_paused"].includes(job.status)));

  useEffect(() => {
    return () => { scope.current += 1; };
  }, [videoId, secondaryId]);
  useEffect(() => {
    setJobId(secondary?.alignment_job_id ?? null);
    setJob(null);
    setError(null);
  }, [videoId, secondaryId, secondary?.alignment_job_id]);
  useEffect(() => {
    busyRef.current = false;
    setBusy(false);
  }, [videoId, secondaryId]);

  useEffect(() => {
    if (!jobId || secondaryId == null) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      try {
        const saved = await getJob(jobId);
        if (cancelled) return;
        setJob(saved);
        setError(null);
        if (["queued", "running", "resource_paused"].includes(saved.status)) {
          timer = setTimeout(() => void poll(), 1000);
        } else if (saved.alignment?.outcome === "applied") {
          const view = await getSecondaryView(videoId);
          if (!cancelled && view && view.video_id === current.current.secondary?.video_id) current.current.onChange(view);
        }
      } catch (failure) {
        if (cancelled) return;
        setError(failure instanceof Error ? failure.message : "无法读取对齐任务");
        timer = setTimeout(() => void poll(), 2000);
      }
    };
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [jobId, videoId, secondaryId]);

  const command = async (action: "start" | "cancel" | "apply" | "undo") => {
    if (busyRef.current || !secondary) return;
    const requestScope = scope.current;
    const isCurrent = () => scope.current === requestScope;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      if (action === "start") {
        const started = await startSecondaryAlignment(videoId);
        if (!isCurrent()) return;
        setJob(null);
        setJobId(started.job_id);
        current.current.onChange({ ...current.current.secondary!, alignment_job_id: started.job_id });
      } else if (action === "cancel") {
        const saved = await cancelJob(jobId!);
        if (isCurrent()) setJob(saved);
      } else {
        const view = await changeSecondaryAlignment(videoId, jobId!, action);
        if (!isCurrent() || view.video_id !== current.current.secondary?.video_id) return;
        current.current.onChange(view);
        const saved = await getJob(jobId!);
        if (isCurrent()) setJob(saved);
      }
    } catch (failure) {
      if (isCurrent()) setError(failure instanceof Error ? failure.message : "自动对齐失败，请重试");
    } finally {
      if (isCurrent()) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };

  return { jobId, job, busy, error, running, command };
}

export function SecondaryAlignmentControl({ alignment, disabled }: {
  alignment: ReturnType<typeof useSecondaryAlignment>; disabled: boolean;
}) {
  const { jobId, job, busy, error, running, command } = alignment;
  const result = job?.alignment;
  return <div className="secondary-alignment">
    <div className="secondary-alignment-actions">
      <button type="button" className="secondary-view-button secondary-view-switch"
        disabled={disabled || busy || running} aria-label="自动对齐"
        title={disabled ? "请先保存或恢复手动偏移，等待视角切换完成" : "自动对齐"}
        onClick={() => void command("start")}>
        {running || busy ? <LoaderCircle size={14} className="spin" /> : <WandSparkles size={14} />}自动对齐
      </button>
      {running && <button type="button" className="secondary-view-button" disabled={busy}
        aria-label="取消自动对齐" title="取消自动对齐" onClick={() => void command("cancel")}><Ban size={14} /></button>}
      {result?.outcome === "candidate" && <button type="button" className="secondary-view-button secondary-view-switch"
        disabled={disabled || busy || running} onClick={() => void command("apply")}><Check size={14} />应用候选</button>}
      {result?.outcome === "applied" && <button type="button" className="secondary-view-button secondary-view-switch" disabled={disabled || busy || running}
        aria-label="撤销自动对齐" title="撤销自动对齐，恢复此前的时间偏移" onClick={() => void command("undo")}><Undo2 size={14} />撤销</button>}
    </div>
    {jobId && <span className="secondary-view-mapping" role="status">
      {running ? job?.status === "queued" ? "等待 ASR" : job?.message || "正在读取对齐任务"
        : job?.status === "failed" ? job.error_message || "自动对齐失败，原偏移已保留"
        : job?.status === "interrupted" ? "自动对齐已停止"
        : result ? `${result.message}${["applied", "candidate"].includes(result.outcome) && result.offset_ms != null
          ? `（${result.offset_ms >= 0 ? "+" : ""}${(result.offset_ms / 1000).toFixed(1)} 秒）` : ""}` : job?.message}
    </span>}
    {(result?.model_id || job?.model_id) && <span className="secondary-view-mapping">
      ASR：{result?.model_id || job?.model_id}
      {result?.asr_seconds != null && ` · 已识别 ${Math.ceil(result.asr_seconds)} / 900 秒`}
      {result?.phase === "fallback" && " · 已使用扩大搜索"}
    </span>}
    {result?.primary_model_id && result.primary_model_id !== result.model_id && <span className="secondary-view-mapping">
      主字幕模型：{result.primary_model_id}（与本次 ASR 不同）
    </span>}
    {result?.outcome === "candidate" && result.evidence.length > 0 && <details className="secondary-alignment-evidence">
      <summary>查看对应句</summary>
      {result.evidence.map((e, i) => <div key={i}><p>主视频：{e.primary_text}</p><p>副视频：{e.secondary_text}</p></div>)}
    </details>}
    {error && <span className="secondary-view-error" role="alert">{error}</span>}
  </div>;
}
