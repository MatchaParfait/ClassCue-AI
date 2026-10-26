import { request } from "./client";

export interface VisualSlide {
  id: number; video_id: number; image_hash: string; representative_ms: number;
  visible_from_ms: number; visible_until_ms: number; is_llm_preferred: boolean;
}
export interface VisualBinding {
  slide_id: number | null; capture_id: string; representative_ms: number; missing?: boolean;
  source: "preferred" | "timeline" | "capture";
}
export interface QuestionVisual {
  marker_id: number; question_id: string | null;
  status: "unchecked" | "waiting_slides" | "checking" | "completed" | "failed" | "interrupted";
  verdict: "confirmed" | "not_question" | "uncertain";
  binding: VisualBinding | null; binding_mode?: "manual" | "auto"; stale?: boolean; reason: string;
  algorithm?: string;
  candidates?: VisualSlide[];
  attempts?: { stage: string; slide_ids: number[]; format_repair_count: number; error_counts: Record<string, number>;
    calls: { status: string; seconds: number; usage: { total_tokens?: number | null } }[] }[];
}
export interface ImageSelection { capture_id: string | null; binding_mode: "manual" | "auto" }
export const getQuestionVisuals = (video: number) =>
  request<{ items: QuestionVisual[]; active: boolean }>(`/api/videos/${video}/question-visuals`);
export const getQuestionVisual = (marker: number) =>
  request<QuestionVisual>(`/api/markers/${marker}/question/visual`);
export const assignQuestionImages = (video: number) =>
  request<{ items: QuestionVisual[]; active: boolean }>(`/api/videos/${video}/question-images/assign`, { method: "POST" });
export const autoQuestionImage = (marker: number) =>
  request<QuestionVisual>(`/api/markers/${marker}/question/visual/auto`, { method: "POST" });
export const setQuestionImage = (marker: number, selection: ImageSelection) =>
  request<QuestionVisual>(`/api/markers/${marker}/question/visual/capture-binding`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(selection),
  });
export const captureQuestionImage = (marker: number, frame: { blob: Blob; captured_at_ms: number }) =>
  request<{ visual: QuestionVisual; previous_image: ImageSelection }>(
    `/api/markers/${marker}/question/captures?captured_at_ms=${frame.captured_at_ms}&as_question_image=true`, {
      method: "POST", headers: { "Content-Type": frame.blob.type }, body: frame.blob,
    });
export const verifyQuestionVisual = (marker: number) =>
  request<{ job_id: string }>(`/api/markers/${marker}/question/visual/verify`, { method: "POST" });
export const bindQuestionVisual = (marker: number, slide: number | null) =>
  request<QuestionVisual>(`/api/markers/${marker}/question/visual/binding`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slide_id: slide }),
  });

export function visualLabel(value?: QuestionVisual) {
  if (value?.algorithm === "question-image-time-v1") {
    if (value.binding_mode === "manual") return value.binding
      ? value.binding.source === "capture" ? "手动截图" : "手动关联" : "已取消题图";
    if (value.binding?.missing) return "题图不可用";
    return value.binding ? "自动配图" : "暂无题图";
  }
  if (value?.status === "checking") return "正在核验";
  if (value?.binding_mode === "manual") return value.binding ? "手动关联" : "已取消关联";
  if (value?.status === "failed") return "核验失败";
  if (value?.status === "interrupted") return "核验中断";
  if (value?.stale) return "结果待更新";
  if (value?.status === "waiting_slides") return "等待 PPT";
  if (value?.verdict === "confirmed") return "已确认题图";
  if (value?.verdict === "not_question") return "疑似非例题";
  return value?.status === "completed" ? "待确认" : "尚未核验";
}
