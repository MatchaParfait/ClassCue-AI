import type {
  AiSettings,
  AnalysisRun,
  AnalysisPreview,
  RecallMode,
  RecallScheme,
  RecallSchemeInput,
  RecallPromptPreview,
  AsrModel,
  FireRedResource,
  Job,
  JobStatus,
  Marker,
  MarkerMode,
  MarkerImportance,
  PickerResult,
  ProcessingOptions,
  Subtitle,
  SubtitleSearchResult,
  Video,
  LibraryStatus,
  StorageDirectories,
  StorageDirectoryValues,
  StorageMigrationPlan,
  ApplicationDataPlan,
} from "../types";

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
export interface ApiValue<T> {
  value: T;
  source: "api" | "mock";
}

export function getLiveTranscript(jobId: string | number) {
  return request<{ video_id: number; revision: number; settled_until_ms: number; subtitles: Subtitle[] }>(`/api/jobs/${encodeURIComponent(jobId)}/transcript`);
}

export function getAnalysisPreview(jobId: string | number) {
  return request<AnalysisPreview>(`/api/jobs/${encodeURIComponent(jobId)}/analysis-preview`);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json() as { detail?: unknown; message?: unknown };
      if (typeof payload.detail === "string") detail = payload.detail;
      else if (typeof payload.message === "string") detail = payload.message;
    } catch {
      // Keep the HTTP status when the server did not return JSON.
    }
    throw new Error(detail || `API ${response.status}: ${response.statusText}`);
  }

  return response.json() as Promise<T>;
}

function normalizeVideo(raw: Video): Video {
  return {
    ...raw,
    display_name: raw.display_name ?? raw.filename,
    duration_ms: Number(raw.duration_ms ?? 0),
    video_path: raw.video_path ?? (raw as Video & { source_path?: string }).source_path ?? "",
    source_size: Number(raw.source_size ?? 0),
  };
}

function normalizeSubtitles(raw: unknown): Subtitle[] {
  const list = Array.isArray(raw) ? raw : ((raw as { subtitles?: Subtitle[] })?.subtitles ?? []);
  return list.map((item) => ({
    id: Number(item.id),
    start_ms: Number(item.start_ms),
    end_ms: Number(item.end_ms),
    text: String(item.text),
  }));
}

function normalizeMarkers(raw: unknown): Marker[] {
  const list = Array.isArray(raw) ? raw : ((raw as { markers?: Marker[] })?.markers ?? []);
  return list.map((item) => ({
    ...item,
    id: Number(item.id),
    video_id: Number(item.video_id),
    start_ms: Number(item.start_ms),
    end_ms: Number(item.end_ms),
    analysis_mode: item.analysis_mode as MarkerMode,
    review_status: item.review_status ?? "active",
    importance: item.importance ?? "standard",
  }));
}

export async function getVideo(videoId: number): Promise<ApiValue<Video>> {
  const value = normalizeVideo(await request<Video>(`/api/videos/${videoId}`));
  return { value, source: "api" };
}

export async function getSubtitles(videoId: number): Promise<ApiValue<Subtitle[]>> {
  const value = normalizeSubtitles(await request(`/api/videos/${videoId}/subtitles`));
  return { value, source: "api" };
}

export async function getMarkers(videoId: number): Promise<ApiValue<Marker[]>> {
  const value = normalizeMarkers(await request(`/api/videos/${videoId}/markers`));
  return { value, source: "api" };
}

