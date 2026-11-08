// Invoked by pytest against its isolated, authenticated synthetic HTTP service.
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";

const [connection, videoId] = process.argv.slice(2);
const client = new Client({ name: "batch-protocol-test", version: "1.0.0" });
const transport = new StdioClientTransport({
  command: process.execPath, args: [fileURLToPath(new URL("../server.mjs", import.meta.url))],
  env: { ...process.env, CLASSCUE_CODEX_CONNECTION_FILE: connection }, stderr: "ignore",
});
const evidence = {};
try {
  await client.connect(transport);
  evidence.version = client.getServerVersion().version;
  const listing = await client.listTools();
  const tool = listing.tools.find(item => item.name === "start_batch_processing");
  assert.equal(tool.annotations.openWorldHint, true);
  assert.equal(tool.annotations.idempotentHint, true);
  assert.equal(tool.annotations.readOnlyHint, false);
  assert.ok(tool.outputSchema.required.includes("job_id"));
  const args = { video_ids: [Number(videoId)] };
  const call = (name, arguments_) => client.callTool({ name, arguments: arguments_ });
  const preview = await call("preview_batch", args);
  assert.equal(preview.isError, undefined);
  assert.deepEqual(JSON.parse(preview.content[0].text), preview.structuredContent);
  evidence.preview = preview.structuredContent.valid;
  const request = { ...args, request_id: "stdio-batch-request-001" };
  const start = await call("start_batch_processing", request);
  assert.equal(start.isError, undefined);
  let state = start.structuredContent;
  for (let attempt = 0; attempt < 60 && ["queued", "running"].includes(state.status); attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
    state = (await call("get_job", { job_id: state.job_id })).structuredContent;
  }
  evidence.completed = state.status === "completed" && state.summary.completed === 1;
  const repeated = await call("start_batch_processing", request);
  evidence.reused = repeated.structuredContent.reused && repeated.structuredContent.job_id === state.job_id;
  const invalid = await call("start_batch_processing", { ...args, request_id: "bad" });
  evidence.validationError = invalid.isError === true;
  const conflict = await call("start_batch_processing", {
    ...request, options: { analysis_modes: ["high_recall"] },
  });
  evidence.requestConflict = conflict.isError === true;
  assert.ok(Object.entries(evidence).every(([key, value]) => key === "version" || value === true));
  console.log(JSON.stringify(evidence));
} finally {
  await client.close();
}
