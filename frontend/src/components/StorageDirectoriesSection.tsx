import { useUnsavedDraft } from "./UnsavedChanges";
import { CharacterEye } from "./CharacterAccent";
import { AlertCircle, FolderOpen, HardDrive, LoaderCircle, RefreshCw, RotateCcw, Save } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { createPicker, getJobStatus, getStorageDirectories, pollPicker, previewStorageMigration, saveStorageDirectories, startStorageMigration } from "../api/client";
import type { StorageDirectories, StorageDirectoryValues, StorageMigrationPlan } from "../types";

const DIRECTORY_FIELDS = [
  { key: "asr_storage_root", label: "ASR 资源目录", scope: "迁移权重与下载缓存，重启生效" },
  { key: "log_dir", label: "诊断日志目录", scope: "重启后生效" },
] as const;
type DirectoryKey = typeof DIRECTORY_FIELDS[number]["key"] | "media_storage_root";
const mediaLabel = "视频与音频存储目录";
const parentPath = (value: string) => value.replace(/[\\/][^\\/]+[\\/]?$/, "");
const childPath = (root: string, name: string) => `${root.trim().replace(/[\\/]+$/, "")}${root.includes("\\") ? "\\" : "/"}${name}`;
const bytes = (value: number) => value < 1024 ** 2 ? `${(value / 1024).toFixed(1)} KB`
  : value < 1024 ** 3 ? `${(value / 1024 ** 2).toFixed(1)} MB` : `${(value / 1024 ** 3).toFixed(2)} GB`;

interface Props {
  data?: StorageDirectories;
  isLoading: boolean;
  isError: boolean;
  onRetry: () => void;
  onSuccess: (data: StorageDirectories) => void;
  onError: (error: unknown) => void;
  onStarted?: (id: string) => void;
}