export async function updateMarkerStatus(markerId: number, reviewStatus: Marker["review_status"]): Promise<Marker | null> {
  return request<Marker>(`/api/markers/${markerId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ review_status: reviewStatus }),
  });
}

export async function updateMarkerJumpTime(markerId: number, userStartMs: number | null): Promise<Marker> {
  return request<Marker>(`/api/markers/${markerId}/jump-time`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_start_ms: userStartMs }),
  });
}

export async function updateMarkerImportance(markerId: number, importance: MarkerImportance): Promise<Marker> {
  return request<Marker>(`/api/markers/${markerId}/importance`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ importance }),
  });
}

export async function getVideos(): Promise<Video[]> {
  const raw = await request<Video[] | { videos?: Video[] }>("/api/videos");
  return (Array.isArray(raw) ? raw : raw.videos ?? []).map(normalizeVideo);
}

export interface LibraryFolder {
  id: number;
  name: string;
  parent_id?: number | null;
  library_order?: number;
  created_at?: string;
  updated_at?: string;
  video_count?: number;
  marker_importance_counts?: Video["marker_importance_counts"];
}

export interface LibraryResponse {
  folder: LibraryFolder | null;
  ancestors?: LibraryFolder[];
  parent_id: number | null;
  sort: string;
  order: string;
  items: Array<{ type: "folder" | "video"; folder?: LibraryFolder; video?: Video }>;
}

export async function getLibrary(folderId?: number): Promise<LibraryResponse> {
  const query = folderId === undefined ? "" : `?folder_id=${encodeURIComponent(String(folderId))}`;
  return getLibraryResponse(await request<LibraryResponse>(`/api/library${query}`));
}

function normalizeLibraryFolder(raw: LibraryFolder): LibraryFolder {
  return {
    ...raw,
    id: Number(raw.id),
    parent_id: raw.parent_id == null ? null : Number(raw.parent_id),
    library_order: Number(raw.library_order ?? 0),
    video_count: Number(raw.video_count ?? 0),
  };
}

export async function createLibraryFolder(name: string, parentId?: number | null): Promise<LibraryFolder> {
  return normalizeLibraryFolder(await request<LibraryFolder>("/api/folders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, parent_id: parentId ?? null }),
  }));
}

export async function renameLibraryFolder(folderId: number, name: string): Promise<LibraryFolder> {
  return normalizeLibraryFolder(await request<LibraryFolder>(`/api/folders/${folderId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  }));
}

export async function moveLibraryFolder(folderId: number, parentId: number | null): Promise<LibraryFolder> {
  return normalizeLibraryFolder(await request<LibraryFolder>(`/api/folders/${folderId}/parent`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ parent_id: parentId }),
  }));
}

export interface RecycleBinResult {
  operation_id: string;
  kind: "video" | "folder";
  videos: number;
  folders: number;
  message?: string;
}

export async function deleteLibraryFolder(folderId: number): Promise<RecycleBinResult> {
  return request<RecycleBinResult>(`/api/folders/${folderId}`, { method: "DELETE" });
}

export async function deleteVideo(videoId: number): Promise<RecycleBinResult> {
  return request<RecycleBinResult>(`/api/videos/${videoId}`, { method: "DELETE" });
}

export async function undoRecycleBin(): Promise<RecycleBinResult> {
  return request<RecycleBinResult>("/api/recycle-bin/undo", { method: "POST" });
}

export async function getLatestRecycleBin(): Promise<RecycleBinResult | null> {
  return request<RecycleBinResult | null>("/api/recycle-bin/latest");
}

export async function moveVideoToFolder(videoId: number, folderId: number | null): Promise<Video> {
  return normalizeVideo(await request<Video>(`/api/videos/${videoId}/folder`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ folder_id: folderId }),
  }));
}

export async function reorderLibrary(parentId: number | null, orderedItems: Array<{ type: "folder" | "video"; id: number }>): Promise<LibraryResponse> {
  return getLibraryResponse(await request<LibraryResponse>("/api/library/reorder", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ parent_id: parentId, ordered_items: orderedItems }),
  }));
}

function getLibraryResponse(raw: LibraryResponse): LibraryResponse {
  return {
    ...raw,
    folder: raw.folder ? normalizeLibraryFolder(raw.folder) : null,
    ancestors: raw.ancestors?.map(normalizeLibraryFolder),
    items: Array.isArray(raw.items)
      ? raw.items.map((item) => ({
        ...item,
        folder: item.folder ? normalizeLibraryFolder(item.folder) : undefined,
        video: item.video ? normalizeVideo(item.video) : undefined,
      }))
      : [],
  };
}

export function getStreamUrl(videoId: number, demo = false): string {
  if (demo) return "https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4";
  return `${API_BASE}/api/videos/${videoId}/stream`;
}

