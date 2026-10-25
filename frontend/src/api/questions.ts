import { request } from "./client";
import type { BiliTarget } from "./bilibili";

export interface QuestionMaterial { text: string; slide_ids: number[]; capture_ids?: string[] }
export interface QuestionCapture {
  id: string; marker_id: number; video_id: number; captured_at_ms: number;
  width: number; height: number; image_hash: string; mime_type: string;
}
export interface QuestionFact {
  id: string; kind: "condition" | "task" | "topic"; text: string; sources: string[];
}
export interface QuestionInference { id: string; text: string; basis: string[] }
export interface QuestionCriterion extends QuestionInference {
  required: boolean; depth: "concept" | "method" | "example"; evidence_needed: string;
}
export interface QuestionAnalysis {
  kind: "exercise" | "topic" | "unclear";
  facts: QuestionFact[];
  knowledge_points: QuestionInference[];
  methods: QuestionInference[];
  criteria: QuestionCriterion[];
  allowed_variations: string[];
  unknowns: string[];
  keywords: string[];
}
export interface QuestionRun {
  id: string; job_id: string; version: string; input_hash: string;
  status: "queued" | "analyzing" | "completed" | "failed" | "interrupted";
  analysis: QuestionAnalysis | null; target: BiliTarget | null; error: string; stale: boolean;
  profile?: { model: string; base_url?: string };
  input?: {
    classroom: {
      marker: { title: string; note: string; source_quote: string };
      context: { course: string; notes: string };
      classroom_segments: { id: number; text: string; start_ms: number; end_ms: number }[];
    };
    question: { text: string; images: {
      source: string; slide_id?: number; capture_id?: string; captured_at_ms?: number;
      start_ms: number; end_ms: number; representative_ms: number;
    }[] };
  };
}
export interface QuestionState { material: QuestionMaterial; runs: QuestionRun[]; captures?: QuestionCapture[] }
export const getQuestionState = (marker: number) =>
  request<QuestionState>(`/api/markers/${marker}/question`);
export const saveQuestionMaterial = (marker: number, material: QuestionMaterial) =>
  request<QuestionMaterial>(`/api/markers/${marker}/question`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(material),
  });
export const analyzeQuestion = (marker: number) =>
  request<{ analysis_id: string; job_id: string }>(`/api/markers/${marker}/question/analyze`, { method: "POST" });
export const uploadQuestionCapture = (marker: number, frame: { blob: Blob; captured_at_ms: number }) =>
  request<QuestionCapture>(`/api/markers/${marker}/question/captures?captured_at_ms=${frame.captured_at_ms}`, {
    method: "POST", headers: { "Content-Type": frame.blob.type }, body: frame.blob,
  });
export const deleteQuestionCapture = (marker: number, id: string) =>
  request<{ deleted: boolean }>(`/api/markers/${marker}/question/captures/${encodeURIComponent(id)}`, { method: "DELETE" });
export const questionCaptureUrl = (marker: number, id: string) =>
  `${(import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "")}/api/markers/${marker}/question/captures/${encodeURIComponent(id)}`;
