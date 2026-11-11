import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, access, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { callCourseTool } from "../course-tools.mjs";

const realTimeout = globalThis.setTimeout;
const pause = ms => new Promise(resolve => realTimeout(resolve, ms));

for (const [name, args, semantic, expires] of [
  ["search_library", { semantic_query: "操作能否依次执行" }, true],
  ["search_library", { query: "事务", semantic_query: "操作能否依次执行" }, true],
  ["search_library", { semantic_query: "操作能否依次执行" }, true, true],
  ["search_library", { query: "事务" }, false],
  ["list_courses", {}, false],
]) {
  test(`${name} ${JSON.stringify(args)} ${expires ? "expires" : "preserves its deadline"}`,
    { timeout: 10000 }, async t => {
      const root = await mkdtemp(join(tmpdir(), "classcue-course-timeout-"));
      const ready = join(root, "ready"), release = join(root, "release");
      await mkdir(join(root, "scripts"));
      // A real child speaks the bridge protocol; only the parent's timeout clock is mocked.
      await writeFile(join(root, "scripts", "codex_course_tools.py"), `
const fs = require("node:fs");
process.stdin.resume();
process.stdin.once("end", () => {
  fs.writeFileSync("ready", "");
  const timer = setInterval(() => {
    if (!fs.existsSync("release")) return;
    clearInterval(timer);
    process.stdout.write(JSON.stringify({ result: { completed: true } }));
  }, 5);
});
`);
      const connection = join(root, "connection.json");
      await writeFile(connection, JSON.stringify({
        format: "classcue-codex-connection", version: 1, roots: [root],
        application: { project_root: root, python: process.execPath },
      }));
      t.mock.timers.enable({ apis: ["setTimeout"] });
      const pending = callCourseTool(name, args, {
        CLASSCUE_CODEX_CONNECTION_FILE: connection,
      }).then(value => ({ value }), error => ({ error: error.message }));
      t.after(async () => {
        await writeFile(release, "");
        await pending;
        t.mock.timers.reset();
        await rm(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 20 });
      });
      const deadline = Date.now() + 5000;
      while (true) {
        try { await access(ready); break; }
        catch {
          assert.ok(Date.now() < deadline, "synthetic bridge child did not start");
          await pause(10);
        }
      }
      t.mock.timers.tick(expires ? 210001 : 150001);
      await writeFile(release, "");
      const result = await pending;
      if (semantic && !expires) assert.deepEqual(result.value, { completed: true });
      else assert.match(result.error, /本地工具调用超时/);
    });
}
