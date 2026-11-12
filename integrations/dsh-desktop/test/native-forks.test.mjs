import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Bridge, BridgeError, readDiscussionStates } from "../bridge.mjs";

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), "classcue-forks-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const sessions = new Map(), headers = [], states = new Map(), archived = new Set();
  let created = 0;
  const native = {
    workspaces: () => [{ workspace_id: "database", path: root }],
    ensureSession: async (id, workspace) => { created++; sessions.set(id, workspace); },
    flush: async () => {},
    check: async (id, workspace) => {
      if (!sessions.has(id)) throw new BridgeError("session_missing", "原对话不存在");
      if (sessions.get(id) !== workspace) throw new BridgeError("session_detached", "归属变化");
    },
    forkHeaders: async () => headers,
    discussionStates: async rows => new Map(rows.map(row => [row.session_id,
      archived.has(row.session_id) ? "archived"
        : !sessions.has(row.session_id) ? "missing" : states.get(row.session_id) ?? "draft"])),
  };
  const bridge = new Bridge(root, native);
  const prepare = (question = "question", request = "original", library = "library", version = 1) =>
    bridge.prepare({ request_id: request, library_id: library, course_id: "course",
      course_name: "合成课程", document_id: "paper", question_id: question,
      content_version: version, workspace_id: "database", content: [{ type: "text", text: "完整题目" }] });
  function fork(parent, id, extra = {}) {
    const header = { id, parentSession: parent, isSeeded: true, createdAt: Date.now(),
      cwd: root, workspace_id: "database", durable: true, ...extra };
    headers.push(header);
    sessions.set(id, header.workspace_id);
    bridge.forksChanged();
    return header;
  }
  return { root, native, bridge, prepare, fork, headers, sessions, states, archived, created: () => created };
}

test("native forks and forks of forks inherit one question; discovery is idempotent across restart", async t => {
  const f = fixture(t);
  const original = await f.prepare();
  f.fork("child", "grandchild"); // Native header listings need not be in ancestry order.
  f.fork(original.session_id, "child");
  const rows = await f.bridge.discussions({ library_id: "library", question_id: "question" });
  assert.equal(rows.length, 3);
  const child = rows.find(row => row.session_id === "child");
  const grandchild = rows.find(row => row.session_id === "grandchild");
  assert.equal(child.parent_request_id, original.request_id);
  assert.equal(grandchild.parent_request_id, child.request_id);
  assert.equal(child.content_version, 1);
  assert.equal(child.status, "linked");
  assert.equal(f.created(), 1);
  const restarted = new Bridge(f.root, f.native);
  assert.deepEqual(await restarted.discussions({ library_id: "library", question_id: "question" }), rows);
  assert.equal(readdirSync(join(f.root, "requests")).length, 1); // No copied image/question payloads.
});

test("opening even an empty fork only navigates, never injects a question or recreates a deleted session", async t => {
  const f = fixture(t);
  const original = await f.prepare();
  const first = await f.bridge.next("client");
  await f.bridge.acknowledge("client", first.request_id, first.command, "draft_ready");
  f.fork(original.session_id, "child");
  const rows = await f.bridge.discussions({ library_id: "library", question_id: "question" });
  const child = rows.find(row => row.origin === "fork");
  await f.bridge.open(child.request_id);
  const command = await f.bridge.next("client");
  assert.equal(command.session_id, "child");
  assert.deepEqual(command.content, []);
  await f.bridge.acknowledge("client", command.request_id, command.command, "opened");
  await f.bridge.sent("child");
  assert.equal(f.bridge.request(child.request_id).status, "linked");
  f.sessions.delete("child");
  await assert.rejects(f.bridge.open(child.request_id), error => error.code === "session_missing");
  assert.equal(f.created(), 1);
  assert.equal((await f.bridge.open(original.request_id)).session_id, original.session_id);
});

test("unrelated sessions, subagents and their descendants are excluded; libraries and versions stay separate", async t => {
  const f = fixture(t);
  const original = await f.prepare();
  f.fork(original.session_id, "subagent", { origin: "subagent" });
  f.fork("subagent", "subagent-fork");
  f.fork("unrelated", "other");
  f.fork(original.session_id, "not-seeded", { isSeeded: false });
  const newer = await f.bridge.prepare({ request_id: "new-version", library_id: "library", course_id: "course",
    document_id: "paper", question_id: "question", content_version: 2, new_discussion: true,
    content: [{ type: "text", text: "修正题目" }] });
  f.fork(original.session_id, "old-version-child");
  f.fork(newer.session_id, "new-version-child");
  const foreign = await f.prepare("question", "foreign", "another-library");
  f.fork(foreign.session_id, "foreign-child");
  const rows = await f.bridge.discussions({ library_id: "library", question_id: "question" });
  assert.deepEqual(rows.filter(r => r.origin === "fork").map(r => [r.session_id, r.content_version]),
    [["old-version-child", 1], ["new-version-child", 2]]);
  assert.equal((await f.bridge.discussions({ library_id: "another-library", question_id: "question" })).length, 2);
});

