import { CharacterEye } from "./CharacterAccent";
import { ProcessingExperience } from "./ProcessingExperience";
import { useCyberspacePreference } from "../cyberspace/preferences";
import { AnimatePresence, AnimatedPopup } from "./Motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowRight,
  Ban,
  CheckCircle2,
  Clock3,
  FileVideo,
  FolderOpen,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  Settings2,
  Sparkles,
  PauseCircle,
  Pencil,
  Save,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  getJob,
  cancelJob,
  getPicker,
  getVideo,
  getVideoProcessingOptions,
  getAsrSelection,
  getAiSettings,
  openPicker,
  processVideo,
  registerVideo,
  renameVideoFile,
  retryVideo,
  resumeJob,
} from "../api/client";
import type { Job, PickerResult, ProcessingOptions, Video } from "../types";
import { buildProcessPayload, RECALL_SELECTION_REQUIRED, type ProcessForm } from "../processOptions";
import { QwenResourceControls } from "./QwenResourceControls";
import { FireRedResourceProfile, FireRedResourceStatus } from "./FireRedResourceStatus";
import { RecallModeControl } from "./RecallModeControl";
import { hasActivePreparation, ModelPreparationProgress } from "./ModelPreparationProgress";
import { ProcessVideoPreview } from "./ProcessVideoPreview";
import { WarmAtmosphere } from "./WarmAtmosphere";
import { useAsrStatus } from "../hooks/useAsrStatus";
import { useTaskStatus } from "../hooks/useTaskStatus";
import { useRecallSelection } from "../hooks/useRecallSelection";

const VIDEO_EXTENSIONS = [".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts", ".flv"];

const DEFAULT_FORM: ProcessForm = {
  displayName: "",
  recallModes: ["medium_recall"],
  keywords: "",
  filterFormula: "",
  filterLowInformation: false,
  forceRetranscribe: false,
};

