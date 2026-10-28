window.__ModuleLoader__.load({
  id: "classcue-dsh-desktop",
  factory: require => {
    const React = require("react");
    const { Modal, Button, IconFolderOutlineRegular } = require("@deepseek-ai/dsh-client-ui-primitives");
    const h = React.createElement;
    const unwrap = answer => {
      if (!answer.ok) throw new Error(answer.error?.message ?? "DSH 桥接操作失败");
      return answer.value;
    };

    class DraftController {
      constructor(conversation, markers) {
        this.conversation = conversation;
        this.markers = markers;
      }
      async prepare(command, shell, isBlank) {
        if (command.status === "sent" || !isBlank) return "opened";
        const key = `${command.instance_id}/${command.session_id}`;
        const marker = this.markers.get(key);
        if (marker && marker.ids.every(id => this.conversation.resolveDraftAttachments([id]).length === 1))
          return "opened";
        const text = command.content.filter(p => p.type === "text").map(p => p.text).join("\n\n");
        const ids = shell.snapshot.attachmentIds;
        if (ids.length > 0 || shell.snapshot.draft && shell.snapshot.draft !== text)
          return "draft_conflict";
        const files = command.content.filter(p => p.type === "image").map((p, index) => {
          const bytes = Uint8Array.from(atob(p.data), c => c.charCodeAt(0));
          return new File([bytes], `question-${index + 1}.${p.media_type.split("/")[1]}`, { type: p.media_type });
        });
        const drafts = this.conversation.createDrafts(command.session_id, files);
        if (shell.requestDraftInitialization({ prompt: text, whenDraftExists: "keep" }) === "blocked"
          || !shell.addAttachments(drafts.map(d => d.id))) {
          this.conversation.releaseDraftAttachments(drafts);
          return "draft_conflict";
        }
        this.markers.set(key, { ids: drafts.map(d => d.id) });
        return "draft_ready";
      }
    }

    function WorkspaceDialog({ store, select, add, cancel }) {
      const state = React.useSyncExternalStore(store.subscribe, store.getSnapshot);
      const [selected, setSelected] = React.useState("");
      const [busy, setBusy] = React.useState(false);
      const [error, setError] = React.useState("");
      React.useEffect(() => { setSelected(""); setError(""); }, [state?.request_id]);
      if (!state) return null;
      const run = async operation => {
        setBusy(true);
        setError("");
        try { await operation(); } catch (e) { setError(e.message); } finally { setBusy(false); }
      };
      return h(Modal, {
        open: true, title: "选择题目讨论的工作区", closeLabel: "取消",
        onClose: () => { if (!busy) cancel(); },
        footer: h(React.Fragment, null,
          h(Button, { variant: "outline", disabled: busy, onClick: cancel }, "取消"),
          h(Button, { variant: "primary", disabled: busy || !selected,
            onClick: () => run(() => select(selected)) }, busy ? "正在准备…" : "开始讨论")),
      },
      h("div", { style: { display: "grid", gap: 12, minWidth: 0 } },
        h("div", { style: { overflowWrap: "anywhere" } }, state.title || "所选题目"),
        h("label", { style: { display: "grid", gap: 8 } }, "工作区",
          h("select", { "aria-label": "工作区", value: selected, disabled: busy,
            onChange: e => setSelected(e.target.value),
            style: { width: "100%", minWidth: 0, padding: 8, borderRadius: 6,
              background: "var(--dsw-alias-background-primary, transparent)", color: "inherit",
              border: "1px solid var(--dsw-alias-border-primary, #888)", font: "inherit" } },
          h("option", { value: "" }, "请选择"),
          ...state.workspaces.map(w => h("option", { key: w.workspace_id, value: w.workspace_id }, w.title || w.path)))),
        h(Button, { variant: "outline", disabled: busy, onClick: () => run(add) },
          IconFolderOutlineRegular ? h(IconFolderOutlineRegular, { size: 16 }) : null, "选择其他文件夹"),
        error ? h("div", { role: "alert", style: { color: "var(--dsw-alias-state-error-primary)", overflowWrap: "anywhere" } }, error) : null));
    }

    function apply(ctx) {
      const clientId = crypto.randomUUID();
      const markers = new Map();
      const drafts = new DraftController(ctx.conversation, markers);
      const watched = new Map();
      const completed = new Map();
      const listeners = new Set();
      let choice = null;
      let disposed = false;
      let timer;
      let inFlight = Promise.resolve();
      const store = {
        subscribe: fn => { listeners.add(fn); return () => listeners.delete(fn); },
        getSnapshot: () => choice,
      };
      const publish = value => { choice = value; for (const fn of listeners) fn(); };
      // This local plugin uses named SRC Remote arguments over the existing desktop carrier.
      const call = (method, args) => ctx.connection.rpc.call("/api", `classcueBridge/${method}`, { args }, AbortSignal.timeout(10000));
      const rpc = {
        next: clientId => call("next", { clientId }),
        choose: (requestId, workspaceId) => call("choose", { requestId, workspaceId }),
        acknowledge: (clientId, requestId, command, result) => call("acknowledge", { clientId, requestId, command, result }),
        backup: (requestId, content) => call("backup", { requestId, content }),
      };
      const ack = async (command, result) => unwrap(await rpc.acknowledge(
        clientId, command.request_id, command.command, result,
      ));
      ctx.slots.inject("shell.overlay", () => ctx.slots.register({
        name: "shell.overlay", id: "classcue-workspace",
        inject: () => ({
          store,
          select: async workspaceId => {
            const command = choice;
            unwrap(await rpc.choose(command.request_id, workspaceId));
            publish(null);
          },
          add: async () => {
            const path = unwrap(await ctx.remote.directoryPicker.pick());
            if (!path) return;
            const created = unwrap(await ctx.remote.workspace.create({ path }));
            unwrap(await rpc.choose(choice.request_id, created.workspace.workspaceId));
            publish(null);
          },
          cancel: () => {
            const command = choice;
            ack(command, "cancelled").then(() => publish(null)).catch(e => console.warn("ClassCue:", e.message));
          },
        }),
      }, WorkspaceDialog));
      async function handle(command) {
        if (command.status === "choose_workspace") {
          if (choice?.command !== command.command) publish(command);
          return;
        }
        const known = completed.get(command.command);
        if (known) { await ack(command, known); return; }
        let result;
        // Retention gives the native composer a stable binding; navigation retains its own reference.
        try {
          await ctx.sessions.using(command.session_id, { source: "classcueBridge" }, async reference => {
            const binding = reference.binding;
            const shell = ctx.conversation.input.for(binding.ctx);
            result = await drafts.prepare(command, shell, binding.session.getSnapshot().blank);
            ctx.uiWorkspace.openSession(command.session_id);
            if (command.status !== "sent" && result !== "draft_conflict" && !watched.has(command.session_id)) {
              const watch = { requestId: command.request_id, reference: binding, fingerprint: null,
                shell, pending: Promise.resolve(), unsubscribe: null };
              watched.set(command.session_id, watch);
              watch.unsubscribe = shell.state.subscribe(() => {
                watch.pending = watch.pending.then(() => backupOne(command.session_id, watch))
                  .catch(e => console.warn("ClassCue 草稿备份失败:", e.message));
              });
            }
          });
        } catch (error) {
          completed.set(command.command, "error");
          await ack(command, "error");
          throw error;
        }
        completed.set(command.command, result);
        await ack(command, result);
      }
      async function backupOne(sessionId, watch) {
        const binding = ctx.sessions.binding(sessionId);
        if (binding && !binding.session.getSnapshot().blank) {
          watch.unsubscribe?.(); watched.delete(sessionId); return;
        }
        const shell = watch.shell;
        const snapshot = shell.snapshot;
        if (["adjudicating", "submitting"].includes(snapshot.phase)) return;
        const fingerprint = JSON.stringify([snapshot.draft, snapshot.attachmentIds]);
        if (fingerprint === watch.fingerprint) return;
        const encoded = await ctx.conversation.serializeDraftAttachments(snapshot.attachmentIds);
        if (encoded.attachments.some(a => a.type !== "image")) return;
        if (fingerprint !== JSON.stringify([shell.snapshot.draft, shell.snapshot.attachmentIds])) return;
        const content = [
          ...(snapshot.draft.trim() ? [{ type: "text", text: snapshot.draft }] : []),
          ...encoded.attachments.map(a => ({ type: "image", media_type: a.mediaType, data: a.data })),
        ];
        unwrap(await rpc.backup(watch.requestId, content));
        watch.fingerprint = fingerprint;
        if (!binding) { watch.unsubscribe?.(); watched.delete(sessionId); }
      }
      async function backup() {
        for (const [sessionId, watch] of watched) {
          await watch.pending;
          await backupOne(sessionId, watch);
        }
      }
      async function tick() {
        try {
          const command = unwrap(await rpc.next(clientId));
          if (command) await handle(command);
          await backup();
        } catch (error) {
          // No prompt submission and no replacement Session on a transport failure.
          console.warn("ClassCue 桥接等待重连:", error.message);
        } finally {
          if (!disposed) timer = setTimeout(() => { inFlight = tick(); }, 1000);
        }
      }
      ctx.effect(() => {
        inFlight = tick();
        return async () => {
          disposed = true;
          clearTimeout(timer);
          await inFlight;
          try { await backup(); } finally {
            for (const watch of watched.values()) watch.unsubscribe?.();
            watched.clear();
            publish(null);
          }
        };
      });
    }
    return { apply, inject: ["connection", "remote", "sessions", "conversation", "uiWorkspace", "slots"], DraftController };
  },
});
