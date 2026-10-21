import { expect, it } from "vitest";
import type { Job, Video } from "../types";
import { videoFileChangeBlockReason } from "./useTaskStatus";

const video: Video = { id: 2, filename: "lesson.mp4", display_name: "Lesson", video_path: "lesson.mp4",
  duration_ms: 1000, status: "review_ready" };

it.each(["queued", "running", "resource_paused"])("locks only the %s task's video or batch members", status => {
  const job: Job = { id: "task", status, video_id: 1 };
  expect(videoFileChangeBlockReason(video, [job])).toBeNull();
  expect(videoFileChangeBlockReason(video, [{ ...job, video_id: 2 }])).toBeTruthy();
  expect(videoFileChangeBlockReason(video, [{ ...job, job_type: "batch_video_process",
    batch_entries: [{ video_id: 2, status: "pending_asr" }] }])).toBeTruthy();
  expect(videoFileChangeBlockReason(video, [{ ...job, job_type: "storage_migration" }])).toContain("目录正在迁移");
});

it.each(["completed", "failed", "interrupted", "awaiting_api"])("does not lock files for %s tasks", status => {
  expect(videoFileChangeBlockReason(video, [{ id: "task", status, video_id: 2 }])).toBeNull();
});

it("honors all paused queues, not just the task currently displayed", () => {
  expect(videoFileChangeBlockReason(video, [
    { id: "active", video_id: 1, status: "running" },
    { id: "paused", status: "resource_paused", batch_entries: [{ video_id: 2, status: "pending_ai" }] },
  ])).toContain("暂停");
});

it("uses authoritative server restrictions and falls back to status for older servers", () => {
  expect(videoFileChangeBlockReason({ ...video, file_change_block_reason: "目录迁移中" }, [])).toBe("目录迁移中");
  expect(videoFileChangeBlockReason({ ...video, status: "processing" }, [])).toBeTruthy();
  expect(videoFileChangeBlockReason({ ...video, status: "resource_paused" }, [])).toBeTruthy();
  expect(videoFileChangeBlockReason({ ...video, status: "resource_paused", file_change_block_reason: null }, [])).toBeNull();
});
