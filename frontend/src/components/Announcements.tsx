import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Bell, CheckCheck, ChevronDown, Download, ExternalLink, RefreshCw, X } from "lucide-react";
import { safeExternal, updateRequest, updatesKey, useUpdates } from "../api/updates";
import { useModalFocus } from "../hooks/useModalFocus";
import "../updates.css";

export function UpdateNotice({ active = true }: { active?: boolean }) {
  const query = useUpdates(active);
  return query.data?.release ? <span className="update-available" role="status" title={`新版本 v${query.data.release.version}`}><Download size={13} aria-hidden="true" />更新</span> : null;
}

export function Announcements({ active = true }: { active?: boolean }) {
  const query = useUpdates(active);
  const client = useQueryClient();
  const [open, setOpen] = useState(false), [expanded, setExpanded] = useState<string | null>(null);
  const dialog = useRef<HTMLDivElement>(null), closeButton = useRef<HTMLButtonElement>(null);
  const items = query.data?.announcements ?? [];
  const unread = items.filter(item => !item.read);
  const notify = query.data?.preferences?.show_announcements !== false;
  const read = useMutation({
    mutationFn: (ids: string[]) => updateRequest("/read", { ids }),
    onSuccess: value => client.setQueryData(updatesKey, value),
  });
  const check = useMutation({
    mutationFn: () => updateRequest("/check", {}),
    onSuccess: value => client.setQueryData(updatesKey, value),
  });
  useModalFocus(open && active, dialog, closeButton, () => setOpen(false));
  return <>
    <button type="button" className={`announcement-button${notify && unread.some(item => item.level === "important") ? " has-important" : ""}`}
      onClick={() => setOpen(true)} title="公告" aria-haspopup="dialog" aria-expanded={open}>
      <Bell size={18} aria-hidden="true" /><span>公告</span>
      {notify && unread.length > 0 && <span className="announcement-count" aria-label={`${unread.length} 条未读`}>{unread.length}</span>}
    </button>
    {open && active && createPortal(<div className="modal-backdrop" onClick={() => setOpen(false)}>
      <div className="announcements-dialog" role="dialog" aria-modal="true" aria-labelledby="announcements-title"
        tabIndex={-1} ref={dialog} onClick={event => event.stopPropagation()}>
        <header><h2 id="announcements-title">项目公告</h2><button ref={closeButton} className="icon-button" title="关闭公告" aria-label="关闭公告" onClick={() => setOpen(false)}><X size={19} /></button></header>
        <div className="announcement-toolbar">
          <button className="secondary-button" disabled={check.isPending || query.data?.checking} onClick={() => check.mutate()}>
            <RefreshCw size={15} className={query.data?.checking ? "spin" : ""} />{query.data?.checking ? "正在检查" : "检查公告"}</button>
          <button className="secondary-button" disabled={!unread.length || read.isPending} onClick={() => read.mutate(unread.map(item => item.id))}><CheckCheck size={16} />全部已读</button>
        </div>
        {(query.isError || query.data?.error || read.error || check.error) && <p role="alert" className="field-error">{read.error?.message || check.error?.message || query.data?.error || "公告读取失败，请重试。"}</p>}
        {!items.length && <p className="announcement-empty">{query.isLoading ? "正在读取公告…" : "暂无有效公告"}</p>}
        <div className="announcement-list">{items.map(item => <article key={item.id} className={`announcement-item${item.read ? "" : " unread"}`}>
          <button className="announcement-heading" aria-expanded={expanded === item.id} onClick={() => {
            setExpanded(expanded === item.id ? null : item.id);
            if (!item.read) read.mutate([item.id]);
          }}>
            <span>{item.level === "important" && <b className="announcement-important">重要</b>}<strong>{item.title}</strong><time dateTime={item.published_at}>{new Date(item.published_at).toLocaleDateString()}</time></span>
            <ChevronDown size={17} />
          </button>
          {expanded === item.id && <div className="announcement-body"><p>{item.body}</p>
            {safeExternal(item.url) && <a href={safeExternal(item.url)} target="_blank" rel="noopener noreferrer">查看详情 <ExternalLink size={14} /></a>}
          </div>}
        </article>)}</div>
      </div>
    </div>, document.body)}
  </>;
}