export function ProcessPage({ embedded = false, active = true, onClose, onJob, onRegistered }: {
  embedded?: boolean; active?: boolean; onClose?: () => void; onJob?: (id: string | number) => void; onRegistered?: (video: Video) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const initialVideoId = Number(searchParams.get("videoId") ?? 0);
  const [videoPath, setVideoPath] = useState("");
  const [filename, setFilename] = useState("");
  const [formOptions, setForm] = useState<ProcessForm>(DEFAULT_FORM);
  const [recallModes, setRecallModes] = useRecallSelection();
  const form = { ...formOptions, recallModes };
  const [registeredVideo, setRegisteredVideo] = useState<Video | null>(null);
  const [jobId, setJobId] = useState<string | number | null>(null);
  const [pickerId, setPickerId] = useState<string | number | null>(null);
  const [openingPicker, setOpeningPicker] = useState(false);
  const [pickerError, setPickerError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [stoppingJobId, setStoppingJobId] = useState<string | number | null>(null);
  const [editingFilename, setEditingFilename] = useState(false);
  const spacePreference = useCyberspacePreference();
  const [spaceOpen, setSpaceOpen] = useState(true);
  const [spaceReturning, setSpaceReturning] = useState(false);
  const startButton = useRef<HTMLButtonElement>(null);
  useEffect(() => { setSpaceOpen(true); setSpaceReturning(false); }, [jobId, spacePreference]);
  const hydratedExisting = useRef(false);

  const existingVideo = useQuery({
    queryKey: ["process", "video", initialVideoId],
    queryFn: () => getVideo(initialVideoId),
    enabled: active && initialVideoId > 0,
    staleTime: 30_000,
  });

  useEffect(() => {
    const video = existingVideo.data?.value;
    if (!video) return;
    if (hydratedExisting.current) {
      if (registeredVideo?.id === video.id && registeredVideo !== video) setRegisteredVideo(video);
      return;
    }
    if (registeredVideo) return;
    hydratedExisting.current = true;
    setRegisteredVideo(video);
    setVideoPath(video.video_path || "");
    setFilename(video.filename);
  }, [existingVideo.data, registeredVideo]);

  const options = useQuery({
    queryKey: ["process", "options", registeredVideo?.id],
    queryFn: () => getVideoProcessingOptions(registeredVideo!.id),
    enabled: active && Boolean(registeredVideo?.id),
    staleTime: 30_000,
  });
  const ai = useQuery({
    queryKey: ["settings", "ai"],
    queryFn: getAiSettings,
    enabled: active,
    staleTime: 30_000,
  });
  const selection = useQuery({
    queryKey: ["settings", "asr", "selection"],
    queryFn: getAsrSelection,
    enabled: active && !registeredVideo && initialVideoId <= 0,
    staleTime: 30_000,
  });

  useEffect(() => {
    const activeJob = options.data?.active_job;
    if (activeJob && (activeJob.job_id ?? activeJob.id) !== undefined) {
      setJobId(activeJob.job_id ?? activeJob.id ?? null);
    }
  }, [options.data]);

  const picker = useQuery({
    queryKey: ["picker", pickerId],
    queryFn: () => getPicker(pickerId!),
    enabled: active && pickerId !== null,
    refetchInterval: (query) => {
      const state = query.state.data;
      return state && isPickerTerminal(state) ? false : 500;
    },
  });

  useEffect(() => {
    const state = picker.data;
    if (!state || !isPickerTerminal(state)) return;
    setPickerId(null);
    if (state.path) {
      setVideoPath(state.path);
      setFilename(fileName(state.path));
      if (registeredVideo && registeredVideo.video_path !== state.path) {
        setRegisteredVideo(null);
        setJobId(null);
      }
      setPickerError(null);
      setNotice("已选择视频文件");
    } else if (state.status === "cancelled" || state.status === "canceled") {
      setNotice("已取消选择视频");
    } else if (state.error || state.message) {
      setPickerError(state.error || state.message || "文件选择器未返回路径");
    }
    void queryClient.removeQueries({ queryKey: ["picker"] });
  }, [picker.data, queryClient]);

  const job = useTaskStatus(jobId, active);
  const terminalSeen = useRef("");
  const openedCompletedJob = useRef<string | number | null>(null);

  useEffect(() => {
    const completed = job.data;
    if (!active || !registeredVideo || completed?.status !== "completed") return;
    if (completed.video_id != null && completed.video_id !== registeredVideo.id) return;
    const id = completed.job_id ?? completed.id;
    if (openedCompletedJob.current === id) return;
    openedCompletedJob.current = id;
    const query = registeredVideo.library_id ? `?library_id=${encodeURIComponent(registeredVideo.library_id)}` : "";
    // Completion, not the decorative reveal or a secondary metadata request, owns navigation.
    navigate(`/review/${registeredVideo.id}${query}`, {
      replace: true, state: { completedJobId: id, completedVideoId: registeredVideo.id },
    });
  }, [active, job.data, registeredVideo?.id, registeredVideo?.library_id, navigate]);

  useEffect(() => {
    const currentJob = job.data;
    if (!currentJob) return;
    if (isJobTerminal(currentJob)) {
      const identity = `${currentJob.id}:${currentJob.status}`;
      if (terminalSeen.current === identity) return;
      terminalSeen.current = identity;
      setStoppingJobId(null);
      if (["interrupted", "cancelled", "canceled"].includes(currentJob.status)) {
        setNotice("处理已停止，可以调整选项后继续");
      }
      if (currentJob.status === "completed" && registeredVideo) {
        moveProcessedVideoToRoot(registeredVideo.id);
      }
      void queryClient.invalidateQueries({ queryKey: ["videos"] });
      if (registeredVideo) {
        void queryClient.invalidateQueries({ queryKey: ["process", "video", registeredVideo.id] });
        void queryClient.invalidateQueries({ queryKey: ["study", "video", registeredVideo.id] });
        void queryClient.invalidateQueries({ queryKey: ["study", "subtitles", registeredVideo.id] });
        void queryClient.invalidateQueries({ queryKey: ["study", "markers", registeredVideo.id] });
        void queryClient.invalidateQueries({ queryKey: ["process", "options", registeredVideo.id] });
        void getVideo(registeredVideo.id).then(({ value }) => {
          setRegisteredVideo((current) => current?.id === value.id ? value : current);
        }).catch(() => {});
      }
    }
  }, [job.data, queryClient, registeredVideo?.id]);

  const registerMutation = useMutation({
    mutationFn: () =>
      registerVideo({
        video_path: videoPath.trim(),
        filename: filename.trim() || undefined,
      }),
    onSuccess: (video) => {
      queryClient.setQueryData(["process", "video", video.id], { value: video, source: "api" });
      setRegisteredVideo(video);
      setEditingFilename(false);
      setVideoPath(video.video_path);
      setFilename(video.filename);
      setFormError(null);
      setNotice(video.storage_warning || "视频已导入视频库，可以开始处理");
      onRegistered?.(video);
      void queryClient.invalidateQueries({ queryKey: ["videos"] });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const renameMutation = useMutation({
    mutationFn: () => renameVideoFile(registeredVideo!.id, filename.trim()),
    onSuccess: (video) => {
      queryClient.setQueryData(["process", "video", video.id], { value: video, source: "api" });
      setRegisteredVideo(video);
      setVideoPath(video.video_path);
      setFilename(video.filename);
      setFormError(null);
      setNotice(video.storage_warning || "文件名已修改");
      setEditingFilename(false);
      void queryClient.invalidateQueries({ queryKey: ["videos"] });
      void queryClient.invalidateQueries({ queryKey: ["process", "video", video.id] });
      void queryClient.invalidateQueries({ queryKey: ["study", "video", video.id] });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const processMutation = useMutation<Job, Error, boolean>({
    mutationFn: (retry = false) => {
      if (!registeredVideo) throw new Error("请先登记视频");
      const payload = {
        ...buildProcessPayload(form),
        asr_model_id: typeof options.data?.asr_model === "object" ? options.data.asr_model?.id : undefined,
      };
      return retry ? retryVideo(registeredVideo.id, payload) : processVideo(registeredVideo.id, payload);
    },
    onSuccess: (result) => {
      queryClient.setQueryData(["jobs", result.job_id ?? result.id], { ...result, id: result.job_id ?? result.id }, { updatedAt: 0 });
      setRegisteredVideo((current) => current ? { ...current, status: "processing" } : current);
      setJobId(result.job_id ?? result.id ?? null);
      setStoppingJobId(null);
      setFormError(null);
      setNotice("处理任务已提交，可切换页面，进度会自动恢复");
      if (result.job_id ?? result.id) onJob?.(result.job_id ?? result.id);
      void queryClient.invalidateQueries({ queryKey: ["videos"] });
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const cancelMutation = useMutation({
    mutationFn: (id: string | number) => cancelJob(id),
    onSuccess: (result) => {
      queryClient.setQueryData(["jobs", result.job_id ?? result.id], result);
      setStoppingJobId(result.job_id ?? result.id ?? null);
      setNotice(["interrupted", "cancelled", "canceled"].includes(result.status)
        ? "处理已停止，可以调整选项后继续" : "已请求停止，正在安全释放当前处理资源…");
      setFormError(null);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });
  const resumeMutation = useMutation({
    mutationFn: () => resumeJob(jobId!),
    onSuccess: (result) => {
      setJobId(result.job_id ?? result.id);
      onJob?.(result.job_id ?? result.id);
      setStoppingJobId(null);
      setFormError(null);
    },
    onError: (error) => setFormError(errorMessage(error)),
  });

  const canRegister = Boolean(videoPath.trim()) && !registerMutation.isPending && !renameMutation.isPending;
  const validExtension = useMemo(() => hasVideoExtension(videoPath), [videoPath]);
  const currentJob = job.data;
  const stopped = Boolean(currentJob && ["interrupted", "cancelled", "canceled"].includes(currentJob.status));
  useEffect(() => {
    if (active && stopped && !spaceOpen) startButton.current?.focus({ preventScroll: true });
  }, [active, stopped, spaceOpen]);
  useEffect(() => {
    if (!active || !spaceOpen || spacePreference === "off" || (!stopped && !spaceReturning)) return;
    setSpaceReturning(true);
    const timer = window.setTimeout(() => {
      setSpaceOpen(false);
      setSpaceReturning(false);
    }, window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ? 0 : 900);
    return () => window.clearTimeout(timer);
  }, [active, stopped, spaceReturning, spaceOpen, spacePreference, jobId]);
  const currentAsr = options.data?.asr_model ?? selection.data;
  const asrStatus = useAsrStatus(active && typeof currentAsr === "object" ? currentAsr : undefined);
  const isFireRed = typeof currentAsr === "object" && currentAsr?.engine === "firered";
  const isProcessing = processMutation.isPending || Boolean(currentJob && !isJobTerminal(currentJob));
  const showRetry = stopped || currentJob?.status === "failed" || registeredVideo?.status === "failed"
    || registeredVideo?.status === "resource_paused";
  const pickerBusy = openingPicker || pickerId !== null;
  const fileChangesBusy = isProcessing || registerMutation.isPending || renameMutation.isPending
    || Boolean(registeredVideo && options.isLoading);
  useEffect(() => {
    if (!notice || !active) return;
    const timer = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(timer);
  }, [notice, active]);

  const chooseFile = async () => {
    if (pickerBusy) return;
    setOpeningPicker(true);
    setPickerError(null);
    try {
      const started = await openPicker("file");
      setPickerId(started.picker_id ?? started.id ?? null);
      if (started.path) {
        setVideoPath(started.path);
        setFilename(fileName(started.path));
        if (registeredVideo && registeredVideo.video_path !== started.path) {
          setRegisteredVideo(null);
          setJobId(null);
        }
        setNotice("已选择视频文件");
      }
    } catch (error) {
      setPickerError(errorMessage(error));
    } finally {
      setOpeningPicker(false);
    }
  };

  const register = () => {
    setFormError(null);
    if (!videoPath.trim()) {
      setFormError("请输入视频文件路径，或使用本机选择器");
      return;
    }
    if (!validExtension) {
      setFormError(`仅支持 ${VIDEO_EXTENSIONS.join("、")} 视频格式`);
      return;
    }
    registerMutation.mutate();
  };

  const startProcess = () => {
    setFormError(null);
    if (!registeredVideo) {
      setFormError("请先登记视频");
      return;
    }
    processMutation.mutate(Boolean(showRetry));
  };

  if (existingVideo.isLoading) {
    return <div className="loading-screen"><LoaderCircle className="spin" size={24} /><span>正在读取视频信息…</span></div>;
  }

  if (existingVideo.isError && !registeredVideo && !hydratedExisting.current) {
    return <main className="process-page has-warm-atmosphere"><WarmAtmosphere active={active} /><div className="process-inner">
      <div className="process-header"><h1>处理视频</h1><Link className="secondary-button" to="/">返回视频库 <ArrowRight size={16} /></Link></div>
      <p className="field-error" role="alert"><AlertCircle size={16} />{errorMessage(existingVideo.error)}</p>
      <button className="secondary-button" type="button" onClick={() => void existingVideo.refetch()}><RefreshCw size={16} />重新读取视频</button>
    </div></main>;
  }

  const showSpace = spacePreference !== "off" && spaceOpen && Boolean(currentJob && registeredVideo);
  const commands: JobCommandsProps | undefined = currentJob ? {
    job: currentJob,
    onReview: () => registeredVideo && navigate(`/review/${registeredVideo.id}`),
    onRetry: () => currentJob.status === "resource_paused" ? resumeMutation.mutate() : startProcess(),
    onCancel: () => cancelMutation.mutate(currentJob.job_id ?? currentJob.id),
    cancelling: cancelMutation.isPending || stoppingJobId === (currentJob.job_id ?? currentJob.id),
    retrying: processMutation.isPending || resumeMutation.isPending,
  } : undefined;
  return (
    <main data-space-motion={spacePreference} className={`process-page has-warm-atmosphere${registeredVideo ? " process-page-selected" : ""}${embedded ? " embedded-page" : ""}${showSpace ? " is-cyberspace" : ""}${spaceReturning ? " is-space-returning" : ""}${spacePreference !== "off" && (showSpace || processMutation.isPending) && !spaceReturning ? " is-space-cooling" : ""}`}>
      <WarmAtmosphere active={active && (!showSpace || spaceReturning)} />
      <div className="process-inner">
        <div className="process-header">
          <div>
            <p className="eyebrow">单视频处理</p>
            <h1 tabIndex={-1}>{registeredVideo ? "处理视频" : "导入视频"}</h1>
            {spacePreference !== "off" && currentJob && !spaceOpen && <button className="text-button" type="button" onClick={() => setSpaceOpen(true)}><Sparkles size={16} />打开字幕空间</button>}
            {!registeredVideo && <p>视频将保存到视频库，是否保留原文件取决于设置中的“导入后删除原文件”。</p>}
          </div>
          {embedded ? <button className="secondary-button panel-return" onClick={onClose}>返回视频库</button>
            : <Link className="secondary-button" to="/"><ArrowRight size={16} /> 返回视频库</Link>}
        </div>

        {showSpace && active && currentJob && registeredVideo && <ProcessingExperience key={`${registeredVideo.id}:${currentJob.job_id ?? currentJob.id}`} job={currentJob} video={registeredVideo}
          preference={spacePreference} connectionError={job.isError} onClose={() => setSpaceReturning(true)} actions={commands && <JobCommands {...commands} />}
          asrLabel={optionLabel(currentAsr, "等待模型信息")} aiLabel={optionLabel(options.data?.ai_service ?? ai.data, "等待服务信息")}
          aiModel={ai.data?.model ?? modelName(options.data?.ai_service)}
          telemetry={<>{commands && <JobStatus {...commands} hideActions />}
            {formError && <p className="field-error" role="alert">{formError}</p>}
            {job.isError && <p className="field-error" role="alert">任务状态读取失败<button className="text-button" onClick={() => void job.refetch()}>重新读取</button></p>}</>} />}

        <section hidden={showSpace} className="process-section process-video-section" aria-label={registeredVideo ? "当前视频" : "视频选择"}>
          {!registeredVideo && <div className="section-heading compact">
            <div><p className="eyebrow">当前视频库</p><h2><CharacterEye heading />视频选择</h2></div>
            <span className="process-section-step">1</span>
          </div>}
          {!registeredVideo && <div className="path-picker-row">
            <div className="video-file-picker" aria-busy={pickerBusy}>
              <FileVideo className="video-file-picker-icon" size={36} aria-hidden="true" />
              <button className="primary-button" type="button" onClick={chooseFile} disabled={pickerBusy || fileChangesBusy}>
                {pickerBusy ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : <FolderOpen size={16} aria-hidden="true" />}
                {pickerBusy ? "等待选择…" : "选择文件"}
              </button>
              <p className="field-hint">MP4、MKV、MOV、AVI、WebM 等视频格式</p>
            </div>
            {pickerError && <p className="field-error" role="alert"><AlertCircle size={14} />{pickerError}</p>}
            <details className="video-path-details">
              <summary>手动输入路径</summary>
              <div className="field-group">
                <label className="field-label" htmlFor="video-path">本机视频路径</label>
                <input id="video-path" value={videoPath} disabled={isProcessing || registerMutation.isPending || renameMutation.isPending} onChange={(event) => {
                  const nextPath = event.target.value;
                  setVideoPath(nextPath);
                  setFilename(fileName(nextPath));
                }} placeholder="例如：D:\课程\第一讲.mp4" />
              </div>
            </details>
          </div>}

          {registeredVideo && !showSpace && <ProcessVideoPreview key={registeredVideo.id} video={registeredVideo} active={active} />}
          {(registeredVideo || videoPath) && (
            <div className={`video-info${registeredVideo ? " video-info-selected" : ""}`}>
              <div className="video-info-icon"><FileVideo size={registeredVideo ? 28 : 22} /></div>
              <div className="video-info-main">
                {registeredVideo && <div className="video-selection-state"><span className="eyebrow">当前视频</span><span className="video-imported-state"><CheckCircle2 size={14} />已导入视频库</span></div>}
                <strong role={registeredVideo ? "heading" : undefined} aria-level={registeredVideo ? 2 : undefined}>{registeredVideo?.filename || fileName(videoPath) || "待登记视频"}</strong>
                {!registeredVideo && <span>{videoPath}</span>}
                {registeredVideo && <>
                  <div className="video-info-meta"><span className={`status-badge status-${registeredVideo.status === "review_ready" ? "ready" : "pending"}`}>{videoStatusLabel(isProcessing ? "processing" : registeredVideo.status)}</span><span>时长 {formatDuration(registeredVideo.duration_ms)}</span><span>大小 {formatBytes(registeredVideo.source_size)}</span></div>
                  <details className="video-path-details video-storage-details">
                    <summary>文件位置</summary>
                    <dl><div><dt>视频库文件</dt><dd>{registeredVideo.video_path}</dd></div><div><dt>来源目录</dt><dd>{registeredVideo.source_folder || directoryName(registeredVideo.video_path)}</dd></div></dl>
                  </details>
                </>}
              </div>
            </div>
          )}

          {registeredVideo && <div className="video-selected-tools">
            <button className="secondary-button compact" type="button" onClick={chooseFile} disabled={pickerBusy || fileChangesBusy}>
              {pickerBusy ? <LoaderCircle className="spin" size={15} /> : <FolderOpen size={15} />}
              {pickerBusy ? "等待选择…" : "选择其他视频"}
            </button>
            <button className="icon-button" type="button" title="修改文件名" aria-label="修改文件名"
              aria-expanded={editingFilename} aria-controls="video-filename-editor" disabled={fileChangesBusy || pickerBusy}
              onClick={() => { setFilename(registeredVideo.filename); setEditingFilename((current) => !current); }}>
              <Pencil size={16} />
            </button>
          </div>}
          {registeredVideo && pickerError && <p className="field-error" role="alert"><AlertCircle size={14} />{pickerError}</p>}

          {(!registeredVideo || editingFilename) && <div className="video-filename-row" id="video-filename-editor">
            <div className="field-group">
              <label className="field-label" htmlFor="video-filename">修改文件名</label>
              <input id="video-filename" value={filename} maxLength={200} autoFocus={editingFilename}
                disabled={fileChangesBusy}
                onChange={(event) => setFilename(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && registeredVideo && filename.trim() && filename.trim() !== registeredVideo.filename && !isProcessing && !renameMutation.isPending) {
                    event.preventDefault();
                    renameMutation.mutate();
                  }
                }} placeholder="使用原文件名" />
            </div>
            {registeredVideo && <button className="icon-button" type="button" title="保存文件名" aria-label="保存文件名"
              disabled={isProcessing || registerMutation.isPending || renameMutation.isPending || !filename.trim() || filename.trim() === registeredVideo.filename}
              onClick={() => renameMutation.mutate()}>{renameMutation.isPending ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}</button>}
          </div>}
          {(!registeredVideo || registeredVideo.status === "review_ready" || registeredVideo.has_transcript) && <div className="process-actions">
            {!registeredVideo && <button className="primary-button" type="button" onClick={register} disabled={!canRegister || isProcessing}><FileVideo size={16} /> {registerMutation.isPending ? "正在导入视频库…" : "导入视频库"}</button>}
            {registeredVideo?.status === "review_ready" && <button className="secondary-button" type="button" onClick={() => navigate(`/review/${registeredVideo.id}`)}>打开复习页面 <ArrowRight size={16} /></button>}
            {registeredVideo?.has_transcript && registeredVideo.status !== "review_ready" && !isProcessing && <button className="secondary-button" type="button" onClick={() => navigate(`/review/${registeredVideo.id}`)}>查看已保存字幕 <ArrowRight size={16} /></button>}
          </div>}
        </section>

        <section hidden={showSpace} className="process-section process-options-section">
          <div className="section-heading compact process-config-heading">
            <div><p className="eyebrow">处理配置</p><h2><CharacterEye heading />处理选项</h2></div>
          </div>
          <div className="readonly-config-grid">
            <div className="readonly-config"><span>当前 ASR 模型</span><strong>{optionLabel(currentAsr ?? options.data?.asr_model_name, "未配置")}</strong>
              {asrStatus.isFetching && <small role="status">检查中</small>}
              {asrStatus.isError && <small role="status">状态检查失败</small>}
              <Link to="/settings?section=asr"><Settings2 size={14} /> 修改</Link></div>
            <div className="readonly-config"><span>当前 AI 服务</span><strong>{ai.isPending ? "正在读取配置" : ai.isError ? "配置读取失败" : ai.data?.model || "未配置"}</strong><Link to="/settings?section=ai"><Settings2 size={14} /> 修改</Link></div>
          </div>
          {isFireRed ? <FireRedResourceProfile modelId={currentAsr!.id} active={active} />
            : typeof currentAsr === "object" && currentAsr?.engine === "qwen"
              ? <QwenResourceControls form={form} onChange={setForm} disabled={isProcessing} /> : null}
          <div className="option-grid">
            <label className="check-option"><input type="checkbox" checked={Boolean(form.forceRetranscribe)} disabled={isProcessing} onChange={(event) => setForm((current) => ({ ...current, forceRetranscribe: event.target.checked }))} /><span><strong>使用当前模型重新转写</strong><small>成功后替换原字幕并重建复习结果；失败时保留原结果</small></span></label>
            <RecallModeControl value={form.recallModes} active={active} disabled={isProcessing} onChange={(recallModes) => {
              setRecallModes(recallModes);
              setForm((current) => ({ ...current, recallModes }));
              setFormError((current) => current === RECALL_SELECTION_REQUIRED ? null : current);
            }} />
          </div>
          <div className="process-primary-actions">
            <button ref={startButton} className="primary-button process-start-button" type="button" onClick={startProcess} disabled={!registeredVideo || isProcessing || options.isLoading || pickerBusy || renameMutation.isPending || editingFilename}>
              {isProcessing ? <><LoaderCircle className="spin" size={16} /> 正在处理…</> : stopped ? <><RotateCcw size={16} /> 继续处理</> : showRetry ? <><RotateCcw size={16} /> 重试处理</> : <><Sparkles size={16} /> 开始处理</>}
            </button>
          </div>
          {formError && <p className="field-error process-error" role="alert"><AlertCircle size={14} />{formError}</p>}
          {job.isFetching && !isProcessing && <span className="refresh-note"><RefreshCw className="spin" size={13} /> 正在刷新任务状态</span>}
        </section>

        {jobId !== null && !showSpace && <section className="process-section process-status-section">
          <div className="section-heading compact">
            <div><p className="eyebrow">异步任务</p><h2><CharacterEye heading />当前任务状态</h2></div>
          </div>
          {commands ? <JobStatus {...commands} /> : <div className="job-empty"><Clock3 size={18} /><span>正在读取任务状态…</span></div>}
        </section>}
      </div>
      <AnimatePresence>{notice && <AnimatedPopup duration={.18} className="toast" role="status"><CheckCircle2 size={17} /><span>{notice}</span><button type="button" onClick={() => setNotice(null)} aria-label="关闭提示">×</button></AnimatedPopup>}</AnimatePresence>
    </main>
  );
}

interface JobCommandsProps {
  job: Job;
  onReview: () => void;
  onRetry: () => void;
  onCancel: () => void;
  cancelling: boolean;
  retrying: boolean;
}

function JobCommands({ job, onReview, onRetry, onCancel, cancelling, retrying }: JobCommandsProps) {
  return <div className="process-job-commands">
    {(!isJobTerminal(job) || job.status === "resource_paused") && <button className="stop-job-button" type="button" onClick={onCancel} disabled={cancelling}><Ban size={15} /> {cancelling ? "正在请求停止…" : "停止处理"}</button>}
    {job.status === "completed" && <button className="secondary-button compact" type="button" onClick={onReview}>打开复习页面 <ArrowRight size={14} /></button>}
    {["failed", "awaiting_api"].includes(job.status) && <button className="secondary-button compact" type="button" onClick={onRetry} disabled={retrying}><RotateCcw size={14} /> {retrying ? "正在重试…" : "重试任务"}</button>}
    {["interrupted", "cancelled", "canceled"].includes(job.status) && <button className="secondary-button compact" type="button" onClick={onRetry} disabled={retrying}><RotateCcw size={14} />{retrying ? "正在继续…" : "继续处理"}</button>}
    {job.status === "resource_paused" && <button className="secondary-button compact" type="button" onClick={onRetry} disabled={retrying}><RotateCcw size={14} /> {retrying ? "正在继续…" : "继续处理"}</button>}
    {job.status === "awaiting_api" && <Link className="text-button" to="/settings?section=ai"><Settings2 size={14} />AI 服务设置</Link>}
  </div>;
}

function JobStatus({ hideActions, ...commands }: JobCommandsProps & { hideActions?: boolean }) {
  const { job } = commands;
  const rawProgress = Number(job.progress ?? 0);
  const progress = Math.round(Math.max(0, Math.min(100, rawProgress <= 1 ? rawProgress * 100 : rawProgress)) * 10) / 10;
  const stage = job.stage || job.status;
  return (
    <div className={`job-status job-${job.status}`}>
      <div className="job-status-heading">
        <div className="job-status-title">{job.status === "completed" ? <CheckCircle2 size={20} /> : ["resource_paused", "awaiting_api", "interrupted"].includes(job.status) ? <PauseCircle size={20} /> : job.status === "failed" ? <AlertCircle size={20} /> : <LoaderCircle className="spin" size={20} />}<strong>{jobStatusLabel(job.status)}</strong></div>
        <span>{progress}%</span>
      </div>
      <div className="job-progress" role="progressbar" aria-label="任务进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><span style={{ width: `${progress}%` }} /></div>
      <div className="job-stage-row"><span>当前阶段：{stageLabel(stage)}</span><span>{job.message || "正在等待后台更新"}</span></div>
      {hasActivePreparation(job) && <ModelPreparationProgress job={job} />}
      <FireRedResourceStatus resource={job.resource} />
      {job.error_message && !["interrupted", "cancelled", "canceled"].includes(job.status) && <p className="job-error"><AlertCircle size={14} />{job.error_message}</p>}
      {!hideActions && <JobCommands {...commands} />}
    </div>
  );
}

function isPickerTerminal(value: PickerResult) {
  return Boolean(value.path) || ["completed", "done", "cancelled", "canceled", "failed", "error", "timeout"].includes(String(value.status).toLowerCase());
}

function isJobTerminal(value: Job) {
  return ["completed", "failed", "interrupted", "resource_paused", "awaiting_api", "cancelled", "canceled"].includes(String(value.status).toLowerCase());
}

function hasVideoExtension(path: string) {
  const normalized = path.trim().toLowerCase().split(/[?#]/)[0];
  return VIDEO_EXTENSIONS.some((extension) => normalized.endsWith(extension));
}

function fileName(path: string) {
  return path.trim().split(/[\\/]/).pop() || "";
}

function directoryName(path: string) {
  const value = path.trim();
  const index = Math.max(value.lastIndexOf("\\"), value.lastIndexOf("/"));
  return index > 0 ? value.slice(0, index) : "本机";
}

function formatBytes(value?: number) {
  const bytes = Number(value ?? 0);
  if (!bytes) return "未知";
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatDuration(value?: number) {
  const seconds = Math.max(0, Math.round(Number(value ?? 0) / 1000));
  if (!seconds) return "未知";
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function videoStatusLabel(status: string) {
  return { uploaded: "待处理", processing: "处理中", transcribed: "已转写", review_ready: "可复习", failed: "处理失败", resource_paused: "资源暂停" }[status] ?? status;
}

function jobStatusLabel(status: string) {
  return { queued: "排队中", running: "处理中", completed: "处理完成", failed: "处理失败", interrupted: "已停止", resource_paused: "资源暂停", awaiting_api: "等待 AI 恢复", cancelled: "已取消", canceled: "已取消" }[status] ?? status;
}

function stageLabel(stage: string) {
  return { starting: "准备处理", processing: "准备处理", completed: "已完成", failed: "失败", register: "登记", reading: "读取视频", transcribing: "ASR 转写", transcript: "保存字幕", analyzing: "AI 分析", saving: "保存重点", saving_markers: "保存重点", review_ready: "可复习", interrupted: "已停止" }[stage] ?? stage;
}

function optionLabel(value: unknown, fallback: string) {
  if (typeof value === "string" && value.trim()) return value;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    const label = String(record.name ?? record.label ?? record.model ?? record.id ?? record.provider ?? fallback);
    const supportsAed = record.supports_aed === true
      || record.has_aed === true
      || /firered|aed/i.test(`${record.id ?? ""} ${record.engine ?? ""} ${record.model_family ?? ""}`);
    return supportsAed && !/aed/i.test(label) ? `${label}（含 AED）` : label;
  }
  return fallback;
}

function modelName(value: unknown): string | undefined {
  if (value && typeof value === "object" && "model" in value && typeof value.model === "string") return value.model;
  return undefined;
}

function moveProcessedVideoToRoot(videoId: number) {
  try {
    const key = "classroom-video-library";
    const raw = window.localStorage.getItem(key);
    if (!raw) return;
    const state = JSON.parse(raw) as { assignments?: Record<string, string | null> };
    if (!state.assignments || !(String(videoId) in state.assignments) || state.assignments[String(videoId)] === null) return;
    state.assignments[String(videoId)] = null;
    window.localStorage.setItem(key, JSON.stringify(state));
  } catch {
    // A malformed local library should never block video processing completion.
  }
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "请求失败，请检查本地服务。";
}