export function getThumbnailUrl(video: Video): string {
  const version = ["native-jpeg444-q2-v2", video.library_id ?? "", video.video_path, video.source_size ?? "", video.source_mtime_ns ?? ""];
  return `${API_BASE}/api/videos/${video.id}/thumbnail?v=${encodeURIComponent(JSON.stringify(version))}`;
}

export function getSearchPreviewUrl(videoId: number, timeMs: number, libraryId: string): string {
  const params = new URLSearchParams({ time_ms: String(timeMs), library_id: libraryId });
  return `${API_BASE}/api/videos/${videoId}/preview-frame?${params}`;
}

export function getApiBaseUrl(): string {
  return API_BASE || "same-origin /api";
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function pickString(record: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    if (typeof record[key] === "string") return record[key] as string;
  }
  return "";
}

function pickNumber(record: Record<string, unknown>, ...keys: string[]): number {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) return Number(value);
  }
  return 0;
}

function pickBoolean(record: Record<string, unknown>, ...keys: string[]): boolean | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "boolean") return value;
    if (typeof value === "string") {
      if (value.toLowerCase() === "true") return true;
      if (value.toLowerCase() === "false") return false;
    }
  }
  return undefined;
}

function normalizeLibraryStatus(raw: unknown): LibraryStatus {
  const record = asRecord(raw);
  return {
    library_id: pickString(record, "library_id") || undefined,
    library_scope: pickString(record, "library_scope") || undefined,
    data_root: pickString(record, "data_root"),
    database_path: pickString(record, "database_path", "db_path"),
    temp_audio_dir: pickString(record, "temp_audio_dir", "temporary_audio_dir", "audio_dir"),
    video_count: pickNumber(record, "video_count", "videos", "count"),
    active_job_id: (record.active_job_id ?? record.running_job_id ?? record.job_id ?? null) as number | string | null,
    processing: Boolean(record.processing ?? record.has_running_job ?? record.active_job_id),
  };
}

export async function getLibraryStatus(): Promise<LibraryStatus> {
  return normalizeLibraryStatus(await request("/api/library/status"));
}

export function searchSubtitles(query: string, offset = 0, signal?: AbortSignal, preferredVideoId?: number): Promise<SubtitleSearchResult> {
  const params = new URLSearchParams({ q: query.trim(), limit: "20", offset: String(offset) });
  if (preferredVideoId !== undefined) params.set("preferred_video_id", String(preferredVideoId));
  return request(`/api/library/search?${params}`, { signal });
}

export async function getStorageDirectories(): Promise<StorageDirectories> {
  return request("/api/settings/directories");
}

export async function saveStorageDirectories(values: StorageDirectoryValues & { library_id: string }): Promise<StorageDirectories> {
  return request("/api/settings/directories", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(values),
  });
}

export async function previewStorageMigration(values: StorageDirectoryValues & { library_id: string }): Promise<StorageMigrationPlan> {
  return request("/api/settings/directories/preview", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
  });
}

export async function previewApplicationData(values: { destination: string; library_id: string }): Promise<ApplicationDataPlan> {
  return request("/api/settings/application-data/preview", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
  });
}

export async function migrateApplicationData(values: { destination: string; library_id: string; plan_id: string }): Promise<{ id: string; status: string }> {
  return request("/api/settings/application-data/migrate", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
  });
}

export async function startStorageMigration(values: StorageDirectoryValues & { library_id: string; plan_id: string }): Promise<{ job_id: string }> {
  return request("/api/settings/directories/migrate", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values),
  });
}

export async function getAsrSelection(): Promise<AsrModel> {
  return normalizeAsrModel(await request("/api/asr/selection"));
}

export async function getAsrResourceProfile(modelId: string): Promise<FireRedResource> {
  return request<FireRedResource>(`/api/asr/models/${encodeURIComponent(modelId)}/resource-profile`);
}

