import { request } from "./client";

export interface BiliContext { course: string; notes: string }
export interface BiliQuery { text: string; purpose: string }
export interface BiliTarget {
  course: string; keywords: string[]; unknowns: string[];
  requirements: { id: string; text: string; required: boolean; depth: "concept" | "method" | "example"; sources: string[] }[];
  comparison?: {
    kind: "exercise" | "topic"; conditions: string[]; tasks: string[]; knowledge_points: string[];
    inferred_methods: string[]; allowed_variations: string[]; unknowns: string[];
  } | null;
}
export interface BiliEvidence { id: number; start_ms: number; end_ms: number; text: string; url: string }
export interface BiliFrame {
  id: string; captured_ms: number; requested_ms: number; width: number; height: number;
  image_hash: string; time_source: "decoded_frame" | "player_current_time"; url?: string;
}
export interface BiliDimension {
  status: "yes" | "partial" | "no" | "unknown"; reason: string; frame_ids: string[]; segment_ids: number[];
}
export interface BiliUnit {
  id: string; page: number; cid: number; part: string; url: string; status: string; error?: string;
  verdict?: "covers" | "partial" | "unrelated" | "insufficient"; reason?: string;
  evidence?: BiliEvidence[];
  multipart?: boolean; selection_reason?: string;
  assessments?: { requirement_id: string; status: string; depth: string; reason: string }[];
  missing_requirements?: string[];
  source?: { language?: string; automatic?: boolean };
  coverage?: { partial: boolean; selected_segments: number; total_segments: number };
  verification_mode?: "visual";
  frames?: BiliFrame[];
  dimensions?: { knowledge: BiliDimension; problem_type: BiliDimension; explanation: BiliDimension };
  visual?: { status: string; rounds?: number; error?: string };
  localized_hits?: { start_ms: number; end_ms: number; reason: string }[];
}
export interface BiliCandidate {
  bvid: string; title: string; author: string; url: string; status: string; error?: string;
  unchecked_pages?: number; units: BiliUnit[];
  collection_title?: string; selection_reason?: string; directory_unread?: boolean;
}
export interface BiliBudgetStop {
  scope: "task" | "exact" | "transport"; phase?: "exact" | "course";
  resource: string | null; used: number | null; limit: number | null; requested: number | null; reserve?: number;
}
export interface BiliRun {
  id: string; status: string; job_id: string | null; stale: boolean; error: string;
  queries: BiliQuery[]; candidates: BiliCandidate[];
  version?: string; legacy?: boolean; target?: BiliTarget; course_queries?: BiliQuery[];
  profile?: { model: string };
  input?: { question?: { analysis_id?: string } };
  phase?: "exact" | "course"; stop_reason?: string; auto_rewrite?: boolean;
  pending_queries?: BiliQuery[]; rewrite_reason?: string; budget_extended?: boolean;
  budget?: Record<string, number>; limits?: Record<string, number>;
  phase_limits?: { exact?: Record<string, number> }; budget_stop?: BiliBudgetStop;
  trace?: { seq: number; action: string; phase: string; reason?: string; budget_stop?: BiliBudgetStop }[];
  directories?: { id: string; title: string; complete: boolean; total: number; episodes: unknown[] }[];
  visual_verification?: boolean;
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
  options: { course_queries: BiliQuery[]; target?: BiliTarget; auto_rewrite: boolean; visual_verification?: boolean }) =>
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
export const biliFrameUrl = (marker: number, run: string, frame: string) =>
  `${(import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "")}/api/markers/${marker}/bilibili/runs/${encodeURIComponent(run)}/frames/${encodeURIComponent(frame)}`;
