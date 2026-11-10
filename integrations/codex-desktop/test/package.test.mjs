import test from "node:test";
import assert from "node:assert/strict";
import { cp, mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { readConnection } from "../connection.mjs";

test("shipped MCP works without node_modules and rereads APP setup without restart", { timeout: 20000 }, async t => {
  const root = await mkdtemp(join(tmpdir(), "classcue-package-"));
  const plugin = join(root, "plugin"), home = join(root, "codex-home"), workspace = join(root, "workspace");
  await mkdir(plugin);
  await mkdir(workspace);
  for (const name of ["server.bundle.mjs", "discussion.html"])
    await cp(fileURLToPath(new URL(`../${name}`, import.meta.url)), join(plugin, name));
  const transport = new StdioClientTransport({ command: process.execPath,
    args: [join(plugin, "server.bundle.mjs")], cwd: root,
    env: { CODEX_HOME: home }, stderr: "ignore" });
  const client = new Client({ name: "synthetic-package-test", version: "1" });
  t.after(async () => { await client.close(); await rm(root, { recursive: true, force: true }); });
  await client.connect(transport);
  assert.equal(client.getServerVersion().version, "0.11.0");
  const tools = (await client.listTools()).tools;
  assert.equal(tools.length, 23);
  assert.equal(tools.find(tool => tool.name === "search_library").annotations.readOnlyHint, true);
  assert.ok(!tools.some(tool => ["search_bookmarks", "search_subtitles"].includes(tool.name)));
  const unconnected = await client.callTool({ name: "list_courses", arguments: {} });
  assert.equal(unconnected.isError, true);
  assert.match(unconnected.content[0].text, /尚未连接.*设置/);
  const resource = await client.readResource({ uri: "ui://classcue/discussion.html" });
  assert.match(resource.contents[0].text, /<main id="preview"/);
  assert.doesNotMatch(resource.contents[0].text, /CLASSCUE_SCRIPT|CLASSCUE_ICON/);
  const contextDirectory = join(workspace, ".classcue", "discussions", "a".repeat(32));
  await mkdir(contextDirectory, { recursive: true });
  const file = join(contextDirectory, "context.json");
  await writeFile(file, JSON.stringify({ format: "classcue-codex-context", version: 1,
    request_id: "a".repeat(32), content: [{ type: "text", text: "合成原题" }] }));
  await mkdir(join(home, "classcue"), { recursive: true });
  await writeFile(join(home, "classcue", "connection.json"), JSON.stringify({
    format: "classcue-codex-connection", version: 1, roots: [workspace] }));
  const result = await client.callTool({ name: "read_discussion_context", arguments: { context_file: file } });
  assert.notEqual(result.isError, true);
  assert.equal(result.content[0].text, "合成原题");
});

test("explicit connection override never falls back to a different home", async t => {
  const home = await mkdtemp(join(tmpdir(), "classcue-config-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  await mkdir(join(home, "classcue"));
  await writeFile(join(home, "classcue", "connection.json"), JSON.stringify({
    format: "classcue-codex-connection", version: 1, roots: [home] }));
  assert.deepEqual((await readConnection({ CODEX_HOME: home })).roots, [home]);
  assert.deepEqual((await readConnection({
    PLUGIN_ROOT: join(home, "plugins", "cache", "classcue", "classcue", "0.11.0"),
  })).roots, [home]);
  await assert.rejects(readConnection({ PLUGIN_ROOT: join(home, "unknown-layout") }), /无法确定/);
  await assert.rejects(readConnection({ CODEX_HOME: home,
    CLASSCUE_CODEX_CONNECTION_FILE: join(home, "missing.json") }), /尚未连接/);
  await assert.rejects(readConnection({ CODEX_HOME: "relative" }), /设置无效/);
});
