import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import { DraftStore, threadIdentity, openDraftStore } from "../drafts.mjs";
import { DraftController } from "../draft-controller.mjs";
import { App } from "@modelcontextprotocol/ext-apps";
import { AppBridge } from "@modelcontextprotocol/ext-apps/app-bridge";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { configuredRoots, discussionStore } from "../connection.mjs";

const id = "a".repeat(32);
async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), "classcue-draft-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const directory = join(root, "paper", ".classcue", "discussions", id);
  await mkdir(directory, { recursive: true });
  const file = join(directory, "context.json");
  const raw = JSON.stringify({ format: "classcue-codex-context", version: 1, request_id: id,
    content: [{ type: "text", text: "合成题目" }] });
  await writeFile(file, raw);
  await mkdir(join(root, ".classcue-codex"));
  const path = join(root, ".classcue-codex", "drafts.sqlite");
  const db = new DatabaseSync(path);
  db.exec(`CREATE TABLE drafts(request_id TEXT PRIMARY KEY,thread_id TEXT UNIQUE,context_file TEXT,
    title TEXT,context_sha256 TEXT,status TEXT DEFAULT 'pending',attempt TEXT,error TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP)`);
  db.prepare("INSERT INTO drafts(request_id,thread_id,context_file,title,context_sha256) VALUES(?,?,?,?,?)")
    .run(id, "thread-1", file, "合成试卷", createHash("sha256").update(raw).digest("hex"));
  db.close();
  return { root, path, file };
}

test("requires host thread identity and rejects a mismatched or missing binding", async t => {
  assert.throws(() => threadIdentity({}), /对话内/);
  assert.throws(() => threadIdentity({ threadId: "one", thread_id: "two" }), /对话内/);
  const { root } = await fixture(t);
  const store = await openDraftStore(root);
  try {
    await assert.rejects(store.load("other-thread"), /没有.*绑定/);
    await assert.rejects(store.claim("thread-1", "b".repeat(32)), /不匹配/);
    assert.equal((await store.load("thread-1")).status, "pending");
  } finally { store.close(); }
});

test("atomic claim survives process restart and unknown acknowledgements never duplicate", async t => {
  const { root, path } = await fixture(t);
  const first = new DraftStore(path, root), second = new DraftStore(path, root);
  const results = await Promise.all([first.claim("thread-1", id), second.claim("thread-1", id)]);
  assert.equal(results.filter(value => value.claimed).length, 1);
  const attempt = results.find(value => value.claimed).attempt;
  first.close(); second.close();
  const restarted = await openDraftStore(root);
  try {
    assert.equal((await restarted.claim("thread-1", id)).claimed, false);
    assert.equal((await restarted.claim("thread-1", id, attempt)).claimed, false);
    restarted.acknowledge("thread-1", id, attempt, "unknown");
    assert.equal((await restarted.claim("thread-1", id)).claimed, false);
    restarted.acknowledge("thread-1", id, attempt, "attached");
    assert.equal((await restarted.claim("thread-1", id)).claimed, false);
    const retry = await restarted.claim("thread-1", id, attempt);
    assert.equal(retry.claimed, true);
    assert.notEqual(retry.attempt, attempt);
    assert.throws(() => restarted.acknowledge("thread-1", id, attempt, "attached"), /已变化/);
  } finally { restarted.close(); }
});

test("modified snapshot is rejected before a draft operation", async t => {
  const { root, path, file } = await fixture(t);
  const store = new DraftStore(path, root);
  try {
    const context = JSON.parse(await (await import("node:fs/promises")).readFile(file, "utf8"));
    context.content[0].text = "altered";
    await writeFile(file, JSON.stringify(context));
    await assert.rejects(store.claim("thread-1", id), /快照已变化/);
    assert.equal(store.row("thread-1").status, "pending");
  } finally { store.close(); }
});

test("running plugin rereads workspace changes and retains old discussion bindings", async t => {
  const first = await fixture(t), second = await fixture(t);
  const secondDb = new DatabaseSync(second.path);
  secondDb.exec("UPDATE drafts SET thread_id='thread-2'");
  secondDb.close();
  const file = join(first.root, "connection.json");
  const env = { CLASSCUE_CODEX_ROOT: first.root, CLASSCUE_CODEX_CONNECTION_FILE: file };
  await writeFile(file, JSON.stringify({ format: "classcue-codex-connection", version: 1, roots: [first.root] }));
  let store = await discussionStore("thread-1", env);
  assert.equal((await store.load("thread-1")).thread_id, "thread-1");
  store.close();
  await assert.rejects(discussionStore("thread-2", env), /没有可读取/);
  await writeFile(file, "\uFEFF" + JSON.stringify({ format: "classcue-codex-connection",
    version: 1, roots: [second.root, first.root] }));
  store = await discussionStore("thread-2", env);
  assert.equal((await store.load("thread-2")).thread_id, "thread-2");
  store.close();
  store = await discussionStore("thread-1", env);
  assert.equal((await store.load("thread-1")).thread_id, "thread-1");
  store.close();
});