export function StorageDirectoriesSection({ data, isLoading, isError, onRetry, onSuccess, onError, onStarted }: Props) {
  const [draft, setDraft] = useState<StorageDirectoryValues | null>(null);
  const [mediaRootDraft, setMediaRootDraft] = useState<string | null>(null);
  const [pickerKey, setPickerKey] = useState<DirectoryKey | null>(null);
  const [plan, setPlan] = useState<StorageMigrationPlan | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const notified = useRef<string | number | null>(null);
  const activeId = jobId ?? (data?.migration_job && ["queued", "running"].includes(data.migration_job.status) ? String(data.migration_job.id) : null);
  const progress = useQuery({
    queryKey: ["storage-migration", activeId], queryFn: () => getJobStatus(activeId!),
    enabled: Boolean(activeId),
    refetchInterval: (query) => query.state.data && !["queued", "running"].includes(query.state.data.status) ? false : 500,
    retry: 2,
  });
  const job = progress.data ?? data?.migration_job;
  const showMigrationProgress = job && (job.status !== "completed" || String(job.id) === activeId);
  const migrating = Boolean(activeId && (!progress.data || ["queued", "running"].includes(progress.data.status)));
  useEffect(() => {
    if (!activeId || !job || String(job.id) !== activeId || notified.current === job.id || ["queued", "running"].includes(job.status)) return;
    notified.current = job.id;
    if (job.status === "completed") {
      void getStorageDirectories().then((value) => {
        setDraft(null); setMediaRootDraft(null); setPlan(null); onSuccess(value);
      }).catch(onError);
    } else if (activeId) {
      onError(new Error(job.error_message || job.message || "迁移未完成，原文件保留"));
    }
  }, [job, activeId, onSuccess, onError]);
  const save = useMutation({
    mutationFn: (values: StorageDirectoryValues & { library_id: string }) => saveStorageDirectories(values),
    onSuccess: (value) => { setDraft(null); setMediaRootDraft(null); clearUnsaved(); onSuccess(value); },
    onError: (error) => onError(error),
  });
  const preview = useMutation({
    mutationFn: (value: StorageDirectoryValues & { library_id: string }) => previewStorageMigration(value),
    onSuccess: setPlan,
    onError: (error) => onError(error),
  });
  const start = useMutation({
    mutationFn: (value: StorageDirectoryValues & { library_id: string; plan_id: string }) => startStorageMigration(value),
    onSuccess: (value) => { setPlan(null); setJobId(value.job_id); clearUnsaved(); onStarted?.(value.job_id); },
    onError: (error) => { setPlan(null); onError(error); },
  });
  const form = draft ?? data;
  const savedMediaRoot = data?.media_storage_root ?? (data && parentPath(data.video_library_dir) === parentPath(data.audio_cache_dir)
    ? parentPath(data.video_library_dir) : "");
  const mediaRoot = mediaRootDraft ?? savedMediaRoot;
  const mediaChanged = mediaRootDraft !== null && (mediaRoot.trim() !== savedMediaRoot || !data?.media_storage_bound);
  const busy = Boolean(data?.busy || migrating || save.isPending || preview.isPending || start.isPending || pickerKey);
  const pathsChanged = Boolean(mediaChanged || data && form && DIRECTORY_FIELDS.some(({ key }) => form[key].trim() !== data[key]));
  const changed = Boolean(data && form && (
    mediaChanged || DIRECTORY_FIELDS.some(({ key }) => form[key].trim() !== data[key]) ||
    (form.delete_source_after_import ?? true) !== (data.delete_source_after_import ?? true)
  ));
  const valid = Boolean(form && DIRECTORY_FIELDS.every(({ key }) => form[key].trim())
    && (mediaRootDraft === null || mediaRoot.trim()));
  const update = (key: DirectoryKey | "delete_source_after_import", value: string | boolean) => {
    if (!form) return;
    setPlan(null);
    if (key === "media_storage_root") { setMediaRootDraft(String(value)); return; }
    setDraft({ asr_storage_root: form.asr_storage_root, audio_cache_dir: form.audio_cache_dir, log_dir: form.log_dir, video_library_dir: form.video_library_dir, delete_source_after_import: form.delete_source_after_import ?? true, [key]: value });
  };
  const values = () => ({
    asr_storage_root: form!.asr_storage_root.trim(), log_dir: form!.log_dir.trim(),
    ...(mediaRootDraft !== null ? { media_storage_root: mediaRoot.trim() } : {
      audio_cache_dir: data!.audio_cache_dir, video_library_dir: data!.video_library_dir,
    }),
    delete_source_after_import: form!.delete_source_after_import ?? true, library_id: data!.library_id,
  });
  const clearUnsaved = useUnsavedDraft({ label: "存储与导入", dirty: changed && !migrating,
    busy: save.isPending || preview.isPending || start.isPending || Boolean(pickerKey),
    save: async () => {
      if (busy) throw new Error("当前有任务或目录操作，请稍后保存。");
      if (!valid) throw new Error("请填写有效的存储目录。");
      if (pathsChanged) {
        await preview.mutateAsync(values());
        requestAnimationFrame(() => document.querySelector('.storage-migration-plan')?.scrollIntoView({ block: "center" }));
        return false;
      }
      await save.mutateAsync(values()); return true;
    },
    discard: () => { setDraft(null); setMediaRootDraft(null); setPlan(null); },
  });
  const choose = async (key: DirectoryKey) => {
    setPickerKey(key);
    try {
      const selected = await pollPicker(await createPicker("directory"));
      if (selected) update(key, selected);
    } catch (error) {
      onError(error);
    } finally {
      setPickerKey(null);
    }
  };

  return (
    <section className="settings-section storage-directories-section" aria-labelledby="storage-settings-title">
      <div className="settings-section-heading">
        <div className="settings-section-title"><span className="settings-section-icon"><HardDrive size={18} /></span><div><p className="eyebrow">本机目录</p><h2 id="storage-settings-title"><CharacterEye heading />存储与缓存目录</h2></div></div>
        {data && <span className={`settings-state ${data.busy || data.restart_required ? "busy" : "ready"}`}>
          {migrating ? "正在迁移，暂不可修改" : data.busy ? "任务未结束，暂不可修改" : data.restart_required ? "已保存，待重启生效" : "已生效"}
        </span>}
      </div>
      {isLoading && <div className="settings-loading"><LoaderCircle className="spin" size={18} /> 正在读取存储目录…</div>}
      {isError && <div className="settings-error" role="alert"><AlertCircle size={18} /><span>存储目录读取失败</span><button className="icon-button" type="button" aria-label="重新读取存储目录" title="重新读取存储目录" onClick={onRetry}><RefreshCw size={16} /></button></div>}
      {!isLoading && !isError && data && form && (
        <>
          <fieldset className="storage-directory-fields" disabled={busy}>
            <label className="check-option">
              <input type="checkbox" aria-label="导入后删除原文件" checked={form.delete_source_after_import ?? true}
                onChange={(event) => update("delete_source_after_import", event.target.checked)} />
              <span>导入后删除原文件<small>保存后下次导入生效。取消勾选只复制到视频库，保留原位置文件。</small></span>
            </label>
            <div className="storage-directory-row">
              <label className="settings-field grow"><span>{mediaLabel}<small>视频与转写缓存一起迁移</small></span>
                <input value={mediaRoot} onChange={(event) => update("media_storage_root", event.target.value)}
                  aria-label={mediaLabel} placeholder="选择统一存储目录" spellCheck={false} autoComplete="off" />
              </label>
              <button className="icon-button settings-picker-button" type="button" aria-label={`选择${mediaLabel}`} title={`选择${mediaLabel}`}
                onClick={() => void choose("media_storage_root")}>
                {pickerKey === "media_storage_root" ? <LoaderCircle className="spin" size={16} /> : <FolderOpen size={16} />}
              </button>
              <button className="icon-button settings-picker-button" type="button"
                disabled={busy || Boolean(data.media_storage_bound && mediaRoot === (data.media_storage_default_root ?? data.data_root))}
                aria-label="恢复默认存储目录" title="恢复默认存储目录"
                onClick={() => update("media_storage_root", data.media_storage_default_root ?? data.data_root)}><RotateCcw size={16} /></button>
            </div>
            <p className="settings-hint">选择一个文件夹，自动在其中创建 videos 和 audio 子文件夹，分别保存视频、临时音频与转写缓存。</p>
            <dl className="storage-derived-paths" aria-label={mediaRootDraft === null ? "当前视频与音频位置" : "迁移后的视频与音频位置"}>
              <div><dt>视频文件</dt><dd>{mediaRootDraft === null ? data.video_library_dir : mediaRoot.trim() ? childPath(mediaRoot, "videos") : "请选择目标目录"}</dd></div>
              <div><dt>音频与转写缓存</dt><dd>{mediaRootDraft === null ? data.audio_cache_dir : mediaRoot.trim() ? childPath(mediaRoot, "audio") : "请选择目标目录"}</dd></div>
            </dl>
            {DIRECTORY_FIELDS.map(({ key, label, scope }) => (
              <div className="storage-directory-row" key={key}>
                <label className="settings-field grow"><span>{label}<small>{scope}</small></span><input
                  value={form[key]} onChange={(event) => update(key, event.target.value)}
                  aria-label={label} spellCheck={false} autoComplete="off"
                /></label>
                <button className="icon-button settings-picker-button" type="button" aria-label={`选择${label}`} title={`选择${label}`} onClick={() => void choose(key)}>
                  {pickerKey === key ? <LoaderCircle className="spin" size={16} /> : <FolderOpen size={16} />}
                </button>
              </div>
            ))}
          </fieldset>
          <dl className="storage-derived-paths">
            <div><dt>模型权重</dt><dd>{data.model_dir}</dd></div>
            <div><dt>独立运行环境</dt><dd>{data.runtime_dir}</dd></div>
            <div><dt>环境下载缓存</dt><dd>{data.package_cache_dir}</dd></div>
          </dl>
          <p className="settings-hint">模型、运行环境及下载缓存体积较大，由 ASR 资源目录统一管理。数据库、设置与凭据可在下方“应用数据”中迁移。</p>
          {data.restart_required && <dl className="storage-derived-paths storage-active-paths">
            <div><dt>当前 ASR 资源</dt><dd>{data.active_asr_storage_root}</dd></div>
            <div><dt>当前日志</dt><dd>{data.active_log_dir}</dd></div>
          </dl>}
          <p className="settings-hint">更换目录会先复制并校验已有文件，成功切换后清理原文件。视频仅迁移视频库内已登记的文件，原始来源与数据库不搬动。旧运行环境及历史日志保留；ASR 资源更换后请重启，在模型页按需重新准备运行环境。导入后是否删除原文件只影响后续导入。</p>
          {data.has_paused_jobs && <p className="field-error"><AlertCircle size={14} /> 暂停的任务仍依赖原缓存，完成或停止后才能修改目录。</p>}
          {plan && <section className="storage-migration-plan" aria-label="迁移确认">
            <h3>确认迁移范围</h3>
            <p>共 {plan.files_total.toLocaleString()} 个文件 · {bytes(plan.bytes_total)}。校验完成前保留原文件；目标同名文件内容不同时停止，不会覆盖。</p>
            <dl className="storage-derived-paths">{plan.sections.map((item) => <div key={item.label}>
              <dt>{item.label}<small>{item.files} 个 · {bytes(item.bytes)}</small></dt>
              <dd><span>{item.source}</span><strong>→ {item.target}</strong></dd>
            </div>)}</dl>
            {plan.disks.map((disk) => <p className="settings-hint" key={disk.path}>目标磁盘可用 {bytes(disk.free_bytes)}，本次预留 {bytes(disk.required_bytes)}</p>)}
            {plan.warnings.map((warning) => <p className="settings-hint" key={warning}>{warning}</p>)}
            <div className="settings-action-row">
              <button type="button" className="primary-button" disabled={busy} onClick={() => start.mutate({ ...values(), plan_id: plan.plan_id })}>
                {start.isPending ? <LoaderCircle className="spin" size={16} /> : <FolderOpen size={16} />} 开始迁移
              </button>
              <button type="button" className="secondary-button" disabled={busy} onClick={() => setPlan(null)}>返回修改</button>
            </div>
          </section>}
          {job && showMigrationProgress && !plan && <section className="model-preparation storage-migration-progress" aria-label="目录迁移进度">
            <div className="model-preparation-heading" role="status"><strong>{job.message || "正在准备迁移…"}</strong>
              <span>{job.status === "completed" ? "完成" : job.status === "failed" || job.status === "interrupted" ? "未完成" : `${Math.floor((job.progress ?? 0) * 100)}%`}</span></div>
            <div className="model-preparation-track" role="progressbar" aria-label="目录迁移" aria-valuemin={0} aria-valuemax={100}
              aria-valuenow={Math.floor((job.progress ?? 0) * 100)}><span style={{ width: `${(job.progress ?? 0) * 100}%` }} /></div>
            {job.migration && <p>已复制 {job.migration.files_completed} / {job.migration.files_total} 个文件 · {bytes(job.migration.bytes_completed)} / {bytes(job.migration.bytes_total)}</p>}
            {job.migration?.current_file && <p className="model-preparation-file">{job.migration.current_file}</p>}
            {progress.isError && <p role="status">进度连接暂时中断，后台迁移继续；正在重连，也可刷新页面查看。</p>}
            {job.error_message && <p className="field-error" role="alert">{job.error_message}。原文件保留，请检查后重试。</p>}
            {job.status === "completed" && job.migration?.warnings.map((warning) => <p className="settings-hint" key={warning}>{warning}</p>)}
          </section>}
          {!plan && <div className="settings-action-row">
            <button className="primary-button" type="button" disabled={busy || !changed || !valid || Boolean(plan)} onClick={() => {
              if (pathsChanged) preview.mutate(values());
              else save.mutate(values());
            }}>{save.isPending || preview.isPending ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />} {pathsChanged ? "检查并迁移" : "保存设置"}</button>
            <button className="icon-button" type="button" disabled={busy || !changed} title="撤销未保存的修改" aria-label="撤销未保存的目录修改" onClick={() => { setDraft(null); setMediaRootDraft(null); setPlan(null); }}><RotateCcw size={16} /></button>
          </div>}
        </>
      )}
    </section>
  );
}
