import { useUnsavedDraft } from "../components/UnsavedChanges";
import { CharacterEye } from "../components/CharacterAccent";
import { WarmAtmosphere } from "../components/WarmAtmosphere";
import { SettingsHerons } from "../components/SettingsHerons";
import { ShortcutSettings } from "../components/ShortcutSettings";
import { DesktopSettings } from "../components/DesktopSettings";
import { InteractionSettings } from "../components/InteractionSettings";
import { RecallSchemeSettings } from "../components/RecallSchemeSettings";
import { ExperimentalSettings } from "../components/ExperimentalSettings";
import { UpdateSettings } from "../components/UpdateSettings";
import { SelectionIndicator, useEntryMotion, useSelectionScope, AnimatePresence, AnimatedPopup } from "../components/Motion";
import {
  Check,
  CheckCircle2,
  Copy,
  ChevronDown,
  CircuitBoard,
  Database,
  FolderOpen,
  FlaskConical,
  HardDrive,
  LoaderCircle,
  Plus,
  Gauge,
  Languages,
  Monitor,
  Keyboard,
  RefreshCw,
  Info,
  Save,
  Server,
  SlidersHorizontal,
  Target,
  Timer,
  Trash2,
  WandSparkles,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { ModelPreparationProgress } from "../components/ModelPreparationProgress";
import { ModelDownloadControls } from "../components/ModelDownloadControls";
import { ModelCleanupProgress } from "../components/ModelCleanupProgress";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import {
  clearAsrModel,
  clearAsrRuntime,
  activateAiConnection,
  createAiConnection,
  deleteAiConnection,
  getAiRemoteModels,
  getAiSettings,
  getJobStatus,
  getAsrModels,
  getAsrModelStatus,
  getStorageDirectories,
  prepareAsrModel,
  saveAiSettings,
  selectAsrModel,
  testAiSettings,
} from "../api/client";
import type { AiSettings, AsrModel } from "../types";
import { SavedApiKey } from "../components/SavedApiKey";
import { QwenHardwareWarning } from "../components/QwenResourceControls";
import { FireRedDeviceControls } from "../components/FireRedDeviceControls";
import { GpuDeviceStatus } from "../components/GpuDeviceStatus";
import { ParaformerDeviceControls } from "../components/ParaformerDeviceControls";
import { StorageDirectoriesSection } from "../components/StorageDirectoriesSection";
import { ApplicationDataSection } from "../components/ApplicationDataSection";
import { asrStatusOptions } from "../hooks/useAsrStatus";
import { useModalFocus } from "../hooks/useModalFocus";

const DEFAULT_AI: AiSettings = {
  provider: "deepseek",
  base_url: "https://api.deepseek.com",
  model: "deepseek-chat",
  output_tokens: 16384,
  token_parameter: "max_tokens",
  json_mode: false,
  timeout_seconds: 120,
  max_retries: 5,
  reasoning_effort: "high",
  has_api_key: false,
};

function normalizeAiSettings(settings: AiSettings): AiSettings {
  return { ...settings, reasoning_effort: settings.reasoning_effort || "high" };
}

export function SettingsPage({ embedded = false, active = true, onClose, onConfigurationSaved, onJob }: {
  embedded?: boolean; active?: boolean; onClose?: () => void; onConfigurationSaved?: () => void;
  onJob?: (id: string | number) => void;
}) {
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const location = useLocation();
  const section = ["storage", "asr", "ai", "recall", "experimental", "desktop", "shortcuts", "updates"].includes(searchParams.get("section") ?? "") ? searchParams.get("section")! : "storage";
  const asr = useQuery({ queryKey: ["settings", "asr"], queryFn: getAsrModels, enabled: active && section === "asr", staleTime: 60_000 });
  const ai = useQuery({ queryKey: ["settings", "ai"], queryFn: getAiSettings, enabled: active && section === "ai" });
  const directoriesKey = ["settings", "directories"];
  const directories = useQuery({ queryKey: directoriesKey, queryFn: getStorageDirectories, enabled: active && section === "storage", refetchInterval: active && section === "storage" ? 5000 : false });
  const [notice, setNotice] = useState<{ tone: "success" | "error"; text: string } | null>(null);

  const invalidateAsr = () => queryClient.invalidateQueries({ queryKey: ["settings", "asr"] });
  const invalidateAi = () => queryClient.invalidateQueries({ queryKey: ["settings", "ai"] });

  const notify = (tone: "success" | "error", text: string) => setNotice({ tone, text });
  const previousSection = useRef(section);
  useEffect(() => {
    if (active && previousSection.current !== section) window.scrollTo({ top: 0, behavior: "instant" });
    previousSection.current = section;
  }, [active, section]);
  useEffect(() => {
    if (notice?.tone !== "success" || !active) return;
    const timer = window.setTimeout(() => setNotice(null), 4000);
    return () => window.clearTimeout(timer);
  }, [notice, active]);

  const selectionScope = useSelectionScope();
  const contentRef = useRef<HTMLDivElement>(null);
  const sectionReady = (section === "ai" ? !ai.isLoading
    : section === "asr" ? !asr.isLoading : section === "storage" ? !directories.isLoading : true);
  useEntryMotion(contentRef, { active, changeKey: section, initial: false, distance: 3, duration: .14,
    selector: ":scope > div:not([hidden]) .settings-section-heading" });
  useEntryMotion(contentRef, { active: active && sectionReady, changeKey: section, initial: false, distance: 3, duration: .14, delay: .02,
    selector: ":scope > div:not([hidden]) .settings-section > :not(.settings-section-heading):not(.modal-backdrop)" });
  return (
    <main className={`settings-page has-warm-atmosphere${embedded ? " embedded-page" : ""}`}>
      <SettingsHerons />
      <WarmAtmosphere active={active} />
      <div className="settings-page-inner">
        <header className="settings-page-heading">
          <div>
            <h1 tabIndex={-1}>设置</h1>
          </div>
          <div className="settings-heading-actions">
            {embedded ? <button className="secondary-button panel-return" onClick={onClose}>返回视频库</button>
              : <Link className="secondary-button" to="/"><Database size={16} /> 返回视频库</Link>}
          </div>
        </header>

        <AnimatePresence>{notice && (
          <AnimatedPopup duration={.18} className={`settings-notice ${notice.tone}`} role="status">
            {notice.tone === "success" ? <CheckCircle2 size={17} /> : <XCircle size={17} />}
            <span>{notice.text}</span>
            <button type="button" className="icon-button" onClick={() => setNotice(null)} aria-label="关闭提示"><XCircle size={14} /></button>
          </AnimatedPopup>
        )}</AnimatePresence>

        <div className="settings-layout">
          <nav className="settings-navigation" aria-label="设置分类">
            {([ ["storage", "存储与导入", FolderOpen], ["asr", "ASR 模型", WandSparkles], ["ai", "AI 服务", Server], ["recall", "考点提取", Target], ["desktop", "桌面设置", Monitor], ["shortcuts", "快捷键", Keyboard], ["experimental", "实验功能", FlaskConical], ["updates", "关于与更新", Info] ] as const).map(([key, label, Icon]) =>
              <button key={key} type="button" aria-current={section === key ? "page" : undefined} onClick={() => setSearchParams({ section: key }, { replace: true, state: location.state })}>
                {section === key && <SelectionIndicator id={`${selectionScope}-settings`} />}<Icon size={18} /><span>{label}</span>
              </button>)}
          </nav>
          <div ref={contentRef} className="settings-content">
        <div hidden={section !== "updates"}><UpdateSettings active={active && section === "updates"} /></div>
        <div hidden={section !== "recall"}><RecallSchemeSettings active={active && section === "recall"} onSaved={onConfigurationSaved} /></div>
        <div hidden={section !== "shortcuts"}><ShortcutSettings /></div>
        <div hidden={section !== "desktop"}><DesktopSettings active={active && section === "desktop"} /><InteractionSettings /></div>
        <div hidden={section !== "experimental"}><ExperimentalSettings active={active && section === "experimental"} /></div>
        <div hidden={section !== "storage"}>
        <StorageDirectoriesSection
          data={directories.data}
          isLoading={directories.isLoading}
          isError={directories.isError}
          onRetry={() => void directories.refetch()}
          onStarted={onJob}
          onSuccess={(value) => {
            queryClient.setQueryData(directoriesKey, value);
            void queryClient.invalidateQueries({ queryKey: ["library"] });
            void queryClient.invalidateQueries({ queryKey: ["videos"] });
            void queryClient.invalidateQueries({ queryKey: ["video"] });
            void queryClient.invalidateQueries({ queryKey: ["library-status"] });
            void queryClient.invalidateQueries({ queryKey: ["settings", "asr"] });
            notify("success", value.restart_required ? "目录已保存。请重启软件后使用新的模型资源与日志目录。" : "存储设置已生效。");
          }}
          onError={(error) => notify("error", errorMessage(error))}
        />
        <ApplicationDataSection active={active && section === "storage"} directories={directories.data} onError={(error) => notify("error", errorMessage(error))} />
        </div>
        <div hidden={section !== "asr"}>
        <AsrSection
          active={active && section === "asr"}
          onJob={onJob}
          onSaved={onConfigurationSaved}
          models={asr.data ?? []}
          isLoading={asr.isLoading}
          isError={asr.isError}
          onSuccess={(text) => {
            invalidateAsr();
            notify("success", text);
          }}
          onError={(error) => notify("error", errorMessage(error))}
        />
        </div>
        <div hidden={section !== "ai"}>
        <AiSection
          active={active && section === "ai"}
          onSaved={onConfigurationSaved}
          data={ai.data}
          isLoading={ai.isLoading}
          isError={ai.isError}
          onSuccess={(value) => {
            if (typeof value === "string") {
              notify("success", value);
            } else {
              queryClient.setQueryData(["settings", "ai"], value);
              void queryClient.invalidateQueries({ queryKey: ["process", "options"] });
              notify("success", "AI 服务配置已保存。");
            }
          }}
          onError={(error) => notify("error", errorMessage(error))}
          onInvalidate={invalidateAi}
        />
        </div>
          </div>
        </div>
      </div>
    </main>
  );
}

function AsrSection({
  active = true,
  onSaved,
  models: allModels,
  isLoading,
  isError,
  onSuccess,
  onError,
  onJob,
}: {
  active?: boolean;
  onSaved?: () => void;
  models: AsrModel[];
  isLoading: boolean;
  isError: boolean;
  onSuccess: (text: string) => void;
  onError: (error: unknown) => void;
  onJob?: (id: string | number) => void;
}) {
  const queryClient = useQueryClient();
  const models = useMemo(() => allModels.filter((model) =>
    model.selectable !== false && model.engine !== "nano" && model.id !== "fun-asr-nano-2512",
  ).sort((left, right) => asrModelOrder(left) - asrModelOrder(right)), [allModels]);
  const [searchParams] = useSearchParams();
  const editing = searchParams.get("section") === "asr";
  const checkedModels = editing ? models : models.filter((model) => model.selected);
  const statusQueries = useQueries({
    queries: checkedModels.map((model) => ({ ...asrStatusOptions(model, editing), enabled: active })),
  });
  const displayModels = useMemo(() => {
    const statuses = new Map(statusQueries.filter((query) => query.data).map((query) => [query.data!.id, query.data!]));
    return models.map((model) => {
      const previous = queryClient.getQueryData<AsrModel>(asrStatusOptions(model, !editing).queryKey);
      const previousFields = Object.fromEntries(Object.entries(previous ?? {}).filter(([, value]) => value != null));
      return {
        ...model, ...previousFields, ...(statuses.get(model.id) ?? {}), selected: model.selected, device: model.device,
      };
    });
  }, [models, statusQueries, queryClient, editing]);
  const queryFor = (id: string) => statusQueries[checkedModels.findIndex((model) => model.id === id)];
  const refreshModels = async (targets: AsrModel[]) => {
    await Promise.allSettled(targets.map((model) => queryClient.fetchQuery({
      ...asrStatusOptions(model, editing), staleTime: 0,
      queryFn: () => getAsrModelStatus(model.id, { full: editing, refresh: true }),
    })));
  };
  const [selectedId, setSelectedId] = useState("");
  const [acceptedSelection, setAcceptedSelection] = useState<string | null>(null);
  const [deviceDrafts, setDeviceDrafts] = useState<Record<string, "gpu" | "cpu">>({});
  const savedModel = models.find((model) => model.selected);
  const deviceFor = (model: AsrModel): "gpu" | "cpu" => {
    if (deviceDrafts[model.id]) return deviceDrafts[model.id];
    if (isFireRedModel(model)) return model.selected ? model.device ?? "gpu" : "gpu";
    // Keep an explicitly saved device; new selections prefer a usable CUDA runtime.
    if (model.selected && model.device) return model.device;
    return model.gpu_available === true ? "gpu" : "cpu";
  };
  const setDevice = (model: AsrModel, device: "gpu" | "cpu") => {
    setDeviceDrafts((previous) => ({ ...previous, [model.id]: device }));
  };
  const selectDevice = (model: AsrModel, device: "gpu" | "cpu") => {
    setSelectedId(model.id);
    setDevice(model, device);
  };
  const [preparingId, setPreparingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [operation, setOperation] = useState<{ modelId: string; jobId: string | number; kind: "preparing" | "deleting" | "cleaning_partial" | "cleaning_runtime" } | null>(null);
  const finishedJobs = useRef(new Set<string>());
  const moreModels = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (operation && models.some((model) => model.id === operation.modelId && model.engine === "whisper")
      && moreModels.current) moreModels.current.open = true;
  }, [operation, models, editing]);
  useEffect(() => {
    if (!active || operation) return;
    const running = displayModels.find((model) => model.active_job_id != null
      && model.active_job_kind && !finishedJobs.current.has(String(model.active_job_id)));
    if (running) {
      setOperation({ modelId: running.id, jobId: running.active_job_id!, kind: running.active_job_kind! });
      onJob?.(running.active_job_id!);
    }
  }, [active, displayModels, operation, onJob]);
  const operationQuery = useQuery({
    queryKey: ["jobs", operation?.jobId ?? null],
    queryFn: () => getJobStatus(operation!.jobId),
    enabled: active && operation !== null,
    staleTime: 1000,
    refetchInterval: active ? (query) => {
      const status = query.state.data?.status;
      return status === "completed" || status === "failed" || status === "interrupted" ? false : 1000;
    } : false,
  });
  const selectionMutation = useMutation({
    mutationFn: ({ modelId, device }: { modelId: string; device: "gpu" | "cpu" }) =>
      models.find((model) => model.id === modelId)?.engine === "paraformer"
        ? selectAsrModel(modelId, undefined, device)
        : models.find((model) => model.id === modelId)?.engine === "dolphin"
          ? selectAsrModel(modelId, undefined, undefined, undefined, device)
          : models.find((model) => model.id === modelId)?.engine === "firered"
            ? selectAsrModel(modelId, device) : selectAsrModel(modelId),
    onError,
  });

  useEffect(() => {
    setSelectedId(savedModel?.id ?? "");
  }, [savedModel?.id]);

  useEffect(() => {
    const job = operationQuery.data;
    if (!job || !operation) return;
    if (job.status === "completed") {
      finishedJobs.current.add(String(operation.jobId));
      onSuccess(operation.kind === "preparing" ? "模型准备完成。" : operation.kind === "cleaning_partial"
        ? "未完成下载已清理，已完成权重和运行环境已保留。" : operation.kind === "cleaning_runtime"
          ? "运行环境清理完成，模型权重已保留。" : "模型缓存清理完成。");
      setOperation(null);
      void queryClient.invalidateQueries({ queryKey: ["asr-status", operation.modelId] });
      void queryClient.invalidateQueries({ queryKey: ["settings", "asr"] });
    } else if (job.status === "failed" || job.status === "interrupted") {
      finishedJobs.current.add(String(operation.jobId));
      if (job.stage === "download_paused") onSuccess("下载已暂停，稍后可继续下载。");
      else onError(new Error(job.error_message || job.message || "模型任务失败"));
      setOperation(null);
      void queryClient.invalidateQueries({ queryKey: ["asr-status", operation.modelId] });
      void queryClient.invalidateQueries({ queryKey: ["settings", "asr"] });
    }
  }, [operationQuery.data, operation, onSuccess, onError, queryClient]);

  const saveSelection = async (leaving = false) => {
    if (!selectedModel || unavailableDevice) throw new Error("请选择可用的 ASR 模型和处理设备。");
    if (selectionMutation.isPending) throw new Error("模型选择正在保存，请稍候。");
    await selectionMutation.mutateAsync({ modelId: selectedId, device: deviceFor(selectedModel) });
    setAcceptedSelection(`${selectedId}:${deviceFor(selectedModel)}`);
    clearAsrDraft();
    onSuccess("ASR 模型和处理设备已保存。");
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["settings", "asr", "selection"] }),
      queryClient.invalidateQueries({ queryKey: ["settings", "asr"], exact: true }),
      queryClient.invalidateQueries({ queryKey: ["asr-status", selectedId] }),
      queryClient.invalidateQueries({ queryKey: ["process", "options"] }),
    ]);
    if (!leaving) onSaved?.();
    return true;
  };

  const prepare = async (model: AsrModel, device?: "cpu" | "gpu") => {
    if (operation || preparingId || deletingId) return;
    setPreparingId(model.id);
    try {
      const result = model.engine === "paraformer"
        ? await prepareAsrModel(model.id, device ?? deviceFor(model))
        : model.engine === "dolphin" ? await prepareAsrModel(model.id, device ?? deviceFor(model))
        : await prepareAsrModel(model.id);
      if (result.job_id !== undefined) {
        setOperation({ modelId: model.id, jobId: result.job_id, kind: "preparing" });
        onJob?.(result.job_id);
      } else {
        onSuccess(`${model.name || model.id} 已加入准备任务。`);
      }
      await queryClient.invalidateQueries({ queryKey: ["settings", "asr"] });
    } catch (error) {
      onError(error);
    } finally {
      setPreparingId(null);
    }
  };

  const remove = async (model: AsrModel, removeRuntime = false) => {
    if (operation || preparingId || deletingId) return;
    setDeletingId(model.id);
    try {
      const result = removeRuntime
        ? await clearAsrRuntime(model.id, ["paraformer", "dolphin"].includes(model.engine) ? deviceFor(model) : undefined)
        : await clearAsrModel(model.id, false);
      if (result.job_id !== undefined) {
        setOperation({ modelId: model.id, jobId: result.job_id, kind: removeRuntime ? "cleaning_runtime" : "deleting" });
        onJob?.(result.job_id);
      } else {
        onSuccess(`${model.name || model.id} 的缓存清理任务已提交。`);
      }
      await queryClient.invalidateQueries({ queryKey: ["settings", "asr"] });
    } catch (error) {
      onError(error);
    } finally {
      setDeletingId(null);
    }
  };

  const renderModel = (model: AsrModel) => {
    const selected = selectedId === model.id;
    const activeOperation = operation?.modelId === model.id ? operation : null;
    const unavailable = model.status === "preparing" || model.status === "deleting" || Boolean(activeOperation);
    const operationBusy = Boolean(operation || preparingId || deletingId || unavailable);
    const modelReady = model.downloaded === true && !activeOperation && preparingId !== model.id
      && model.download_state?.resumable !== true;
    const fireRed = isFireRedModel(model);
    const dualDevice = ["paraformer", "dolphin"].includes(model.engine);
    const savedRuntime = model.selected && (!dualDevice || deviceFor(model) === (model.device ?? "cpu"));
    const profile = asrModelProfile(model);
    return (
      <article className={`asr-model-option${selected ? " selected" : ""}`} key={model.id} data-model-id={model.id}
        data-selection-disabled={selectionMutation.isPending || undefined}
        onClick={(event) => {
          if (selectionMutation.isPending || !(event.target instanceof Element)
            || event.target.closest("button, a, input, label, select, textarea, summary, [role='button'], .model-preparation, .model-download-controls")
            || window.getSelection()?.isCollapsed === false) return;
          setSelectedId(model.id);
          event.currentTarget.querySelector<HTMLInputElement>('input[name="asr-model"]')?.focus({ preventScroll: true });
        }}>
        <label className="asr-model-main">
          <input type="radio" name="asr-model" checked={selected} disabled={selectionMutation.isPending} onChange={() => setSelectedId(model.id)} />
          <span><strong>{asrModelName(model)}</strong></span>
          {selected && <CheckCircle2 className="asr-selected-icon" size={18} />}
        </label>
        <dl className="asr-model-meta">
          <div><dt>状态</dt><dd role="status">{activeOperation ? activeOperation.kind === "preparing" ? "准备中" : "清理中" : asrStatusLabel(model, queryFor(model.id))}</dd></div>
          <div><dt>缓存</dt><dd>{formatBytes(model.cache_size_bytes)}</dd></div>
        </dl>
        {profile ? <div className="asr-model-features">
          <div className="asr-model-traits">
            <span className={`asr-feature-flag ${profile.tone}`}><profile.icon size={15} aria-hidden="true" />{profile.feature}</span>
          </div>
          <p className="asr-model-description">{profile.description}</p>
          {profile.comparison && <div className="asr-model-comparison">
            <span><Timer size={14} aria-hidden="true" />{profile.comparison.label}</span>
            <strong>{profile.comparison.value}</strong>
          </div>}
        </div> : model.recommendation && <p className="asr-recommendation">{model.recommendation}</p>}
        {model.engine === "qwen" && <>
          <QwenHardwareWarning />
          <div className="firered-device-controls">
            <fieldset className="firered-device-selector"><legend>处理设备</legend>
              <div className="firered-device-modes"><span className="asr-fixed-device"><CircuitBoard size={16} aria-hidden="true" />GPU</span></div>
            </fieldset>
            <GpuDeviceStatus model={model} />
          </div>
        </>}
        {fireRed && <FireRedDeviceControls model={model} device={deviceFor(model)} onChange={(value) => setDevice(model, value)}
          onSelect={(value) => selectDevice(model, value)} />}
        {(model.engine === "paraformer" || model.engine === "dolphin") && <ParaformerDeviceControls
          device={deviceFor(model)} model={model} onChange={(value) => setDevice(model, value)}
          onSelect={(value) => selectDevice(model, value)} />}
        {model.error_message && <p className="asr-error">{model.error_message}</p>}
        {activeOperation?.kind === "preparing" && <ModelPreparationProgress job={operationQuery.data} connectionError={operationQuery.isError} />}
        {activeOperation && activeOperation.kind !== "preparing" && <ModelCleanupProgress
          job={operationQuery.data} connectionError={operationQuery.isError} />}
        <ModelDownloadControls model={model} job={activeOperation?.kind === "preparing" ? operationQuery.data : undefined}
          preparing={activeOperation?.kind === "preparing"} busy={operationBusy}
          onJob={(jobId, kind) => {
            setOperation({ modelId: model.id, jobId, kind });
            onJob?.(jobId);
          }} />
        {queryFor(model.id)?.isError && <div className="asr-check-error">
          <span>状态检查失败{model.downloaded != null ? "，保留上次结果" : ""}</span>
          <button className="icon-button" type="button" title="重新检查" aria-label={`重新检查${model.name}`}
            onClick={() => void refreshModels([model])}><RefreshCw size={14} /></button>
        </div>}
        <div className="asr-model-actions">
          {(modelReady || !model.download_state || model.download_state.resumable === false || Boolean(activeOperation)) && <button className="secondary-button compact" type="button" disabled={operationBusy || modelReady} title={modelReady ? "模型已准备，无需重复操作" : undefined} onClick={() => void prepare(model)}>
            {modelReady ? <CheckCircle2 size={14} /> : preparingId === model.id || activeOperation?.kind === "preparing" ? <LoaderCircle className="spin" size={14} /> : <WandSparkles size={14} />} {modelReady ? "模型已准备" : "准备模型"}
          </button>}
          {(model.engine === "paraformer" || model.engine === "dolphin") && !model.gpu_runtime_ready && (
            <button className="secondary-button compact" type="button"
              disabled={!model.gpu_prepare_available || operationBusy}
              onClick={() => void prepare(model, "gpu")}>
              <WandSparkles size={14} /> 准备 GPU 环境
            </button>
          )}
          <button className="icon-button danger-icon" type="button" title="清理模型缓存" aria-label={`清理${model.name || model.id}缓存`} disabled={(!model.downloaded && !(model.cache_size_bytes && model.cache_size_bytes > 0)) || model.selected || operationBusy} onClick={() => void remove(model)}>
            {deletingId === model.id || activeOperation?.kind === "deleting" ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}
          </button>
          {(model.engine === "qwen" || fireRed || model.engine === "paraformer" || model.engine === "dolphin") && <button className="icon-button danger-icon" type="button"
            title={`清理 ${asrEngineLabel(model)} ${["paraformer", "dolphin"].includes(model.engine) ? deviceFor(model).toUpperCase() : "GPU"} 运行环境（保留模型权重）`}
            aria-label={`清理 ${asrEngineLabel(model)} ${["paraformer", "dolphin"].includes(model.engine) ? deviceFor(model).toUpperCase() : "GPU"} 运行环境（保留模型权重）`}
            disabled={savedRuntime || operationBusy} onClick={() => void remove(model, true)}><HardDrive size={14} /></button>}
        </div>
      </article>
    );
  };
  const whisperModels = displayModels.filter((model) => model.engine === "whisper");
  const selectedModel = displayModels.find((model) => model.id === selectedId);
  const selectedDevice = selectedModel ? deviceFor(selectedModel) : undefined;
  const savedDevice = savedModel ? savedModel.device ?? (isFireRedModel(savedModel) ? "gpu" : "cpu") : undefined;
  const hasDeviceChoice = selectedModel && (isFireRedModel(selectedModel) || selectedModel.engine === "paraformer" || selectedModel.engine === "dolphin");
  const unavailableDevice = selectedModel && (
    (isFireRedModel(selectedModel) && selectedDevice !== "gpu")
    || ((selectedModel.engine === "paraformer" || selectedModel.engine === "dolphin")
      && selectedDevice === "gpu" && selectedModel.gpu_available !== true)
  );
  const selectionUnchanged = selectedId === savedModel?.id
    && (!hasDeviceChoice || selectedDevice === savedDevice);

  const clearAsrDraft = useUnsavedDraft({
    label: "ASR 模型与设备", dirty: Boolean(selectedModel && !selectionUnchanged && `${selectedId}:${deviceFor(selectedModel)}` !== acceptedSelection),
    busy: selectionMutation.isPending, save: () => saveSelection(true),
    discard: () => { setSelectedId(savedModel?.id ?? ""); setDeviceDrafts({}); setAcceptedSelection(null); },
  });
  return (
    <section className="settings-section" aria-labelledby="asr-settings-title">
      <div className="settings-section-heading">
        <div className="settings-section-title"><span className="settings-section-icon"><WandSparkles size={18} /></span><div><p className="eyebrow">ASR 模型</p><h2 id="asr-settings-title"><CharacterEye heading />选择转写引擎</h2></div></div>
      </div>
      {isLoading && <SettingsLoading label="正在读取模型列表…" />}
      {isError && <SettingsError label="ASR 模型状态读取失败" />}
      {!isLoading && !isError && !editing && (
        <div className="asr-current-summary">
          {displayModels.filter((model) => model.selected).map((model) => (
            <div key={model.id}>
              <strong>{model.name}</strong>
              <span>{model.device?.toUpperCase() || asrEngineLabel(model)}</span>
              <span role="status">{asrStatusLabel(model, queryFor(model.id))}</span>
              {queryFor(model.id)?.isError && <button className="icon-button" type="button"
                title="重新检查当前模型" aria-label="重新检查当前模型" onClick={() => void refreshModels([model])}>
                <RefreshCw size={16} />
              </button>}
            </div>
          ))}
          {!displayModels.some((model) => model.selected) && <span>尚未选择模型</span>}
        </div>
      )}
      {!isLoading && !isError && editing && (
        <>
          <div className="asr-model-grid">
            {displayModels.filter((model) => model.engine !== "whisper").map(renderModel)}
          </div>
          {whisperModels.length > 0 && <details className="asr-more-models" ref={moreModels}>
            <summary><ChevronDown size={17} aria-hidden="true" /><span className="asr-more-open">展开更多</span><span className="asr-more-close">收起更多</span><small>Whisper · {whisperModels.length} 个模型</small></summary>
            <div className="asr-model-grid asr-more-grid">{whisperModels.map(renderModel)}</div>
          </details>}
          <p className="asr-benchmark-note">耗时为 60 分钟视频的参考值，含按相对速度换算的数据；实际表现随硬件和录音条件变化。</p>
          <div className="settings-action-row">
            <button className="primary-button" type="button" disabled={!selectedModel || selectionMutation.isPending
              || Boolean(unavailableDevice) || selectionUnchanged} onClick={() => void saveSelection().catch(() => undefined)}>
              {selectionMutation.isPending ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />} 保存模型选择
            </button>
            {selectedModel && <span className="asr-current-choice">当前选择：<strong>{asrModelName(selectedModel)}</strong></span>}
          </div>
        </>
      )}
    </section>
  );
}

