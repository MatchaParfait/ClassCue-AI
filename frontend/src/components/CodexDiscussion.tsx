import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardCopy, ExternalLink, FolderOpen, LoaderCircle, MessageSquare, Plus, Settings2 } from "lucide-react";
import { Link } from "react-router-dom";
import { createPicker, pollPicker } from "../api/client";
import { attachCodexDiscussion, copyCodexImages, getCodexDiscussion, getCodexDiscussions, getCodexSettings, openCodexDiscussion, prepareCodexDiscussion,
  setCodexSettings } from "../api/codexBridge";
import { useDesktopAPI } from "../hooks/useDesktopAPI";
import { setDiscussionProvider, useDiscussionProvider } from "../hooks/useDiscussionProvider";
import "./DshQuestionActions.css";

export function CodexSettingsSection({ active = true, scope = "default" }: { active?: boolean; scope?: string }) {
  const desktop = useDesktopAPI();
  if (!desktop) return null;
  return <section className="settings-section" aria-labelledby="codex-settings-title">
    <div className="settings-section-heading"><div className="settings-section-title">
      <MessageSquare size={20} /><h2 id="codex-settings-title">Codex 讨论</h2>
    </div></div>
    <CodexSettingsForm active={active} scope={scope} />
  </section>;
}

function CodexSettingsForm({ active, scope }: { active: boolean; scope: string }) {
  const cache = useQueryClient();
  const provider = useDiscussionProvider();
  const [error, setError] = useState("");
  const [picking, setPicking] = useState(false);
  const [executable, setExecutable] = useState("");
  const settings = useQuery({ queryKey: ["codex-settings", scope], queryFn: getCodexSettings, enabled: active });
  const save = useMutation({
    mutationFn: (root: string) => setCodexSettings({ root, ...(executable.trim() ? { executable: executable.trim() } : {}) }),
    onSuccess: value => {
      cache.setQueryData(["codex-settings", scope], value);
      void cache.invalidateQueries({ queryKey: ["codex-settings"] });
      setDiscussionProvider("codex");
    },
    onError: reason => setError(reason.message),
  });
  const busy = picking || save.isPending;
  return <div className="dsh-connection-settings">
    <label className="settings-field"><span>默认讨论工具</span>
      <select aria-label="默认讨论工具" value={provider} disabled={busy || !active}
        onChange={event => setDiscussionProvider(event.target.value as "dsh" | "codex")}>
        <option value="codex">Codex</option><option value="dsh">DSH</option>
      </select>
    </label>
    <div className="dsh-root-setting">
      <span className="dsh-root-label">默认工作区</span>
      <output className="dsh-root-path" aria-label="Codex 默认工作区">
        {settings.isLoading ? "正在读取…" : settings.error ? "读取失败" : settings.data?.root || "尚未设置"}
      </output>
      <button type="button" className="secondary-button" disabled={busy || !active}
        onClick={async () => {
          setPicking(true); setError("");
          try {
            const directory = await pollPicker(await createPicker("directory"));
            if (directory) await save.mutateAsync(directory);
          } catch (reason) { setError((reason as Error).message); }
          finally { setPicking(false); }
        }}>
        {busy ? <LoaderCircle size={16} className="spin" /> : <FolderOpen size={16} />}
        {settings.data ? "更改默认工作区" : "选择默认工作区"}
      </button>
    </div>
    <details><summary>Codex CLI 路径</summary>
      <label className="settings-field"><span>codex.exe</span>
        <input aria-label="Codex CLI 路径" value={executable}
          onChange={event => setExecutable(event.target.value)} disabled={busy || !active}
          placeholder={settings.data?.executable || "自动检测"} spellCheck={false} />
      </label>
    </details>
    {save.isSuccess && !error && <p role="status">Codex 工作区已保存。</p>}
    {(error || settings.error) && <p className="field-error" role="alert">{error || settings.error?.message}</p>}
  </div>;
}

