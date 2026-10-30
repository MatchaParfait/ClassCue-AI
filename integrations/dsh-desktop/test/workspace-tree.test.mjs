import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readdirSync, realpathSync, rmSync, renameSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, basename } from "node:path";
import test from "node:test";
import { Bridge } from "../bridge.mjs";

function fixture(t) {
  const home = mkdtempSync(join(tmpdir(), "classcue-tree-"));
  t.after(() => rmSync(home, { recursive: true, force: true }));
  const root = join(home, "classcue-AI-DSH");
  mkdirSync(root);
  const workspaces = new Map(), sessions = new Map();
  const native = {
    workspaces: () => [...workspaces.values()],
    createWorkspace: async (path, title) => {
      path = realpathSync(path);
      const found = [...workspaces.values()].find(w => w.path === path);
      if (found) return found;
      const row = { workspace_id: `workspace-${workspaces.size}`, path, title: title ?? basename(path) };
      workspaces.set(row.workspace_id, row);
      return row;
    },
    ensureSession: async (session, workspace) => { sessions.set(session, workspace); },
    check: async (session, workspace) => {
      if (!sessions.has(session)) throw Object.assign(new Error("missing"), { code: "session_missing" });
      assert.equal(sessions.get(session), workspace);
    },
    flush: async () => {},
  };
  const bridge = new Bridge(join(home, "bridge"), native);
  const configure = (config_id = "root", path = root) =>
    bridge.configureRoot({ library_id: "library", config_id, path });
  const question = (request, course = "database", document = "spring", version = 1) => ({
    request_id: request, library_id: "library", course_id: course, course_name: course === "database" ? "数据库" : "算法",
    document_id: document, question_id: `${course}-${document}-${request}`, content_version: version,
    source: "模拟试卷.md", title: `第${request}题`, layout: "workspace_tree", root_id: "root",
    content: [{ type: "text", text: "共同情景（1）（2）" }],
  });
  return { home, root, bridge, native, workspaces, sessions, configure, question };
}

test("one root registers two courses, two papers each, and independent question sessions", async t => {
  const f = fixture(t);
  const configured = await f.configure();
  for (const course of ["database", "algorithms"]) {
    for (const doc of ["spring", "autumn"]) {
      const first = await f.bridge.prepare({ ...f.question("1", course, doc), request_id: `${course}-${doc}-1` });
      const second = await f.bridge.prepare({ ...f.question("2", course, doc), request_id: `${course}-${doc}-2` });
      assert.equal(first.workspace_id, second.workspace_id);
      assert.notEqual(first.session_id, second.session_id);
      const workspace = f.workspaces.get(first.workspace_id);
      assert.equal(dirname(dirname(workspace.path)), f.root);
      assert.equal(f.sessions.get(first.session_id), workspace.workspace_id);
    }
  }
  assert.equal(f.workspaces.size, 7);
  assert.equal(f.sessions.size, 8);
  assert.ok(f.workspaces.has(configured.workspace_id));
  const again = await f.bridge.prepare({ ...f.question("1"), request_id: "database-spring-1" });
  const restarted = new Bridge(join(f.home, "bridge"), f.native);
  assert.equal((await restarted.open(again.request_id)).session_id, again.session_id);
  assert.equal(f.sessions.size, 8);
});

test("same names, Windows-invalid names, source videos and renamed sources keep stable mappings", async t => {
  const f = fixture(t);
  await f.configure();
  const a = await f.bridge.prepare({ ...f.question("a"), course_name: "CON", source: "某次:试卷?.pdf" });
  const b = await f.bridge.prepare({ ...f.question("b", "database", "duplicate"), course_name: "改名", source: "某次:试卷?.md" });
  assert.equal(basename(dirname(a.cwd)), "_CON");
  assert.equal(basename(a.cwd), "某次_试卷_");
  assert.equal(basename(b.cwd), "某次_试卷_ (1)");
  const renamed = await f.bridge.prepare({ ...f.question("c"), course_name: "数据库概论", source: "新名称.pdf" });
  assert.equal(renamed.cwd, a.cwd);
  const video = await f.bridge.prepare({ ...f.question("video", "database", "video-1"), source: "第一讲.mp4" });
  assert.equal(basename(video.cwd), "第一讲");
  assert.equal(dirname(video.cwd), dirname(a.cwd));
});

