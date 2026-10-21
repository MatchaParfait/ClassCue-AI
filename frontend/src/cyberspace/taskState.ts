import type { Job, Marker, MarkerMode } from "../types";

export type SpacePhase = "preparing" | "transcribing" | "analyzing" | "completed" | "waiting" | "paused" | "failed" | "stopped";
export function spacePhase(job: Job): SpacePhase {
  if (job.status === "awaiting_api") return "waiting";
  if (job.status === "resource_paused") return "paused";
  if (job.status === "failed") return "failed";
  if (["interrupted", "cancelled", "canceled"].includes(job.status)) return "stopped";
  if (job.status === "completed") return "completed";
  if (job.preparation?.active || job.status === "queued") return "preparing";
  if (["analyzing", "saving_markers", "completed"].includes(job.stage ?? "")) return "analyzing";
  if (["transcribing", "recovering"].includes(job.stage ?? "")) return "transcribing";
  return "preparing";
}

export const phaseLabels: Record<SpacePhase, string> = {
  preparing: "准备音频与模型", transcribing: "语音转写中", analyzing: "AI 分析中", completed: "处理完成",
  waiting: "等待 AI 恢复", paused: "资源暂停", failed: "处理失败", stopped: "已停止",
};

export const modeLabels: Record<MarkerMode, string> = {
  high_recall: "高召回", medium_recall: "中召回", low_recall: "低召回", strict_exam_intent: "明确考试意图",
};

export function canReadSavedText(phase: SpacePhase) {
  return !["preparing", "transcribing"].includes(phase);
}

export function savedMarkers(markers: Marker[], mode: MarkerMode) {
  return markers.filter((marker) => marker.analysis_mode === mode && marker.review_status !== "excluded");
}
