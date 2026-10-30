import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../client.js", import.meta.url), "utf8");

async function workspaceDialog(picker) {
  let plugin, dialog, dispose;
  const calls = [];
  const command = { request_id: "synthetic-request", command: "choose", status: "choose_workspace",
    title: "合成题目", workspaces: [] };
  const sandbox = {
    window: { __ModuleLoader__: { load: module => {
      plugin = module.factory(name => name === "react" ? { createElement() {} } : {});
    } } },
    crypto: { randomUUID: () => "synthetic-client" }, AbortSignal, console,
    setTimeout: () => 1, clearTimeout() {},
    ...(picker ? { __DSH_DIRECTORY_PICKER__: { pick: picker } } : {}),
  };
  vm.runInNewContext(source, sandbox);
  plugin.apply({
    connection: { rpc: { call: async (_route, method, { args }) => {
      calls.push({ method, args });
      return { ok: true, value: method === "classcueBridge/next" ? command : {} };
    } } },
    remote: { workspace: { create: async ({ path }) => {
      calls.push({ method: "create", path });
      return { ok: true, value: { workspace: { workspaceId: "synthetic-workspace" } } };
    } } },
    uiWorkspace: { pickDirectory: async () => {
      calls.push({ method: "fallback" }); return null;
    } },
    conversation: {},
    slots: {
      inject: (_slot, register) => register(),
      register: options => { dialog = options.inject(); },
    },
    effect: start => { dispose = start(); },
  });
  // Let the first local polling turn publish its workspace request.
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(dialog.store.getSnapshot().request_id, command.request_id);
  return { dialog, calls, dispose };
}

test("native picker cancellation resolves without creating or selecting a workspace", async () => {
  let picked = 0;
  const harness = await workspaceDialog(async () => { picked++; return null; });
  await harness.dialog.add();
  assert.equal(picked, 1);
  assert.equal(harness.dialog.store.getSnapshot().status, "choose_workspace");
  assert.equal(harness.calls.some(call => ["fallback", "create", "classcueBridge/choose"].includes(call.method)), false);
  await harness.dispose();
});

test("native selection creates the chosen workspace and releases the dialog", async () => {
  const harness = await workspaceDialog(async () => "synthetic-folder");
  await harness.dialog.add();
  assert.equal(harness.calls.find(call => call.method === "create").path, "synthetic-folder");
  assert.equal(harness.calls.find(call => call.method === "classcueBridge/choose").args.workspaceId, "synthetic-workspace");
  assert.equal(harness.dialog.store.getSnapshot(), null);
  assert.equal(harness.calls.some(call => call.method === "fallback"), false);
  await harness.dispose();
});

test("missing desktop preload retains the DSH service cancellation contract", async () => {
  const harness = await workspaceDialog();
  await harness.dialog.add();
  assert.equal(harness.calls.filter(call => call.method === "fallback").length, 1);
  assert.equal(harness.calls.some(call => call.method === "create"), false);
  await harness.dispose();
});

test("an image-only question prepares attachments with an empty prompt and preserves later user edits", async () => {
  let plugin;
  vm.runInNewContext(source, {
    window: { __ModuleLoader__: { load: module => {
      plugin = module.factory(() => ({}));
    } } },
    atob, File,
  });
  let prompt, attachments = [], draft = "", created = 0;
  const conversation = {
    createDrafts: (_session, files) => {
      created++;
      assert.equal(files.length, 1);
      assert.equal(files[0].type, "image/png");
      return [{ id: "image-1" }];
    },
    resolveDraftAttachments: ids => ids.filter(id => attachments.includes(id)),
  };
  const shell = {
    get snapshot() { return { draft, attachmentIds: attachments }; },
    requestDraftInitialization: options => { prompt = options.prompt; return "initialized"; },
    addAttachments: ids => { attachments = [...ids]; return true; },
  };
  const command = { status: "bound", instance_id: "instance", session_id: "session",
    content: [{ type: "image", media_type: "image/png", data: "iVBORw0KGgo=" }] };
  const controller = new plugin.DraftController(conversation, new Map());
  assert.equal(await controller.prepare(command, shell, true), "draft_ready");
  assert.equal(prompt, "");
  assert.deepEqual([...attachments], ["image-1"]);
  draft = "User's own question";
  assert.equal(await controller.prepare(command, shell, true), "opened");
  assert.equal(draft, "User's own question");
  assert.equal(created, 1);
});
