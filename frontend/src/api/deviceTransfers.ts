import { request } from "./client";

export interface TransferDevice {
  id: string; name: string; paired: boolean; root_available: boolean; foreground: boolean; message?: string;
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