function normalizeAsrModel(raw: unknown): AsrModel {
  const record = asRecord(raw);
  const capabilities = Array.isArray(record.capabilities)
    ? record.capabilities.filter((value): value is string => typeof value === "string")
    : undefined;
  return {
    id: pickString(record, "id", "model_id", "key"),
    name: pickString(record, "name", "display_name", "model_name"),
    engine: pickString(record, "engine", "engine_type"),
    selectable: pickBoolean(record, "selectable") ?? undefined,
    device: record.device === "cpu" ? "cpu" : record.device === "gpu" ? "gpu" : undefined,
    gpu_available: pickBoolean(record, "gpu_available") ?? undefined,
    gpu_runtime_ready: pickBoolean(record, "gpu_runtime_ready") ?? undefined,
    gpu_prepare_available: pickBoolean(record, "gpu_prepare_available") ?? undefined,
    gpu_unavailable_reason: pickString(record, "gpu_unavailable_reason"),
    gpu_name: pickString(record, "gpu_name"),
    cpu_notice: pickString(record, "cpu_notice"),
    cpu_available: pickBoolean(record, "cpu_available") ?? undefined,
    downloaded: pickBoolean(record, "downloaded", "is_downloaded", "ready") ?? null,
    cache_size_bytes: record.cache_size_bytes == null && record.cache_size == null && record.size_bytes == null
      ? null : pickNumber(record, "cache_size_bytes", "cache_size", "size_bytes"),
    runtime_size_bytes: typeof record.runtime_size_bytes === "number" ? record.runtime_size_bytes : null,
    inspection_status: record.inspection_status === "unknown" || record.inspection_status === "cached"
      || record.inspection_status === "ready" ? record.inspection_status : undefined,
    status_checked_at: typeof record.status_checked_at === "number" ? record.status_checked_at : null,
    gpu_total_memory_mib: typeof record.gpu_total_memory_mib === "number" ? record.gpu_total_memory_mib : null,
    gpu_batch_size: typeof record.gpu_batch_size === "number" ? record.gpu_batch_size : null,
    qwen_ready: record.qwen_ready == null && record.qwen_runtime_ready == null
      ? null
      : Boolean(record.qwen_ready ?? record.qwen_runtime_ready),
    aed_ready: pickBoolean(record, "aed_ready", "aed_runtime_ready", "aed_enabled"),
    supports_aed: pickBoolean(record, "supports_aed", "has_aed", "aed") ?? undefined,
    capabilities,
    model_family: pickString(record, "model_family", "family"),
    recommendation: pickString(record, "recommendation", "recommended_for", "recommended_use"),
    selected: Boolean(record.selected ?? record.is_selected),
    status: pickString(record, "status"),
    error_message: typeof record.error_message === "string" ? record.error_message : null,
    active_job_id: typeof record.active_job_id === "string" || typeof record.active_job_id === "number"
      ? record.active_job_id : null,
    active_job_kind: record.active_job_kind === "preparing" || record.active_job_kind === "deleting" || record.active_job_kind === "cleaning_partial" || record.active_job_kind === "cleaning_runtime"
      ? record.active_job_kind : null,
    download_state: record.download_state && typeof record.download_state === "object"
      ? record.download_state as AsrModel["download_state"] : null,
  };
}

export async function getAsrModels(): Promise<AsrModel[]> {
  const raw = await request<unknown>("/api/asr/models");
  const record = asRecord(raw);
  const list = Array.isArray(raw) ? raw : (Array.isArray(record.models) ? record.models : []);
  const selectedId = pickString(record, "selected_model_id", "selection", "current_model");
  return list.map((model) => {
    const normalized = normalizeAsrModel(model);
    return selectedId && normalized.id === selectedId ? { ...normalized, selected: true } : normalized;
  }).filter((model) => model.selectable !== false && model.engine !== "nano" && model.id !== "fun-asr-nano-2512");
}

export async function getAsrModelStatus(
  modelId: string, options: { full?: boolean; refresh?: boolean } = {},
): Promise<AsrModel> {
  const query = new URLSearchParams();
  if (options.full !== undefined) query.set("full", String(options.full));
  if (options.refresh) query.set("refresh", "true");
  const suffix = query.size ? `?${query}` : "";
  return normalizeAsrModel(await request(`/api/asr/models/${encodeURIComponent(modelId)}/status${suffix}`));
}

