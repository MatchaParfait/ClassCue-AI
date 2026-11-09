import { afterEach, expect, it, vi } from "vitest";
import { searchSubtitles, getSearchPreviewUrl, getRecentJobs, uploadSecondaryView, startSecondaryAlignment } from "./client";
import { getAiSettings, saveAiSettings } from "./client";
import { createManualQuestion, renameManualQuestion, getAsrModels, getAsrModelStatus, getStorageDirectories, saveStorageDirectories, updateMarkerJumpTime, updateMarkerImportance, updateQuestionEmphasis, getMarkers, previewBatch, getLatestBatch, retryBatch, getThumbnailUrl, uploadVideo, processSelectedVideos } from "./client";

afterEach(() => vi.unstubAllGlobals());

it("guards automatic secondary alignment with the imported association while preserving manual requests", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ job_id: "alignment", status: "queued" }) });
  vi.stubGlobal("fetch", fetch);
  await startSecondaryAlignment(1, { video_id: 2, offset_ms: 5000 });
  expect(fetch).toHaveBeenLastCalledWith("/api/videos/1/secondary-view/alignment", expect.objectContaining({
    method: "POST", headers: expect.objectContaining({ "Content-Type": "application/json" }),
    body: JSON.stringify({ expected_secondary_video_id: 2, expected_offset_ms: 5000 }),
  }));
  await startSecondaryAlignment(1);
  expect(fetch.mock.calls[1][1].body).toBeUndefined();
});

it("normalizes recent tasks with the same progress and identity rules as individual tasks", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => [
    { job_id: "slides", status: "running", progress: .45, job_type: "slide_extract", video_name: "Lesson" },
  ] });
  vi.stubGlobal("fetch", fetch);
  expect(await getRecentJobs()).toEqual([
    { id: "slides", job_id: "slides", status: "running", progress: 45, job_type: "slide_extract", video_name: "Lesson" },
  ]);
  expect(fetch).toHaveBeenCalledWith("/api/jobs/recent?limit=20", expect.anything());
});

it("normalizes legacy API concurrency and sends the saved value to the backend", async () => {
  const profile = { base_url: "https://relay.test/v1", model: "test", max_tokens: 2048 };
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => profile });
  vi.stubGlobal("fetch", fetch);
  const legacy = await getAiSettings();
  expect(legacy.request_concurrency).toBe(1);
  await saveAiSettings({ ...legacy, request_concurrency: 6 });
  expect(JSON.parse(fetch.mock.calls[1][1].body).request_concurrency).toBe(6);
  fetch.mockResolvedValue({ ok: true, json: async () => ({ ...profile, request_concurrency: 6 }) });
  expect((await getAiSettings()).request_concurrency).toBe(6);
});

it("preserves active model jobs through normalization and clears finished job identifiers", async () => {
  const model = { id: "paraformer-large-cpu", engine: "paraformer", active_job_id: "existing",
    active_job_kind: "preparing" };
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => [model] });
  vi.stubGlobal("fetch", fetch);
  expect((await getAsrModels())[0]).toMatchObject({ active_job_id: "existing", active_job_kind: "preparing" });
  fetch.mockResolvedValue({ ok: true, json: async () => ({ ...model, active_job_id: null, active_job_kind: null }) });
  expect(await getAsrModelStatus(model.id)).toMatchObject({ active_job_id: null, active_job_kind: null });
});

it("scopes preview frames to the hit time and library", () => {
  const url = new URL(getSearchPreviewUrl(7, 1877520, "library + &"), "http://local");
  expect(url.pathname).toBe("/api/videos/7/preview-frame");
  expect(url.searchParams.get("time_ms")).toBe("1877520");
  expect(url.searchParams.get("library_id")).toBe("library + &");
});

