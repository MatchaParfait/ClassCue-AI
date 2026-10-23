import { request } from "./client";

export interface BiliContext { course: string; notes: string }
export interface BiliQuery { text: string; purpose: string }
export interface BiliTarget {
  course: string; keywords: string[]; unknowns: string[];
  requirements: { id: string; text: string; required: boolean; depth: "concept" | "method" | "example"; sources: string[] }[];
}
export interface BiliEvidence { id: number; start_ms: number; end_ms: number; text: string; url: string }
export interface BiliUnit {
  id: string; page: number; cid: number; part: string; url: string; status: string; error?: string;
  verdict?: "covers" | "partial" | "unrelated" | "insufficient"; reason?: string;
  evidence?: BiliEvidence[];
  multipart?: boolean; selection_reason?: string;
  assessments?: { requirement_id: string; status: string; depth: string; reason: string }[];
  missing_requirements?: string[];
  source?: { language?: string; automatic?: boolean };
  coverage?: { partial: boolean; selected_segments: number; total_segments: number };
}
export interface BiliCandidate {
  bvid: string; title: string; author: string; url: string; status: string; error?: string;
  unchecked_pages?: number; units: BiliUnit[];
  collection_title?: string; selection_reason?: string; directory_unread?: boolean;
}
export interface BiliRun {
  id: string; status: string; job_id: string | null; stale: boolean; error: string;
  queries: BiliQuery[]; candidates: BiliCandidate[];
  version?: string; legacy?: boolean; target?: BiliTarget; course_queries?: BiliQuery[];
  phase?: "exact" | "course"; stop_reason?: string; auto_rewrite?: boolean;
  pending_queries?: BiliQuery[]; rewrite_reason?: string; budget_extended?: boolean;
  budget?: Record<string, number>; limits?: Record<string, number>;
  trace?: { seq: number; action: string; phase: string; reason?: string }[];
  directories?: { id: string; title: string; complete: boolean; total: number; episodes: unknown[] }[];
}
export interface BiliState { context: BiliContext; runs: BiliRun[] }
export interface BiliAccount { configured: boolean; pause: { reason: string; at: number } | null }
export interface BiliStarted { run_id: string; job_id: string }
export interface BiliLoginSession {
  session_id: string;
  status: "waiting_scan" | "waiting_confirm" | "success" | "expired" | "cancelled" | "failed";
  image: string;
  expires_at: number;
  poll_after_ms: number;
}

const json = (value: unknown, method = "POST"): RequestInit => ({
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
});
export const getBiliState = (marker: number) => request<BiliState>(`/api/markers/${marker}/bilibili`);
export const saveBiliContext = (marker: number, context: BiliContext) =>
  request<BiliContext>(`/api/markers/${marker}/bilibili/context`, json(context, "PUT"));
export const planBiliSearch = (marker: number, consent: boolean) =>
  request<BiliStarted>(`/api/markers/${marker}/bilibili/plan`, json({ consent }));
export const startBiliSearch = (marker: number, run: string, queries: BiliQuery[], consent: boolean,
  options: { course_queries: BiliQuery[]; target?: BiliTarget; auto_rewrite: boolean }) =>
  request<BiliStarted>(`/api/markers/${marker}/bilibili/search`, json({ run_id: run, queries, consent, ...options }));
export const continueBiliSearch = (marker: number, run: string, queries?: BiliQuery[], increaseBudget = false) =>
  request<BiliStarted>(`/api/markers/${marker}/bilibili/continue`, json({
    run_id: run, consent: true, queries, increase_budget: increaseBudget,
  }));
export const getBiliAccount = () => request<BiliAccount>("/api/bilibili/account");
export const startBiliLogin = (signal?: AbortSignal) =>
  request<BiliLoginSession>("/api/bilibili/login/start", { ...json({}), cache: "no-store", signal });
export const pollBiliLogin = (sessionId: string, signal?: AbortSignal) =>
  request<BiliLoginSession>("/api/bilibili/login/poll", {
    ...json({ session_id: sessionId }), cache: "no-store", signal,
  });
export const cancelBiliLogin = (sessionId: string) =>
  request<{ status: "cancelled" }>("/api/bilibili/login/cancel", json({ session_id: sessionId }));
export const deleteBiliAccount = () => request<BiliAccount>("/api/bilibili/account", { method: "DELETE" });
export const resumeBiliReading = (acknowledge: boolean) =>
  request<BiliAccount>("/api/bilibili/resume", json({ acknowledge_account_risk: acknowledge }));
