import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowUpRight, AudioLines, Check, Cpu, FileText, PanelRight, RotateCcw, Settings2, X } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { getAnalysisPreview, getLiveTranscript, getMarkers, getSubtitles } from "../api/client";
import type { Job, Marker, MarkerMode, Subtitle, Video } from "../types";
import type { EvidenceCue, TaskSpace } from "../cyberspace/runtime";
import { EvidenceFeedback } from "./EvidenceFeedback";
import { canReadSavedText, modeLabels, phaseLabels, savedMarkers, spacePhase } from "../cyberspace/taskState";
import { useAnalysisQuality, useAnalysisVariant, useTaskAnalysisVariant, type CyberspacePreference } from "../cyberspace/preferences";
import { TranscriptTicker } from "./TranscriptTicker";
import { PhaseLabel } from "./PhaseLabel";
import { PreparationCaption } from "./PreparationCaption";
import { motion } from "./Motion";
import { usePanelTilt } from "./usePanelTilt";
import { recallLabel, useRecallSchemes } from "../hooks/useRecallSchemes";

const NO_TEXT: Subtitle[] = [];
function time(ms: number) { const seconds = Math.floor(ms / 1000); return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`; }

export function ProcessingExperience({ job, video, preference, connectionError, onClose, actions, telemetry, asrLabel, aiLabel, aiModel }: {
  job: Job; video: Video; preference: CyberspacePreference; connectionError: boolean; onClose(): void; actions?: ReactNode; telemetry?: ReactNode;
  asrLabel?: string; aiLabel?: string; aiModel?: string;
}) {
  const phase = spacePhase(job);
  const pipelineId = useId();
  const schemes = useRecallSchemes();
  const labelForMode = (value: MarkerMode) => job.analysis_schemes?.[value]?.name ?? modeLabels[value] ?? recallLabel(value, schemes.data);
  const analysisQuality = useAnalysisQuality();
  const analysisVariant = useTaskAnalysisVariant(useAnalysisVariant(), aiModel,
    `${video.library_id ?? "local"}:${video.id}:${job.job_id ?? job.id}`);
  const [mode, setMode] = useState<MarkerMode>(job.analysis_modes?.[0] ?? "medium_recall");
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [evidenceCue, setEvidenceCue] = useState<EvidenceCue | null>(null);
  const [error, setError] = useState("");
  const [quality, setQuality] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [visible, setVisible] = useState(() => !document.hidden);
  const [reduced, setReduced] = useState(() => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false);
  const [entrance, setEntrance] = useState<"waiting" | "running" | "done">(() => !reduced && ["preparing", "transcribing"].includes(phase) ? "waiting" : "done");
  const panelMotion = visible && preference === "dynamic" && !reduced;
  const titleTilt = usePanelTilt(panelMotion && entrance === "done");
  const transcriptTilt = usePanelTilt(panelMotion && entrance === "done");
  const canvas = useRef<HTMLCanvasElement>(null);
  const controller = useRef<TaskSpace | null>(null);
  const resultsButton = useRef<HTMLButtonElement>(null);
  const readable = canReadSavedText(phase);
  const terminal = !["preparing", "transcribing", "analyzing"].includes(phase);
  const live = useQuery({
    queryKey: ["job-transcript", video.library_id, video.id, job.job_id ?? job.id],
    queryFn: () => getLiveTranscript(job.job_id ?? job.id),
    enabled: !terminal && phase !== "analyzing", retry: false,
    refetchInterval: !terminal && phase !== "analyzing" ? 2500 : false,
    refetchIntervalInBackground: false, refetchOnWindowFocus: false,
  });
  // Fetch only durable text. Existing transcripts are not presented as new ASR output.
  const subtitles = useQuery({
    queryKey: ["study", "subtitles", video.id, "api"], queryFn: () => getSubtitles(video.id),
    enabled: readable, staleTime: 0, refetchOnWindowFocus: false,
  });
  const markers = useQuery({
    queryKey: ["study", "markers", video.id, "api", video.library_id, job.job_id ?? job.id], queryFn: () => getMarkers(video.id),
    enabled: terminal, staleTime: 0, refetchOnWindowFocus: false,
  });
  const analysis = useQuery({
    queryKey: ["job-analysis-preview", video.library_id, video.id, job.job_id ?? job.id],
    queryFn: () => getAnalysisPreview(job.job_id ?? job.id), enabled: readable,
    retry: false, refetchInterval: phase === "analyzing" ? 2500 : false,
    refetchIntervalInBackground: false, refetchOnWindowFocus: false,
  });
  const previousPhase = useRef(phase);
  useEffect(() => {
    if (terminal && previousPhase.current !== phase) {
      void subtitles.refetch(); void analysis.refetch(); void markers.refetch();
    }
    previousPhase.current = phase;
  }, [terminal, phase]);
  const streamed = live.data?.video_id === video.id ? live.data.subtitles : NO_TEXT;
  const text = readable ? subtitles.data?.value?.length ? subtitles.data.value : streamed : streamed;
  const isLive = text === streamed && text.length > 0;
  const preview = analysis.data?.video_id === video.id ? analysis.data : undefined;
  const previewMarkers = useMemo(() => Object.values(preview?.modes ?? {}).flatMap(state => state?.markers ?? []), [preview]);
  const allMarkers = terminal && (phase === "completed" || !previewMarkers.length)
    ? markers.data?.value ?? previewMarkers : previewMarkers;
  const visibleMarkers = useMemo(() => savedMarkers(allMarkers, mode), [allMarkers, mode]);
  const provisional = !terminal || visibleMarkers.some(marker => marker.preview);
  const chunkState = preview?.modes?.[preview.active_mode ?? mode];
  const modes = useMemo(() => [...new Set<MarkerMode>([...(job.analysis_modes?.length ? job.analysis_modes : ["medium_recall" as const]), ...allMarkers.map((item) => item.analysis_mode)])], [job.analysis_modes, allMarkers]);
  const snapshot = useMemo(() => ({ subtitles: text, markers: visibleMarkers,
    phase: connectionError ? "paused" as const : phase, reduced, resultScope: mode }), [text, visibleMarkers, phase, connectionError, reduced, mode]);
  const latest = useRef(snapshot); latest.current = snapshot;

  useEffect(() => {
    if (reduced || connectionError || !["preparing", "transcribing"].includes(phase)) setEntrance("done");
  }, [reduced, connectionError, phase]);
  useEffect(() => {
    if (entrance !== "waiting" || !visible) return;
    // Status and recovery controls must remain available even if WebGL loads slowly.
    const timer = window.setTimeout(() => setEntrance("done"), 1800);
    return () => window.clearTimeout(timer);
  }, [entrance, visible]);

  useEffect(() => {
    const change = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", change);
    return () => document.removeEventListener("visibilitychange", change);
  }, []);
  useEffect(() => {
    const media = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!media) return;
    const change = () => setReduced(media.matches);
    media.addEventListener("change", change);
    return () => media.removeEventListener("change", change);
  }, []);
  useEffect(() => {
    let disposed = false;
    let instance: TaskSpace | null = null;
    setError("");
    setQuality("");
    setEvidenceCue(null);
    import("../cyberspace/runtime.js").then(({ createTaskSpace }) => {
      if (disposed || !canvas.current) return;
      instance = createTaskSpace(canvas.current, {
        key: `${video.library_id ?? "local"}:${video.id}:${job.job_id ?? job.id}`,
        analysisQuality, analysisVariant,
        onSelect: (id) => { setEntrance("done"); setSelected(id); setOpen(true); },
        onEvidence: cue => { if (!disposed) setEvidenceCue(cue); },
        onReady: (animated) => { if (!disposed) setEntrance(current => current === "waiting" ? animated ? "running" : "done" : current); },
        onError: (message) => { if (!disposed) { setEntrance("done"); setError(message); } },
        onQuality: (message) => { if (!disposed) setQuality(message); },
      });
      controller.current = instance;
      instance.update(latest.current);
      instance.select(selected);
    }).catch(() => { if (!disposed) { setEntrance("done"); setError("当前窗口无法显示三维空间，后台任务不受影响。"); } });
    return () => { disposed = true; instance?.dispose(); controller.current = null; };
  }, [video.id, video.library_id, job.id, attempt, analysisQuality, analysisVariant]);
  useEffect(() => { controller.current?.update(snapshot); }, [snapshot]);
  useEffect(() => { controller.current?.select(selected); }, [selected]);
  useEffect(() => {
    if (!open) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopPropagation(); setOpen(false); resultsButton.current?.focus();
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [open]);
  const detail = visibleMarkers.find((item) => item.id === selected);
  const latestEvidence = visibleMarkers.at(-1);
  const closeResults = () => { setOpen(false); resultsButton.current?.focus(); };
  const step = phase === "completed" ? 3 : ["analyzing", "waiting"].includes(phase) ? 2 : phase === "preparing" ? 0 : 1;
  const title = connectionError ? "连接暂时中断" : phaseLabels[phase];
  return <section className="task-space" data-phase={phase} data-analysis-variant={analysisQuality === "high" && readable ? analysisVariant : undefined} data-entrance={entrance} data-moving={panelMotion && !terminal && !connectionError} data-panel-motion={panelMotion} data-hidden={!visible} aria-label="实验性字幕空间" onKeyDown={(event) => {
    if (event.key === "Escape" && open) { event.stopPropagation(); closeResults(); }
  }}>
    <header className="task-space-heading">
      <div><span className="task-space-badge">实验性</span><strong title={video.filename}>{video.filename}</strong></div>
      <div className="task-space-tools">
        <button className="icon-button" type="button" aria-label="重置视角" title="重置视角" disabled={Boolean(error)} onClick={() => controller.current?.resetView()}><RotateCcw size={17} /></button>
        <button className="text-button" type="button" onClick={() => { setEntrance("done"); onClose(); }}><Settings2 size={16} />查看配置</button>
        <button ref={resultsButton} className="icon-button" type="button" aria-label={provisional ? "本次考点预览" : "已保存考点"} title={provisional ? "本次考点预览" : "已保存考点"} aria-expanded={open} onClick={() => { setEntrance("done"); setOpen(!open); }}><PanelRight size={17} /></button>
      </div>
    </header>
    <div className={`task-space-body${open ? " has-results" : ""}`}>
      <div className="task-space-viewport">
        <canvas key={attempt} ref={canvas} aria-label="任务字幕可视化" />
        {error && <div className="task-space-fallback" role="status"><AlertTriangle size={24} /><p>{error}</p><button type="button" className="secondary-button" onClick={() => setAttempt((value) => value + 1)}>重试显示</button></div>}
      </div>
      <div className="task-space-visual">
        <div className="task-space-entry task-space-title-entry" onFocusCapture={() => setEntrance("done")}>
        <div className="task-space-float task-space-status-surface" {...titleTilt}><div className="task-space-float-face">
        <div className="task-space-title"><span><AudioLines size={14} aria-hidden="true" /> SUBTITLE SPACE <b>/ {String(video.id).padStart(3, "0")}</b></span><h2 aria-label={title}><PhaseLabel text={title} /></h2>
          <p>{connectionError ? "正在尝试恢复连接" : phase === "preparing" ? "先静一会儿，正在准备课堂声音。" : phase === "transcribing" ? text.some(s => s.text.trim()) ? "将课堂声音整理为可检索的文字" : "音频已就绪，正在识别第一段字幕" : phase === "analyzing" ? "从字幕中定位课堂考点" : phase === "completed" ? "字幕与考点已就绪" : phase === "stopped" ? "已停止，正在返回处理选项" : "课堂重点复习"}</p></div>
        <ol className="task-space-pipeline" aria-label="处理阶段">{["音频准备", "语音转写", "考点分析"].map((label, i) => <li key={label} data-state={i < step ? "done" : i === step ? "current" : "pending"} aria-current={i === step ? "step" : undefined}><span>{i < step ? <Check size={12} aria-hidden="true" /> : `0${i + 1}`}</span><strong>{label}</strong>{i === step && <motion.i className="task-space-step-indicator" aria-hidden="true" layoutId={`${pipelineId}-step`} transition={{ duration: reduced ? 0 : .18 }} />}</li>)}</ol>
        </div></div>
        </div>
        <div className="task-space-breathing-room">
          {!connectionError && !error && ["preparing", "transcribing"].includes(phase) && !text.some(s => s.text.trim()) &&
            <PreparationCaption waitingFirstText={phase === "transcribing"} />}
          {phase === "analyzing" && latestEvidence && <button type="button" className="task-space-evidence"
            key={`${mode}:${latestEvidence.start_ms}:${latestEvidence.end_ms}:${latestEvidence.source_quote}`}
            aria-label={`查看新考点：${latestEvidence.title}`}
            onClick={() => { controller.current?.select(latestEvidence.id, { travel: true }); setSelected(latestEvidence.id); setOpen(true); }}>
            <EvidenceFeedback cue={evidenceCue} id={latestEvidence.id} />
            <span className="task-space-evidence-label">发现考点 <span>点击查看原文</span></span>
            <strong>{latestEvidence.title}</strong>
            <span className="task-space-evidence-quote">{latestEvidence.source_quote}</span>
          </button>}
        </div>
        <section className="task-space-entry task-space-transcript" aria-label="字幕片段" onAnimationEnd={event => {
          if (event.target === event.currentTarget && event.animationName === "space-unroll-up") setEntrance("done");
        }} onFocusCapture={() => setEntrance("done")}>
          <div className="task-space-float" {...transcriptTilt}><div className="task-space-float-face">
          <div className="task-space-strip-heading"><h3><FileText size={14} aria-hidden="true" />{isLive ? "本次转写片段" : "字幕记录"}</h3><span>{text.length ? <><b>{text.length}</b> 条{isLive ? "预览" : "已保存"}</> : "等待文字输出"}</span></div>
          {isLive && /^qwen/i.test(asrLabel ?? "") && <p>分块识别预览，精确时间将在对齐后更新。</p>}
          {text.length ? <TranscriptTicker key={`${video.library_id}:${video.id}:${job.job_id ?? job.id}`} subtitles={text} live={isLive} reduced={reduced} paused={!visible || entrance !== "done"} />
            : <p>{live.isError ? "实时字幕暂不可用，后台转写仍在继续。" : "首批文字返回后在此显示；尚未返回时不显示示例字幕。"}</p>}
          </div></div>
        </section>
      </div>
      <div className="task-space-entry task-space-dossier" onFocusCapture={() => setEntrance("done")}>
        <section className="task-space-telemetry" aria-label="实时任务状态"><div className="task-space-strip-heading"><h3>运行状态</h3><span>任务 / {String(job.job_id ?? job.id).slice(-6)}</span></div>{telemetry}
          {chunkState && <div className="task-space-chunks" role="status">
            <strong>{phase === "analyzing" && chunkState.current_chunk ? `正在分析第 ${chunkState.current_chunk} / ${chunkState.total_chunks} 块` : `已校验 ${chunkState.completed_chunks} / ${chunkState.total_chunks} 块`}</strong>
            <span>{labelForMode(preview?.active_mode ?? mode)} · 约 10 分钟 / 块 · 重叠 30 秒</span>
            {phase !== "completed" && <span>{chunkState.candidate_count} 个已校验考点 · 待最终汇总{chunkState.truncated ? "（仅展示部分预览）" : ""}</span>}
          </div>}
          {analysis.isError && <p className="task-space-preview-error">分块预览暂不可用，后台分析不受影响。<button className="text-button" type="button" onClick={() => void analysis.refetch()}>重试读取</button></p>}
        </section>
        {!open && <section className="task-space-source"><div className="task-space-strip-heading"><h3><Cpu size={14} aria-hidden="true" />处理配置</h3><span>SESSION</span></div><dl>
          <div><dt>ASR 模型</dt><dd>{asrLabel || "当前模型"}</dd></div><div><dt>AI 服务</dt><dd>{aiLabel || "当前服务"}</dd></div>
          <div><dt>视频时长</dt><dd>{time(video.duration_ms || 0)}</dd></div><div><dt>考点召回</dt><dd>{(job.analysis_modes?.length ? job.analysis_modes : ["medium_recall" as const]).map(labelForMode).join(" / ")}</dd></div>
        </dl></section>}
      {open && <aside className="task-space-results" aria-label={provisional ? "本次考点预览结果" : "已保存考点结果"}>
        <div className="task-space-result-heading"><h2>{provisional ? "考点预览" : "已保存考点"} <span>{visibleMarkers.length}</span></h2><button className="icon-button" aria-label="关闭考点结果" title="关闭考点结果" onClick={closeResults}><X size={16} /></button></div>
        {provisional && <p className="task-space-preview-note">已校验，待最终汇总；尚未写入复习记录。</p>}
        <select aria-label="考点召回方案" value={mode} onChange={(event) => { setMode(event.target.value as MarkerMode); setSelected(null); }}>
          {modes.map((value) => <option key={value} value={value}>{labelForMode(value)}</option>)}
        </select>
        {detail && <section className="task-space-detail"><h3>{detail.title}</h3><blockquote>{detail.source_quote}</blockquote><p>{detail.note}</p>
          {!detail.preview && <Link to={`/review/${video.id}?start_ms=${detail.user_start_ms ?? detail.start_ms}&library_id=${encodeURIComponent(video.library_id ?? "default-library")}`}>进入复习 <ArrowUpRight size={14} /></Link>}</section>}
        {!visibleMarkers.length && <p className="task-space-empty">{!terminal ? "等待分析结果" : markers.isError ? "考点暂时无法读取" : markers.isPending ? "正在读取考点" : phase === "completed" ? "当前方案未识别到考点" : "暂无已保存考点"}</p>}
        <div className="task-space-result-list">{visibleMarkers.map((marker) => <button key={marker.id} type="button" aria-pressed={marker.id === selected} onClick={() => { controller.current?.select(marker.id, { travel: true }); setSelected(marker.id); }}>
          <EvidenceFeedback cue={evidenceCue} id={marker.id} />
          <time>{time(marker.start_ms)}</time><strong>{marker.title}</strong></button>)}</div>
      </aside>}
      </div>
    </div>
    <footer className="task-space-footer"><div><span>{reduced ? "已减少动态效果" : quality || "字幕空间"}</span><Link to="/settings?section=experimental">实验功能设置</Link></div>{actions}</footer>
    {(subtitles.isError || markers.isError) && <p className="task-space-error" role="alert">字幕或考点读取失败，处理任务不受影响。<button type="button" onClick={() => { void subtitles.refetch(); void markers.refetch(); }}>重新读取</button></p>}
  </section>;
}