it("encodes literal subtitle searches and forwards cancellation with pagination", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ items: [] }) });
  vi.stubGlobal("fetch", fetch);
  const signal = new AbortController().signal;
  await searchSubtitles("  分治 + % &  ", 20, signal);
  const [url, options] = fetch.mock.calls[0];
  expect(new URL(url, "http://local").searchParams.get("q")).toBe("分治 + % &");
  expect(url).toContain("offset=20");
  expect(options.signal).toBe(signal);
  expect(new URL(url, "http://local").searchParams.has("preferred_video_id")).toBe(false);
  await searchSubtitles("分治", 40, signal, 7);
  const preferred = new URL(fetch.mock.calls[1][0], "http://local");
  expect(preferred.searchParams.get("preferred_video_id")).toBe("7");
  expect(preferred.searchParams.get("offset")).toBe("40");
  expect(preferred.searchParams.has("video_id")).toBe(false);
  expect(preferred.searchParams.has("folder_id")).toBe(false);
  await searchSubtitles("分治", 20, signal, undefined, 12);
  const folder = new URL(fetch.mock.calls[2][0], "http://local");
  expect(folder.searchParams.get("folder_id")).toBe("12");
  expect(folder.searchParams.get("offset")).toBe("20");
  expect(folder.searchParams.has("preferred_video_id")).toBe(false);
});

it("starts a durable batch from explicit library video IDs without importing folders", async () => {
  const result = { job_id: "selected-batch", status: "queued", total: 2 };
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => result });
  vi.stubGlobal("fetch", fetch);
  expect(await processSelectedVideos([7, 3], { analysis_mode: "low_recall" })).toEqual(result);
  expect(fetch).toHaveBeenCalledWith("/api/batch/process", expect.objectContaining({
    method: "POST", body: JSON.stringify({ analysis_mode: "low_recall", video_ids: [7, 3] }),
  }));
});

it("passes logical folder IDs with individual video IDs to the existing recursive batch endpoint", async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ job_id: "folder-batch", total: 25 }) });
  vi.stubGlobal("fetch", fetch);
  await processSelectedVideos([3], { folder_ids: [7, 8], analysis_mode: "medium_recall" });
  expect(fetch).toHaveBeenCalledWith("/api/batch/process", expect.objectContaining({
    method: "POST", body: JSON.stringify({ folder_ids: [7, 8], analysis_mode: "medium_recall", video_ids: [3] }),
  }));
});

it("uploads actual browser file bytes with an encoded filename and normalizes the result", async () => {
  const file = new File(["fixture"], "课堂 #1.mp4", { type: "video/mp4" });
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    video: { id: 3, filename: file.name, duration_ms: "1000" }, existed: false,
  }) });
  vi.stubGlobal("fetch", fetch);
  expect(await uploadVideo(file)).toMatchObject({ video: { id: 3, display_name: file.name, duration_ms: 1000 }, existed: false });
  expect(fetch).toHaveBeenCalledWith(`/api/videos/upload?filename=${encodeURIComponent(file.name)}`, {
    method: "POST", headers: { Accept: "application/json", "Content-Type": "application/octet-stream" }, body: file,
  });
});

it("uploads a secondary recording with its parent in one request", async () => {
  const file = new File(["secondary"], "camera #2.mp4", { type: "video/mp4" });
  const secondary = { video_id: 3, offset_ms: 0, filename: file.name, duration_ms: 1000 };
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    video: { id: 3, filename: file.name, duration_ms: "1000" }, secondary_view: secondary,
  }) });
  vi.stubGlobal("fetch", fetch);
  expect(await uploadSecondaryView(7, file)).toMatchObject({
    secondary_view: secondary, video: { id: 3, duration_ms: 1000 },
  });
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch).toHaveBeenCalledWith(
    `/api/videos/upload?filename=${encodeURIComponent(file.name)}&secondary_for=7`,
    expect.objectContaining({ method: "POST", body: file }),
  );
});

