import { useQuery } from "@tanstack/react-query";

export type Announcement = {
  id: string; title: string; body: string; published_at: string; expires_at?: string;
  level?: "info" | "important"; url?: string; read: boolean;
};
export type UpdatePreferences = { auto_check: boolean; auto_download: boolean; show_announcements: boolean };
export type UpdateStatus = {
  version: string; repository: string; configured: boolean; portable: boolean; busy?: boolean;
  checking: boolean; downloading: boolean; downloaded_bytes: number; last_checked: string | null;
  error: string | null; download_error: string | null; compatible: boolean; ready: boolean;
  preferences: UpdatePreferences; announcements: Announcement[];
  release: { version: string; size: number; notes: string; url: string } | null;
  install: { status: string; version: string; message: string } | null;
};
export const updatesKey = ["public-updates"] as const;
const base = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
export async function updateRequest(path = "", body?: unknown, method = "POST"): Promise<UpdateStatus> {
  const response = await fetch(`${base}/api/updates${path}`, {
    method, headers: { Accept: "application/json", ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    throw new Error(typeof value.detail === "string" ? value.detail : `更新服务暂不可用 (${response.status})`);
  }
  return response.json();
}
export function useUpdates(active = true) {
  return useQuery({
    queryKey: updatesKey, queryFn: () => updateRequest("", undefined, "GET"), enabled: active,
    staleTime: 15_000, retry: false,
    refetchInterval: query => active ? query.state.data?.checking || query.state.data?.downloading ? 1000 : 60_000 : false,
  });
}
export function safeExternal(value: string | undefined) {
  try { const url = new URL(value ?? ""); return url.protocol === "https:" && !url.username && !url.password ? url.href : undefined; }
  catch { return undefined; }
}