function AiSection({
  active,
  onSaved,
  data,
  isLoading,
  isError,
  onSuccess,
  onError,
  onInvalidate,
}: {
  active: boolean;
  onSaved?: () => void;
  data?: AiSettings;
  isLoading: boolean;
  isError: boolean;
  onSuccess: (data: AiSettings | string) => void;
  onError: (error: unknown) => void;
  onInvalidate: () => void;
}) {
  const [form, setForm] = useState<AiSettings>(DEFAULT_AI);
  const [apiKey, setApiKey] = useState("");
  const [keyRevision, setKeyRevision] = useState(0);
  const [remoteModels, setRemoteModels] = useState<string[]>([]);
  const [modelsDialogOpen, setModelsDialogOpen] = useState(false);
  const [modelSearch, setModelSearch] = useState("");
  const [testResult, setTestResult] = useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [testing, setTesting] = useState(false);
  const [loadingModels, setLoadingModels] = useState(false);
  const dirty = useRef(false);
  const baseline = useRef<AiSettings>(DEFAULT_AI);
  const [draftingNew, setDraftingNew] = useState(false);
  const [copySourceId, setCopySourceId] = useState<string | undefined>();
  const [removeKey, setRemoveKey] = useState(false);
  const [saving, setSaving] = useState(false);
  type ConnectionAction = { kind: "switch" | "create" | "duplicate" | "delete"; id?: string };
  const [connectionAction, setConnectionAction] = useState<ConnectionAction | null>(null);
  const [connectionDialog, setConnectionDialog] = useState<"unsaved" | "delete" | null>(null);
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [connectionBusy, setConnectionBusy] = useState(false);
  const connectionRunning = useRef(false);
  const connectionDialogRef = useRef<HTMLElement>(null);
  const connectionCloseRef = useRef<HTMLButtonElement>(null);
  const connectionNameRef = useRef<HTMLInputElement>(null);
  useModalFocus(active && Boolean(connectionDialog), connectionDialogRef, connectionCloseRef,
    () => { if (!connectionRunning.current) setConnectionDialog(null); });
  useEffect(() => {
    if (active && connectionDialog && !connectionBusy) {
      connectionCloseRef.current?.focus({ preventScroll: true });
    }
  }, [active, connectionDialog, connectionBusy]);
  const modelsDialogRef = useRef<HTMLElement>(null);
  const modelsCloseRef = useRef<HTMLButtonElement>(null);
  useModalFocus(active && modelsDialogOpen, modelsDialogRef, modelsCloseRef, () => setModelsDialogOpen(false));
  useEffect(() => { if (!active) setModelsDialogOpen(false); }, [active]);
  useEffect(() => {
    if (!data || draftingNew || dirty.current || apiKey.trim() || removeKey) return;
    const normalized = normalizeAiSettings(data);
    baseline.current = normalized;
    setForm(normalized);
  }, [data]);
  useEffect(() => { if (draftingNew && active) connectionNameRef.current?.focus(); }, [draftingNew, active]);

  const update = <K extends keyof AiSettings>(key: K, value: AiSettings[K]) => {
    const next = { ...form, [key]: value };
    const editable = (settings: AiSettings) => [
      settings.connection_name, settings.base_url, settings.model, settings.output_tokens,
      settings.token_parameter, settings.json_mode, settings.timeout_seconds,
      settings.max_retries, settings.reasoning_effort ?? "high",
    ];
    dirty.current = JSON.stringify(editable(next)) !== JSON.stringify(editable(baseline.current));
    if (key === "base_url") {
      next.has_api_key = String(value).trim().replace(/\/+$/, "") === baseline.current.base_url
        ? baseline.current.has_api_key : false;
      setApiKey("");
      setRemoveKey(false);
      setKeyRevision((revision) => revision + 1);
      setRemoteModels([]);
    }
    setTestResult(null);
    setForm(next);
  };
  const credentials = () => apiKey.trim() ? { api_key: apiKey.trim() } : removeKey ? { remove_key: true } : undefined;
  const requestDraft = () => {
    const { has_api_key: _hasKey, ...settings } = form;
    return { settings, ...(apiKey.trim() ? { api_key: apiKey.trim() }
      : removeKey || !form.has_api_key ? { api_key: "" } : {}) };
  };
  const persistDraft = async () => {
    if (!form.connection_name?.trim() || !form.base_url.trim() || !form.model.trim())
      throw new Error("请填写连接名称、接口地址和模型名称。");
    const { has_api_key: _hasKey, ...settings } = form;
    const value = draftingNew
      ? await createAiConnection(form.connection_name.trim(), copySourceId, settings, credentials())
      : await saveAiSettings(settings, credentials());
    acceptConnection(value);
    return value;
  };
  const save = async (leaving = false) => {
    if (connectionRunning.current) throw new Error("连接正在保存，请稍候。");
    connectionRunning.current = true;
    setSaving(true);
    try { await persistDraft(); clearAiDraft(); if (!leaving) onSaved?.(); return true; }
    catch (error) { onError(error); if (leaving) throw error; return false; }
    finally { connectionRunning.current = false; setSaving(false); }
  };
  const test = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const response = await testAiSettings(form.base_url, form.connection_id ?? copySourceId, form.model, requestDraft());
      const message = response.message?.trim() || "API 已连接，服务返回成功。";
      setTestResult({ tone: "success", text: message });
      onSuccess(message);
    } catch (error) {
      setTestResult({ tone: "error", text: errorMessage(error) });
      onError(error);
    } finally {
      setTesting(false);
    }
  };
  const loadModels = async () => {
    setLoadingModels(true);
    setTestResult(null);
    try {
      const models = await getAiRemoteModels(form.base_url, form.connection_id ?? copySourceId, requestDraft());
      setRemoteModels(models);
      setModelSearch("");
      setModelsDialogOpen(true);
      onSuccess(models.length ? `已获取 ${models.length} 个远程模型，请选择要使用的模型。` : "接口返回了空的模型列表。");
    } catch (error) {
      setModelsDialogOpen(false);
      setTestResult({ tone: "error", text: errorMessage(error) });
      onError(error);
    } finally {
      setLoadingModels(false);
    }
  };

  const hasChanges = draftingNew || dirty.current || Boolean(apiKey.trim()) || removeKey;
  const emptyConnections = data?.connections?.length === 0;
  const busy = connectionBusy || saving || testing || loadingModels;
  const endpointReady = Boolean(form.base_url.trim() && (apiKey.trim() || (form.has_api_key && !removeKey)));
  const acceptConnection = (value: AiSettings, notify = true) => {
    value = normalizeAiSettings(value);
    dirty.current = false;
    baseline.current = value;
    setForm(value);
    setDraftingNew(false);
    setCopySourceId(undefined);
    setRemoveKey(false);
    setApiKey("");
    setKeyRevision((revision) => revision + 1);
    setRemoteModels([]);
    setTestResult(null);
    clearAiDraft();
    if (notify) onSuccess(value);
  };
  const performAction = async (action: ConnectionAction) => {
    if (action.kind === "switch") {
      acceptConnection(await activateAiConnection(action.id!));
      setConnectionDialog(null);
    } else if (action.kind === "delete") {
      setConnectionDialog("delete");
    } else {
      const source = baseline.current;
      const next: AiSettings = action.kind === "duplicate"
        ? { ...source, connection_id: undefined, connection_name: `${source.connection_name || "当前连接"} 副本` }
        : { ...DEFAULT_AI, provider: "openai_compatible", base_url: "", model: "", connection_name: "" };
      dirty.current = false;
      baseline.current = next;
      setForm(next);
      setDraftingNew(true);
      setCopySourceId(action.kind === "duplicate" ? source.connection_id : undefined);
      setApiKey("");
      setRemoveKey(false);
      setKeyRevision((revision) => revision + 1);
      setRemoteModels([]);
      setTestResult(null);
      setConnectionDialog(null);
    }
  };
  const runConnectionTask = async (operation: () => Promise<void>) => {
    if (connectionRunning.current) return;
    connectionRunning.current = true;
    setConnectionBusy(true);
    setConnectionError(null);
    try { await operation(); } catch (error) {
      setConnectionError(errorMessage(error));
      onError(error);
    } finally {
      connectionRunning.current = false;
      setConnectionBusy(false);
    }
  };
  const requestAction = (action: ConnectionAction) => {
    setConnectionAction(action);
    setConnectionError(null);
    if (hasChanges) setConnectionDialog("unsaved");
    else if (action.kind === "switch") void runConnectionTask(() => performAction(action));
    else void performAction(action);
  };
  const continueAction = (saveDraft: boolean) => void runConnectionTask(async () => {
    if (!connectionAction) return;
    if (saveDraft) {
      await persistDraft();
    }
    // Keep the current draft until the requested server operation succeeds.
    await performAction(connectionAction);
  });
  const submitConnection = () => void runConnectionTask(async () => {
    acceptConnection(await deleteAiConnection(form.connection_id!));
    setConnectionDialog(null);
  });
  const resetDraft = () => {
    if (data) acceptConnection(data, false);
  };

  const clearAiDraft = useUnsavedDraft({ label: "AI 服务连接", dirty: hasChanges, busy,
    save: () => save(true), discard: () => { resetDraft(); setConnectionDialog(null); setConnectionError(null); },
  });
  return (
    <section className="settings-section" aria-labelledby="ai-settings-title">
      <div className="settings-section-heading">
        <div className="settings-section-title"><span className="settings-section-icon"><Server size={18} /></span><div><p className="eyebrow">AI 服务</p><h2 id="ai-settings-title"><CharacterEye heading />分析服务连接</h2></div></div>
        <span className={`settings-state ${draftingNew || emptyConnections ? "muted" : "ready"}`}>{draftingNew ? "新连接 · 尚未启用" : emptyConnections ? "尚未配置" : "当前连接"}</span>
      </div>
      {isLoading && <SettingsLoading label="正在读取 AI 配置…" />}
      {isError && <SettingsError label="AI 服务配置读取失败" />}
      {!isLoading && !isError && (
        <fieldset className="ai-connection-editor" disabled={busy}>
          {data?.connections && <div className="ai-connections">
            <label className="settings-field"><span>已保存连接</span>
              <select aria-label="已保存连接" value={draftingNew ? "__new__" : form.connection_id ?? data.connection_id}
                onChange={(event) => requestAction({ kind: "switch", id: event.target.value })}>
                {draftingNew && <option value="__new__" disabled>新连接（未保存）</option>}
                {!draftingNew && emptyConnections && <option value="" disabled>暂无已保存连接</option>}
                {(data.connections ?? []).map((connection) => <option key={connection.id} value={connection.id}>{connection.name}{connection.model ? ` · ${connection.model}` : ""}</option>)}
              </select>
            </label>
            <div className="ai-connection-actions">
              <button className="secondary-button compact" type="button" onClick={() => requestAction({ kind: "create" })}><Plus size={15} />新增</button>
              <button className="secondary-button compact" type="button" disabled={draftingNew || emptyConnections} aria-label="复制当前连接" onClick={() => requestAction({ kind: "duplicate" })}><Copy size={15} />复制</button>
              <button className="icon-button danger-icon" type="button" aria-label="删除当前连接" title="删除当前连接"
                disabled={draftingNew || emptyConnections} onClick={() => requestAction({ kind: "delete" })}><Trash2 size={16} /></button>
            </div>
            <p className="settings-hint">{draftingNew
              ? `${emptyConnections ? "当前未配置连接。" : `正在使用：${data.connection_name || "当前连接"}。`}新连接保存后启用。`
              : emptyConnections ? "尚未保存 AI 服务连接，点击“新增”开始配置。正在运行的任务不受影响。"
              : "选择连接后立即用于新任务；运行中的任务继续使用原连接。"}</p>
          </div>}
          {(!emptyConnections || draftingNew) && <>
          <div className="settings-form-grid">
            <label className="settings-field full"><span>连接名称</span><input ref={connectionNameRef} maxLength={80} value={form.connection_name ?? ""}
              onChange={(event) => update("connection_name", event.target.value)} placeholder="例如：日常分析 / 高精度模型" /></label>
            <div className="settings-field full ai-endpoint-field">
              <div className="ai-endpoint-heading"><label htmlFor="ai-base-url">接口地址（Base URL）</label>
                <div className="ai-address-presets" aria-label="快捷填写地址"><span>快捷填写</span>
                  <button className="text-button" type="button" onClick={() => update("base_url", "https://api.deepseek.com")}>DeepSeek</button>
                  <button className="text-button" type="button" onClick={() => update("base_url", "https://api.openai.com/v1")}>OpenAI</button>
                </div>
              </div>
              <input id="ai-base-url" value={form.base_url} onChange={(event) => update("base_url", event.target.value)} placeholder="https://你的服务地址/v1" />
              <p className="settings-hint">填写服务商提供的 API 根地址；预设仅填写地址，自定义服务可直接粘贴。</p>
            </div>
          </div>
          <div className="ai-key-row ai-unified-key-row">
            <span className="field-label">API Key</span>
            <SavedApiKey key={`${form.connection_id}:${form.base_url}:${removeKey}:${form.has_api_key}:${keyRevision}`}
              connectionId={form.connection_id ?? copySourceId} baseUrl={form.base_url} hasApiKey={form.has_api_key && !removeKey}
              onError={onError} editable draftValue={apiKey} onDraftChange={(value) => { setApiKey(value); setRemoveKey(false); setTestResult(null); }} />
            <button className="icon-button danger-icon" type="button"
              title={removeKey ? "恢复已保存的 API Key" : "移除已保存的 API Key"} aria-label={removeKey ? "恢复已保存的 API Key" : "移除已保存的 API Key"}
              disabled={!form.has_api_key} onClick={() => { setRemoveKey((value) => !value); setApiKey(""); setTestResult(null); }}><Trash2 size={14} /></button>
            <p className="settings-hint">{removeKey ? "保存连接后移除 Key；再次点击可恢复。"
              : apiKey.trim() ? "新 Key 尚未保存，可直接获取模型或测试连接。"
              : form.has_api_key ? "已保存 Key。留空保留原 Key，输入新值可替换。"
              : "粘贴 API Key 后即可获取模型，最后统一保存连接。"}</p>
          </div>
          <div className="settings-form-grid">
            <div className="ai-model-field">
              <label className="settings-field"><span>模型名称</span><input value={form.model} onChange={(event) => update("model", event.target.value)} placeholder="获取后选择，或手动输入" /></label>
              <button className="secondary-button compact" type="button" disabled={!endpointReady || loadingModels} onClick={() => void loadModels()}>
                {loadingModels ? <LoaderCircle className="spin" size={16} /> : <Server size={16} />} 获取模型
              </button>
            </div>
            <label className="settings-field full"><span>思考强度</span>
              <select aria-label="思考强度" aria-describedby="ai-reasoning-hint" value={form.reasoning_effort ?? "high"}
                onChange={(event) => update("reasoning_effort", event.target.value as AiSettings["reasoning_effort"])}>
                <option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option>
              </select>
            </label>
          </div>
          <p id="ai-reasoning-hint" className="settings-hint">GPT-6 Luna、DeepSeek 4.1 Flash 建议最低使用 Medium；默认 High。</p>
          <details className="advanced-settings">
            <summary><SlidersHorizontal size={16} />高级参数</summary>
            <p className="settings-hint">接口格式：OpenAI 兼容 Chat Completions。以下参数跟随当前连接保存。</p>
            <div className="settings-form-grid">
            <label className="settings-field"><span>输出 Token 上限</span><input type="number" min={1} value={form.output_tokens} onChange={(event) => update("output_tokens", Number(event.target.value))} /></label>
            <label className="settings-field"><span>Token 参数名称</span><input value={form.token_parameter} onChange={(event) => update("token_parameter", event.target.value)} placeholder="max_tokens" /></label>
            <label className="settings-field"><span>请求超时（秒）</span><input type="number" min={1} value={form.timeout_seconds} onChange={(event) => update("timeout_seconds", Number(event.target.value))} /></label>
            <label className="settings-field"><span>最大重试次数</span><input type="number" min={0} value={form.max_retries} onChange={(event) => update("max_retries", Number(event.target.value))} /></label>
            <label className="settings-checkbox full"><input type="checkbox" checked={form.json_mode} onChange={(event) => update("json_mode", event.target.checked)} /><span>启用 JSON 模式</span></label>
          </div>
          </details>
          <div className="settings-action-row">
            <button className="primary-button" type="button" disabled={!hasChanges || !form.connection_name?.trim() || !form.base_url.trim() || !form.model.trim()} onClick={() => void save()}>{saving ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />} 保存连接</button>
            <button className="secondary-button" type="button" disabled={!endpointReady || !form.model.trim() || testing} onClick={() => void test()}>{testing ? <LoaderCircle className="spin" size={16} /> : <CheckCircle2 size={16} />} 测试连接</button>
            {hasChanges && <button className="text-button" type="button" onClick={resetDraft}>{draftingNew ? "取消新增" : "放弃修改"}</button>}
            <button className="icon-button" type="button" title="刷新 AI 配置" aria-label="刷新 AI 配置" onClick={onInvalidate}><RefreshCw size={16} /></button>
          </div>
          <div className="ai-result-row"><span>{hasChanges ? "有未保存修改；地址、Key、模型和参数会一起保存。" : "连接已保存"}</span>{remoteModels.length > 0 && <span>已获取 {remoteModels.length} 个远程模型</span>}</div>
          {testResult && <div className={`ai-test-result ${testResult.tone}`} role="status"><span>{testResult.tone === "success" ? <CheckCircle2 size={15} /> : <XCircle size={15} />}</span><span>{testResult.text}</span></div>}
          </>}
        </fieldset>
      )}
      {connectionDialog && active && <div className="modal-backdrop" role="presentation" onClick={() => { if (!connectionBusy) setConnectionDialog(null); }}>
        <section ref={connectionDialogRef} className="settings-dialog ai-connection-dialog" role="dialog" aria-modal="true" aria-labelledby="ai-connection-dialog-title" onClick={(event) => event.stopPropagation()}>
          <div className="dialog-heading"><h2 id="ai-connection-dialog-title">
            {connectionDialog === "unsaved" ? "保存当前修改？" : "删除连接"}
          </h2><button ref={connectionCloseRef} type="button" className="icon-button" aria-label="关闭连接对话框" disabled={connectionBusy} onClick={() => setConnectionDialog(null)}><XCircle size={18} /></button></div>
          {connectionDialog === "unsaved" ? <>
            <p>当前连接有未保存的配置或 API Key，请选择保存后继续，或放弃这些修改。</p>
            <div className="settings-action-row">
              <button className="primary-button" disabled={connectionBusy} onClick={() => continueAction(true)}>保存并继续</button>
              <button className="secondary-button" disabled={connectionBusy} onClick={() => continueAction(false)}>放弃修改并继续</button>
              <button className="text-button" disabled={connectionBusy} onClick={() => setConnectionDialog(null)}>取消</button>
            </div>
          </> : <>
            <p>删除“{form.connection_name}”后{(data?.connections?.length ?? 0) > 1
              ? "将切换到列表中的首个连接。" : "将不再有已保存连接，新 AI 分析任务需先添加连接。"}正在运行的任务不受影响。</p>
            <div className="settings-action-row"><button className="secondary-button danger-icon" disabled={connectionBusy} onClick={submitConnection}>确认删除连接</button>
              <button className="secondary-button" disabled={connectionBusy} onClick={() => setConnectionDialog(null)}>取消</button></div>
          </>}
          {connectionError && <p className="field-error" role="alert">{connectionError}</p>}
        </section>
      </div>}
      {modelsDialogOpen && active && (
        <div className="modal-backdrop" role="presentation" onClick={() => setModelsDialogOpen(false)}>
          <section ref={modelsDialogRef} className="models-dialog" role="dialog" aria-modal="true" aria-labelledby="remote-models-title" onClick={(event) => event.stopPropagation()}>
            <div className="dialog-heading">
              <div><p className="eyebrow">远程模型</p><h2 id="remote-models-title">选择模型名称</h2></div>
              <button ref={modelsCloseRef} className="icon-button" type="button" onClick={() => setModelsDialogOpen(false)} aria-label="关闭模型列表"><XCircle size={17} /></button>
            </div>
            <input className="models-search" value={modelSearch} onChange={(event) => setModelSearch(event.target.value)} placeholder="搜索模型名称" autoFocus />
            <div className="models-list">
              {remoteModels.filter((model) => model.toLowerCase().includes(modelSearch.trim().toLowerCase())).map((model) => (
                <button className={`model-option${model === form.model ? " selected" : ""}`} type="button" key={model} onClick={() => {
                  update("model", model);
                  setModelsDialogOpen(false);
                  setTestResult({ tone: "success", text: `已选择模型：${model}` });
                }}>
                  <span>{model}</span>{model === form.model && <Check size={16} />}
                </button>
              ))}
              {!remoteModels.some((model) => model.toLowerCase().includes(modelSearch.trim().toLowerCase())) && <div className="models-empty">没有匹配的模型。</div>}
            </div>
          </section>
        </div>
      )}
    </section>
  );
}