export async function selectAsrModel(
  modelId: string, fireredDevice?: "gpu" | "cpu", paraformerDevice?: "gpu" | "cpu",
  nanoDevice?: "gpu" | "cpu", dolphinDevice?: "gpu" | "cpu",
): Promise<void> {
  await request("/api/asr/selection", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      model_id: modelId, firered_device: fireredDevice, paraformer_device: paraformerDevice,
      nano_device: nanoDevice, dolphin_device: dolphinDevice,
    }),
  });
}

export async function prepareAsrModel(modelId: string, device?: "cpu" | "gpu"): Promise<{ job_id?: number | string; status?: string }> {
  const query = device ? `?device=${device}` : "";
  return request(`/api/asr/models/${encodeURIComponent(modelId)}/prepare${query}`, { method: "POST" });
}

export async function clearAsrModel(modelId: string, removeRuntime = false): Promise<{ job_id?: number | string; status?: string }> {
  const query = removeRuntime ? "?remove_runtime=true" : "";
  return request(`/api/asr/models/${encodeURIComponent(modelId)}${query}`, { method: "DELETE" });
}

export async function clearAsrRuntime(modelId: string, device?: "cpu" | "gpu"): Promise<{ job_id: string | number }> {
  const query = device ? `?device=${device}` : "";
  return request(`/api/asr/models/${encodeURIComponent(modelId)}/runtime${query}`, { method: "DELETE" });
}

function normalizeAiSettings(raw: unknown): AiSettings {
  const record = asRecord(raw);
  return {
    ...(typeof record.connection_id === "string" ? { connection_id: record.connection_id,
      connection_name: pickString(record, "connection_name"),
      connections: (record.connections ?? []) as AiSettings["connections"] } : {}),
    ...(typeof record.connection_name === "string" ? { connection_name: record.connection_name } : {}),
    provider: pickString(record, "provider", "service_type", "type") || "deepseek",
    base_url: pickString(record, "base_url", "endpoint"),
    model: pickString(record, "model", "model_name"),
    output_tokens: pickNumber(record, "output_tokens", "max_tokens"),
    token_parameter: pickString(record, "token_parameter", "token_param") || "max_tokens",
    json_mode: Boolean(record.json_mode ?? record.response_json),
    timeout_seconds: pickNumber(record, "timeout_seconds", "timeout") || 60,
    max_retries: pickNumber(record, "max_retries", "retries"),
    reasoning_effort: (record.reasoning_effort as AiSettings["reasoning_effort"]) ?? null,
    has_api_key: Boolean(record.has_api_key),
  };
}

export async function getAiSettings(): Promise<AiSettings> {
  return normalizeAiSettings(await request("/api/settings/ai"));
}

export async function revealAiApiKey(baseUrl: string, signal?: AbortSignal, connectionId?: string): Promise<string> {
  const result = await request<{ api_key: string }>("/api/settings/ai/key/reveal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_url: baseUrl, ...(connectionId ? { connection_id: connectionId } : {}) }),
    cache: "no-store",
    signal,
  });
  return result.api_key;
}

export interface AiCredentialsDraft { api_key?: string; remove_key?: boolean }
export interface AiRequestDraft { api_key?: string; settings?: Omit<AiSettings, "has_api_key"> }

export async function saveAiSettings(settings: Omit<AiSettings, "has_api_key">, credentials?: AiCredentialsDraft): Promise<AiSettings> {
  const payload = {
    ...(settings.connection_id ? { connection_id: settings.connection_id } : {}),
    ...(settings.connection_name ? { connection_name: settings.connection_name } : {}),
    ...credentials,
    provider: settings.provider,
    service_type: settings.provider,
    base_url: settings.base_url,
    model: settings.model,
    max_tokens: settings.output_tokens,
    token_parameter: settings.token_parameter,
    json_mode: settings.json_mode,
    timeout_seconds: settings.timeout_seconds,
    max_retries: settings.max_retries,
    reasoning_effort: settings.reasoning_effort ?? null,
  };
  return normalizeAiSettings(await request("/api/settings/ai", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }));
}

