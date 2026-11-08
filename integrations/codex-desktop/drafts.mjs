import { DatabaseSync } from "node:sqlite";
import { realpath, lstat } from "node:fs/promises";
import { isAbsolute, join, relative } from "node:path";
import { randomUUID } from "node:crypto";
import { readContext } from "./context.mjs";

const identity = /^[a-zA-Z0-9_-]{1,100}$/;
export function threadIdentity(meta) {
  const id = meta?.threadId ?? meta?.thread_id;
  if (typeof id !== "string" || !identity.test(id)
      || meta?.threadId && meta?.thread_id && meta.threadId !== meta.thread_id)
    throw new Error("请在已绑定的 Codex 对话内打开 ClassCue。");
  return id;
}

export async function openDraftStore(root) {
  if (!root || !isAbsolute(root)) throw new Error("请先配置 ClassCue 学习工作区。");
  const canonical = await realpath(root);
  const path = join(canonical, ".classcue-codex", "drafts.sqlite");
  const resolved = await realpath(path);
  if (relative(canonical, resolved) !== join(".classcue-codex", "drafts.sqlite")
      || (await lstat(path)).isSymbolicLink()) throw new Error("插件绑定目录不可用。");
  return new DraftStore(resolved, canonical);
}

export class DraftStore {
  constructor(path, root) {
    this.root = root;
    this.db = new DatabaseSync(path);
    this.db.exec("PRAGMA busy_timeout=2000");
  }
  close() { this.db.close(); }
  row(threadId) {
    const row = this.db.prepare("SELECT * FROM drafts WHERE thread_id=?").get(threadId);
    if (!row) throw new Error("当前对话没有 ClassCue 题目绑定，请从原题打开讨论。");
    return row;
  }
  async load(threadId) {
    const row = this.row(threadId);
    const context = await readContext(row.context_file, this.root);
    if (context.metadata.request_id !== row.request_id || context.digest !== row.context_sha256)
      throw new Error("原题快照已变化，已停止加入输入框。");
    return { request_id: row.request_id, thread_id: row.thread_id, title: row.title,
      source_kind: context.metadata.source_kind,
      status: row.status, attempt: row.attempt, error: row.error, content: context.content };
  }
  async claim(threadId, requestId, retryAttempt) {
    await this.load(threadId);
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const row = this.row(threadId);
      if (row.request_id !== requestId) throw new Error("题目与当前对话不匹配。");
      const stale = row.status === "inserting"
        && Date.now() - Date.parse(`${row.updated_at.replace(" ", "T")}Z`) > 30000;
      const retry = retryAttempt && retryAttempt === row.attempt
        && (["blocked", "unknown", "attached"].includes(row.status) || stale);
      if (row.status !== "pending" && !retry) {
        this.db.exec("COMMIT");
        return { claimed: false, status: row.status };
      }
      const attempt = randomUUID();
      this.db.prepare("UPDATE drafts SET status='inserting',attempt=?,error=NULL,updated_at=CURRENT_TIMESTAMP WHERE request_id=?")
        .run(attempt, requestId);
      this.db.exec("COMMIT");
      return { claimed: true, attempt, status: "inserting" };
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }
  acknowledge(threadId, requestId, attempt, status) {
    if (!["attached", "blocked", "unknown"].includes(status)) throw new Error("无效的草稿回执。");
    const row = this.row(threadId);
    if (row.request_id !== requestId || row.attempt !== attempt)
      throw new Error("草稿操作已变化，未覆盖新回执。");
    if (row.status !== status && !["inserting", "unknown"].includes(row.status))
      throw new Error("草稿操作已完成。");
    const updated = this.db.prepare(`UPDATE drafts SET status=?,updated_at=CURRENT_TIMESTAMP
      WHERE request_id=? AND attempt=? AND status IN ('inserting','unknown',?)`)
      .run(status, requestId, attempt, status);
    if (updated.changes !== 1) throw new Error("草稿操作已变化，未覆盖新回执。");
    return { status };
  }
}
