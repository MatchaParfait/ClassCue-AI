import { request } from "./client";

export interface TransferDevice {
  id: string; name: string; paired: boolean; root_available: boolean; foreground: boolean; message?: string;
  capabilities?: string[];
}
export interface StudySyncTask {
  id: string; name: string; peer_name: string; video_id: number | null;
  direction: "incoming" | "outgoing"; state: string; error?: string;
  preview?: { added: number; removed: number; edited: number; bootstrap: boolean;
    conflicts: Array<{ id: string; title: string; fields: string[]; local: Record<string, unknown>; remote: Record<string, unknown> }> };
}
export interface TransferPreview {
  id: string; name: string; need_video: boolean; conflict: boolean; same: boolean; blocked: string;
  subtitle_count: number; marker_count: number;
}
export interface TransferTask {
  id: string; direction: "send" | "receive"; title: string; state: string; peer_name: string;
  total_bytes: number; transferred_bytes: number; items: TransferPreview[]; video_count: number; error: string;
  results: Array<{ id: string; name: string; state: string; message: string; video_id?: number | null }>;
}
export interface TransferStatus {
  devices: TransferDevice[]; tasks: TransferTask[];
  sync_tasks?: StudySyncTask[];
  pairings: Array<{ id: string; name: string; code: string; accepted: boolean; confirmed: boolean }>;
  snapshots: Array<{ id: string; video_id: number; created: number; restored: number }>; error: string;
}
export interface TransferSelection { title: string; videoIds?: number[]; folderId?: number }
export type TransferAction = "receive" | "overwrite" | "skip";
export const transferStatus = () => request<TransferStatus>("/api/device-transfers");
export function transferPost<T>(path: string, payload?: object) {
  return request<T>(`/api/device-transfers${path}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload ?? {}),
  });
}
export function openDeviceTransfer(selection?: TransferSelection) {
  window.dispatchEvent(new CustomEvent("classcue:device-transfer", { detail: selection }));
}