test("root changes affect unbound documents; a bound document's new questions keep the original path", async t => {
  const f = fixture(t);
  await f.configure();
  const first = await f.bridge.prepare(f.question("first"));
  const nextRoot = join(f.home, "new-root");
  mkdirSync(nextRoot);
  await f.configure("next-root", nextRoot);
  const same = await f.bridge.prepare({ ...f.question("second"), root_id: "next-root" });
  assert.equal(same.cwd, first.cwd);
  assert.equal(same.root_id, "root");
  const other = await f.bridge.prepare({ ...f.question("third", "database", "new-paper"), root_id: "next-root" });
  assert.equal(dirname(dirname(other.cwd)), nextRoot);
  assert.equal((await f.bridge.prepare({ ...f.question("second"), root_id: "next-root" })).session_id, same.session_id);
});

test("partial registration, session creation and flush failures resume the persisted intent without duplicates", async t => {
  for (const stage of ["registration", "session", "flush"]) {
    await t.test(stage, async sub => {
      const f = fixture(sub);
      await f.configure();
      const method = stage === "registration" ? "createWorkspace" : stage === "session" ? "ensureSession" : "flush";
      const original = f.native[method];
      let failed = false;
      f.native[method] = async (...args) => {
        const value = await original(...args);
        if (!failed) { failed = true; throw new Error("synthetic timeout after write"); }
        return value;
      };
      await assert.rejects(f.bridge.prepare(f.question("first")), /synthetic timeout/);
      const restarted = new Bridge(join(f.home, "bridge"), f.native);
      const recovered = await restarted.open("first");
      assert.equal(recovered.status, "bound");
      assert.equal(f.workspaces.size, 3);
      assert.equal(f.sessions.size, 1);
      assert.deepEqual(readdirSync(f.root), ["数据库"]);
      assert.deepEqual(readdirSync(join(f.root, "数据库")), ["模拟试卷"]);
    });
  }
});

test("removed native nodes, missing directories and missing bound sessions report errors without recreating history", async t => {
  const f = fixture(t);
  await f.configure();
  const first = await f.bridge.prepare(f.question("first"));
  f.sessions.delete(first.session_id);
  await assert.rejects(f.bridge.open(first.request_id), e => e.code === "session_missing");
  assert.equal(f.sessions.size, 0);
  f.sessions.set(first.session_id, first.workspace_id);
  const workspace = f.workspaces.get(first.workspace_id);
  f.workspaces.delete(first.workspace_id);
  await assert.rejects(f.bridge.open(first.request_id), e => e.code === "workspace_missing");
  f.workspaces.set(workspace.workspace_id, workspace);
  renameSync(first.cwd, `${first.cwd}-moved`);
  await assert.rejects(f.bridge.open(first.request_id), e => e.code === "directory_unavailable");
  assert.equal(f.sessions.size, 1);
  assert.equal(readdirSync(dirname(first.cwd)).length, 1);
});

test("new versions and explicit independent discussions preserve old sessions and the document workspace", async t => {
  const f = fixture(t);
  await f.configure();
  const input = f.question("first");
  const first = await f.bridge.prepare(input);
  const revised = { ...input, request_id: "revised", content_version: 2, content: [{ type: "text", text: "修正题目" }] };
  await assert.rejects(f.bridge.prepare(revised), e => e.code === "question_changed");
  const second = await f.bridge.prepare({ ...revised, new_discussion: true });
  assert.equal(second.cwd, first.cwd);
  assert.notEqual(second.session_id, first.session_id);
  assert.equal((await f.bridge.open("first")).session_id, first.session_id);
  await assert.rejects(f.bridge.prepare({ ...input, root_id: "altered" }), e => e.code === "request_conflict");
});

test("root setup is idempotent, library scoped and rejects missing or relative paths", async t => {
  const f = fixture(t);
  const first = await f.configure();
  assert.deepEqual(await f.configure(), first);
  assert.equal(f.workspaces.size, 1);
  await assert.rejects(f.bridge.configureRoot({ config_id: "root", library_id: "other", path: f.root }),
    e => e.code === "root_conflict");
  await assert.rejects(f.configure("relative", "relative"), e => e.code === "directory_unavailable");
  await assert.rejects(f.configure("missing", join(f.home, "missing")), e => e.code === "directory_unavailable");
  await assert.rejects(f.bridge.prepare({ ...f.question("x"), root_id: "not-configured" }),
    e => e.code === "root_missing");
  assert.equal(f.sessions.size, 0);
});
