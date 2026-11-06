import { useQuery } from "@tanstack/react-query";
import { getJob } from "../api/client";
import type { Job, Video } from "../types";

export function isTaskTerminal(job: Job) {
  return ["completed", "failed", "interrupted", "resource_paused", "awaiting_api", "cancelled", "canceled"].includes(job.status);
}

export function videoFileChangeBlockReason(video: Video, jobs: Job[]): string | null {
  for (const job of jobs) {
    if (!["queued", "running", "resource_paused"].includes(job.status)) continue;
    if (job.job_type === "storage_migration") return "目录正在迁移，请等待迁移完成后修改文件名";
    if (job.video_id === video.id || job.batch_entries?.some(entry => entry.video_id === video.id)) {
      return job.status === "resource_paused"
        ? "该视频被资源暂停任务占用，请继续或停止任务后修改文件名"
        : "该视频在处理任务或批次队列中，请完成或停止任务后修改文件名";
    }
  }
  if (video.file_change_block_reason !== undefined) return video.file_change_block_reason;
  return ["processing", "resource_paused"].includes(video.status) ? "该视频被处理任务占用，暂不能修改文件名" : null;
}

export function useTaskStatus(id: string | number | null, active = true, poll = true) {
  return useQuery({
    queryKey: ["jobs", id],
    queryFn: () => getJob(id!),
    enabled: active && id !== null,
    staleTime: 1000,
    refetchInterval: poll ? (query) => query.state.data && isTaskTerminal(query.state.data) ? false : 1000 : false,
    refetchIntervalInBackground: false,
  });
}

export function taskLabel(status: string) {
  return {
    queued: "排队中", running: "处理中", completed: "处理完成", failed: "处理失败",
    interrupted: "已停止", resource_paused: "资源暂停", awaiting_api: "等待 AI 恢复",
    cancelled: "已取消", canceled: "已取消",
  }[status] ?? status;
}

export function taskTypeLabel(job: Job) {
  if (job.job_type === "slide_extract") return "PPT 提取";
  if (job.job_type === "video_analysis") return job.analysis_modes?.length === 1
    && job.analysis_modes[0] === "question_extraction" ? "题目提取" : "考点提取";
  return ({
    batch_video_process: job.import_only ? "批量导入" : "批量处理",
    video_process: "视频处理", video_transcribe: "重新转写",
    secondary_view_align: "副视角自动对齐",
    question_analysis: "题目分析", question_visual: "题图复核",
    material_preprocess: "资料整理",
    asr_prepare: "模型准备", asr_download_cleanup: "下载清理",
    asr_delete: "模型清理", asr_runtime_delete: "运行环境清理", storage_migration: "目录迁移",
  } as Record<string, string>)[job.job_type ?? ""] ?? (job.video_id ? "视频处理" : "模型任务");
}