export async function saveAiApiKey(apiKey: string, baseUrl?: string, connectionId?: string): Promise<AiSettings> {
  return normalizeAiSettings(await request("/api/settings/ai/key", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: apiKey, ...(baseUrl ? { base_url: baseUrl } : {}), ...(connectionId ? { connection_id: connectionId } : {}) }),
  }));
}

export async function deleteAiApiKey(baseUrl?: string, connectionId?: string): Promise<AiSettings> {
  return normalizeAiSettings(await request("/api/settings/ai/key", {
    method: "DELETE",
    ...(baseUrl || connectionId ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ base_url: baseUrl, connection_id: connectionId }) } : {}),
  }));
}

export async function testAiSettings(baseUrl?: string, connectionId?: string, model?: string, draft?: AiRequestDraft): Promise<{ ok?: boolean; message?: string }> {
  return request("/api/settings/ai/test", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...(baseUrl ? { base_url: baseUrl } : {}), ...(connectionId ? { connection_id: connectionId } : {}), ...(model ? { model } : {}), ...draft }),
  });
}

export async function getAiRemoteModels(baseUrl?: string, connectionId?: string, draft?: AiRequestDraft): Promise<string[]> {
  const raw = await request<unknown>("/api/settings/ai/models", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...(baseUrl ? { base_url: baseUrl } : {}), ...(connectionId ? { connection_id: connectionId } : {}), ...draft }),
  });
  if (Array.isArray(raw)) return raw.map(String);
  const record = asRecord(raw);
  const models = Array.isArray(record.models) ? record.models : [];
  return models.map((model) => typeof model === "string" ? model : pickString(asRecord(model), "id", "name", "model"));
}

export async function createPicker(kind: "directory" | "file" = "directory"): Promise<string> {
  const raw = asRecord(await request("/api/pickers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind }),
  }));
  return pickString(raw, "picker_id", "id");
}

export async function pollPicker(pickerId: string, timeoutMs = 60_000): Promise<string | null> {
  const deadline = Date.now() + timeoutMs;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    while (Date.now() < deadline) {
      const record = asRecord(await request(`/api/pickers/${encodeURIComponent(pickerId)}`, {
        signal: controller.signal,
      }));
      const status = pickString(record, "status").toLowerCase();
      const path = pickString(record, "path", "selected_path", "value");
      if (path || status === "completed" || status === "selected") return path || null;
      if (status === "cancelled" || status === "canceled" || status === "failed") return null;
      await new Promise((resolve) => window.setTimeout(resolve, 500));
    }
    throw new Error("选择器等待超时");
  } catch (error) {
    const timedOut = controller.signal.aborted;
    window.clearTimeout(timer);
    const cleanup = new AbortController();
    const cleanupTimer = window.setTimeout(() => cleanup.abort(), 5_000);
    try {
      await request(`/api/pickers/${encodeURIComponent(pickerId)}`, {
        method: "DELETE", signal: cleanup.signal,
      }).catch(() => undefined);
    } finally {
      window.clearTimeout(cleanupTimer);
    }
    if (timedOut) throw new Error("选择器等待超时");
    throw error;
  } finally {
    window.clearTimeout(timer);
  }
}

export async function getJobStatus(jobId: number | string): Promise<JobStatus> {
  return request(`/api/jobs/${encodeURIComponent(String(jobId))}`);
}

function unwrapValue<T>(raw: T | { value?: T; data?: T }): T {
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    const record = raw as { value?: T; data?: T };
    if (record.value !== undefined) return record.value;
    if (record.data !== undefined) return record.data;
  }
  return raw as T;
}

export async function openPicker(kind: "file" | "directory"): Promise<PickerResult> {
  const pickerId = await createPicker(kind);
  return { picker_id: pickerId, id: pickerId, status: "pending" };
}

