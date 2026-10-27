import { request } from "./client";

export type MaterialStudy = "unreviewed" | "consolidate" | "mastered";
export interface MaterialCollection { id: string; name: string; folder_ids: number[]; total: number; mastered: number }
export interface MaterialDocument {
  id: string; collection_id: string; name: string; format: string; state: string; reason: string;
  deleted: number; block_count: number; remaining: number; pending_count: number;
}
export interface MaterialCategory {
  id: string; collection_id: string; parent_id: string | null; name: string; aliases: string[];
  position: number; count: number;
}
export interface MaterialSource { block_id: string; bbox?: number[]; start?: number; end?: number }
export interface MaterialBlock {
  id: string; document_id: string; ordinal: number; type: string; text: string;
  coverage: string; reason: string; issue?: string; page?: number; image?: string;
  width?: number; height?: number; line_start?: number; line_end?: number; block_number?: number;
  images?: { image: string; width: number; height: number }[];
}
export type MaterialRegion = MaterialBlock & MaterialSource;
export interface MaterialValues {
  title: string; number: string; kind: "question" | "context" | "answer"; quality: "ready" | "pending";
  reason: string; sources: MaterialSource[]; category_ids: string[]; tags: string[];
  parent_id: string | null; references: { label: string; item_id: string | null }[];
  correction: string; correction_image: string | null; answer_sources: MaterialSource[];
}
export interface MaterialItem extends MaterialValues {
  id: string; document_id: string; document_name: string; ordinal: number; version: number;
  study_status: MaterialStudy; origin: string; deleted: number; document_deleted?: number;
}
export interface MaterialDetail extends MaterialItem {
  regions: MaterialRegion[]; answer_regions: MaterialRegion[];
  shared: (MaterialItem & { regions: MaterialRegion[] }) | null;
  answers: MaterialItem[]; versions: { version: number; study_status: MaterialStudy; created_at: string }[];
}
export interface MaterialRun { id: string; state: string; stage: string; reason: string; updated_at: string }
export interface MaterialFolder { id: number; name: string; parent_id: number | null }

export const materialRequest = <T,>(path: string, method: string, value?: unknown) =>
  request<T>(path, { method, headers: { "Content-Type": "application/json" },
    body: value === undefined ? undefined : JSON.stringify(value) });
export const getCollections = () => request<MaterialCollection[]>("/api/material-collections");
export const getMaterialFolders = () => request<MaterialFolder[]>("/api/material-collections/folders");
export const getDocuments = (id: string) => request<MaterialDocument[]>(`/api/material-documents?collection_id=${id}&include_deleted=true`);
export const getCategories = (id: string) => request<MaterialCategory[]>(`/api/material-categories?collection_id=${id}`);
export const getMaterialItems = (id: string, query = "") => request<MaterialItem[]>(`/api/material-items?collection_id=${id}&view=all&include_deleted=true&query=${encodeURIComponent(query)}`);
export const getMaterialItem = (id: string) => request<MaterialDetail>(`/api/material-items/${id}`);
export const getMaterialBlocks = (id: string) => request<MaterialBlock[]>(`/api/material-documents/${id}/blocks`);
export const getMaterialRuns = (id: string) => request<MaterialRun[]>(`/api/material-collections/${id}/runs`);
export const uploadMaterial = (collectionId: string, file: File) =>
  request<MaterialDocument>(`/api/material-documents?collection_id=${collectionId}&name=${encodeURIComponent(file.name)}`, {
    method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
  });
export const uploadCorrection = (documentId: string, file: File) =>
  request<{ resource: string }>(`/api/material-documents/${documentId}/corrections`, {
    method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file,
  });
const base = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
export const materialOriginalUrl = (documentId: string) => `${base}/api/material-documents/${documentId}/original`;
export const materialAssetUrl = (documentId: string, resource: string) =>
  `${base}/api/material-documents/${documentId}/asset?resource=${encodeURIComponent(resource)}`;
export function materialRegionUrl(documentId: string, source: MaterialSource) {
  const [left, top, right, bottom] = source.bbox ?? [0, 0, 1, 1];
  return `${base}/api/material-documents/${documentId}/region?${new URLSearchParams({
    block_id: source.block_id, left: String(left), top: String(top), right: String(right), bottom: String(bottom),
  })}`;
}
export function itemValues(item: MaterialValues): MaterialValues {
  const { title, number, kind, quality, reason, sources, category_ids, tags, parent_id, references,
    correction, correction_image, answer_sources } = item;
  return { title, number, kind, quality, reason, sources, category_ids, tags, parent_id, references,
    correction, correction_image, answer_sources };
}
