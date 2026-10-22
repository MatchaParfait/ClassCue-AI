import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, ArrowRight, Check, LoaderCircle, RefreshCw } from "lucide-react";
import { getAiSettings, getAsrModelStatus, getAsrSelection, getStorageDirectories, getVideos } from "../api/client";
import { asrStatusOptions } from "../hooks/useAsrStatus";
import { useNavigationLock, useUnsavedLeave } from "./UnsavedChanges";
import { OnboardingSpotlight } from "./OnboardingSpotlight";
import type { AsrModel } from "../types";

export const ONBOARDING_RESET_EVENT = "classroom-onboarding-reset";
const statePrefix = "classroom-onboarding:v1:";
const steps = [
  { key: "storage", label: "确认存储" },
  { key: "model", label: "准备字幕模型" },
  { key: "ai", label: "连接 AI 服务" },
  { key: "shortcuts", label: "认识常用快捷键" },
] as const;
type StepKey = typeof steps[number]["key"];
type GuideState = {
  started?: boolean; dismissed?: boolean; step?: StepKey;
  storageConfirmed?: boolean; modelConfirmed?: boolean; aiConfirmed?: boolean;
  shortcutsSeen?: boolean;
};
interface Props {
  active: boolean; scope: string;
  onActiveChange?: (active: boolean) => void;
}
function readState(key: string): GuideState {
  try {
    const value = JSON.parse(window.localStorage?.getItem(key) ?? "{}");
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    const legacyComplete = value.step === "process" && value.storageConfirmed === true
      && value.modelConfirmed === true && value.aiConfirmed === true && value.shortcutsSeen === true;
    return {
      started: value.started === true, dismissed: value.dismissed === true || legacyComplete,
      step: steps.find(item => item.key === value.step)?.key,
      storageConfirmed: value.storageConfirmed === true, modelConfirmed: value.modelConfirmed === true,
      aiConfirmed: value.aiConfirmed === true, shortcutsSeen: value.shortcutsSeen === true,
    };
  } catch { return {}; }
}
function saveState(key: string, value: GuideState) {
  try { window.localStorage?.setItem(key, JSON.stringify(value)); } catch { /* local state is optional */ }
}
function isModelReady(model: AsrModel | undefined) {
  if (!model || model.downloaded !== true || model.active_job_kind) return false;
  if (model.engine === "qwen") return model.qwen_ready === true;
  if (model.engine === "firered") return model.aed_ready === true;
  if (["paraformer", "dolphin"].includes(model.engine) && model.device === "gpu") return model.gpu_runtime_ready === true;
  return true;
}