it("versions thumbnail URLs by library and source, not processing state", () => {
  const video = { id: 2, display_name: "Lesson", filename: "lesson.mp4", duration_ms: 1000,
    video_path: "E:\\Videos\\lesson.mp4", status: "pending", library_id: "a", source_size: 500 };
  const url = getThumbnailUrl(video);
  expect(url).toContain("/api/videos/2/thumbnail?v=");
  expect(JSON.parse(decodeURIComponent(url.split("?v=")[1]))[0]).toBe("native-jpeg444-q2-v2");
  expect(getThumbnailUrl({ ...video, status: "review_ready" })).toBe(url);
  expect(getThumbnailUrl({ ...video, library_id: "b" })).not.toBe(url);
  expect(getThumbnailUrl({ ...video, source_mtime_ns: 100 })).not.toBe(url);
});

it.each(["standard", "important", "critical"] as const)("persists %s importance with a dedicated field-only patch", async (importance) => {
  const marker = { id: 9, importance };
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => marker });
  vi.stubGlobal("fetch", fetch);
  expect(await updateMarkerImportance(9, importance)).toEqual(marker);
  expect(fetch).toHaveBeenCalledWith("/api/markers/9/importance", expect.objectContaining({
    method: "PATCH", body: JSON.stringify({ importance }),
  }));
});

it("defaults old markers to standard importance and preserves saved importance", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => [
    { id: 1, video_id: 1, start_ms: 0, end_ms: 1000 },
    { id: 2, video_id: 1, start_ms: 1000, end_ms: 2000, importance: "critical" },
  ] }));
  expect((await getMarkers(1)).value.map((marker) => marker.importance)).toEqual(["standard", "critical"]);
});

it.each([true, false, null])("saves question emphasis %s without sending other marker edits", async (emphasized) => {
  const marker = { id: 9, question_emphasized: emphasized ?? true,
    question_emphasis_override: emphasized === null ? null : Number(emphasized) };
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => marker });
  vi.stubGlobal("fetch", fetch);
  expect(await updateQuestionEmphasis(9, emphasized)).toEqual(marker);
  expect(fetch).toHaveBeenCalledWith("/api/markers/9/question-emphasis", expect.objectContaining({
    method: "PATCH", body: JSON.stringify({ emphasized }),
  }));
});

it("keeps automatic emphasis and explicit manual cancellation distinct", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => [
    { id: 1, question_emphasized: true, question_emphasis_override: null },
    { id: 2, question_emphasized: false, question_emphasis_override: 0 },
  ] }));
  expect((await getMarkers(1)).value.map(marker => [marker.question_emphasized, marker.question_emphasis_override]))
    .toEqual([[true, null], [false, false]]);
});

it("creates an independent manual point using only the captured playback time", async () => {
  const marker = { id: 9, start_ms: 150_425, analysis_mode: "question_extraction" };
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => marker });
  vi.stubGlobal("fetch", fetch);
  expect(await createManualQuestion(7, 150_425)).toEqual(marker);
  expect(fetch).toHaveBeenCalledWith("/api/videos/7/manual-questions", expect.objectContaining({
    method: "POST", body: JSON.stringify({ time_ms: 150_425 }),
  }));
});

it("renames a manual point without submitting its timestamps or status", async () => {
  const marker = { id: 9, title: "Updated" };
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => marker });
  vi.stubGlobal("fetch", fetch);
  expect(await renameManualQuestion(9, "Updated")).toEqual(marker);
  expect(fetch).toHaveBeenCalledWith("/api/markers/9/title", expect.objectContaining({
    method: "PATCH", body: JSON.stringify({ title: "Updated" }),
  }));
});
it("checks folders without starting a job and retries by durable batch id", async () => {
  const preview = { valid: true, folders: [] };
  const saved = { id: "batch", status: "awaiting_api", progress: 1, batch_summary: { total: 2, awaiting_ai: 2 } };
  const fetch = vi.fn()
    .mockResolvedValueOnce({ ok: true, json: async () => preview })
    .mockResolvedValueOnce({ ok: true, json: async () => saved })
    .mockResolvedValueOnce({ ok: true, json: async () => ({ ...saved, id: "retry", status: "queued", progress: 0 }) });
  vi.stubGlobal("fetch", fetch);
  expect(await previewBatch(["D:\\course"])).toEqual(preview);
  expect(fetch).toHaveBeenLastCalledWith("/api/batch/preview", expect.objectContaining({
    method: "POST", body: JSON.stringify({ folders: ["D:\\course"] }),
  }));
  expect(await getLatestBatch()).toEqual({ ...saved, job_id: "batch", progress: 100 });
  expect(await retryBatch("batch")).toMatchObject({ id: "retry", status: "queued", progress: 0 });
  expect(fetch).toHaveBeenLastCalledWith("/api/jobs/batch/retry", expect.objectContaining({ method: "POST" }));
});