function SettingsLoading({ label }: { label: string }) {
  return <div className="settings-loading"><LoaderCircle className="spin" size={18} /> {label}</div>;
}

function SettingsError({ label }: { label: string }) {
  return <div className="settings-error"><XCircle size={18} /> {label}</div>;
}

function asrStatusLabel(model: AsrModel, query?: { isFetching: boolean; isError: boolean }): string {
  if (model.download_state?.resumable !== false) {
    if (model.download_state?.status === "paused") return "下载已暂停";
    if (model.download_state?.status === "interrupted") return "准备未完成";
  }
  const previous = model.downloaded == null ? "" : model.downloaded ? "已下载" : "未准备";
  if (query?.isFetching) return previous ? `${previous} · 检查中` : "检查中";
  if (query?.isError) return previous ? `${previous} · 上次结果` : "检查失败";
  if (model.inspection_status === "cached") return previous ? `${previous} · 上次结果` : "尚未检查";
  return previous || "尚未检查";
}

function formatBytes(value: number | null): string {
  if (value == null) return "待统计";
  if (!value) return "未缓存";
  if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  return `${(value / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

function isFireRedModel(model: AsrModel): boolean {
  return /firered/i.test(`${model.id} ${model.name} ${model.engine} ${model.model_family ?? ""}`);
}

function asrModelOrder(model: AsrModel): number {
  if (model.engine === "qwen") return 0;
  if (isFireRedModel(model)) return 1;
  if (model.engine === "paraformer") return 2;
  if (model.engine === "dolphin") return 3;
  if (model.engine === "whisper") return model.id === "whisper-turbo" ? 5 : 6;
  return 4;
}

function asrModelName(model: AsrModel): string {
  if (isFireRedModel(model)) return "FireRedASR2S";
  if (model.id === "paraformer-large-cpu") return "Paraformer-large";
  if (model.id === "dolphin-small-cn") return "Dolphin 中文 small";
  return model.name || model.id;
}

// Relative quality and timings follow the project's supplied classroom comparisons.
const ASR_PROFILES = {
  qwen: {
    feature: "精度最高", icon: Target, tone: "accuracy",
    description: "当前模型中识别精度最高，适合优先追求转写质量的课堂录音。",
    comparison: { label: "60 分钟视频", value: "约 400 秒" },
  },
  firered: {
    feature: "高精度 · 更快", icon: Gauge, tone: "accuracy",
    description: "识别精度略低于 Qwen，但推理速度明显更快。",
    comparison: { label: "60 分钟视频", value: "约 200 秒" },
  },
  paraformer: {
    feature: "速度最快", icon: Gauge, tone: "speed",
    description: "安静环境下识别精度可用；收音嘈杂时不推荐。",
    comparison: { label: "60 分钟视频", value: "GPU 约 40 秒 · CPU 约 60 秒" },
  },
  dolphin: {
    feature: "中等精度 · 较快", icon: Target, tone: "accuracy",
    description: "识别精度高于 Paraformer-large，兼顾精度与处理速度。",
    comparison: { label: "60 分钟视频", value: "GPU 约 50 秒 · CPU 约 75 秒" },
  },
  turbo: {
    feature: "多语言 · 速度优先", icon: Languages, tone: "language",
    description: "不推荐用于中文课堂，仅建议用于多语言录音；速度快于 Whisper large-v3。",
    comparison: undefined,
  },
  large: {
    feature: "多语言 · 精度优先", icon: Languages, tone: "language",
    description: "相较 Whisper turbo，识别精度更高，但推理速度更慢。",
    comparison: undefined,
  },
};

function asrModelProfile(model: AsrModel) {
  if (isFireRedModel(model)) return ASR_PROFILES.firered;
  if (model.engine === "qwen") return ASR_PROFILES.qwen;
  if (model.engine === "paraformer") return ASR_PROFILES.paraformer;
  if (model.engine === "dolphin") return ASR_PROFILES.dolphin;
  if (model.id === "whisper-turbo") return ASR_PROFILES.turbo;
  if (model.id === "whisper-large-v3") return ASR_PROFILES.large;
  return null;
}

function asrEngineLabel(model: AsrModel): string {
  if (isFireRedModel(model)) return "FireRedASR2S";
  if (model.engine === "qwen") return "Qwen";
  if (model.engine === "dolphin") return "Dolphin";
  if (model.engine === "whisper") return "Whisper";
  return model.engine || "ASR 引擎";
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请检查本地服务。";
}
