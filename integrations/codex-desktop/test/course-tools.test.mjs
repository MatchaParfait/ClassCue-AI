import test from "node:test";
import assert from "node:assert/strict";
import { courseTools, legacyCourseTools, callCourseTool } from "../course-tools.mjs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

test("course tools are independent from bound discussion metadata", () => {
  assert.deepEqual(courseTools.map(t => t.name), ["list_courses", "list_videos", "search_library",
    "get_semantic_status", "start_semantic_prepare", "start_semantic_setup", "start_semantic_index",
    "read_transcript", "open_video", "start_transcription", "start_processing_sequence", "preview_batch",
    "start_batch_processing", "retry_batch", "resume_batch", "get_job", "cancel_job",
    "list_slides", "read_slide", "read_video_frame", "read_analysis", "list_jobs", "get_processing_status"]);
  for (const tool of courseTools) {
    assert.equal(tool.inputSchema.additionalProperties, false);
    assert.equal(tool.annotations.openWorldHint,
      ["start_batch_processing", "retry_batch", "resume_batch",
        "start_semantic_prepare", "start_semantic_setup"].includes(tool.name));
    if (["read_slide", "read_video_frame"].includes(tool.name))
      assert.equal(tool.outputSchema, undefined);
    else assert.equal(tool.outputSchema.type, "object");
  }
  assert.equal(courseTools.find(t => t.name === "start_transcription").annotations.readOnlyHint, false);
  for (const name of ["start_semantic_prepare", "start_semantic_setup"]) {
    const semantic = courseTools.find(t => t.name === name);
    assert.equal(semantic.annotations.readOnlyHint, false);
    assert.equal(semantic.annotations.idempotentHint, true);
    assert.deepEqual(semantic.inputSchema.required, ["request_id"]);
    assert.deepEqual(semantic.inputSchema.properties.device.enum, ["cpu", "cuda"]);
    assert.equal(semantic.inputSchema.properties.device.default, undefined);
  }
  assert.deepEqual(courseTools.find(t => t.name === "start_semantic_setup").inputSchema.anyOf,
    [{ required: ["course_id"] }, { required: ["video_id"] }]);
  const unified = courseTools.find(t => t.name === "search_library");
  assert.equal(unified.annotations.readOnlyHint, true);
  assert.deepEqual(unified.outputSchema.properties.match_type.enum,
    ["literal_passage", "phonetic_passage", "semantic_passage", "hybrid_passage"]);
  assert.deepEqual(unified.inputSchema.properties.phonetic_mode.enum, ["auto", "off", "always"]);
  assert.equal(unified.inputSchema.properties.phonetic_mode.default, "auto");
  assert.ok(unified.outputSchema.required.includes("phonetic_index_pending"));
  const searchEvidence = unified.outputSchema.properties.items.items.properties.evidence.items;
  assert.equal(searchEvidence.properties.phonetic_matches.maxItems, 8);
  assert.equal(searchEvidence.properties.phonetic_matches.items.properties.requires_verification.const, true);
  assert.deepEqual(unified.inputSchema.properties.sources.default,
    ["catalog", "bookmarks", "subtitles", "analysis"]);
  assert.equal(unified.outputSchema.properties.items.items.properties.evidence.maxItems, 6);
  assert.deepEqual(legacyCourseTools.map(tool => tool.name), ["search_bookmarks", "search_subtitles"]);
  const bookmarks = legacyCourseTools.find(t => t.name === "search_bookmarks");
  assert.equal(bookmarks.annotations.readOnlyHint, true);
  assert.deepEqual(bookmarks.inputSchema.required, []);
  assert.equal(bookmarks.outputSchema.properties.evidence_type.const, "user_bookmark");
  assert.deepEqual(bookmarks.outputSchema.properties.items.items.properties.position_seconds.type,
    ["number", "null"]);
});

test("missing connection never guesses a library or launches a program", async t => {
  const home = await mkdtemp(join(tmpdir(), "classcue-unconnected-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  await assert.rejects(callCourseTool("list_courses", {}, { CODEX_HOME: home }), /尚未连接.*设置/);
  await assert.rejects(callCourseTool("list_courses", {}, { CLASSCUE_CODEX_CONNECTION_FILE: "relative.json" }),
    /设置无效/);
});
