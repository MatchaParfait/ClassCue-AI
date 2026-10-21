import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, ExternalLink, Github, Pause, RefreshCw, RotateCw } from "lucide-react";
import { safeExternal, updateRequest, updatesKey, useUpdates, type UpdatePreferences } from "../api/updates";
import { useDesktopAPI } from "../hooks/useDesktopAPI";
import { useModalFocus } from "../hooks/useModalFocus";
import { useUnsavedLeave } from "./UnsavedChanges";
import { Announcements } from "./Announcements";
import "../updates.css";

const size = (value: number) => `${(value / 1024 / 1024).toFixed(1)} MB`;
export function UpdateSettings({ active }: { active: boolean }) {
  const query = useUpdates(active), client = useQueryClient(), api = useDesktopAPI(), leave = useUnsavedLeave();
  const [confirm, setConfirm] = useState(false), [installing, setInstalling] = useState(false), [failure, setFailure] = useState("");
  const dialog = useRef<HTMLDivElement>(null), cancel = useRef<HTMLButtonElement>(null);
  const action = useMutation({
    mutationFn: ({ path, body = {}, method = "POST" }: { path: string; body?: unknown; method?: string }) => updateRequest(path, body, method),
    onSuccess: () => { void client.invalidateQueries({ queryKey: updatesKey }); },
  });
  useModalFocus(confirm && active, dialog, cancel, () => { if (!installing) setConfirm(false); });
  const data = query.data, release = data?.release;
  const preference = (name: keyof UpdatePreferences, value: boolean) =>
    action.mutate({ path: "/preferences", body: { [name]: value }, method: "PUT" });
  const install = async () => {
    if (installing || !api?.install_update) return;
    setInstalling(true); setFailure("");
    try {
      await api.set_unsaved_changes?.(false);
      await api.install_update();
    } catch (error) {
      setInstalling(false); setFailure(error instanceof Error ? error.message : String(error));
    }
  };
  return <section className="settings-section update-settings">
    <div className="settings-section-heading"><div><h2>关于与更新</h2><p>课堂视频重点复习</p></div><Announcements active={active} /></div>
    {query.isLoading && <p role="status">正在读取版本信息…</p>}
    {query.isError && <p className="field-error" role="alert">版本信息读取失败。<button className="secondary-button" onClick={() => void query.refetch()}><RefreshCw size={15} />重试</button></p>}
    {data && <>
      <div className="update-version-row"><div><span className="settings-hint">当前版本</span><strong className="update-version">v{data.version}</strong></div>
        <div className="update-actions"><a className="secondary-button" target="_blank" rel="noopener noreferrer" href={`https://github.com/${data.repository}`}><Github size={16} />开源仓库</a>
          <button className="secondary-button" disabled={action.isPending || data.checking || data.downloading} onClick={() => action.mutate({ path: "/check" })}>
            <RefreshCw size={16} className={data.checking ? "spin" : ""} />{data.checking ? "正在检查…" : "检查更新"}</button></div>
      </div>
      <p className="settings-hint">{data.last_checked ? `上次成功检查：${new Date(data.last_checked).toLocaleString()}` : "尚未成功连接发行源"}</p>
      {!data.configured && <p className="field-error">维护者尚未配置发行签名。</p>}
      {!data.portable && <p className="settings-hint">当前为源码运行，不执行原地更新。</p>}
      {data.error && <p className="field-error" role="alert">{data.error}</p>}
      <div className="update-preferences">
        <label><input type="checkbox" checked={data.preferences.auto_check} disabled={action.isPending} onChange={event => preference("auto_check", event.target.checked)} /><span>每天自动检查版本和公告</span></label>
        <label><input type="checkbox" checked={data.preferences.auto_download} disabled={action.isPending || !data.portable} onChange={event => preference("auto_download", event.target.checked)} /><span>自动下载兼容更新</span></label>
        <label><input type="checkbox" checked={data.preferences.show_announcements} disabled={action.isPending} onChange={event => preference("show_announcements", event.target.checked)} /><span>显示公告未读提醒</span></label>
      </div>
      {release ? <div className="update-release">
        <h3>新版本 v{release.version}</h3>
        {release.notes && <p className="update-notes">{release.notes}</p>}
        {!data.compatible && <p className="settings-hint">此版本的依赖或数据格式已变化，需要手动升级。</p>}
        {data.downloading && <div role="status"><progress max={release.size} value={data.downloaded_bytes} aria-label="更新包下载进度" /><span>{size(data.downloaded_bytes)} / {size(release.size)}</span></div>}
        {data.download_error && <p className="field-error" role="alert">{data.download_error}</p>}
        <div className="update-actions">
          {data.portable && data.compatible && !data.ready && <button className="primary-button" disabled={data.downloading || action.isPending} onClick={() => action.mutate({ path: "/download" })}><Download size={16} />{data.downloading ? "正在下载…" : `下载更新 · ${size(release.size)}`}</button>}
          {data.downloading && <button className="secondary-button" disabled={action.isPending} onClick={() => action.mutate({ path: "/pause" })}><Pause size={16} />暂停下载</button>}
          {data.ready && data.portable && data.compatible && <button className="primary-button" disabled={Boolean(data.busy) || !api?.install_update || installing}
            onClick={() => leave(() => { setFailure(""); setConfirm(true); })}><RotateCw size={16} />安装并重启</button>}
          {safeExternal(release.url) && <a className="secondary-button" href={safeExternal(release.url)} target="_blank" rel="noopener noreferrer"><ExternalLink size={16} />手动下载发行包</a>}
        </div>
        {data.ready && !api?.install_update && <p className="settings-hint">请在独立桌面窗口中安装更新。</p>}
        {data.busy && data.ready && <p className="settings-hint">请先完成或停止运行及暂停中的任务。</p>}
      </div> : data.last_checked && !data.error ? <p role="status">当前已是最新稳定版。</p> : null}
      {data.install?.message && <p className="settings-hint">{data.install.message}</p>}
      <p className="settings-hint">MIT License · 更新检查仅访问公开 GitHub 发行源，不上传视频、字幕或密钥。</p>
    </>}
    {action.error && <p role="alert" className="field-error">{action.error.message}</p>}
    {confirm && active && createPortal(<div className="modal-backdrop" onClick={() => { if (!installing) setConfirm(false); }}>
      <div className="update-confirm" ref={dialog} role="dialog" aria-modal="true" aria-labelledby="update-confirm-title" tabIndex={-1} onClick={event => event.stopPropagation()}>
        <h2 id="update-confirm-title">安装 v{release?.version} 并重启？</h2>
        <p>软件将退出并替换程序文件。视频、模型、设置和处理结果保留，安装前会创建程序及数据库备份。</p>
        <p>安装期间请勿关闭电脑或移动应用目录。</p>
        {failure && <p role="alert" className="field-error">{failure}</p>}
        {installing && <p role="status">正在校验更新包并退出软件…</p>}
        <div className="update-actions"><button ref={cancel} className="secondary-button" disabled={installing} onClick={() => setConfirm(false)}>取消</button>
          <button className="primary-button" disabled={installing} onClick={() => void install()}><RotateCw size={16} />{installing ? "正在准备…" : "安装并重启"}</button></div>
      </div>
    </div>, document.body)}
  </section>;
}
