import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { Bridge } from "../bridge.mjs";

function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), "classcue-course-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const sessions = new Map();
  const native = {
    workspaces: () => ["database", "algorithms"].map(id => ({ workspace_id: id, path: id, title: id })),
    ensureSession: async (session, workspace) => { sessions.set(session, workspace); },
    check: async (session, workspace) => { assert.equal(sessions.get(session), workspace); },
    flush: async () => {},
  };
  return { root, native, sessions, bridge: new Bridge(root, native) };
}

function question(request, course, document = "paper", id = request) {
  return { request_id: request, library_id: "library", course_id: course, course_name: course,
    document_id: document, question_id: id, content_version: 1,
    content: [{ type: "text", text: "合成题目" }] };
}

test("different documents and video examples in one course share a workspace, with separate durable sessions", async t => {
  const { root, native, bridge } = fixture(t);
  const first = await bridge.prepare(question("first", "course-a", "paper-a"));
  assert.equal(first.status, "choose_workspace");
  const chosen = await bridge.choose(first.request_id, "database");
  const second = await bridge.prepare(question("second", "course-a", "paper-b"));
  const video = await bridge.prepare(question("video", "course-a", "video-1"));
  assert.equal(second.workspace_id, "database");
  assert.equal(video.workspace_id, "database");
  assert.equal(new Set([chosen.session_id, second.session_id, video.session_id]).size, 3);
  assert.equal(bridge.next("client").course_name, "course-a");
  const restarted = new Bridge(root, native);
  assert.equal((await restarted.prepare(question("fourth", "course-a"))).workspace_id, "database");
  assert.equal((await restarted.open(chosen.request_id)).session_id, chosen.session_id);
});

test("courses require separate workspaces and cannot silently override course memory", async t => {
  const { bridge, sessions } = fixture(t);
  await bridge.prepare(question("a", "course-a"));
  await bridge.choose("a", "database");
  await bridge.prepare(question("b", "course-b"));
  await assert.rejects(bridge.choose("b", "database"), error => error.code === "workspace_in_use");
  assert.equal(bridge.request("b").status, "choose_workspace");
  await bridge.choose("b", "algorithms");
  const follow = await bridge.prepare({ ...question("a2", "course-a"), workspace_id: "algorithms" });
  assert.equal(follow.workspace_id, "database");
  assert.equal(sessions.size, 3);
});

test("queued questions use the course choice made while another question was waiting", async t => {
  const { bridge } = fixture(t);
  await bridge.prepare(question("a", "course-a"));
  await bridge.prepare(question("b", "course-a"));
  await bridge.choose("a", "database");
  assert.equal((await bridge.choose("b", "algorithms")).workspace_id, "database");
});

test("old document-scoped discussions reopen without adopting their workspace for new course discussions", async t => {
  const { bridge, native } = fixture(t);
  const old = { ...question("old", "course-a"), course_id: undefined, session_id: "old-session",
    workspace_id: "database", status: "sent", snapshot_hash: "legacy" };
  bridge.state.requests.old = old;
  bridge.state.documents["library/paper"] = "database";
  native.check = async () => {};
  bridge.save();
  assert.equal((await bridge.open("old")).workspace_id, "database");
  assert.equal((await bridge.prepare(question("new", "course-a"))).status, "choose_workspace");
  await assert.rejects(bridge.prepare({ ...question("missing", "course-b"), course_id: undefined }),
    error => error.code === "course_required");
});

test("legacy pending discussions keep their explicit workspace choices after upgrade", async t => {
  const { root, native, bridge, sessions } = fixture(t);
  for (const [id, document] of [["old-a", "paper-a"], ["old-b", "paper-b"]]) {
    bridge.state.requests[id] = { ...question(id, undefined, document),
      session_id: `session-${id}`, status: "choose_workspace" };
  }
  bridge.save();
  const restarted = new Bridge(root, native);
  await restarted.choose("old-a", "database");
  const second = await restarted.choose("old-b", "algorithms");
  assert.equal(second.workspace_id, "algorithms");
  assert.equal(sessions.get("session-old-a"), "database");
  assert.equal(sessions.get("session-old-b"), "algorithms");
  assert.deepEqual(restarted.state.courses, {});
  assert.deepEqual(restarted.state.documents, { "library/paper-a": "database", "library/paper-b": "algorithms" });
});

test("legacy reserved discussions resume without claiming a course workspace", async t => {
  const { root, native, bridge, sessions } = fixture(t);
  for (const [id, workspace] of [["old-a", "database"], ["old-b", "algorithms"]]) {
    bridge.state.requests[id] = { ...question(id, undefined, id),
      session_id: `session-${id}`, status: "reserved", workspace_id: workspace };
  }
  bridge.save();
  const restarted = new Bridge(root, native);
  await restarted.open("old-a");
  await restarted.open("old-b");
  assert.equal(sessions.get("session-old-a"), "database");
  assert.equal(sessions.get("session-old-b"), "algorithms");
  assert.deepEqual(restarted.state.courses, {});
});
