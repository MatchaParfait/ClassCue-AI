import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, renameSync, existsSync } from "node:fs";
import { join } from "node:path";

export const PROTOCOL = 1;
export const MAX_FRAME = 32 * 1024 * 1024;
const ID = /^[a-zA-Z0-9_-]{1,100}$/;

export class BridgeError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

export function requireId(value) {
  if (typeof value !== "string" || !ID.test(value) || ["__proto__", "prototype", "constructor"].includes(value))
    throw new BridgeError("invalid_request", "无效记录编号");
  return value;
}

export function digest(value) {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

export function validateContent(content) {
  if (!Array.isArray(content) || content.length === 0 || content.length > 24)
    throw new BridgeError("invalid_content", "题目需要文字或图片，最多 24 个片段");
  let bytes = 0;
  for (const part of content) {
    if (part.type === "text" && typeof part.text === "string" && part.text.trim()) {
      bytes += Buffer.byteLength(part.text);
    } else if (part.type === "image" && ["image/png", "image/jpeg", "image/webp"].includes(part.media_type)
      && typeof part.data === "string" && /^[A-Za-z0-9+/]+={0,2}$/.test(part.data)) {
      const image = Buffer.from(part.data, "base64");
      const valid = part.media_type === "image/png" ? image.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex"))
        : part.media_type === "image/jpeg" ? image[0] === 255 && image[1] === 216 && image[2] === 255
          : image.subarray(0, 4).toString() === "RIFF" && image.subarray(8, 12).toString() === "WEBP";
      if (!valid || image.toString("base64") !== part.data)
        throw new BridgeError("invalid_image", "图片格式或编码不正确");
      bytes += image.length;
    } else throw new BridgeError("invalid_content", "不支持的题目片段");
  }
  if (bytes > 20 * 1024 * 1024) throw new BridgeError("content_too_large", "单题内容不能超过 20 MB");
}

export function atomicJson(path, value) {
  const temp = `${path}.${randomUUID()}.tmp`;
  writeFileSync(temp, JSON.stringify(value), { mode: 0o600, flag: "wx", flush: true });
  renameSync(temp, path);
}

/** One writer per desktop profile; every side effect follows a persisted intent. */
export class Bridge {
  constructor(root, native) {
    mkdirSync(root, { recursive: true });
    this.payloadRoot = join(root, "requests");
    mkdirSync(this.payloadRoot, { recursive: true });
    this.path = join(root, "state.json");
    this.native = native;
    this.state = existsSync(this.path) ? JSON.parse(readFileSync(this.path, "utf8")) : {
      protocol: PROTOCOL, instance_id: randomUUID(), requests: {}, documents: {},
    };
    if (this.state.protocol !== PROTOCOL || !this.state.requests || !this.state.documents)
      throw new BridgeError("state_incompatible", "桥接记录版本不兼容");
    this.tail = Promise.resolve();
    this.leases = new Map();
    this.client = null;
    this.save();
  }

  save() {
    // Images live per request; a receipt/lease update must not rewrite the entire question library.
    for (const row of Object.values(this.state.requests)) {
      if (Object.hasOwn(row, "content")) {
        atomicJson(join(this.payloadRoot, `${requireId(row.request_id)}.question.json`), row.content);
        delete row.content;
      }
      if (Object.hasOwn(row, "draft")) {
        atomicJson(join(this.payloadRoot, `${requireId(row.request_id)}.draft.json`), row.draft);
        row.has_draft = true;
        delete row.draft;
      }
    }
    atomicJson(this.path, this.state);
  }
  content(row) {
    const kind = row.has_draft ? "draft" : "question";
    try {
      return JSON.parse(readFileSync(join(this.payloadRoot, `${requireId(row.request_id)}.${kind}.json`), "utf8"));
    } catch {
      throw new BridgeError("snapshot_missing", "题目快照缺失或损坏，未创建替代对话");
    }
  }
  serial(operation) {
    const result = this.tail.then(operation);
    this.tail = result.catch(() => {});
    return result;
  }
  receipt(row) {
    return {
      request_id: row.request_id, library_id: row.library_id, document_id: row.document_id,
      question_id: row.question_id, content_version: row.content_version,
      instance_id: this.state.instance_id, workspace_id: row.workspace_id ?? null,
      cwd: row.cwd ?? null, session_id: row.session_id, status: row.status,
      error: row.error ?? null, outcome: row.outcome ?? null,
    };
  }
  status() {
    return { protocol: PROTOCOL, desktop_version: "0.2.1-rc.3",
      instance_id: this.state.instance_id, client_connected: this.client !== null && Date.now() - this.client.time < 5000,
      capabilities: ["question_draft", "multiple_images", "open_session", "workspace_memory"] };
  }
  workspaces() { return this.native.workspaces(); }
  get(requestId) {
    const row = this.state.requests[requireId(requestId)];
    if (!row) throw new BridgeError("request_missing", "传输记录不存在");
    return row;
  }
  request(requestId) { return this.receipt(this.get(requestId)); }

  prepare(input) {
    return this.serial(async () => {
      const { request_id, library_id, document_id, question_id, content_version } = input;
      for (const id of [request_id, library_id, document_id, question_id]) requireId(id);
      if (!Number.isInteger(content_version) || content_version < 1)
        throw new BridgeError("invalid_version", "题目版本无效");
      validateContent(input.content);
      const key = `${library_id}/${question_id}`;
      const snapshotHash = digest(input.content);
      let row = this.state.requests[request_id];
      if (row) {
        if (row.library_id !== library_id || row.question_id !== question_id
          || row.document_id !== document_id || row.content_version !== content_version || row.snapshot_hash !== snapshotHash)
          throw new BridgeError("request_conflict", "相同请求编号不能传入不同内容");
      } else {
        const previous = Object.values(this.state.requests).filter(r => `${r.library_id}/${r.question_id}` === key);
        if (previous.length && !input.new_discussion) {
          row = previous.findLast(r => r.content_version === content_version);
          if (!row) throw new BridgeError("question_changed", "题目已修改，请明确新建讨论；旧版本对话仍保留");
          if (row.snapshot_hash !== snapshotHash) throw new BridgeError("version_conflict", "题目内容已变化但版本未更新");
        } else {
          const docKey = `${library_id}/${document_id}`;
          const workspaceId = input.workspace_id ?? this.state.documents[docKey];
          row = { request_id, library_id, document_id, question_id, content_version,
            title: String(input.title ?? "").slice(0, 200),
            source: String(input.source ?? "").slice(0, 1000),
            content: input.content, snapshot_hash: snapshotHash,
            session_id: `session-${randomUUID()}`, status: workspaceId ? "reserved" : "choose_workspace",
            workspace_id: workspaceId, command: randomUUID(), action: "prepare",
          };
          this.state.requests[request_id] = row;
          this.save();
        }
      }
      if (row.workspace_id) await this.ensureBound(row);
      row.action = "prepare";
      row.command = randomUUID();
      row.error = null;
      row.outcome = null;
      this.save();
      return this.receipt(row);
    });
  }

  async ensureBound(row) {
    const workspace = this.workspaces().find(w => w.workspace_id === row.workspace_id);
    if (!workspace) throw new BridgeError("workspace_missing", "原工作区已移除，请先在 DSH 中恢复");
    // Only an unbound reserved request may create. A missing bound session is never recreated.
    if (row.status !== "reserved") {
      await this.native.check(row.session_id, row.workspace_id);
      return;
    }
    row.cwd = workspace.path;
    this.save();
    await this.native.ensureSession(row.session_id, row.workspace_id);
    await this.native.flush(row.session_id);
    row.status = "bound";
    this.state.documents[`${row.library_id}/${row.document_id}`] = row.workspace_id;
    this.save();
  }

  choose(requestId, workspaceId) {
    return this.serial(async () => {
      const row = this.get(requestId);
      if (row.status === "choose_workspace") {
        if (!this.workspaces().some(w => w.workspace_id === workspaceId))
          throw new BridgeError("workspace_missing", "工作区已移除，请重新选择");
        row.workspace_id = requireId(workspaceId);
        row.status = "reserved";
        this.save();
      }
      await this.ensureBound(row);
      row.command = randomUUID();
      this.save();
      return this.receipt(row);
    });
  }

  open(requestId) {
    return this.serial(async () => {
      const row = this.get(requestId);
      if (row.workspace_id) await this.ensureBound(row);
      row.action = "prepare";
      row.command = randomUUID();
      row.error = null;
      row.outcome = null;
      this.save();
      return this.receipt(row);
    });
  }

  next(clientId) {
    requireId(clientId);
    this.client = { id: clientId, time: Date.now() };
    for (const row of Object.values(this.state.requests)) {
      if (!row.action || row.status === "reserved") continue;
      const lease = this.leases.get(row.command);
      if (lease && lease.client !== clientId && Date.now() < lease.until) continue;
      this.leases.set(row.command, { client: clientId, until: Date.now() + 30000 });
      return { ...this.receipt(row), command: row.command, action: row.action,
        title: row.title, source: row.source,
        content: ["sent", "choose_workspace"].includes(row.status) ? [] : this.content(row),
        workspaces: row.status === "choose_workspace" ? this.workspaces() : [],
      };
    }
    return null;
  }

  acknowledge(clientId, requestId, command, result) {
    return this.serial(async () => {
      const row = this.get(requestId);
      if (row.command !== command || this.leases.get(command)?.client !== clientId)
        throw new BridgeError("stale_command", "传输指令已过期");
      if (!["draft_ready", "opened", "draft_conflict", "cancelled", "error"].includes(result))
        throw new BridgeError("invalid_result", "不支持的草稿结果");
      if (row.status !== "sent" && result === "draft_ready") row.status = "draft_ready";
      row.error = ["error", "draft_conflict"].includes(result) ? result : null;
      row.outcome = result;
      delete row.action;
      this.leases.delete(command);
      this.save();
      return this.receipt(row);
    });
  }

  backup(requestId, content) {
    return this.serial(async () => {
      const row = this.get(requestId);
      if (row.status === "sent") return;
      if (!Array.isArray(content)) throw new BridgeError("invalid_content", "草稿格式无效");
      if (content.length) validateContent(content);
      row.draft = content;
      this.save();
    });
  }

  sent(sessionId) {
    return this.serial(async () => {
      let changed = false;
      for (const row of Object.values(this.state.requests)) {
        if (row.session_id !== sessionId || row.status === "sent") continue;
        row.status = "sent";
        delete row.draft;
        row.has_draft = false;
        changed = true;
      }
      if (changed) this.save();
    });
  }
}
