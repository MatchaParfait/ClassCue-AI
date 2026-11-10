import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertCircle, ArrowDownToLine, ArrowUpFromLine, Cable, Check, CheckCircle2, LoaderCircle, Pause, Play, RefreshCw, RotateCcw, Send, ShieldCheck, X } from "lucide-react";
import { getLibrary, getVideos } from "../api/client";
import { transferPost, transferStatus, type TransferAction, type TransferSelection, type TransferTask } from "../api/deviceTransfers";
import { useModalFocus } from "../hooks/useModalFocus";
import { AnimatedOverlay, AnimatedPopup } from "./Motion";
import { DeviceStudySyncTask } from "./DeviceStudySync";
import "../device-transfers.css";

const stateLabels: Record<string, string> = {
  preparing: "正在准备", awaiting_confirmation: "等待接收确认", sending: "正在发送", receiving: "正在接收", paused: "已暂停",
  verifying: "正在校验", importing: "正在入库", completed: "已完成", cancelled: "已取消", failed: "需要处理",
  partial: "部分完成",
};
function bytes(value: number) {
  return value < 1024 ** 2 ? `${Math.round(value / 1024)} KB` : `${(value / 1024 ** 2).toFixed(1)} MB`;
}
async function folderOptions() {
  const result: Array<{ id: number; label: string }> = [];
  const queue: Array<{ id?: number; prefix: string }> = [{ prefix: "" }];
  while (queue.length && result.length < 10000) {
    const next = queue.shift()!;
    const page = await getLibrary(next.id);
    for (const item of page.items) {
      if (item.type !== "folder" || !item.folder) continue;
      const label = `${next.prefix}${item.folder.name}`;
      result.push({ id: item.folder.id, label });
      queue.push({ id: item.folder.id, prefix: `${label} / ` });
    }
  }
  return result;
}

function ReceivePreview({ task, folders, busy, onAccept }: {
  task: TransferTask; folders: Array<{ id: number; label: string }>; busy: boolean;
  onAccept: (id: string, items: Array<{ id: string; action: TransferAction }>, target: number | null) => void;
}) {
  const [choices, setChoices] = useState<Record<string, TransferAction>>({});
  const [target, setTarget] = useState("");
  return <div className="device-receive-preview">
    <label className="device-field">保存到
      <select aria-label="接收目标文件夹" value={target} disabled={busy} onChange={event => setTarget(event.target.value)}>
        <option value="">自动整理到视频库</option>
        {folders.map(folder => <option key={folder.id} value={folder.id}>{folder.label}</option>)}
      </select>
    </label>
    <div className="device-preview-list">{task.items.map(item => {
      const action = choices[item.id] ?? (item.conflict || item.blocked ? "skip" : "receive");
      return <div className="device-preview-row" key={item.id}>
        <div><strong>{item.name}</strong><span>{item.subtitle_count} 条字幕 · {item.marker_count} 个考点
          {item.same ? " · 资料相同" : item.conflict ? " · 学习资料有差异" : item.need_video ? " · 包含视频" : " · 复用本地视频"}</span>
          {item.blocked && <span className="field-error">{item.blocked}</span>}</div>
        <select value={action} aria-label={`${item.name}的接收操作`} disabled={busy}
          onChange={event => setChoices(current => ({ ...current, [item.id]: event.target.value as TransferAction }))}>
          {!item.blocked && <option value={item.conflict ? "overwrite" : "receive"}>{item.conflict ? "覆盖并备份" : item.same ? "复用资料" : "接收"}</option>}
          <option value="skip">跳过</option>
        </select>
      </div>;
    })}</div>
    <button className="primary-button compact" disabled={busy} onClick={() => onAccept(task.id, task.items.map(item => ({
      id: item.id, action: choices[item.id] ?? (item.conflict || item.blocked ? "skip" : "receive"),
    })), target ? Number(target) : null)}><Check size={15} />确认接收</button>
  </div>;
}