export async function getPicker(pickerId: string | number): Promise<PickerResult> {
  const raw = await request<PickerResult | { picker?: PickerResult }>(`/api/pickers/${encodeURIComponent(String(pickerId))}`);
  const record = raw as { picker?: PickerResult };
  return record.picker ?? unwrapValue(raw as PickerResult);
}

export async function registerVideo(payload: { video_path: string; filename?: string; display_name?: string }): Promise<Video> {
  const raw = await request<Video | { video?: Video; value?: Video }>("/api/videos/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: payload.video_path, filename: payload.filename, display_name: payload.display_name }),
  });
  const record = raw as { video?: Video; value?: Video };
  return normalizeVideo(record.video ?? record.value ?? raw as Video);
}

export async function pauseAsrDownload(jobId: string | number): Promise<void> {
  await request(`/api/jobs/${encodeURIComponent(String(jobId))}/download/pause`, { method: "POST" });
}

export async function resumeAsrDownload(modelId: string): Promise<{ job_id: string | number }> {
  return request(`/api/asr/models/${encodeURIComponent(modelId)}/download/resume`, { method: "POST" });
}

export async function cleanupAsrDownload(modelId: string): Promise<{ job_id: string | number }> {
  return request(`/api/asr/models/${encodeURIComponent(modelId)}/download/incomplete`, { method: "DELETE" });
}
export async function createAiConnection(name: string, sourceId?: string, settings?: Omit<AiSettings, "has_api_key">, credentials?: AiCredentialsDraft): Promise<AiSettings> {
  return normalizeAiSettings(await request("/api/settings/ai/connections", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, ...(sourceId ? { source_id: sourceId } : {}), ...(settings ? { settings } : {}), ...credentials }),
  }));
}

export async function activateAiConnection(id: string): Promise<AiSettings> {
  return normalizeAiSettings(await request(`/api/settings/ai/connections/${encodeURIComponent(id)}/activate`, { method: "POST" }));
}

export async function renameAiConnection(id: string, name: string): Promise<AiSettings> {
  return normalizeAiSettings(await request(`/api/settings/ai/connections/${encodeURIComponent(id)}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }),
  }));
}

export async function deleteAiConnection(id: string): Promise<AiSettings> {
  return normalizeAiSettings(await request(`/api/settings/ai/connections/${encodeURIComponent(id)}`, { method: "DELETE" }));
}

export async function uploadVideo(file: File): Promise<{ video: Video; existed: boolean }> {
  const raw = await request<{ video: Video; existed: boolean }>(
    `/api/videos/upload?filename=${encodeURIComponent(file.name)}`,
    { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file },
  );
  return { ...raw, video: normalizeVideo(raw.video) };
}

export async function renameVideoFile(videoId: number, filename: string): Promise<Video> {
  return normalizeVideo(await request<Video>(`/api/videos/${videoId}/filename`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename }),
  }));
}

export async function getVideoProcessingOptions(videoId: number): Promise<ProcessingOptions> {
  const raw = await request<ProcessingOptions | { options?: ProcessingOptions }>(`/api/videos/${videoId}/processing-options`);
  const record = raw as { options?: ProcessingOptions };
  return record.options ?? unwrapValue(raw);
}

export async function processVideo(videoId: number, payload: Record<string, unknown> = {}): Promise<Job> {
  const raw = await request<Job | { job?: Job }>(`/api/videos/${videoId}/process`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const record = raw as { job?: Job };
  return normalizeJob(record.job ?? unwrapValue(raw as Job));
}

export interface BatchProcessResult {
  job_id: string | number;
  status: string;
  job_type?: string;
  folders?: Array<{
    path: string;
    name: string;
    folder_id: number;
    video_ids: number[];
    video_count: number;
  }>;
  video_ids?: number[];
  total?: number;
  errors?: string[];
}

export async function getAnalysisRuns(videoId: number): Promise<AnalysisRun[]> {
  const value = await request<AnalysisRun[]>(`/api/videos/${videoId}/analysis-runs`);
  return Array.isArray(value) ? value : [];
}

export async function getRecallSchemes(): Promise<RecallScheme[]> {
  const value = await request<RecallScheme[]>("/api/settings/recall-schemes");
  return Array.isArray(value) ? value : [];
}

export async function saveRecallScheme(value: RecallSchemeInput, id?: string): Promise<RecallScheme> {
  return request<RecallScheme>(`/api/settings/recall-schemes${id ? `/${encodeURIComponent(id)}` : ""}`, {
    method: id ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
  });
}

export function previewRecallPrompt(value: Omit<RecallSchemeInput, "name">): Promise<RecallPromptPreview> {
  return request<RecallPromptPreview>("/api/settings/recall-schemes/prompt-preview", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
  });
}

export async function archiveRecallScheme(id: string): Promise<void> {
  await request(`/api/settings/recall-schemes/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function analyzeVideoRecall(videoId: number, mode: RecallMode): Promise<Job> {
  return normalizeJob(await request<Job>(`/api/videos/${videoId}/analysis`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ analysis_mode: mode }),
  }));
}

