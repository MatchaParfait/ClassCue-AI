import { request } from "./client";
import type { BiliTarget } from "./bilibili";

export interface QuestionMaterial { text: string; slide_ids: number[] }
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
}
export interface QuestionState { material: QuestionMaterial; runs: QuestionRun[] }
export const getQuestionState = (marker: number) =>
  request<QuestionState>(`/api/markers/${marker}/question`);
export const saveQuestionMaterial = (marker: number, material: QuestionMaterial) =>
  request<QuestionMaterial>(`/api/markers/${marker}/question`, {
    method: "PUT", body: JSON.stringify(material),
  });
export const analyzeQuestion = (marker: number) =>
  request<{ analysis_id: string; job_id: string }>(`/api/markers/${marker}/question/analyze`, { method: "POST" });