export function CodexQuestionActions({ questionId, scope }: { questionId: string; scope: string }) {
  const cache = useQueryClient();
  const historyKey = ["codex-discussions", scope, questionId];
  const history = useQuery({ queryKey: historyKey, queryFn: () => getCodexDiscussions(questionId), retry: false });
  const prepare = useMutation({
    mutationFn: (newDiscussion: boolean) => prepareCodexDiscussion(questionId, newDiscussion),
    onSettled: () => { void cache.invalidateQueries({ queryKey: historyKey }); },
  });
  const open = useMutation({ mutationFn: openCodexDiscussion });
  const copy = useMutation({ mutationFn: copyCodexImages });
  const attach = useMutation({ mutationFn: attachCodexDiscussion });
  const busy = prepare.isPending || open.isPending || copy.isPending || attach.isPending;
  const result = attach.data || prepare.data || copy.data || open.data;
  const reset = () => { prepare.reset(); open.reset(); copy.reset(); attach.reset(); };
  const transfer = result?.image_transfer;
  const native = result?.native_transfer;
  const receipt = useQuery({
    queryKey: ["codex-draft", scope, result?.request_id],
    queryFn: () => getCodexDiscussion(result!.request_id),
    enabled: Boolean(result?.draft_transfer) && !transfer && !native,
    refetchInterval: query => ["pending", "inserting"].includes(query.state.data?.draft_transfer?.status || "pending")
      ? 2000 : false,
    retry: false,
  });
  const draftStatus = receipt.data?.draft_transfer?.status || result?.draft_transfer?.status;
  return <div className="dsh-question-actions">
    <div className="dsh-question-toolbar">
      <button className="secondary-button compact" type="button" disabled={busy || history.isPending}
        onClick={() => { reset(); prepare.mutate(false); }}>
        {busy ? <LoaderCircle size={16} className="spin" /> : <MessageSquare size={16} />}在 Codex 中讨论
      </button>
      <button className="icon-button" type="button" title="新建独立讨论" aria-label="新建独立讨论" disabled={busy}
        onClick={() => { reset(); prepare.mutate(true); }}><Plus size={16} /></button>
      <Link className="icon-button" title="Codex 讨论设置" aria-label="Codex 讨论设置"
        to="/settings?section=codex"><Settings2 size={16} /></Link>
    </div>
    {(prepare.error || open.error || copy.error || attach.error || history.error) && <p role="alert" className="field-error">
      {(prepare.error || open.error || copy.error || attach.error || history.error)?.message}
    </p>}
    {result && <p role="status">{transfer?.status === "paste_required"
      ? `已复制 ${transfer.image_count} 张题图。在 Codex 输入框按 Ctrl+V 附图，等待你发送。`
      : transfer?.status === "no_images" ? "已打开 Codex，此题没有题图。"
        : native?.status === "pasted" ? "已向 Codex 原生输入框粘贴过原题，请检查后发送。"
          : native?.status === "attached" ? "原题已加入过 Codex 原生输入框，请在 Codex 中查看。"
          : native ? native.error || "附图结果尚未确认，请先检查 Codex 输入框。"
        : draftStatus === "attached" ? "插件已确认加入过原题内容，请在 Codex 中查看。"
          : draftStatus === "unknown" || draftStatus === "inserting" ? "草稿回执尚未确认，请先检查 Codex 输入框。"
            : draftStatus === "blocked" ? "Codex 未接受草稿，请检查后重新附上原题。"
              : draftStatus === "pending" ? "原讨论已准备，请重新点击讨论以完成首次附图。"
                : "已打开原 Codex 讨论。"}</p>}
    {result && native && <button type="button" className="text-button" disabled={busy}
      onClick={() => attach.mutate(result.request_id)}>重新附上原题</button>}
    {result && native && <p>重新附上会追加一次原题；请先检查输入框中是否已有原题。</p>}
    {result?.image_count === 0 && <p>此题为纯文字题，没有图片附件。</p>}
    {receipt.error && <p className="field-error" role="alert">草稿状态读取失败，请在 Codex 中检查。</p>}
    {Boolean(history.data?.length) && <details><summary>讨论记录（{history.data!.length}）</summary>
      <ul>{history.data!.map((entry, index) => <li key={entry.request_id}>
        <button className="text-button" type="button" disabled={busy} onClick={() => {
          reset(); open.mutate(entry.request_id);
        }}><ExternalLink size={14} />讨论 {index + 1} · 题目版本 {entry.content_version}</button>
        <button className="icon-button" type="button" disabled={busy}
          title={`复制讨论 ${index + 1} 的题图并打开 Codex`}
          aria-label={`复制讨论 ${index + 1} 的题图并打开 Codex`}
          onClick={() => { reset(); copy.mutate(entry.request_id); }}><ClipboardCopy size={14} /></button>
      </li>)}</ul>
    </details>}
  </div>;
}