export interface BatchPreview {
  valid: boolean;
  folders: Array<{
    path: string;
    video_count: number;
    non_video_count: number;
    subfolder_count: number;
    examples: string[];
    valid: boolean;
    error: string;
  }>;
}

export async function previewBatch(folders: string[]): Promise<BatchPreview> {
  return request<BatchPreview>("/api/batch/preview", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ folders }),
  });
}

export async function getLatestBatch(): Promise<Job | null> {
  const job = await request<Job | null>("/api/batch/latest");
  return job ? normalizeJob(job) : null;
}

export async function retryBatch(jobId: string | number): Promise<Job> {
  return normalizeJob(await request<Job>(`/api/jobs/${jobId}/retry`, { method: "POST" }));
}

export async function processBatch(
  folders: string[],
  payload: Record<string, unknown> = {},
): Promise<BatchProcessResult> {
  return request<BatchProcessResult>("/api/batch/process", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, folders }),
  });
}

export async function processSelectedVideos(
  videoIds: number[], payload: Record<string, unknown> = {},
): Promise<BatchProcessResult> {
  return request<BatchProcessResult>("/api/batch/process", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...payload, video_ids: videoIds }),
  });
}

export async function processFolder(
  folderId: number,
  payload: Record<string, unknown> = {},
): Promise<BatchProcessResult> {
  return request<BatchProcessResult>(`/api/folders/${folderId}/process`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export async function retryVideo(videoId: number, payload: Record<string, unknown> = {}): Promise<Job> {
  const raw = await request<Job | { job?: Job }>(`/api/videos/${videoId}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const record = raw as { job?: Job };
  return normalizeJob(record.job ?? unwrapValue(raw as Job));
}

export async function getJob(jobId: string | number): Promise<Job> {
  const raw = await request<Job | { job?: Job }>(`/api/jobs/${encodeURIComponent(String(jobId))}`);
  const record = raw as { job?: Job };
  return normalizeJob(record.job ?? unwrapValue(raw as Job));
}

export async function cancelJob(jobId: string | number): Promise<Job> {
  const raw = await request<Job | { job?: Job }>(
    `/api/jobs/${encodeURIComponent(String(jobId))}/cancel`,
    { method: "POST" },
  );
  const record = raw as { job?: Job };
  return normalizeJob(record.job ?? unwrapValue(raw as Job));
}

export async function resumeJob(jobId: string | number, skipCurrent = false): Promise<Job> {
  const raw = await request<Job>(`/api/jobs/${encodeURIComponent(String(jobId))}/resume`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skip_current: skipCurrent }),
  });
  return normalizeJob(raw);
}

export async function getPausedJobs(): Promise<Job[]> {
  return (await request<Job[]>("/api/jobs/paused")).map(normalizeJob);
}

function normalizeJob(raw: Job): Job {
  const rawProgress = Number(raw.progress ?? 0);
  return {
    ...raw,
    id: raw.id ?? raw.job_id ?? "",
    job_id: raw.job_id ?? raw.id,
    progress: rawProgress > 0 && rawProgress <= 1 ? rawProgress * 100 : rawProgress,
    status: raw.status ?? "queued",
  };
}
