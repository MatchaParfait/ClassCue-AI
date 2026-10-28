import { request } from "./client";

export interface DshStatus {
  protocol: number;
  desktop_version: string;
  instance_id: string;
  client_connected: boolean;
  capabilities: string[];
}
export interface DshReceipt {
  request_id: string;
  question_id: string;
  content_version: number;
  instance_id: string;
  workspace_id: string | null;
  cwd: string | null;
  session_id: string;
  status: "choose_workspace" | "reserved" | "bound" | "draft_ready" | "sent";
  error: string | null;
  outcome?: string | null;
}
export interface DshDiscussion {
  request_id: string;
  content_version: number;
  instance_id: string;
  receipt: DshReceipt | null;
  created_at: string;
}
const post = <T,>(path: string, value?: unknown) => request<T>(`/api/dsh-bridge${path}`, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: value === undefined ? undefined : JSON.stringify(value),
});
export const getDshStatus = () => request<DshStatus>("/api/dsh-bridge/status");
export const connectDsh = (descriptorPath: string) => post<DshStatus>("/connection", { descriptor_path: descriptorPath });
export const prepareDshQuestion = (questionId: string, newDiscussion = false) =>
  post<DshReceipt>("/discussions", { question_id: questionId, new_discussion: newDiscussion });
export const getDshDiscussions = (questionId: string) =>
  request<DshDiscussion[]>(`/api/dsh-bridge/questions/${encodeURIComponent(questionId)}/discussions`);
export const getDshRequest = (requestId: string) =>
  request<DshReceipt>(`/api/dsh-bridge/requests/${encodeURIComponent(requestId)}`);
export const openDshDiscussion = (requestId: string) =>
  post<DshReceipt>(`/requests/${encodeURIComponent(requestId)}/open`);
