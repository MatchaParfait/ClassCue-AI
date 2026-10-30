import { createHash, randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, renameSync, existsSync, realpathSync,
  statSync, lstatSync, openSync, closeSync, unlinkSync } from "node:fs";
import { join, basename, dirname, extname, isAbsolute, parse } from "node:path";

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

function folderName(value, fallback) {
  let name = String(value ?? "").normalize("NFC").replace(/[<>:"/\\|?*\x00-\x1f]/g, "_")
    .trim().replace(/[. ]+$/, "").slice(0, 64).replace(/[. ]+$/, "");
  if (!name || /^\.+$/.test(name)) name = fallback;
  if (/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(name)) name = `_${name}`;
  return name;
}

function directory(path) {
  try {
    if (!isAbsolute(path) || (process.platform === "win32" && /^[\\/]+$/.test(parse(path).root)))
      throw new Error();
    const canonical = realpathSync(path);
    if (!statSync(canonical).isDirectory()) throw new Error();
    return canonical;
  } catch {
    throw new BridgeError("directory_unavailable", "工作区目录不存在或无法访问，请恢复原目录后重试");
  }
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
    this.state.courses ??= {};
    this.state.roots ??= {};
    this.state.tree_courses ??= {};
    this.state.tree_documents ??= {};
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
      course_id: row.course_id ?? null, course_name: row.course_name ?? null,
      question_id: row.question_id, content_version: row.content_version,
      instance_id: this.state.instance_id, workspace_id: row.workspace_id ?? null,
      layout: row.layout ?? "legacy", root_id: row.root_id ?? null,
      cwd: row.cwd ?? null, session_id: row.session_id, status: row.status,
      error: row.error ?? null, outcome: row.outcome ?? null,
    };
  }
  status() {
    return { protocol: PROTOCOL, desktop_version: this.native.desktopVersion ?? "0.2.1-rc.5",
      instance_id: this.state.instance_id, client_connected: this.client !== null && Date.now() - this.client.time < 5000,
      capabilities: ["question_draft", "multiple_images", "open_session", "workspace_memory", "course_workspace", "workspace_tree"] };
  }
  workspaces() { return this.native.workspaces(); }
  get(requestId) {
    const row = this.state.requests[requireId(requestId)];
    if (!row) throw new BridgeError("request_missing", "传输记录不存在");
    return row;
  }
  request(requestId) { return this.receipt(this.get(requestId)); }

  configureRoot(input) {
    return this.serial(async () => {
      const { library_id, config_id, path } = input;
      requireId(library_id);
      requireId(config_id);
      if (typeof path !== "string" || !path.trim() || path.length > 4096)
        throw new BridgeError("invalid_root", "请选择默认 DSH 工作区目录");
      const canonical = directory(path);
      let root = this.state.roots[config_id];
      if (root && (root.library_id !== library_id || root.path !== canonical))
        throw new BridgeError("root_conflict", "相同目录配置编号不能用于不同位置");
      if (!root) {
        // Test actual write access before registering a user-selected directory.
        const probe = join(canonical, `.classcue-${randomUUID()}.tmp`);
        try { closeSync(openSync(probe, "wx")); unlinkSync(probe); }
        catch { throw new BridgeError("root_not_writable", "所选目录不可写，请选择有写入权限的文件夹"); }
        root = { config_id, library_id, path: canonical };
        this.state.roots[config_id] = root;
        this.save();
      }
      await this.registerNode(root);
      return { config_id, instance_id: this.state.instance_id, path: root.path, workspace_id: root.workspace_id };
    });
  }

  async registerNode(node) {
    if (node.workspace_id) {
      const workspace = this.workspaces().find(w => w.workspace_id === node.workspace_id);
      if (!workspace) throw new BridgeError("workspace_missing", "原工作区节点已移除，请先在 DSH 中恢复");
      if (directory(node.path) !== workspace.path)
        throw new BridgeError("workspace_path_changed", "原工作区位置已变化，绑定已保留，请先恢复目录");
      return;
    }
    const workspace = await this.native.createWorkspace(directory(node.path), node.title);
    node.workspace_id = requireId(workspace.workspace_id);
    node.path = workspace.path;
    this.save();
  }

  async childNode(table, key, parent, name) {
    let node = table[key];
    if (!node) {
      const stem = folderName(name, "未命名资料");
      let path = join(parent.path, stem);
      let suffix = 1;
      const claimed = [...Object.values(this.state.tree_courses), ...Object.values(this.state.tree_documents)];
      while (existsSync(path) || claimed.some(n => n.path.toLowerCase() === path.toLowerCase()))
        path = join(parent.path, `${stem} (${suffix++})`);
      node = { path, title: basename(path), root_id: parent.root_id ?? parent.config_id };
      table[key] = node;
      this.save();
    }
    if (!node.workspace_id) {
      try { mkdirSync(node.path); }
      catch (error) {
        if (error.code !== "EEXIST")
          throw new BridgeError("directory_create_failed", "无法创建课程或资料目录，请检查权限后重试");
      }
    }
    const canonical = directory(node.path);
    if (lstatSync(node.path).isSymbolicLink() || dirname(canonical) !== directory(parent.path))
      throw new BridgeError("directory_path_changed", "受管理目录已被移动或替换，请先恢复原位置");
    await this.registerNode(node);
    return node;
  }

  async treeWorkspace(row) {
    const documentKey = `${row.library_id}/${row.course_id}/${row.document_id}`;
    const saved = this.state.tree_documents[documentKey];
    const root = this.state.roots[saved?.root_id ?? row.root_id];
    if (!root || root.library_id !== row.library_id)
      throw new BridgeError("root_missing", "默认工作区配置不存在，请在 App 的 DSH 连接设置中重新选择");
    await this.registerNode(root);
    const courseKey = `${row.library_id}/${root.config_id}/${row.course_id}`;
    const course = await this.childNode(this.state.tree_courses, courseKey, root, row.course_name);
    const extension = extname(row.source);
    const label = extension ? row.source.slice(0, -extension.length) : row.source;
    const document = await this.childNode(this.state.tree_documents, documentKey, course, label);
    row.root_id = document.root_id;
    row.workspace_id = document.workspace_id;
    row.cwd = document.path;
    this.save();
  }

  prepare(input) {
    return this.serial(async () => {
      const { request_id, library_id, document_id, question_id, content_version, course_id } = input;
      for (const id of [request_id, library_id, document_id, question_id]) requireId(id);
      if (course_id !== undefined) requireId(course_id);
      if (!Number.isInteger(content_version) || content_version < 1)
        throw new BridgeError("invalid_version", "题目版本无效");
      validateContent(input.content);
      const key = `${library_id}/${question_id}`;
      const snapshotHash = digest(input.content);
      let row = this.state.requests[request_id];
      if (row) {
        if (row.library_id !== library_id || row.question_id !== question_id
          || row.document_id !== document_id || row.course_id !== course_id
          || row.layout !== input.layout || row.requested_root_id !== input.root_id
          || row.content_version !== content_version || row.snapshot_hash !== snapshotHash)
          throw new BridgeError("request_conflict", "相同请求编号不能传入不同内容");
      } else {
        const previous = Object.values(this.state.requests).filter(r => `${r.library_id}/${r.question_id}` === key);
        if (previous.length && !input.new_discussion) {
          row = previous.findLast(r => r.content_version === content_version);
          if (!row) throw new BridgeError("question_changed", "题目已修改，请明确新建讨论；旧版本对话仍保留");
          if (row.snapshot_hash !== snapshotHash) throw new BridgeError("version_conflict", "题目内容已变化但版本未更新");
        } else {
          if (!course_id) throw new BridgeError("course_required", "请先将题目归入课程资料夹");
          const tree = input.layout === "workspace_tree";
          if (input.layout && !tree)
            throw new BridgeError("layout_unsupported", "不支持的工作区组织方式");
          if (tree) {
            requireId(input.root_id);
            if (input.workspace_id) throw new BridgeError("invalid_request", "自动目录不能指定旧课程工作区");
          }
          const courseKey = `${library_id}/${course_id}`;
          const workspaceId = tree ? undefined : this.state.courses[courseKey] ?? input.workspace_id;
          if (workspaceId && Object.entries(this.state.courses).some(([other, workspace]) =>
            other.startsWith(`${library_id}/`) && other !== courseKey && workspace === workspaceId))
            throw new BridgeError("workspace_in_use", "此工作区已属于另一门课程，请选择本课程的独立文件夹");
          row = { request_id, library_id, document_id, question_id, content_version, course_id,
            course_name: String(input.course_name ?? "").slice(0, 240),
            title: String(input.title ?? "").slice(0, 200),
            source: String(input.source ?? "").slice(0, 1000),
            content: input.content, snapshot_hash: snapshotHash,
            ...(tree ? { layout: "workspace_tree", root_id: input.root_id, requested_root_id: input.root_id } : {}),
            session_id: `session-${randomUUID()}`, status: tree || workspaceId ? "reserved" : "choose_workspace",
            workspace_id: workspaceId, command: randomUUID(), action: "prepare",
          };
          this.state.requests[request_id] = row;
          this.save();
        }
      }
      if (row.workspace_id || row.layout === "workspace_tree") await this.ensureBound(row);
      row.action = "prepare";
      row.command = randomUUID();
      row.error = null;
      row.outcome = null;
      this.save();
      return this.receipt(row);
    });
  }

  async ensureBound(row) {
    if (row.layout === "workspace_tree") {
      await this.treeWorkspace(row);
      if (row.status !== "reserved") {
        await this.native.check(row.session_id, row.workspace_id);
        return;
      }
      await this.native.ensureSession(row.session_id, row.workspace_id, row.title);
      await this.native.flush(row.session_id);
      row.status = "bound";
      this.save();
      return;
    }
    const workspace = this.workspaces().find(w => w.workspace_id === row.workspace_id);
    if (!workspace) throw new BridgeError("workspace_missing", "原工作区已移除，请先在 DSH 中恢复");
    // Only an unbound reserved request may create. A missing bound session is never recreated.
    if (row.status !== "reserved") {
      await this.native.check(row.session_id, row.workspace_id);
      return;
    }
    const key = row.course_id ? `${row.library_id}/${row.course_id}` : null;
    const remembered = key ? this.state.courses[key] : null;
    if (remembered && remembered !== row.workspace_id)
      throw new BridgeError("course_workspace_conflict", "本课程已绑定另一工作区，请重新打开讨论");
    if (key && Object.entries(this.state.courses).some(([other, workspace]) =>
      other.startsWith(`${row.library_id}/`) && other !== key && workspace === row.workspace_id))
      throw new BridgeError("workspace_in_use", "此工作区已属于另一门课程，请选择本课程的独立文件夹");
    row.cwd = workspace.path;
    if (key) this.state.courses[key] = row.workspace_id;
    this.save();
    await this.native.ensureSession(row.session_id, row.workspace_id);
    await this.native.flush(row.session_id);
    row.status = "bound";
    if (key) this.state.courses[key] = row.workspace_id;
    else this.state.documents[`${row.library_id}/${row.document_id}`] = row.workspace_id;
    this.save();
  }

  choose(requestId, workspaceId) {
    return this.serial(async () => {
      const row = this.get(requestId);
      if (row.status === "choose_workspace") {
        const key = row.course_id ? `${row.library_id}/${row.course_id}` : null;
        if (key) workspaceId = this.state.courses[key] ?? workspaceId;
        if (!this.workspaces().some(w => w.workspace_id === workspaceId))
          throw new BridgeError("workspace_missing", "工作区已移除，请重新选择");
        if (key && Object.entries(this.state.courses).some(([other, workspace]) =>
          other.startsWith(`${row.library_id}/`) && other !== key && workspace === workspaceId))
          throw new BridgeError("workspace_in_use", "此工作区已属于另一门课程，请选择本课程的独立文件夹");
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
      if (row.workspace_id || row.layout === "workspace_tree") await this.ensureBound(row);
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