test("duplicate bindings and malformed connection files fail without guessing a workspace", async t => {
  const first = await fixture(t), second = await fixture(t);
  const file = join(first.root, "connection.json");
  const env = { CLASSCUE_CODEX_CONNECTION_FILE: file };
  await writeFile(file, JSON.stringify({ format: "classcue-codex-connection",
    version: 1, roots: [second.root, first.root] }));
  await assert.rejects(discussionStore("thread-1", env), /多个工作区/);
  await writeFile(file, JSON.stringify({ format: "classcue-codex-connection", version: 1, roots: ["relative"] }));
  await assert.rejects(configuredRoots(env), /无效/);
});

function hostFixture({ sendError, capability = true, ackError = false } = {}) {
  let status = "pending", calls = [], sent = [];
  const app = {
    getHostCapabilities: () => ({ experimental: { "openai/message": { draft: capability ? {} : undefined } } }),
    callServerTool: async request => {
      calls.push(request);
      if (request.name === "claim_discussion_draft") {
        if (status !== "pending") return { structuredContent: { claimed: false, status } };
        status = "inserting";
        return { structuredContent: { claimed: true, attempt: "attempt-1" } };
      }
      if (ackError) throw new Error("lost receipt");
      status = request.arguments.status;
      return { structuredContent: { status } };
    },
    sendMessage: async request => {
      sent.push(request);
      if (sendError) throw sendError;
      return {};
    },
  };
  const content = [{ type: "text", text: "小问一" }, { type: "image", mimeType: "image/png", data: "one" },
    { type: "text", text: "小问二" }, { type: "image", mimeType: "image/png", data: "two" }];
  return { app, calls, sent, discussion: { request_id: id, content }, status: () => status };
}

test("draft controller preserves ordered content, explicitly disables sending and never repeats", async () => {
  const fixture = hostFixture(), controller = new DraftController(fixture.app);
  assert.equal((await controller.insert(fixture.discussion)).status, "attached");
  assert.deepEqual(fixture.sent[0].content, fixture.discussion.content);
  assert.deepEqual(fixture.sent[0]._meta, { "openai/message": { target: "active", send: false } });
  await new DraftController(fixture.app).insert(fixture.discussion);
  assert.equal(fixture.sent.length, 1);
});

test("unsupported host cannot claim; lost host or receipt replies cannot auto-retry", async () => {
  const unsupported = hostFixture({ capability: false });
  await assert.rejects(new DraftController(unsupported.app).insert(unsupported.discussion), /不支持/);
  assert.equal(unsupported.calls.length, 0);
  for (const config of [{ sendError: new Error("disconnected") }, { ackError: true }]) {
    const fixture = hostFixture(config), controller = new DraftController(fixture.app);
    try { await controller.insert(fixture.discussion); } catch {}
    await new DraftController(fixture.app).insert(fixture.discussion);
    assert.equal(fixture.sent.length, 1);
    assert.ok(["inserting", "unknown"].includes(fixture.status()));
  }
});

test("real MCP Apps SDK handshake preserves no-send metadata on the wire", async t => {
  const app = new App({ name: "synthetic-view", version: "1" }, {}, { autoResize: false });
  const bridge = new AppBridge(null, { name: "synthetic-host", version: "1" },
    { experimental: { "openai/message": { draft: {} } }, serverTools: {} });
  let observed;
  bridge.onmessage = async params => { observed = params; return {}; };
  bridge.oncalltool = async ({ name, arguments: args }) => ({
    content: [], structuredContent: name === "claim_discussion_draft"
      ? { claimed: true, attempt: "synthetic-attempt" } : { status: args.status },
  });
  const [hostTransport, appTransport] = InMemoryTransport.createLinkedPair();
  let message;
  const send = appTransport.send.bind(appTransport);
  appTransport.send = async value => {
    if (value.method === "ui/message") message = structuredClone(value);
    return send(value);
  };
  t.after(async () => { await app.close(); await bridge.close(); });
  await bridge.connect(hostTransport);
  await app.connect(appTransport);
  const content = [{ type: "text", text: "合成题目" },
    { type: "image", mimeType: "image/png", data: "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF0cAAAAASUVORK5CYII=" }];
  await new DraftController(app).insert({ request_id: id, content });
  assert.equal(observed.content[0].text, "合成题目");
  assert.deepEqual(message.params.content, content);
  // The generic SDK host strips vendor metadata; Codex validates its extended schema.
  assert.deepEqual(message.params._meta["openai/message"], { target: "active", send: false });
});
