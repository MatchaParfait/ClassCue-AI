import assert from "node:assert/strict";
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { readContext } from "../context.mjs";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { fileURLToPath } from "node:url";

const png = "iVBORw0KGgoAAAANSUhEUgAAAKAAAABgCAIAAAAVRe7OAAAA9klEQVR4nO3RAQkAIBDAQLV/5zeFCOMuwWB7ZhZd53cAbxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ3CcwXEGxxkcZ/Bqu3jXA71DjnAuAAAAAElFTkSuQmCC";
async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), "classcue-codex-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const directory = join(root, "course", "paper", ".classcue", "discussions", "a".repeat(32));
  await mkdir(directory, { recursive: true });
  const file = join(directory, "context.json");
  await writeFile(file, JSON.stringify({
    format: "classcue-codex-context", version: 1, request_id: "a".repeat(32), question_id: "q",
    content: [{ type: "text", text: "合成题目" }, { type: "image", media_type: "image/png", data: png }],
  }));
  return { root, file };
}

test("returns original text and ordered native MCP images", async t => {
  const { root, file } = await fixture(t);
  const result = await readContext(file, root);
  assert.deepEqual(result.content.map(part => part.type), ["text", "image"]);
  assert.equal(result.content[0].text, "合成题目");
  assert.equal(result.content[1].data, png);
});

test("lecture clips preserve subtitle text, image order and range metadata", async t => {
  const { root, file } = await fixture(t);
  const { readFile } = await import("node:fs/promises");
  const context = JSON.parse(await readFile(file, "utf8"));
  Object.assign(context, { source_kind: "video_clip", video_id: 9, start_ms: 1000, end_ms: 9000 });
  context.content[0].text = "00:00:01,000 --> 00:00:09,000\n教师合成字幕";
  await writeFile(file, JSON.stringify(context));
  const result = await readContext(file, root);
  assert.equal(result.metadata.source_kind, "video_clip");
  assert.equal(result.metadata.start_ms, 1000);
  assert.equal(result.metadata.end_ms, 9000);
  assert.match(result.content[0].text, /教师合成字幕/);
  assert.equal(result.content[1].data, png);
});

test("rejects files outside the selected root and non-context files", async t => {
  const { root, file } = await fixture(t);
  await assert.rejects(readContext(file, join(root, "other")));
  const arbitrary = join(root, "private.json");
  await writeFile(arbitrary, "{}");
  await assert.rejects(readContext(arbitrary, root), /上下文文件/);
  await assert.rejects(readContext(file, ""), /设置/);
});

test("rejects corrupt images before returning any content", async t => {
  const { root, file } = await fixture(t);
  await writeFile(file, JSON.stringify({ format: "classcue-codex-context", version: 1,
    content: [{ type: "image", media_type: "image/png", data: "ZXZpbA==" }] }));
  await assert.rejects(readContext(file, root), /题图格式/);
});

test("MCP initializes and reads text and images over real stdio", async t => {
  const { root, file } = await fixture(t);
  const transport = new StdioClientTransport({
    command: process.execPath,
    args: [fileURLToPath(new URL("../server.mjs", import.meta.url))],
    env: { ...process.env, CLASSCUE_CODEX_ROOT: root },
    stderr: "ignore",
  });
  const client = new Client({ name: "synthetic-test", version: "0.1.0" });
  t.after(() => client.close());
  await client.connect(transport);
  const tools = await client.listTools();
  assert.equal(tools.tools.length, 16);
  assert.equal(tools.tools[0].annotations.readOnlyHint, true);
  const result = await client.callTool({ name: "read_discussion_context", arguments: { context_file: file } });
  assert.equal(result.isError, undefined);
  assert.deepEqual(result.content.map(part => part.type), ["text", "image"]);
  const panel = tools.tools.find(tool => tool.name === "classcue_discussion");
  assert.deepEqual(panel._meta["openai/ui"].entrypoints, [{ type: "thread" }]);
  const resource = await client.readResource({ uri: panel._meta.ui.resourceUri });
  assert.match(resource.contents[0].text, /<main id="preview"/);
  assert.match(resource.contents[0].text, /data:image\/png;base64,/);
  assert.doesNotMatch(resource.contents[0].text, /CLASSCUE_SCRIPT|CLASSCUE_ICON/);
  const unbound = await client.callTool({ name: "classcue_discussion", arguments: {} });
  assert.equal(unbound.isError, true);
});
