import { request } from "./client";

export type CacheCategory = "transcription" | "previews" | "logs" | "updates" | "downloads" | "ai" | "backups";
export type AutomaticCategory = "transcription" | "previews" | "logs" | "updates";
export type CleanupMode = "expired" | "all";
export const automaticCategories: AutomaticCategory[] = ["transcription", "previews", "logs", "updates"];

export interface CleanupPreferences {
  enabled: boolean;
  interval_days: number;
  retention_days: number;
  preview_limit_mb: number;
  categories: AutomaticCategory[];
}
export interface CleanupResult {
  finished_at: number;
  released_bytes: number;
  files_deleted: number;
  skipped: number;
  automatic: boolean;
  cancelled: boolean;
  warnings: string[];
  ai_cache_bytes_removed: number;
}
export interface CleanupStatus {
  preferences: CleanupPreferences;
  busy?: boolean;
  last_run: CleanupResult | null;
}
export interface CleanupInventory {
  categories: { id: CacheCategory; label: string; bytes: number; eligible_bytes: number; automatic: boolean }[];
}
export const getCleanupStatus = () => request<CleanupStatus>("/api/cache");
export const saveCleanupPreferences = (value: CleanupPreferences) => request<CleanupStatus>("/api/cache/preferences", {
  method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
});
export const previewCleanup = (mode: CleanupMode) => request<CleanupInventory>("/api/cache/preview", {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ mode }),
});
export const startCleanup = (categories: CacheCategory[], mode: CleanupMode) => request<{ job_id: string }>("/api/cache/clean", {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ categories, mode }),
});
