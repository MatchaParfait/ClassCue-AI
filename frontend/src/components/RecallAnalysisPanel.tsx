import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, Check, LoaderCircle, RotateCcw, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { analyzeVideoRecall, getAnalysisRuns, getVideoProcessingOptions } from "../api/client";
import { isTaskTerminal, useTaskStatus } from "../hooks/useTaskStatus";
import { RECALL_MODES } from "../processOptions";
import type { Job, RecallMode } from "../types";

/** Remains read-only until the user explicitly starts an additional recall run. */
export function RecallAnalysisPanel({ videoId, mode, label: customLabel }: { videoId: number; mode: RecallMode; label?: string }) {
  const queryClient = useQueryClient();
  const [submitted, setSubmitted] = useState<Job | null>(null);
  const submitting = useRef(false);
  const refreshed = useRef(new Set<string>());
  const label = customLabel ?? RECALL_MODES.find(item => item.value === mode)?.label ?? "自定义方案";
  const runsKey = ["study", "analysis-runs", videoId];
  const optionsKey = ["process", "options", videoId];
  const runs = useQuery({
    queryKey: runsKey, queryFn: () => getAnalysisRuns(videoId), staleTime: 0,
    refetchInterval: query => query.state.data?.some(run => run.status === "processing") ? 1500 : false,
  });
  const options = useQuery({
    queryKey: optionsKey, queryFn: () => getVideoProcessingOptions(videoId), staleTime: 0,
    refetchInterval: query => query.state.data?.active_job && !isTaskTerminal(query.state.data.active_job) ? 1500 : false,
  });
  const snapshot = options.data?.active_job ?? submitted;
  const task = useTaskStatus(snapshot?.id ?? null);
  const job = task.data ?? snapshot;
  const running = Boolean(job && !isTaskTerminal(job));
  const jobModes = job?.analysis_modes ?? snapshot?.analysis_modes ?? [];
  const progress = Math.max(0, Math.min(100, Math.round(job?.progress ?? 0)));
  const currentJob = jobModes.includes(mode);
  const run = runs.data?.find(item => item.analysis_mode === mode);
  const outdated = mode.startsWith("custom_") && Boolean(run && (run.latest_scheme_revision ?? 0) > (run.scheme_revision ?? 0));

  const refreshResults = () => {
    for (const queryKey of [
      runsKey, optionsKey, ["study", "markers", videoId], ["study", "video", videoId],
      ["videos"], ["library"], ["library-status"],
    ]) void queryClient.invalidateQueries({ queryKey });
  };
  useEffect(() => {
    if (!job || !isTaskTerminal(job)) return;
    const key = `job:${job.id}:${job.status}`;
    if (refreshed.current.has(key)) return;
    refreshed.current.add(key);
    refreshResults();
  }, [job?.id, job?.status]);
  useEffect(() => {
    if (run?.status !== "completed") return;
    const key = `run:${mode}:${run.updated_at ?? ""}`;
    if (refreshed.current.has(key)) return;
    refreshed.current.add(key);
    // Also recover results when returning after an additional run completed elsewhere.
    refreshResults();
  }, [run?.status, run?.updated_at, mode, videoId, queryClient]);

  const mutation = useMutation({
    mutationFn: (selected: RecallMode) => analyzeVideoRecall(videoId, selected),
    onSuccess: (result, selected) => {
      const next: Job = { ...result, id: result.job_id ?? result.id, analysis_modes: [selected] };
      setSubmitted(next);
      queryClient.setQueryData(["jobs", next.id], next);
      refreshResults();
    },
    onError: () => {
      void runs.refetch();
      void options.refetch();
    },
    onSettled: () => { submitting.current = false; },
  });

  if (runs.isPending || options.isPending) return <div className="empty-state recall-analysis-state" role="status">
    <LoaderCircle className="spin" size={20} /><strong>正在读取{label}识别状态…</strong>
  </div>;
  if (runs.isError || options.isError) return <div className="empty-state recall-analysis-state">
    <AlertCircle size={20} /><strong>识别状态读取失败</strong>
    <span role="alert">{(runs.error ?? options.error)?.message}</span>
    <button className="secondary-button compact" type="button" onClick={() => { void runs.refetch(); void options.refetch(); }}><RotateCcw size={15} />重新读取</button>
  </div>;
  if (!outdated && (run?.status === "completed" || (currentJob && job?.status === "completed"))) return <div className="empty-state recall-analysis-state">
    <Check size={20} /><strong>{label}已完成识别</strong><span>本方案未发现可显示的考点，可切换其他召回方案。</span>
  </div>;
  if (run?.archived) return <div className="empty-state recall-analysis-state"><strong>{label}已停用</strong><span>历史结果保留。</span></div>;

  const currentError = mutation.variables === mode ? mutation.error?.message : null;
  const failed = run?.status === "failed" || (currentJob && job && isTaskTerminal(job) && job.status !== "completed");
  const busy = mutation.isPending || running;
  const inProgress = running && currentJob;
  const runStuck = run?.status === "processing" && !running;
  return <div className="empty-state recall-analysis-state">
    {busy ? <LoaderCircle className="spin" size={20} /> : <Sparkles size={20} />}
    <strong>{inProgress ? `${label}${job?.status === "queued" ? "排队中" : "识别中"}`
      : failed ? `${label}上次识别未完成` : outdated ? `${label}规则已更新` : runStuck ? `${label}识别尚未结束` : `${label}尚未识别`}</strong>
    <span>{busy ? inProgress ? (job?.message || "正在使用已保存字幕识别考点。") : "请等待当前任务完成，再追加此召回方案。"
      : "使用已保存字幕补充识别，无需重新转写；会额外使用 token。"}</span>
    {inProgress && <div className="job-progress" role="progressbar" aria-label={`${label}识别进度`}
      aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
      <span style={{ width: `${progress}%` }} />
    </div>}
    {(currentError || (failed && currentJob && job?.error_message)) && <span className="field-error" role="alert">{currentError || job?.error_message}</span>}
    {task.isError && <span className="field-error" role="alert">任务状态读取失败。
      <button className="text-button" type="button" onClick={() => void task.refetch()}>重试读取</button></span>}
    <button className="primary-button compact" type="button" disabled={busy}
      onClick={() => {
        if (submitting.current || busy) return;
        submitting.current = true;
        mutation.mutate(mode);
      }}>{busy ? <LoaderCircle className="spin" size={15} /> : <Sparkles size={15} />}
      {mutation.isPending ? "正在提交…" : inProgress ? "正在识别…" : failed || runStuck ? `重试${label}识别` : outdated ? `重新识别${label}` : `识别${label}`}</button>
    {runStuck && <button className="text-button" type="button" onClick={() => { void runs.refetch(); void options.refetch(); }}>刷新识别状态</button>}
  </div>;
}