it("handles a library without any previous batch", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => null }));
  expect(await getLatestBatch()).toBeNull();
});

it("reads and persists directory preferences via the dedicated API", async () => {
  const values = { asr_storage_root: "D:\\assets", audio_cache_dir: "E:\\audio", log_dir: "E:\\logs", library_id: "test-library", video_library_dir: "E:\\course\\videos" };
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => values });
  vi.stubGlobal("fetch", fetch);
  expect(await getStorageDirectories()).toEqual(values);
  expect(fetch).toHaveBeenCalledWith("/api/settings/directories", expect.objectContaining({ headers: { Accept: "application/json" } }));
  expect(await saveStorageDirectories(values)).toEqual(values);
  expect(fetch).toHaveBeenLastCalledWith("/api/settings/directories", expect.objectContaining({
    method: "PUT", body: JSON.stringify(values),
  }));
});
it.each([0, 450000, null])("sends jump time %s through the dedicated endpoint", async (time) => {
  const marker = { id: 9, start_ms: 521000, user_start_ms: time };
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => marker });
  vi.stubGlobal("fetch", fetch);
  expect(await updateMarkerJumpTime(9, time)).toEqual(marker);
  expect(fetch).toHaveBeenCalledWith("/api/markers/9/jump-time", expect.objectContaining({
    method: "PATCH", body: JSON.stringify({ user_start_ms: time }),
  }));
});

it.each([false, true])("filters retired models from the ASR catalogue (wrapped=%s)", async (wrapped) => {
  const models = [
    { id: "fun-asr-nano-2512", name: "Fun-ASR-Nano", engine: "nano", selected: true },
    { id: "dolphin-small-cn", name: "Dolphin", engine: "dolphin" },
    { id: "disabled-model", name: "Disabled", engine: "other", selectable: false },
    { id: "paraformer-large-cpu", name: "Paraformer-large", engine: "paraformer", selectable: true },
  ];
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: true,
    json: async () => wrapped ? { models, selected_model_id: "fun-asr-nano-2512" } : models,
  }));
  const result = await getAsrModels();
  expect(result.map((model) => model.id)).toEqual(["dolphin-small-cn", "paraformer-large-cpu"]);
  expect(result.some((model) => model.selected)).toBe(false);
});

it("preserves unknown ASR observations instead of reporting unprepared or zero bytes", async () => {
  const fetch = vi.fn().mockResolvedValue({
    ok: true, json: async () => ({
      id: "dolphin-small-cn", downloaded: null, cache_size_bytes: null,
      inspection_status: "unknown", status_checked_at: null,
    }),
  });
  vi.stubGlobal("fetch", fetch);
  const model = await getAsrModelStatus("dolphin-small-cn", { full: false });
  expect(model.downloaded).toBeNull();
  expect(model.cache_size_bytes).toBeNull();
  expect(model.inspection_status).toBe("unknown");
  expect(fetch).toHaveBeenLastCalledWith("/api/asr/models/dolphin-small-cn/status?full=false", expect.anything());
  await getAsrModelStatus("dolphin-small-cn", { full: true, refresh: true });
  expect(fetch).toHaveBeenLastCalledWith("/api/asr/models/dolphin-small-cn/status?full=true&refresh=true", expect.anything());
});
