import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, LoaderCircle, MessageSquare, Plus, Settings2, X } from "lucide-react";
import { getDshDiscussions, getDshRequest, getDshStatus, openDshDiscussion, prepareDshQuestion } from "../api/dshBridge";
import { DshConnectionSettings } from "./DshConnectionSettings";
import { useDesktopAPI } from "../hooks/useDesktopAPI";
import { useModalFocus } from "../hooks/useModalFocus";
import { getCourseVideoQuestion } from "../api/studyMaterials";
import "./DshQuestionActions.css";
import { CodexQuestionActions } from "./CodexDiscussion";
import { useDiscussionProvider } from "../hooks/useDiscussionProvider";

export function VideoQuestionDiscussion({ markerId, captureId }: { markerId: number; captureId: string }) {
  const desktop = useDesktopAPI();
  const question = useQuery({ queryKey: ["course-video-question", markerId, captureId],
    queryFn: () => getCourseVideoQuestion(markerId), enabled: Boolean(desktop), retry: false, staleTime: 0 });
  if (!desktop) return null;
  return question.data?.id ? <DshQuestionActions key={`${question.data.id}:${question.data.version}`}
    questionId={question.data.id} contentVersion={question.data.version} />
    : question.error ? <p className="field-error" role="alert">{question.error.message}</p> : null;
}

export function DshQuestionActions({ questionId, contentVersion, scope = "default", active = true }: {
  questionId: string; contentVersion: number; scope?: string; active?: boolean;
}) {
  const desktop = useDesktopAPI();
  const provider = useDiscussionProvider();
  if (!desktop || !active) return null;
  if (provider === "codex") return <CodexQuestionActions key={`${scope}:${questionId}:${contentVersion}`}
    questionId={questionId} scope={scope} />;
  return <DesktopQuestionActions key={`${scope}:${questionId}:${contentVersion}`}
    questionId={questionId} scope={scope} />;
}

function DesktopQuestionActions({ questionId, scope }: {
  questionId: string; scope: string;
}) {
  const cache = useQueryClient();
  const [settings, setSettings] = useState(false);
  const [error, setError] = useState("");
  const [requestId, setRequestId] = useState<string | null>(null);
  const [configBusy, setConfigBusy] = useState(false);
  const modal = useRef<HTMLDivElement>(null);
  const first = useRef<HTMLButtonElement>(null);
  const historyKey = ["dsh-discussions", scope, questionId];
  const statusKey = ["dsh-status", scope];
  const requestKey = (id: string | null) => ["dsh-request", scope, id];
  const status = useQuery({ queryKey: statusKey, queryFn: getDshStatus, retry: false });
  const history = useQuery({ queryKey: historyKey, queryFn: () => getDshDiscussions(questionId), retry: false,
    refetchInterval: () => document.visibilityState === "visible" ? 5000 : false });
  const receipt = useQuery({
    queryKey: requestKey(requestId), queryFn: () => getDshRequest(requestId!), enabled: Boolean(requestId),
    retry: false, refetchInterval: query => {
      const value = query.state.data;
      return query.state.error || value?.error || value?.outcome === "cancelled"
        || value && ["sent", "linked", "draft_ready"].includes(value.status) ? false : 1000;
    },
  });
  const start = useMutation({
    mutationFn: (newDiscussion: boolean) => prepareDshQuestion(questionId, newDiscussion),
    onSuccess: value => {
      setRequestId(value.request_id);
      cache.setQueryData(requestKey(value.request_id), value);
      void cache.invalidateQueries({ queryKey: requestKey(value.request_id) });
      void cache.invalidateQueries({ queryKey: historyKey });
    },
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
    onError: reason => {
      setError((reason as Error).message);
      void cache.invalidateQueries({ queryKey: historyKey });
    },
  });
  const pending = start.isPending || open.isPending;
  const busy = pending || configBusy;
  useModalFocus(settings, modal, first, () => { if (!busy) setSettings(false); });
  const result = receipt.data;
  const notice = result?.outcome === "cancelled" ? "已取消工作区选择，可以再次打开讨论。"
    : result?.error === "draft_conflict" ? "DSH 中已有草稿，已保留。请在原对话中查看。"
    : result?.error ? "无法准备 DSH 草稿，请检查桌面连接后重试。"
    : result?.status === "choose_workspace" ? "这是旧版未完成的讨论，请在 DSH 完成原工作区选择；新建独立讨论会使用 ClassCue 的默认目录。"
    : result?.status === "draft_ready" ? "题目已放入 DSH 草稿，等待你发送。"
    : result?.status === "linked" ? "已打开 DSH 分支讨论。"
    : result?.status === "sent" ? "已打开原讨论。"
    : result ? "正在等待 DSH 准备对话…" : "";
  return <div className="dsh-question-actions">
    <div className="dsh-question-toolbar">
      <button className="secondary-button compact" type="button" disabled={pending || status.isPending || history.isPending}
        onClick={() => {
          setError("");
          start.mutate(false);
        }}>
        {pending ? <LoaderCircle className="spin" size={16} /> : <MessageSquare size={16} />}
        在 DSH 中讨论
      </button>
      <button className="icon-button" type="button" title="新建独立讨论" aria-label="新建独立讨论"
        disabled={pending || status.isPending || history.isPending} onClick={() => { setError(""); start.mutate(true); }}>
        <Plus size={16} />
      </button>
      <button className="icon-button" type="button" title="DSH 桌面连接" aria-label="DSH 桌面连接"
        onClick={() => { setError(""); setSettings(true); }}><Settings2 size={16} /></button>
    </div>
    {notice && <p role="status">{notice}</p>}
    {(error || receipt.error || history.error) && <p className="field-error" role="alert">
      {error || (receipt.error as Error | null)?.message || (history.error as Error).message}
    </p>}
    {Boolean(history.data?.length) && <details><summary>讨论记录（{history.data!.length}）</summary>
      <ul>{history.data!.map((entry, index) => {
        const parent = history.data!.findIndex(d => d.request_id === entry.parent_request_id);
        return <li key={entry.request_id}>
        <button className="text-button" type="button" disabled={pending}
          onClick={() => { setError(""); open.mutate(entry.request_id); }}>
          <ExternalLink size={14} />讨论 {index + 1}
          {entry.origin === "fork" && (parent >= 0 ? ` · 分支自讨论 ${parent + 1}` : " · DSH 分支")}
          {` · 题目版本 ${entry.content_version}`}
        </button>
      </li>;
      })}</ul>
    </details>}
    {settings && createPortal(<div className="modal-backdrop" onClick={() => { if (!busy) setSettings(false); }}>
      <div ref={modal} className="settings-dialog dsh-connection-dialog" role="dialog" aria-modal="true"
        aria-labelledby="dsh-connect-title" onClick={event => event.stopPropagation()}>
        <header><h2 id="dsh-connect-title">DSH 桌面连接</h2>
          <button className="icon-button" type="button" disabled={busy} aria-label="关闭 DSH 连接"
            onClick={() => setSettings(false)}><X size={18} /></button></header>
        <DshConnectionSettings scope={scope} initialRef={first} onBusyChange={setConfigBusy} />
      </div>
    </div>, document.body)}
  </div>;
}
