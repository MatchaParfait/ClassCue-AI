import test from "node:test";
import assert from "node:assert/strict";
import { courseTools, callCourseTool } from "../course-tools.mjs";

test("course tools are independent from bound discussion metadata", () => {
  assert.deepEqual(courseTools.map(t => t.name), ["list_courses", "list_videos", "search_subtitles",
    "read_transcript", "open_video", "start_transcription", "start_processing_sequence", "preview_batch",
    "start_batch_processing", "retry_batch", "resume_batch", "get_job", "cancel_job"]);
  for (const tool of courseTools) {
    assert.equal(tool.inputSchema.additionalProperties, false);
    assert.equal(tool.annotations.openWorldHint,
      ["start_batch_processing", "retry_batch", "resume_batch"].includes(tool.name));
    assert.equal(tool.outputSchema.type, "object");
  }
  assert.equal(courseTools.find(t => t.name === "start_transcription").annotations.readOnlyHint, false);
  assert.equal(courseTools.find(t => t.name === "search_subtitles").annotations.readOnlyHint, true);
});

test("missing connection never guesses a library or launches a program", async () => {
  await assert.rejects(callCourseTool("list_courses", {}, {}), /重新安装/);
  await assert.rejects(callCourseTool("list_courses", {}, { CLASSCUE_CODEX_CONNECTION_FILE: "relative.json" }),
    /重新安装/);
});
