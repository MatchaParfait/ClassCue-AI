import { Collapse, useEntryMotion } from "./Motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, LoaderCircle, RotateCcw, Settings2, Sparkles, X } from "lucide-react";
import { useEffect, useId, useLayoutEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { getAiSettings, getAsrSelection, processSelectedVideos, processVideo } from "../api/client";
import { buildProcessPayload, RECALL_SELECTION_REQUIRED, type ProcessForm } from "../processOptions";
import type { Job, Video } from "../types";
import { RecallModeControl } from "./RecallModeControl";
import { QwenResourceControls } from "./QwenResourceControls";
import { FireRedResourceProfile } from "./FireRedResourceStatus";
import { useRecallSelection } from "../hooks/useRecallSelection";

export function canSelectForProcessing(video: Video) {
  return video.status !== "processing" && video.status !== "resource_paused";
}

export const DEFAULT_LIBRARY_PROCESS_FORM: ProcessForm = {
  displayName: "", recallModes: ["medium_recall"], keywords: "", filterFormula: "", filterLowInformation: false,
};

export interface ProcessingFolder { id: number; name: string; videoCount: number }

export function LibraryProcessing({ videos, folders, selectedIds, selectedFolderIds, onSelect, onSelectFolders, expanded, onExpand, active, blocked, pending, onPending, onSubmitted, form, onFormChange: setForm }: {
  videos: Video[]; selectedIds: number[]; onSelect: (ids: number[]) => void;
  folders: ProcessingFolder[]; selectedFolderIds: number[]; onSelectFolders: (ids: number[]) => void;
  expanded: boolean; onExpand: (open: boolean) => void; active: boolean; blocked: boolean;
  pending: boolean; onPending: (value: boolean) => void;
  onSubmitted: (jobId: string | number, count: number) => void;
  form: ProcessForm; onFormChange: Dispatch<SetStateAction<ProcessForm>>;
}) {
  const [, setRecallModes] = useRecallSelection();
  const queryClient = useQueryClient();
  const rootRef = useRef<HTMLDivElement>(null);
  const [dockHost, setDockHost] = useState<HTMLElement | null>(null);
  const optionsId = useId();
  const selectAll = useRef<HTMLInputElement>(null);
  const submitting = useRef(false);
  const optionsRef = useRef<HTMLElement>(null);
  const revealOptions = useRef(false);
  const candidates = videos.filter(canSelectForProcessing);
  const selected = candidates.filter((video) => selectedIds.includes(video.id));
  const folderCandidates = folders.filter(folder => folder.videoCount > 0);
  const selectedFolders = folderCandidates.filter(folder => selectedFolderIds.includes(folder.id));
  const count = selected.length + selectedFolders.reduce((sum, folder) => sum + folder.videoCount, 0);
  const hasCandidates = candidates.length + folderCandidates.length > 0;
  const allSelected = hasCandidates && selected.length === candidates.length && selectedFolders.length === folderCandidates.length;
  const disabled = pending || blocked;
  useLayoutEffect(() => {
    setDockHost(rootRef.current?.closest<HTMLElement>(".folder-window, .library-inner") ?? null);
  }, []);
  useEffect(() => {
    if (selectAll.current) selectAll.current.indeterminate = count > 0 && !allSelected;
  }, [count, allSelected]);
  useEffect(() => {
    if (expanded && active) optionsRef.current?.focus({ preventScroll: true });
    if (!expanded || !active || !revealOptions.current) return;
    const frame = requestAnimationFrame(() => {
      revealOptions.current = false;
      optionsRef.current?.scrollIntoView?.({ block: "start", behavior: "auto" });
    });
    return () => cancelAnimationFrame(frame);
  }, [expanded, active]);
  const configEnabled = active && expanded;
  const selection = useQuery({
    queryKey: ["settings", "asr", "selection"], queryFn: getAsrSelection, enabled: configEnabled, staleTime: 30_000,
  });
  const ai = useQuery({
    queryKey: ["settings", "ai"], queryFn: getAiSettings, enabled: configEnabled, staleTime: 30_000,
  });
  const mutation = useMutation({
    mutationFn: async ({ ids, folderIds, form: submittedForm, modelId }: {
      ids: number[]; folderIds: number[]; count: number; form: ProcessForm; modelId?: string;
    }) => {
      const payload = { ...buildProcessPayload(submittedForm), asr_model_id: modelId };
      if (ids.length === 1 && folderIds.length === 0) return processVideo(ids[0], payload);
      const result = await processSelectedVideos(ids, folderIds.length ? { ...payload, folder_ids: folderIds } : payload);
      return { ...result, id: result.job_id, progress: 0 } satisfies Job;
    },
    onSuccess: (job, { count }) => {
      const id = job.job_id ?? job.id;
      queryClient.setQueryData(["jobs", id], job);
      onSubmitted(id, "total" in job && typeof job.total === "number" ? job.total : count);
      setForm((current) => ({ ...current, forceRetranscribe: false }));
      void queryClient.invalidateQueries({ queryKey: ["videos"] });
      void queryClient.invalidateQueries({ queryKey: ["library"] });
      void queryClient.invalidateQueries({ queryKey: ["batch-latest"] });
      void queryClient.invalidateQueries({ queryKey: ["library-status"] });
    },
    onSettled: () => { submitting.current = false; onPending(false); },
  });
  const configError = selection.error ?? ai.error;

  return <div className="library-processing" ref={rootRef}>
    <div className="library-selection-toolbar">
      <label className="library-select-all"><input ref={selectAll} type="checkbox" aria-label={folderCandidates.length ? "全选当前列表视频和文件夹" : "全选当前列表视频"}
        checked={allSelected} disabled={disabled || !hasCandidates}
        onChange={() => {
          mutation.reset();
          onSelect(allSelected ? [] : candidates.map(video => video.id));
          onSelectFolders(allSelected ? [] : folderCandidates.map(folder => folder.id));
        }} /><span>全选</span></label>
      <span className="library-selection-count" role="status">已选 {count} 个视频</span>
      {count > 0 && <button className="icon-button" type="button" title="清空选择" aria-label="清空选择" disabled={pending}
        onClick={() => { mutation.reset(); onSelect([]); onSelectFolders([]); onExpand(false); }}><X size={15} /></button>}
      <button className="secondary-button compact library-selection-process" type="button" disabled={count === 0 || disabled}
        aria-expanded={expanded} onClick={() => { mutation.reset(); onExpand(!expanded); }}><Sparkles size={15} />处理</button>
      {blocked && <span className="library-processing-blocked" role="status">请先完成或停止当前任务</span>}
    </div>
    {active && count > 0 && dockHost && createPortal(
      <div className="library-selection-dock" role="region" aria-label="已选视频操作" aria-busy={pending}>
        <strong className="library-selection-dock-count">已选 <span>{count}</span> 个视频</strong>
        <button className="text-button" type="button" disabled={pending} onClick={() => {
          mutation.reset(); onSelect([]); onSelectFolders([]); onExpand(false);
          selectAll.current?.focus({ preventScroll: true });
        }}>取消选择</button>
        <button className="primary-button library-selection-dock-process" type="button" disabled={disabled}
          aria-expanded={expanded} aria-controls={optionsId} onClick={() => {
            mutation.reset();
            if (expanded) {
              optionsRef.current?.focus({ preventScroll: true });
              optionsRef.current?.scrollIntoView?.({ block: "start", behavior: "auto" });
            } else {
              revealOptions.current = true;
              onExpand(true);
            }
          }}>{pending ? <LoaderCircle className="spin" size={17} /> : <Sparkles size={17} />}
          {pending ? "正在提交…" : `处理所选（${count}）`}</button>
        {blocked && <span className="library-selection-dock-blocked">请先完成或停止当前任务</span>}
      </div>, dockHost,
    )}
    <Collapse open={expanded}><section id={optionsId} ref={optionsRef} tabIndex={-1} className="library-processing-options process-options-section" aria-label="视频处理选项">
      <div className="library-processing-heading"><h3>处理选项</h3><span>{selectedFolders.length || count > 1 ? `批处理 · ${count} 个视频` : selected[0]?.display_name ?? "尚未选择视频"}</span>
        <button type="button" className="icon-button" title="收起处理选项" aria-label="收起处理选项" disabled={pending} onClick={() => onExpand(false)}><X size={16} /></button></div>
      <div className="readonly-config-grid">
        <div className="readonly-config"><span>当前 ASR 模型</span><strong>{selection.data?.name || "正在读取配置"}</strong>
          <Link to="/settings?section=asr"><Settings2 size={14} />修改</Link></div>
        <div className="readonly-config"><span>当前 AI 服务</span><strong>{ai.data?.model || (ai.isPending ? "正在读取配置" : "未配置")}</strong>
          <Link to="/settings?section=ai"><Settings2 size={14} />修改</Link></div>
      </div>
      <div className="library-processing-body">
        <div className="library-processing-content">
          {selection.data?.engine === "firered" ? <FireRedResourceProfile modelId={selection.data.id} active={configEnabled} />
            : selection.data?.engine === "qwen" ? <QwenResourceControls form={form} onChange={setForm} disabled={disabled} /> : null}
          <div className="library-processing-fields">
            <RecallModeControl value={form.recallModes} active={active} disabled={disabled} onChange={(recallModes) => {
              setRecallModes(recallModes);
              setForm((current) => ({ ...current, recallModes }));
              if (mutation.error?.message === RECALL_SELECTION_REQUIRED) mutation.reset();
            }} />
            <label className="check-option" title="成功后替换原字幕并重建复习结果；失败时保留原结果"><input type="checkbox" checked={Boolean(form.forceRetranscribe)} disabled={disabled}
              onChange={(event) => setForm((current) => ({ ...current, forceRetranscribe: event.target.checked }))} /><span>使用当前模型重新转写</span></label>
          </div>
          {(count > 1 || selectedFolders.length > 0) && <details className="library-processing-selection"><summary>已选视频（{count}）</summary>
            <ul>{selectedFolders.map(folder => <li key={`folder-${folder.id}`}>{folder.name} · {folder.videoCount} 个视频（含子文件夹）</li>)}
              {selected.map(video => <li key={`video-${video.id}`}>{video.display_name}</li>)}</ul></details>}
          {configError && <div className="field-error" role="alert"><AlertCircle size={15} />配置读取失败：{configError.message}
            <button type="button" className="icon-button" title="重新读取处理配置" aria-label="重新读取处理配置"
              onClick={() => { void selection.refetch(); void ai.refetch(); }}><RotateCcw size={15} /></button></div>}
          {mutation.error && <p className="field-error" role="alert"><AlertCircle size={15} />{mutation.error.message}</p>}
          <button className="primary-button" type="button" disabled={disabled || count === 0 || selection.isPending || ai.isPending || Boolean(configError)}
            onClick={() => {
              if (submitting.current || !count) return;
              submitting.current = true;
              onPending(true);
              mutation.mutate({ ids: selected.map(video => video.id), folderIds: selectedFolders.map(folder => folder.id),
                count, form, modelId: selection.data?.id });
            }}>{pending ? <LoaderCircle className="spin" size={16} /> : <Sparkles size={16} />}{pending ? "正在提交…" : selectedFolders.length || count > 1 ? `开始批处理（${count}）` : "开始处理"}</button>
        </div>
        <ProcessingCompanion />
      </div>
    </section></Collapse>
  </div>;
}

function ProcessingCompanion() {
  const art = useRef<HTMLImageElement>(null);
  useEntryMotion(art, { distance: 4, duration: .16, delay: .02 });
  return <img ref={art} className="library-processing-art" src="/art/processing-companion.webp" width={626} height={640}
    alt="" aria-hidden="true" draggable={false} decoding="async" />;
}