test("creation and persistence races retry on native heartbeat without a ClassCue connection", async t => {
  const f = fixture(t);
  const original = await f.prepare();
  const header = f.fork(original.session_id, "child", { durable: false, workspace_id: undefined });
  await f.bridge.next("client");
  assert.equal(Object.keys(f.bridge.state.requests).length, 1);
  assert.equal(f.bridge.forksDirty, true);
  header.durable = true;
  header.workspace_id = "database";
  f.sessions.set("child", "database");
  await f.bridge.next("client");
  assert.equal(Object.keys(f.bridge.state.requests).length, 2);
  assert.equal(f.bridge.forksDirty, false);
});

test("native discussion state follows real turns, archive, restore and deletion without removing recoverable intents", async t => {
  const f = fixture(t);
  const original = await f.prepare();
  const query = { library_id: "library", question_id: "question" };
  assert.equal((await f.bridge.discussions(query))[0].discussion_state, "draft");
  await f.bridge.sent(original.session_id);
  assert.equal((await f.bridge.discussions(query))[0].discussion_state, "draft");
  f.states.set(original.session_id, "active");
  assert.equal((await f.bridge.discussions(query))[0].discussion_state, "active");
  f.archived.add(original.session_id);
  assert.equal((await f.bridge.discussions(query))[0].discussion_state, "archived");
  f.archived.delete(original.session_id);
  assert.equal((await f.bridge.discussions(query))[0].discussion_state, "active");
  f.sessions.delete(original.session_id);
  assert.equal((await f.bridge.discussions(query))[0].discussion_state, "missing");
  assert.equal(Object.keys(f.bridge.state.requests).length, 1);
  assert.equal(f.created(), 1);
});

test("blank forks are not active by mere linkage and their state persists across restart", async t => {
  const f = fixture(t);
  const original = await f.prepare();
  f.fork(original.session_id, "child");
  const query = { library_id: "library", question_id: "question" };
  let rows = await f.bridge.discussions(query);
  assert.equal(rows.find(row => row.origin === "fork").discussion_state, "draft");
  f.states.set("child", "active");
  f.archived.add(original.session_id);
  rows = await f.bridge.discussions(query);
  assert.equal(rows.find(row => row.origin === "fork").discussion_state, "active");
  assert.equal(rows.find(row => row.request_id === original.request_id).discussion_state, "archived");
  const restarted = new Bridge(f.root, f.native);
  assert.deepEqual(await restarted.discussions(query), rows);
});

test("native state reader uses DSH turn semantics for live and cold sessions and distinguishes faults from absence", async () => {
  const events = new Map([
    ["draft", [{ type: "session/title" }]],
    ["queued", [{ type: "user/message" }]],
    ["cold", [{ type: "turn/start", data: { turn: 1 } }]],
  ]);
  const ctx = {
    sessions: { get: id => id === "draft" || id === "queued" },
    sessionPersistence: { stat: async id => id === "cold" ? { header: { id } } : undefined },
    sessionController: { inspect: async id => ({ events: events.get(id) }) },
    workspaceRegistry: {
      archivedSessionIds: ["archived"],
      get: id => id === "workspace" ? { sessionIds: ["draft", "queued", "cold"] } : undefined,
    },
  };
  const rows = ["draft", "queued", "cold", "archived", "missing", "pending"].map(id => ({
    session_id: id, workspace_id: "workspace", status: id === "pending" ? "reserved" : "sent",
  }));
  assert.deepEqual(Object.fromEntries(await readDiscussionStates(ctx, rows)), {
    draft: "draft", queued: "draft", cold: "active", archived: "archived", missing: "missing", pending: "pending",
  });
  assert.equal((await readDiscussionStates(ctx, [{ ...rows[0], workspace_id: "removed" }])).get("draft"), "unavailable");
  ctx.sessionPersistence.stat = async () => { throw new Error("synthetic disk failure"); };
  await assert.rejects(readDiscussionStates(ctx, [rows[2]]), /synthetic disk failure/);
});
