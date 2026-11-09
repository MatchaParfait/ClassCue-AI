import { useEffect, useState, type RefObject } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FolderOpen, LoaderCircle } from "lucide-react";
import { createPicker, pollPicker } from "../api/client";
import { connectDsh, connectDshAuto, getDshStatus, getDshWorkspaceRoot, setDshWorkspaceRoot,
  type DshStatus } from "../api/dshBridge";
import "./DshQuestionActions.css";

export function DshConnectionSettings({ active = true, scope = "default", initialRef, onBusyChange }: {
  active?: boolean; scope?: string; initialRef?: RefObject<HTMLButtonElement>; onBusyChange?: (busy: boolean) => void;
}) {
  const cache = useQueryClient();
  const [path, setPath] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [picking, setPicking] = useState(false);
  const statusKey = ["dsh-status", scope];
  const rootKey = ["dsh-workspace-root", scope];
  const status = useQuery({ queryKey: statusKey, queryFn: getDshStatus, enabled: active, retry: false });
  const workspaceRoot = useQuery({ queryKey: rootKey, queryFn: getDshWorkspaceRoot, enabled: active, retry: false });
  const connected = (value: DshStatus) => {
    cache.setQueryData(statusKey, value);
    void cache.invalidateQueries({ queryKey: ["dsh-status"] });
    void cache.invalidateQueries({ queryKey: ["dsh-workspace-root"] });
    setError(""); setNotice("DSH 已连接。");
  };
  const connect = useMutation({
    mutationFn: () => connectDsh(path.trim()),
    onSuccess: connected,
    onError: reason => setError((reason as Error).message),
  });
  const autoConnect = useMutation({
    mutationFn: connectDshAuto,
    onSuccess: connected,
    onError: reason => setError((reason as Error).message),
  });
  const busy = picking || connect.isPending || autoConnect.isPending;
  useEffect(() => {
    onBusyChange?.(busy);
    return () => onBusyChange?.(false);
  }, [busy, onBusyChange]);

  return <div className="dsh-connection-settings">
    <div className="dsh-root-setting">
      <span className="dsh-root-label">默认工作区</span>
      <output className="dsh-root-path" aria-label="DSH 默认工作区">
        {workspaceRoot.isLoading ? "正在读取…" : workspaceRoot.error ? "读取失败" : workspaceRoot.data?.path || "尚未设置"}
      </output>
      <button ref={initialRef} className="secondary-button" type="button" disabled={busy || !active}
        onClick={async () => {
          setPicking(true); setError(""); setNotice("");
          try {
            const connection = await connectDshAuto();
            cache.setQueryData(statusKey, connection);
            await cache.invalidateQueries({ queryKey: ["dsh-workspace-root"] });
            if (!connection.capabilities.includes("workspace_tree"))
              throw new Error("请更新 ClassCue 插件至 0.3.0，并完全退出后重新启动 DSH");
            const directory = await pollPicker(await createPicker("directory"));
            if (directory) {
              const saved = await setDshWorkspaceRoot(directory, connection.instance_id);
              cache.setQueryData(rootKey, saved);
              await cache.invalidateQueries({ queryKey: ["dsh-workspace-root"],
                predicate: query => query.queryKey[1] !== scope });
              void cache.invalidateQueries({ queryKey: ["dsh-status"] });
              setNotice("默认工作区已保存。");
            }
          } catch (reason) { setError((reason as Error).message); }
          finally { setPicking(false); }
        }}>
        {picking ? <LoaderCircle size={16} className="spin" /> : <FolderOpen size={16} />}
        {workspaceRoot.data ? "更改默认工作区" : "选择默认工作区"}
      </button>
      {workspaceRoot.data && <p className="settings-hint">更改后，新资料使用新目录，已有资料保留原位置。</p>}
      {workspaceRoot.error && <p role="alert" className="field-error">{workspaceRoot.error.message}</p>}
    </div>
    <button className="primary-button dsh-auto-connect" type="button" disabled={busy || !active}
      onClick={() => { setError(""); setNotice(""); autoConnect.mutate(); }}>
      {autoConnect.isPending && <LoaderCircle size={16} className="spin" />}自动连接 DSH
    </button>
    <details className="dsh-advanced-connection"><summary>自定义 DSH profile</summary>
      <label className="settings-field"><span>插件连接文件</span>
        <input value={path} onChange={event => setPath(event.target.value)}
          placeholder="connection.json" aria-label="插件连接文件" disabled={busy || !active} spellCheck={false} /></label>
      <div className="dsh-question-toolbar">
        <button className="secondary-button" type="button" disabled={busy || !active} onClick={async () => {
          setPicking(true); setError(""); setNotice("");
          try {
            const directory = await pollPicker(await createPicker("directory"));
            if (directory) setPath(`${directory.replace(/[\\/]$/, "")}\\connection.json`);
          } catch (reason) { setError((reason as Error).message); } finally { setPicking(false); }
        }}><FolderOpen size={16} />选择 classcue-bridge 文件夹</button>
        <button className="primary-button" type="button" disabled={busy || !active || !path.trim()}
          onClick={() => { setError(""); setNotice(""); connect.mutate(); }}>
          {connect.isPending && <LoaderCircle size={16} className="spin" />}连接</button>
      </div>
    </details>
    {(error || status.error) && <p role="alert" className="field-error">{error || status.error?.message}</p>}
    {notice && <p role="status">{notice}</p>}
  </div>;
}
