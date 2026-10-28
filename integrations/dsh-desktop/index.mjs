import { createRequire } from "node:module";
import { readFileSync, mkdirSync, unlinkSync } from "node:fs";
import { join, resolve } from "node:path";
import { homedir } from "node:os";
import { execFileSync } from "node:child_process";
import { Remote, RemoteError, TypertRemoteService } from "@deepseek-ai/dsh-typert-protocol";
import { Bridge, BridgeError, atomicJson } from "./bridge.mjs";
import { servePipe } from "./pipe.mjs";

const VERSION = "0.2.1-rc.3";
const initializers = [];

export default class ClassCueBridge extends TypertRemoteService {
  static inject = ["sessionController", "sessions", "sessionPersistence", "workspaceRegistry", "profileContext"];

  constructor(ctx) {
    super(ctx, "classcueBridge");
    for (const initialize of initializers) initialize.call(this);
    const require = createRequire(import.meta.url);
    const version = JSON.parse(readFileSync(require.resolve("@deepseek-ai/dsh-api-session-controller/package.json"), "utf8")).version;
    if (process.platform !== "win32" || ctx.profileContext.name !== "desktop" || version !== VERSION)
      throw new Error(`ClassCue 桥接仅支持 Windows 桌面 DSH ${VERSION}`);
    const root = join(resolve(process.env.DSH_HOME ?? join(homedir(), ".dsh")), "classcue-bridge");
    mkdirSync(root, { recursive: true });
    // Windows ignores POSIX modes. Restrict the pairing descriptor and question snapshots to this account.
    const who = execFileSync("whoami.exe", ["/user", "/fo", "csv", "/nh"], { encoding: "utf8", windowsHide: true });
    const sid = who.match(/S-1-\d+(?:-\d+)+/)?.[0];
    if (!sid) throw new Error("无法确定当前 Windows 用户，桥接未开启");
    execFileSync("icacls.exe", [root, "/inheritance:r", "/grant:r", `*${sid}:(OI)(CI)F`,
      "*S-1-5-18:(OI)(CI)F"], { windowsHide: true, stdio: "ignore" });
    const native = {
      workspaces: () => ctx.workspaceRegistry.list().map(w => ({
        workspace_id: w.id, path: w.path, title: w.title,
      })),
      check: async (id, workspaceId) => {
        if (ctx.workspaceRegistry.archivedSessionIds.includes(id))
          throw new BridgeError("session_archived", "原对话已归档，请先在 DSH 中取消归档");
        const workspace = ctx.workspaceRegistry.get(workspaceId);
        if (!workspace) throw new BridgeError("workspace_missing", "原工作区已移除");
        if (!ctx.sessions.get(id) && !await ctx.sessionPersistence.stat(id))
          throw new BridgeError("session_missing", "原对话不存在，未创建替代对话");
        if (!workspace.sessionIds.includes(id))
          throw new BridgeError("session_detached", "原对话不在绑定的工作区中，请在 DSH 中检查");
      },
      ensureSession: async (id, workspaceId) => {
        const live = ctx.sessions.get(id);
        const stored = live ?? await ctx.sessionPersistence.stat(id);
        if (stored) {
          await ctx.workspaceRegistry.get(workspaceId).attachSession(id);
          return;
        }
        try {
          const created = await ctx.sessionController.create({ workspaceId, sessionId: id });
          if (created.sessionId !== id) throw new BridgeError("session_conflict", "DSH 返回了不同会话，已停止传输");
        } catch (error) {
          if (error?.code === "session/workspace-attach-failed" && error.details?.sessionId === id)
            await ctx.workspaceRegistry.get(workspaceId).attachSession(id);
          else throw error;
        }
      },
      flush: async id => {
        const session = ctx.sessions.get(id);
        if (session) await ctx.sessions.flush(session);
        if (!await ctx.sessionPersistence.stat(id))
          throw new BridgeError("session_not_durable", "对话尚未持久保存，请重试同一请求");
      },
    };
    this.bridge = new Bridge(root, native);
    const path = `\\\\.\\pipe\\classcue-dsh-${this.bridge.state.instance_id}`;
    this.pipe = servePipe(path, (method, params) => {
      switch (method) {
        case "status": return this.bridge.status();
        case "workspaces": return this.bridge.workspaces();
        case "prepare": return this.bridge.prepare(params);
        case "request": return this.bridge.request(params.request_id);
        case "open": return this.bridge.open(params.request_id);
        default: throw new BridgeError("method_unknown", "不支持的桥接操作");
      }
    });
    const descriptor = join(root, "connection.json");
    ctx.effect(() => {
      let disposed = false;
      const started = this.pipe.listen().then(async () => {
        if (disposed) return;
        atomicJson(descriptor, { protocol: 1, pipe: path, token: this.pipe.token,
          instance_id: this.bridge.state.instance_id, desktop_version: VERSION });
        // A send may have been committed immediately before an unclean Host shutdown.
        for (const row of Object.values(this.bridge.state.requests)) {
          if (["reserved", "choose_workspace", "sent"].includes(row.status)) continue;
          try {
            const info = await ctx.sessionController.inspect(row.session_id);
            if (info?.events.some(e => e.type === "user/message")) await this.bridge.sent(row.session_id);
          } catch { /* Keep the binding; opening reports the missing Session. */ }
        }
      });
      started.catch(error => ctx.logger.error("ClassCue 桥接启动失败", error.code ?? error.name));
      return async () => {
        disposed = true;
        await started.catch(() => {});
        await this.pipe.close();
        try {
          if (JSON.parse(readFileSync(descriptor, "utf8")).token === this.pipe.token) unlinkSync(descriptor);
        } catch (error) { if (error.code !== "ENOENT") throw error; }
      };
    });
    ctx.on("session/event", (session, event) => {
      if (event.type === "user/message")
        this.bridge.sent(session.id).catch(() => ctx.logger.error("ClassCue 对话状态保存失败"));
    });
  }
  owner() {
    if (this.ctx.invocation?.peer.principal.kind !== "owner")
      throw new RemoteError("gateway/forbidden", "仅允许桌面所有者使用题目桥接", {});
  }
  next(clientId) { this.owner(); return this.bridge.next(clientId); }
  choose(requestId, workspaceId) { this.owner(); return this.bridge.choose(requestId, workspaceId); }
  acknowledge(clientId, requestId, command, result) {
    this.owner();
    return this.bridge.acknowledge(clientId, requestId, command, result);
  }
  backup(requestId, content) { this.owner(); return this.bridge.backup(requestId, content); }
}

// Apply the public decorator's initializer without a separate TS runtime or a second Cordis copy.
for (const name of ["next", "choose", "acknowledge", "backup"]) {
  Remote(ClassCueBridge.prototype[name], {
    kind: "method", name, static: false, private: false,
    addInitializer: fn => initializers.push(fn),
  });
}
