import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, FolderOpen, LoaderCircle, MessageSquare, Plus, Settings2, X } from "lucide-react";
import { createPicker, pollPicker } from "../api/client";
import { connectDsh, getDshDiscussions, getDshRequest, getDshStatus, openDshDiscussion, prepareDshQuestion } from "../api/dshBridge";
import { useDesktopAPI } from "../hooks/useDesktopAPI";
import { useModalFocus } from "../hooks/useModalFocus";
import "./DshQuestionActions.css";

export function DshQuestionActions({ questionId, contentVersion, scope = "default", active = true }: {
  questionId: string; contentVersion: number; scope?: string; active?: boolean;
}) {
  const desktop = useDesktopAPI();
  if (!desktop || !active) return null;
  return <DesktopQuestionActions key={`${scope}:${questionId}:${contentVersion}`}
    questionId={questionId} contentVersion={contentVersion} scope={scope} />;
}

function DesktopQuestionActions({ questionId, contentVersion, scope }: {
  questionId: string; contentVersion: number; scope: string;
}) {
  const cache = useQueryClient();
  const [settings, setSettings] = useState(false);
  const [path, setPath] = useState("");
  const [error, setError] = useState("");
  const [requestId, setRequestId] = useState<string | null>(null);
  const [picking, setPicking] = useState(false);
  const modal = useRef<HTMLDivElement>(null);
  const first = useRef<HTMLInputElement>(null);
  const historyKey = ["dsh-discussions", scope, questionId];
  const statusKey = ["dsh-status", scope];
  const requestKey = (id: string | null) => ["dsh-request", scope, id];
  const status = useQuery({ queryKey: statusKey, queryFn: getDshStatus, retry: false });
  const history = useQuery({ queryKey: historyKey, queryFn: () => getDshDiscussions(questionId), retry: false });
  const receipt = useQuery({
    queryKey: requestKey(requestId), queryFn: () => getDshRequest(requestId!), enabled: Boolean(requestId),
    retry: false, refetchInterval: query => {
      const value = query.state.data;
      return query.state.error || value?.error || value?.outcome === "cancelled"
        || value && ["sent", "draft_ready"].includes(value.status) ? false : 1000;
    },
  });
  const active = history.data?.filter(d => d.content_version === contentVersion).at(-1);
  const start = useMutation({
    mutationFn: (newDiscussion: boolean) => prepareDshQuestion(questionId, newDiscussion),
    onSuccess: value => { setRequestId(value.request_id); void cache.invalidateQueries({ queryKey: historyKey }); },
    onError: reason => {
      const message = (reason as Error).message;
      setError(message);
      if (message.includes("请先选择 DSH")) setSettings(true);
    },
  });
  const open = useMutation({
    mutationFn: (id: string) => openDshDiscussion(id),
    onSuccess: value => {
      setRequestId(value.request_id);
      cache.setQueryData(requestKey(value.request_id), value);
      void cache.invalidateQueries({ queryKey: requestKey(value.request_id) });
    },
    onError: reason => setError((reason as Error).message),
  });
  const connect = useMutation({
    mutationFn: () => connectDsh(path.trim()),
    onSuccess: value => { cache.setQueryData(statusKey, value); setSettings(false); setError(""); },
    onError: reason => setError((reason as Error).message),
  });
  const pending = start.isPending || open.isPending;
  const busy = pending || connect.isPending || picking;
  useModalFocus(settings, modal, first, () => { if (!busy) setSettings(false); });
  const result = receipt.data;
  const notice = result?.outcome === "cancelled" ? "已取消工作区选择，可以再次打开讨论。"
    : result?.error === "draft_conflict" ? "DSH 中已有草稿，已保留。请在原对话中查看。"
    : result?.error ? "无法准备 DSH 草稿，请检查桌面连接后重试。"
    : result?.status === "choose_workspace" ? "请在 DSH 中选择工作区。"
    : result?.status === "draft_ready" ? "题目已放入 DSH 草稿，等待你发送。"
    : result?.status === "sent" ? "已打开原讨论。"
    : result ? "正在等待 DSH 准备对话…" : "";
  return <div className="dsh-question-actions">
    <div className="dsh-question-toolbar">
      <button className="secondary-button compact" type="button" disabled={pending || status.isPending}
        onClick={() => {
          setError("");
          if (active) open.mutate(active.request_id); else start.mutate(false);
        }}>
        {pending ? <LoaderCircle className="spin" size={16} /> : <MessageSquare size={16} />}
        {active ? "继续 DSH 讨论" : "在 DSH 中讨论"}
      </button>
      <button className="icon-button" type="button" title="新建独立讨论" aria-label="新建独立讨论"
        disabled={pending || status.isPending} onClick={() => { setError(""); start.mutate(true); }}>
        <Plus size={16} />
      </button>
      <button className="icon-button" type="button" title="DSH 桌面连接" aria-label="DSH 桌面连接"
        onClick={() => { setError(""); setSettings(true); }}><Settings2 size={16} /></button>
    </div>
    {notice && <p role="status">{notice}</p>}
    {(error || receipt.error) && <p className="field-error" role="alert">{error || (receipt.error as Error).message}</p>}
    {Boolean(history.data?.length) && <details><summary>讨论记录（{history.data!.length}）</summary>
      <ul>{history.data!.map((entry, index) => <li key={entry.request_id}>
        <button className="text-button" type="button" disabled={pending}
          onClick={() => { setError(""); open.mutate(entry.request_id); }}>
          <ExternalLink size={14} />讨论 {index + 1} · 题目版本 {entry.content_version}
        </button>
      </li>)}</ul>
    </details>}
    {settings && createPortal(<div className="modal-backdrop" onClick={() => { if (!busy) setSettings(false); }}>
      <div ref={modal} className="settings-dialog dsh-connection-dialog" role="dialog" aria-modal="true"
        aria-labelledby="dsh-connect-title" onClick={event => event.stopPropagation()}>
        <header><h2 id="dsh-connect-title">DSH 桌面连接</h2>
          <button className="icon-button" type="button" disabled={busy} aria-label="关闭 DSH 连接"
            onClick={() => setSettings(false)}><X size={18} /></button></header>
        <label className="settings-field"><span>插件连接文件</span>
          <input ref={first} value={path} onChange={event => setPath(event.target.value)}
            placeholder="connection.json" aria-label="插件连接文件" disabled={busy} spellCheck={false} /></label>
        <div className="dsh-question-toolbar">
          <button className="secondary-button" type="button" disabled={busy} onClick={async () => {
            setPicking(true);
            try {
              const directory = await pollPicker(await createPicker("directory"));
              if (directory) setPath(`${directory.replace(/[\\/]$/, "")}\\connection.json`);
            } catch (reason) { setError((reason as Error).message); } finally { setPicking(false); }
          }}><FolderOpen size={16} />选择 classcue-bridge 文件夹</button>
          <button className="primary-button" type="button" disabled={busy || !path.trim()} onClick={() => connect.mutate()}>
            {connect.isPending && <LoaderCircle size={16} className="spin" />}连接</button>
        </div>
        {(error || status.error) && <p role="alert" className="field-error">{error || (status.error as Error).message}</p>}
      </div>
    </div>, document.body)}
  </div>;
}