export default function DeviceTransferDialog({ libraryId, selection, returnFocus, onClose }: {
  libraryId: string; selection?: TransferSelection; returnFocus: HTMLElement | null; onClose: () => void;
}) {
  const dialog = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const submitting = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [source, setSource] = useState("");
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ["device-transfers", libraryId], queryFn: transferStatus, refetchInterval: 2000 });
  const folders = useQuery({ queryKey: ["device-transfer-folders", libraryId], queryFn: folderOptions });
  const videos = useQuery({ queryKey: ["device-transfer-videos", libraryId], queryFn: getVideos });
  const data = status.data;
  const selectedDevice = data?.devices.find(device => device.id === deviceId) ?? data?.devices[0];
  const close = () => { if (!submitting.current) onClose(); };
  useModalFocus(true, dialog, closeButton, close, undefined, returnFocus);
  const completed = [...(data?.tasks ?? []), ...(data?.sync_tasks ?? [])].filter(task => task.state === "completed").map(task => task.id).join(",");
  useEffect(() => {
    if (!completed) return;
    for (const key of ["videos", "library", "library-status", "study", "settings", "device-transfer-folders", "device-transfer-videos"]) {
      void queryClient.invalidateQueries({ queryKey: [key] });
    }
  }, [completed, queryClient]);
  const act = async (operation: () => Promise<unknown>) => {
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    try { await operation(); await status.refetch(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { submitting.current = false; setBusy(false); }
  };
  const send = () => {
    if (!selectedDevice) return;
    const [kind, value] = source.split(":");
    const chosen = selection ?? (kind === "folder" ? { title: "", folderId: Number(value) } : { title: "", videoIds: [Number(value)] });
    void act(() => transferPost("/send", {
      device_id: selectedDevice.id, library_id: libraryId, folder_id: chosen.folderId ?? null, video_ids: chosen.videoIds ?? [],
    }));
  };
  const sync = (selected: boolean) => {
    if (!selectedDevice) return;
    const [kind, value] = source.split(":");
    const chosen = selected ? selection ?? (kind === "folder" ? { folderId: Number(value) } : { videoIds: [Number(value)] }) : undefined;
    void act(() => transferPost("/sync", {
      device_id: selectedDevice.id, library_id: libraryId, video_ids: chosen?.videoIds ?? [], folder_id: chosen?.folderId ?? null,
    }));
  };
  return createPortal(<AnimatedOverlay className="modal-backdrop device-transfer-backdrop"
    onClick={event => { event.stopPropagation(); if (event.target === event.currentTarget) close(); }}>
    <AnimatedPopup ref={dialog} className="device-transfer-dialog" role="dialog" aria-modal="true"
      aria-labelledby="device-transfer-title" tabIndex={-1}>
      <header className="device-transfer-heading"><Cable size={21} /><div><h2 id="device-transfer-title">连接平板</h2>
        <p>课程传送与学习修改同步</p></div>
        <button ref={closeButton} className="icon-button" type="button" title="关闭" aria-label="关闭设备连接" disabled={busy} onClick={close}><X size={19} /></button>
      </header>
      <div className="device-transfer-content">
        <section className="device-connection">
          <div className="device-section-title"><h3>已连接设备</h3>
            <button className="text-button" disabled={busy} onClick={() => void act(() => transferPost("/discover"))}>
              <RefreshCw size={15} />刷新</button></div>
          {status.isLoading && <p className="device-note"><LoaderCircle size={16} className="spin" />正在查找设备…</p>}
          {data?.devices.length === 0 && <p className="device-note">连接数据线，在平板打开“连接电脑”，并将 USB 用途选为“文件传输”。</p>}
          {!!data?.devices.length && <div className="device-connection-row">
            <select aria-label="连接的平板" value={selectedDevice?.id ?? ""} disabled={busy} onChange={event => setDeviceId(event.target.value)}>
              {data.devices.map(device => <option key={device.id} value={device.id}>{device.name}</option>)}
            </select>
            {selectedDevice?.paired ? <><span className="device-paired"><ShieldCheck size={16} />已配对</span>
              <button className="text-button" disabled={busy || !selectedDevice.root_available}
                onClick={() => void act(() => transferPost("/pairings", { device_id: selectedDevice.id }))}>
                <RefreshCw size={14} />重新配对</button></>
              : <button className="secondary-button compact" disabled={busy || !selectedDevice?.root_available}
                onClick={() => void act(() => transferPost("/pairings", { device_id: selectedDevice?.id }))}><Cable size={15} />配对</button>}
          </div>}
          {selectedDevice?.message && <p className="device-note">{selectedDevice.message}</p>}
          {data?.pairings.map(pair => <div className="device-pairing" key={pair.id}>
            <div><strong>{pair.name}</strong><span>核对两端配对码</span></div><output>{pair.code}</output>
            <button className="secondary-button compact" disabled={busy || pair.confirmed}
              onClick={() => void act(() => transferPost(`/pairings/${pair.id}/confirm`))}>
              <Check size={15} />{pair.confirmed ? "等待平板确认" : "配对码一致"}</button>
          </div>)}
        </section>
        <section className="device-send-section">
          <div className="device-section-title"><h3>发送课程</h3><ArrowUpFromLine size={17} /></div>
          <div className="device-send-row">
            {selection ? <strong>{selection.title}</strong> : <select aria-label="要发送的课程" value={source} disabled={busy}
              onChange={event => setSource(event.target.value)}>
              <option value="">选择文件夹或视频</option>
              {!!folders.data?.length && <optgroup label="课程文件夹">{folders.data.map(folder =>
                <option key={folder.id} value={`folder:${folder.id}`}>{folder.label}</option>)}</optgroup>}
              {!!videos.data?.length && <optgroup label="单个视频">{videos.data.map(video =>
                <option key={video.id} value={`video:${video.id}`}>{video.display_name || video.filename}</option>)}</optgroup>}
            </select>}
            <button className="primary-button compact" disabled={busy || !selectedDevice?.paired || !selectedDevice.foreground || (!selection && !source)}
              onClick={send}>{busy ? <LoaderCircle className="spin" size={15} /> : <Send size={15} />}发送到平板</button>
          </div>
        </section>
        <section aria-label="学习修改同步">
          <div className="device-section-title"><h3>学习修改同步</h3>
            <button className="text-button" disabled={busy || !selectedDevice?.paired || !selectedDevice.foreground}
              onClick={() => sync(false)}><RefreshCw size={15} />同步已关联视频</button></div>
          <div className="device-send-row"><span className="device-note">考点与考题</span>
            <button className="secondary-button compact"
              disabled={busy || !selectedDevice?.paired || !selectedDevice.foreground || (!selection && !source)}
              onClick={() => sync(true)}><RefreshCw size={15} />关联并同步所选</button></div>
          {!data?.sync_tasks?.length && <p className="device-note">尚无同步记录</p>}
          {data?.sync_tasks?.map(task => <DeviceStudySyncTask key={`${task.id}:${JSON.stringify(task.preview)}`} task={task} busy={busy}
            execute={(id, action, choices) => void act(() => transferPost(`/sync/${id}/${action}`, { library_id: libraryId, ...(choices ? { choices } : {}) }))} />)}
        </section>
        {(error || status.error || folders.error || videos.error || data?.error) && <p className="field-error device-note" role="alert"><AlertCircle size={16} />
          {error || (status.error ?? folders.error ?? videos.error)?.message || data?.error}</p>}
        <section className="device-tasks">
          <div className="device-section-title"><h3>传输记录</h3><ArrowDownToLine size={17} /></div>
          {data?.tasks.length === 0 && <p className="device-note">尚无传输记录</p>}
          {data?.tasks.map(task => {
            const terminal = ["completed", "partial", "cancelled"].includes(task.state) || task.state === "failed" && task.direction === "send";
            const progress = Math.min(100, task.transferred_bytes / Math.max(1, task.total_bytes) * 100);
            const failedCount = task.results.filter(result => result.state === "failed").length;
            return <article className="device-task" key={task.id}>
              <div className="device-task-heading"><div>{task.direction === "send" ? <ArrowUpFromLine size={16} /> : <ArrowDownToLine size={16} />}
                <strong>{task.title}</strong></div><span>{task.state === "completed" && failedCount ? "部分完成" : stateLabels[task.state] ?? task.state}</span></div>
              <p className="device-task-meta">{task.peer_name} · {task.video_count} 个视频 · {terminal
                ? `已传送 ${bytes(task.transferred_bytes)} 视频`
                : `${bytes(task.transferred_bytes)} / ${bytes(task.total_bytes)}`}</p>
              {!terminal && <progress max={100} value={progress} aria-label={`${task.title}传输进度`} />}
              {task.error && <p className="device-note">{task.error}</p>}
              {task.direction === "receive" && task.state === "awaiting_confirmation" && <ReceivePreview task={task} folders={folders.data ?? []} busy={busy}
                onAccept={(id, items, target) => void act(() => transferPost(`/tasks/${id}/accept`, { library_id: libraryId, items, target_folder_id: target }))} />}
              {!!task.results.length && <details className="device-results"><summary>{task.results.length} 项结果{failedCount ? ` · ${failedCount} 项失败` : ""}</summary>
                {task.results.map(result => <p key={result.id} className={result.state === "failed" ? "field-error" : ""}>
                  {result.state === "failed" ? <AlertCircle size={14} /> : <CheckCircle2 size={14} />}{result.name}：{result.message}
                </p>)}</details>}
              {!terminal && <div className="device-task-actions">
                {["sending", "receiving", "verifying", "importing"].includes(task.state) && <button className="text-button" disabled={busy}
                  onClick={() => void act(() => transferPost(`/tasks/${task.id}/pause`, { library_id: libraryId }))}><Pause size={14} />暂停</button>}
                {(task.state === "paused" || task.state === "failed" && task.direction === "receive") && <button className="text-button" disabled={busy}
                  onClick={() => void act(() => transferPost(`/tasks/${task.id}/resume`, { library_id: libraryId }))}><Play size={14} />继续</button>}
                <button className="text-button" disabled={busy} onClick={() => void act(() => transferPost(`/tasks/${task.id}/cancel`, { library_id: libraryId }))}><X size={14} />取消传输</button>
              </div>}
              {task.direction === "send" && (failedCount > 0 || task.state === "failed") && <div className="device-task-actions">
                <button className="text-button" disabled={busy} onClick={() => void act(() => transferPost(`/tasks/${task.id}/retry`, { library_id: libraryId }))}>
                  <RotateCcw size={14} />重新发送失败项
                </button>
              </div>}
            </article>;
          })}
        </section>
        {!!data?.snapshots.length && <section className="device-snapshots"><h3>覆盖前的备份</h3>
          {data.snapshots.map(snapshot => <div className="device-snapshot" key={snapshot.id}><span>
            视频 {snapshot.video_id} · {new Date(snapshot.created * 1000).toLocaleString()}</span>
            <button className="text-button" disabled={busy || Boolean(snapshot.restored)} onClick={() => {
              if (window.confirm("恢复这份覆盖前的学习资料？当前字幕和考点将被替换。")) void act(() => transferPost(`/snapshots/${snapshot.id}/restore`, { library_id: libraryId })).then(() => {
                for (const key of ["videos", "library", "study"]) void queryClient.invalidateQueries({ queryKey: [key] });
              });
            }}><RotateCcw size={14} />{snapshot.restored ? "已恢复" : "恢复"}</button></div>)}
        </section>}
      </div>
    </AnimatedPopup>
  </AnimatedOverlay>, document.body);
}
