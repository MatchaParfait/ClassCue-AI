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
  const sequenceRequest = { ...args, steps: ["transcribe", "extract_slides"],
    request_id: "stdio-sequence-request-001" };
  const sequence = await call("start_processing_sequence", sequenceRequest);
  assert.equal(sequence.isError, undefined);
  let sequenceState = sequence.structuredContent;
  for (let attempt = 0; attempt < 60 && ["queued", "running"].includes(sequenceState.status); attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
    sequenceState = (await call("get_job", { job_id: sequenceState.job_id })).structuredContent;
  }
  evidence.sequenceCompleted = sequenceState.status === "completed"
    && sequenceState.entries[0].steps[0].status === "skipped"
    && sequenceState.entries[0].steps[1].count === 2;
  const sequenceReplay = await call("start_processing_sequence", sequenceRequest);
  evidence.sequenceReused = sequenceReplay.structuredContent.reused
    && sequenceReplay.structuredContent.job_id === sequenceState.job_id;
  const search = await call("search_subtitles", { video_id: Number(videoId), queries: ["会考", "掌握"] });
  assert.equal(search.isError, undefined);
  const hit = search.structuredContent.items[0];
  const transcript = await call("read_transcript", { video_id: Number(videoId), ...hit.context_range });
  evidence.retrieval = search.structuredContent.total_matches === 1
    && transcript.structuredContent.items.length === 1;
  const slideList = await call("list_slides", { video_id: Number(videoId), start_seconds: 0, end_seconds: 1 });
  const slide = await call("read_slide", { video_id: Number(videoId),
    slide_id: slideList.structuredContent.items[0].slide_id });
  const frame = await call("read_video_frame", { video_id: Number(videoId), start_seconds: 0.2 });
  for (const result of [slide, frame]) {
    assert.equal(result.isError, undefined);
    assert.equal(result.content[1].type, "image");
    assert.equal(result.content[1].mimeType, "image/jpeg");
    assert.ok(Buffer.from(result.content[1].data, "base64").subarray(0, 3).equals(Buffer.from("ffd8ff", "hex")));
    assert.equal(result.structuredContent.image.data, undefined);
    assert.ok(!result.content[0].text.includes(result.content[1].data));
  }
  evidence.nativeImages = true;
  const analysis = await call("read_analysis", { video_id: Number(videoId) });
  evidence.analysis = analysis.isError === undefined && analysis.structuredContent.analysis_runs.length > 0;
  const jobs = await call("list_jobs", { video_id: Number(videoId) });
  evidence.jobDiscovery = jobs.structuredContent.items.some(item => item.job_id === sequenceState.job_id);
  const status = await call("get_processing_status", {});
  evidence.processingStatus = status.structuredContent.service_status === "running"
    && status.structuredContent.ai_connection.credential_checked === false;
  assert.ok(Object.entries(evidence).every(([key, value]) => key === "version" || value === true));
  console.log(JSON.stringify(evidence));
} finally {
  await client.close();
}