export function OnboardingGuide({ active, scope, onActiveChange }: Props) {
  const location = useLocation();
  const navigate = useNavigate();
  const client = useQueryClient();
  const leave = useUnsavedLeave();
  const stateKey = `${statePrefix}${scope}`;
  const [state, setState] = useState<GuideState>(() => readState(stateKey));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const updateState = useCallback((patch: Partial<GuideState>) => {
    setState(current => {
      const next = { ...current, ...patch };
      saveState(stateKey, next);
      return next;
    });
  }, [stateKey]);
  const videos = useQuery({ queryKey: ["videos"], queryFn: getVideos, enabled: active, staleTime: 15_000 });
  const visible = active && !state.dismissed && (state.started || videos.isSuccess && videos.data.length === 0);
  const firstIncomplete = !state.storageConfirmed ? "storage" : !state.modelConfirmed ? "model"
    : !state.aiConfirmed ? "ai" : "shortcuts";
  const step = steps.find(item => item.key === state.step) ?? steps.find(item => item.key === firstIncomplete)!;
  const index = steps.indexOf(step);
  const section = step.key === "model" ? "asr" : step.key;
  const inSettings = location.pathname === "/settings";
  const currentSection = new URLSearchParams(location.search).get("section") ?? "storage";
  const inForm = inSettings && currentSection === section;
  const storage = useQuery({ queryKey: ["settings", "directories"], queryFn: getStorageDirectories, enabled: visible, staleTime: 30_000 });
  const selection = useQuery({ queryKey: ["settings", "asr", "selection"], queryFn: getAsrSelection, enabled: visible, staleTime: 30_000 });
  const status = useQuery({
    ...asrStatusOptions(selection.data), enabled: visible && Boolean(selection.data) && step.key === "model",
    refetchInterval: visible && step.key === "model" ? 2000 : false,
  });
  const modelReady = isModelReady(selection.data ? { ...selection.data, ...status.data, device: selection.data.device } : undefined);
  const ai = useQuery({ queryKey: ["settings", "ai"], queryFn: getAiSettings, enabled: visible, staleTime: 30_000 });
  const aiReady = Boolean(ai.data?.configured ?? (ai.data?.base_url && ai.data.model && ai.data.has_api_key));
  const storageLocked = Boolean(storage.data?.busy || storage.data?.has_paused_jobs);
  useNavigationLock(visible, next => next.pathname === "/settings"
    && ((new URLSearchParams(next.search).get("section") ?? "storage") === section
      || !inSettings && !new URLSearchParams(next.search).has("section")));
  useLayoutEffect(() => { onActiveChange?.(Boolean(visible)); }, [visible, onActiveChange]);
  useEffect(() => () => { onActiveChange?.(false); }, [onActiveChange]);
  useEffect(() => {
    const reset = () => {
      const next: GuideState = { started: true, step: "storage" };
      saveState(stateKey, next); setState(next); setError("");
    };
    window.addEventListener(ONBOARDING_RESET_EVENT, reset);
    return () => window.removeEventListener(ONBOARDING_RESET_EVENT, reset);
  }, [stateKey]);
  useEffect(() => { saveState(stateKey, state); }, [stateKey, state]);
  useEffect(() => { if (visible && !state.started) updateState({ started: true }); }, [visible, state.started, updateState]);
  useEffect(() => { setError(""); }, [step.key, location.pathname, currentSection]);

  const changeStep = (next: StepKey) => leave(() => updateState({ step: next }));
  const confirm = () => leave(async () => {
    if (saving) return;
    setSaving(true); setError("");
    try {
      if (step.key === "storage") {
        const saved = await client.fetchQuery({ queryKey: ["settings", "directories"], queryFn: getStorageDirectories, staleTime: 0 });
        if (!saved.library_id) throw new Error("视频库尚未就绪，请重新读取。");
        updateState({ storageConfirmed: true, step: "model" });
      } else if (step.key === "model") {
        const selected = await client.fetchQuery({ queryKey: ["settings", "asr", "selection"], queryFn: getAsrSelection, staleTime: 0 });
        const checked = await client.fetchQuery({ ...asrStatusOptions(selected), staleTime: 0,
          queryFn: () => getAsrModelStatus(selected.id, { full: false, refresh: true }) });
        if (!isModelReady({ ...selected, ...checked, device: selected.device })) throw new Error("请先保存模型选择并完成下载与运行环境准备。");
        updateState({ modelConfirmed: true, step: "ai" });
      } else if (step.key === "ai") {
        const saved = await client.fetchQuery({ queryKey: ["settings", "ai"], queryFn: getAiSettings, staleTime: 0 });
        if (!(saved.configured ?? (saved.base_url && saved.model && saved.has_api_key))) throw new Error("请先保存可用的 AI 连接。");
        updateState({ aiConfirmed: true, step: "shortcuts" });
      }
    } catch (failure) { setError(failure instanceof Error ? failure.message : String(failure)); }
    finally { setSaving(false); }
  });
  let selector = '[data-onboarding="settings"]';
  let description = "点击高亮的“设置”，先确认视频和模型的保存位置。";
  if (inSettings && !inForm) {
    selector = `[data-onboarding-section="${section}"]`;
    description = `点击设置中的“${step.key === "model" ? "ASR 模型" : step.key === "ai" ? "AI 服务" : step.key === "shortcuts" ? "快捷键" : "存储与导入"}”。`;
  } else if (inForm) {
    selector = step.key === "storage" ? ".storage-directories-section" : step.key === "shortcuts" ? ".shortcut-settings" : `[data-onboarding="${section}"]`;
    description = step.key === "storage" ? storageLocked
      ? "当前有运行或暂停的任务，暂不能修改存储。点击“继续”使用现有设置。"
      : "这里可以查看视频和模型的保存位置。点击“继续”使用现有设置。"
      : step.key === "model" ? "选择并保存字幕模型，再完成下载和运行环境准备。已就绪的模型无需重新下载。"
      : step.key === "ai" ? "填写服务地址、Key 和模型，测试连接并保存。字幕会发送给所选服务，可能产生费用。"
      : "常用快捷键就在设置的“快捷键”中，包括搜索、播放、字幕和考点跳转；桌面版还可设置唤起快捷键。修改后请保存。";
  }
  if (!visible) return null;
  const ready = step.key === "storage" ? Boolean(storage.data?.library_id)
    : step.key === "model" ? modelReady : step.key === "ai" ? aiReady : true;
  const loadingError = storage.isError || selection.isError || status.isError || ai.isError || videos.isError;
  const mascot = saving || inForm && step.key === "model" && Boolean(status.data?.active_job_kind) ? "reading"
    : step.key === "shortcuts" && inForm || step.key === "ai" && inForm ? "complete"
      : step.key === "storage" ? "welcome" : "point";
  return <OnboardingSpotlight selector={selector} phase={`${step.key}:${location.pathname}:${currentSection}`} mascot={mascot}>
    <p className="eyebrow">首次使用 · {index + 1} / {steps.length}</p>
    <h2 id="onboarding-title">{step.label}</h2>
    <p id="onboarding-description">{description}</p>
    {error && <p className="field-error" role="alert">{error}</p>}
    {loadingError && <button type="button" className="text-button" onClick={() => {
      void videos.refetch(); void storage.refetch(); void selection.refetch(); void ai.refetch();
      if (selection.data) void status.refetch();
    }}><RefreshCw size={14} />重新读取状态</button>}
    <div className="onboarding-actions">
      {index > 0 && <button className="icon-button" type="button" title="上一步" aria-label="上一步" disabled={saving}
        onClick={() => changeStep(steps[index - 1].key)}><ArrowLeft size={16} /></button>}
      {inForm && step.key !== "shortcuts" && <button type="button" className="primary-button"
        disabled={saving || !ready} onClick={confirm}>{saving ? <LoaderCircle size={16} className="spin" /> : <ArrowRight size={16} />}
        继续</button>}
      {inForm && step.key === "shortcuts" && <button type="button" className="primary-button"
        onClick={() => leave(() => updateState({ shortcutsSeen: true, dismissed: true }))}><Check size={16} />完成引导</button>}
      {!inSettings && location.pathname !== "/" && <button type="button" className="secondary-button"
        onClick={() => navigate(`/settings?section=${section}`)}>返回当前步骤</button>}
    </div>
  </OnboardingSpotlight>;
}
