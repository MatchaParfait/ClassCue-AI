import { useUnsavedDraft } from "./UnsavedChanges";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Database, FolderOpen, LoaderCircle, RotateCcw } from "lucide-react";
import { createPicker, getStorageDirectories, migrateApplicationData, pollPicker, previewApplicationData } from "../api/client";
import type { ApplicationDataPlan, StorageDirectories } from "../types";
import { AnimatePresence, AnimatedOverlay, AnimatedPopup } from "./Motion";
import { useModalFocus } from "../hooks/useModalFocus";

const restartKey = "classroom-application-data-restart";
const bytes = (n: number) => n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)} GB`
  : n >= 1024 ** 2 ? `${(n / 1024 ** 2).toFixed(2)} MB` : `${(n / 1024).toFixed(1)} KB`;
function rememberedRestart(): { id: string; started: number } | null {
  try { return JSON.parse(sessionStorage.getItem(restartKey) || "null"); } catch { return null; }
}

export function ApplicationDataSection({ directories, onError, active = true }: {
  directories?: StorageDirectories; onError: (error: unknown) => void; active?: boolean;
}) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<string | null>(null);
  const [plan, setPlan] = useState<ApplicationDataPlan | null>(null);
  const [picker, setPicker] = useState(false);
  const [restart, setRestart] = useState(rememberedRestart);
  const [notice, setNotice] = useState("");
  const handled = useRef("");
  const dialog = useRef<HTMLDivElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const previewButton = useRef<HTMLButtonElement>(null);
  const reconnect = useQuery({
    queryKey: ["application-data-restart", restart?.id], queryFn: getStorageDirectories,
    enabled: Boolean(restart), retry: false, refetchInterval: restart ? 1000 : false,
  });
  const current = reconnect.data ?? directories;
  const data = current?.application_data;
  useEffect(() => {
    const record = reconnect.data?.application_data?.migration;
    if (!restart || record?.id !== restart.id || !["completed", "failed"].includes(record.status) || handled.current === record.id) return;
    handled.current = record.id;
    setRestart(null);
    sessionStorage.removeItem(restartKey);
    if (record.status === "completed") {
      setDraft(null); setPlan(null); setNotice("应用数据已迁移，软件已重新启动。原数据保留为备份。");
      void client.invalidateQueries();
    } else onError(new Error(record.message || "迁移失败，已继续使用原数据启动"));
  }, [reconnect.data, restart, client, onError]);
  const preview = useMutation({
    mutationFn: () => previewApplicationData({ destination: draft!.trim(), library_id: directories!.library_id }),
    onSuccess: setPlan, onError,
  });
  const move = useMutation({
    mutationFn: () => migrateApplicationData({ destination: plan!.destination, library_id: directories!.library_id, plan_id: plan!.plan_id }),
    onSuccess: (value) => {
      const token = { id: value.id, started: Date.now() };
      sessionStorage.setItem(restartKey, JSON.stringify(token));
      setRestart(token); setPlan(null); setNotice(""); clearUnsaved();
    }, onError: (error) => { setPlan(null); onError(error); },
  });
  useModalFocus(active && Boolean(plan), dialog, cancelButton, () => { if (!move.isPending) setPlan(null); },
    undefined, previewButton.current);
  const clearUnsaved = useUnsavedDraft({ label: "应用数据目录",
    dirty: Boolean(data && draft !== null && draft.trim() !== (data.root ?? "") && !restart),
    busy: preview.isPending || move.isPending || picker,
    save: async () => {
      if (!draft?.trim()) throw new Error("请填写应用数据目录。");
      if (!directories || directories.busy || restart) throw new Error("当前有任务或迁移，请稍后修改目录。");
      await preview.mutateAsync(); return false;
    },
    discard: () => { setDraft(null); setPlan(null); },
  });
  if (!data || !directories) return null;
  const value = draft ?? data.root ?? "";
  const busy = Boolean(directories.busy || picker || preview.isPending || move.isPending || restart);
  return <section className="settings-section" aria-labelledby="application-data-title">
    <div className="settings-section-heading"><div className="settings-section-title">
      <Database size={18} /><h2 id="application-data-title">应用数据</h2>
    </div></div>
    <p className="settings-hint">将数据库、设置和加密凭据统一放在一个文件夹中，分别使用 library 与 config 子文件夹。迁移时自动停止后端，校验后立即重启；原数据保留为备份。</p>
    <fieldset className="storage-directory-fields" disabled={busy}>
      <div className="storage-directory-row">
        <label className="settings-field grow"><span>应用数据目录<small>迁移并立即重启</small></span>
          <input aria-label="应用数据目录" value={value} placeholder="选择目录，自动创建 library 和 config"
            onChange={event => { setDraft(event.target.value); setPlan(null); }} spellCheck={false} autoComplete="off" />
        </label>
        <button className="icon-button settings-picker-button" aria-label="选择应用数据目录" title="选择应用数据目录" type="button" onClick={async () => {
          setPicker(true);
          try {
            const selected = await pollPicker(await createPicker("directory"));
            if (selected) { setDraft(selected); setPlan(null); }
          } catch (error) { onError(error); } finally { setPicker(false); }
        }}>{picker ? <LoaderCircle className="spin" size={16} /> : <FolderOpen size={16} />}</button>
      </div>
    </fieldset>
    <dl className="storage-derived-paths">
      <div><dt>视频库数据库</dt><dd>{data.database_dir} · {bytes(data.database_bytes)}</dd></div>
      <div><dt>设置与加密凭据</dt><dd>{data.config_dir} · {bytes(data.config_bytes)}</dd></div>
    </dl>
    <p className="settings-hint">视频、音频和 ASR 资源使用上方各自的目录。独立运行环境不能直接搬动，需在新位置重新准备。程序目录仅保留很小的启动定位文件。</p>
    {notice && <p role="status">{notice}</p>}
    {restart && <div className="settings-loading" role="status"><LoaderCircle className="spin" size={18} />
      正在迁移并重启，等待自动重连…{reconnect.isError && " 服务暂时离线，请稍候。"}
    </div>}
    {restart && Date.now() - restart.started > 120000 && <p role="alert">重启尚未完成。请重新打开应用查看迁移结果；原数据和迁移记录均已保留。</p>}
    {data.migration?.status === "failed" && !restart && <p className="field-error" role="alert">上次迁移未完成：{data.migration.message}。原数据仍保留。</p>}
    {createPortal(<AnimatePresence>{plan && active && !restart && <AnimatedOverlay className="modal-backdrop" key="application-data-confirm"
      onClick={() => { if (!move.isPending) setPlan(null); }}>
      <AnimatedPopup ref={dialog} className="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="application-data-confirm-title"
        aria-describedby="application-data-confirm-warning" tabIndex={-1} onClick={event => event.stopPropagation()}>
      <section>
      <h3 id="application-data-confirm-title">确认迁移并立即重启？</h3>
      <p id="application-data-confirm-warning"><strong>修改应用数据目录后，软件会自动立即重启。</strong>重启期间页面会短暂断开连接，完成后自动重连。</p>
      <p>{plan.files_total} 个文件 · {bytes(plan.bytes_total)}，磁盘可用 {bytes(plan.free_bytes)}。</p>
      <dl className="storage-derived-paths"><div><dt>数据库</dt><dd>{plan.database_dir}</dd></div>
        <div><dt>设置与凭据</dt><dd>{plan.config_dir}</dd></div></dl>
      <p>原数据库和配置保留为备份；现有视频文件位置不变。</p>
      <div className="settings-action-row"><button className="primary-button" disabled={busy} onClick={() => move.mutate()}>
        {move.isPending ? <LoaderCircle className="spin" size={16} /> : <RotateCcw size={16} />}迁移并立即重启</button>
        <button ref={cancelButton} className="secondary-button" disabled={busy} onClick={() => setPlan(null)}>取消</button></div>
      </section></AnimatedPopup>
    </AnimatedOverlay>}</AnimatePresence>, document.body)}
    {!restart && <div className="settings-action-row"><button ref={previewButton} className="primary-button"
      disabled={busy || !draft?.trim() || draft.trim() === data.root} onClick={() => preview.mutate()}>
      {preview.isPending ? <LoaderCircle className="spin" size={16} /> : <FolderOpen size={16} />}检查迁移范围
    </button></div>}
  </section>;
}
