import { request } from "./client";

export interface StudyReading {
  page: number; scroll_top: number; zoom: number; question_id: string | null;
  left_open: boolean; right_open: boolean;
}
export interface StudyDocument {
  id: string; name: string; format: "pdf" | "markdown" | "image"; state: string; reason: string;
  page_count: number; question_count: number; deleted: boolean; reading: Partial<StudyReading>;
}
export interface StudyBlock {
  id: string; document_id: string; ordinal: number; kind: "pdf_page" | "image" | "markdown";
  page?: number; width?: number; height?: number; text?: string; image?: string;
  line_start?: number; line_end?: number; has_missing_images?: boolean; supplement?: boolean; label?: string;
}
export type StudyBBox = [number, number, number, number];
export interface StudyRegion { block_id: string; bbox?: StudyBBox; start?: number; end?: number; selection_group?: string }
export interface StudyTextBounds { status: "ready" | "no_text" | "unavailable"; bbox: StudyBBox | null }
export interface StudyQuestion {
  id: string; document_id: string; title: string; number: string; version: number; deleted: boolean;
  regions: (StudyRegion & { block: StudyBlock })[];
}
export interface StudyQuestionInput { document_id: string; title: string; number: string; regions: StudyRegion[] }
export const studyWrite = <T,>(path: string, method: string, payload?: unknown) => request<T>(path, {
  method, headers: { "Content-Type": "application/json" },
  body: payload === undefined ? undefined : JSON.stringify(payload),
});
export const getStudyDocuments = () => request<StudyDocument[]>("/api/study-documents?include_deleted=true");
export const getStudyBlocks = (id: string) => request<StudyBlock[]>(`/api/study-documents/${id}/blocks`);
export const getStudyTextBounds = (id: string, block: string) =>
  request<StudyTextBounds>(`/api/study-documents/${id}/blocks/${block}/text-bounds`);
export const getStudyQuestions = (id: string) =>
  request<StudyQuestion[]>(`/api/study-questions?document_id=${id}&include_deleted=true`);
export const saveStudyQuestion = (values: StudyQuestionInput, id?: string) =>
  studyWrite<StudyQuestion>(`/api/study-questions${id ? `/${id}` : ""}`, id ? "PUT" : "POST", values);
export const saveStudyReading = (id: string, value: StudyReading) =>
  studyWrite(`/api/study-documents/${id}/reading`, "PUT", value);
export const uploadStudyDocument = (file: File) =>
  request<StudyDocument>(`/api/study-documents?name=${encodeURIComponent(file.name)}`, {
    method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
  });
export const uploadStudyAttachment = (id: string, file: File) =>
  request<StudyBlock>(`/api/study-documents/${id}/attachments?name=${encodeURIComponent(file.name)}`, {
    method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
  });
const base = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
export const studyPageUrl = (id: string, page: number) => `${base}/api/study-documents/${id}/page/${page}`;
export function studyRegionUrl(id: string, region: StudyRegion) {
  const [left, top, right, bottom] = region.bbox ?? [0, 0, 1, 1];
  return `${base}/api/study-documents/${id}/blocks/${region.block_id}/region?${new URLSearchParams({
    left: String(left), top: String(top), right: String(right), bottom: String(bottom),
  })}`;
}
export const studySourceLabel = (block: StudyBlock) =>
  block.supplement ? block.label || "补充图片" : block.page ? `第 ${block.page} 页` : `第 ${block.line_start}–${block.line_end} 行`;
