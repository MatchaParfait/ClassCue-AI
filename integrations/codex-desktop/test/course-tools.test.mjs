import test from "node:test";
import assert from "node:assert/strict";
import { courseTools, callCourseTool } from "../course-tools.mjs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("course tools are independent from bound discussion metadata", () => {
  assert.deepEqual(courseTools.map(t => t.name), ["list_courses", "list_videos", "search_subtitles",
    "read_transcript", "open_video", "start_transcription", "start_processing_sequence", "preview_batch",
    "start_batch_processing", "retry_batch", "resume_batch", "get_job", "cancel_job",
    "list_slides", "read_slide", "read_video_frame", "read_analysis", "list_jobs", "get_processing_status"]);
  for (const tool of courseTools) {
    assert.equal(tool.inputSchema.additionalProperties, false);
    assert.equal(tool.annotations.openWorldHint,
      ["start_batch_processing", "retry_batch", "resume_batch"].includes(tool.name));
    assert.equal(tool.outputSchema.type, "object");
  }
  assert.equal(courseTools.find(t => t.name === "start_transcription").annotations.readOnlyHint, false);
  assert.equal(courseTools.find(t => t.name === "search_subtitles").annotations.readOnlyHint, true);
});

test("missing connection never guesses a library or launches a program", async t => {
  const home = await mkdtemp(join(tmpdir(), "classcue-unconnected-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  await assert.rejects(callCourseTool("list_courses", {}, { CODEX_HOME: home }), /尚未连接.*设置/);
  await assert.rejects(callCourseTool("list_courses", {}, { CLASSCUE_CODEX_CONNECTION_FILE: "relative.json" }),
    /设置无效/);
});
