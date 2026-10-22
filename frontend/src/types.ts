export type BuiltinRecallMode = "high_recall" | "medium_recall" | "low_recall";
export type RecallMode = BuiltinRecallMode | `custom_${string}`;
export type MarkerMode = RecallMode | "strict_exam_intent";
export interface TeacherRule {
  phrase: string;
  meaning: "emphasis" | "ordinary" | "deemphasize";
}
export interface RecallSchemeInput {
  name: string;
  base_mode: BuiltinRecallMode;
  rules: TeacherRule[];
  notes: string;
  advanced_prompt?: string | null;
}
export interface RecallPromptPreview {
  prompt: string;
  protocol: string;
  messages: { role: string; content: string }[];
  repair: string;
  evidence_repair: string;
  advanced: boolean;
}
export interface RecallScheme extends RecallSchemeInput {
  id: `custom_${string}`;
  revision: number;
  archived: boolean;
}
export type ReviewStatus = "active" | "mastered" | "excluded";
export type ReviewFilter = "all" | "unviewed" | "viewed";
export type MarkerImportance = "standard" | "important" | "critical";

export interface Video {
  id: number;
  display_name: string;
  filename: string;
  duration_ms: number;
  video_path: string;
  source_size?: number;
  source_folder?: string;
  source_mtime_ns?: number;
  folder_id?: number | null;
  library_order?: number;
  created_at?: string;
  status: string;
  file_change_block_reason?: string | null;
  library_id?: string;
  has_transcript?: boolean;
  transcription_model?: string;
  storage_warning?: string | null;
  marker_importance_counts?: Record<MarkerImportance, number>;
}

export interface VideoLibraryFolder {
  id: string;
  name: string;
  createdAt: number;
  parentId?: string | null;
  videoCount?: number;
  markerImportanceCounts?: Record<MarkerImportance, number>;
}

export interface VideoLibraryState {
  folders: VideoLibraryFolder[];
  assignments: Record<string, string | null>;
  rootOrder: string[];
  folderOrders: Record<string, string[]>;
}

export interface Subtitle {
  id: number;
  start_ms: number;
  end_ms: number;
  text: string;
}

export interface SubtitleSearchHit {
  video_id: number;
  video_name: string;
  filename: string;
  video_status: string;
  duration_ms: number;
  folder_id: number | null;
  folder_name: string | null;
  video_match_count: number;
  segment_id: number;
  start_ms: number;
  end_ms: number;
  text: string;
  snippet: string;
  snippet_parts: { text: string; match: boolean }[];
}

export interface SubtitleSearchResult {
  query: string;
  library_id: string;
  total: number;
  total_videos: number;
  limit: number;
  offset: number;
  has_more: boolean;
  items: SubtitleSearchHit[];
}

export interface Marker {
  id: number;
  video_id: number;
  start_ms: number;
  end_ms: number;
  title: string;
  user_start_ms?: number | null;
  user_start_updated_at?: string | null;
  note: string;
  source_quote: string;
  trigger_phrase?: string | null;
  confidence?: number | null;
  analysis_mode: MarkerMode;
  review_status: ReviewStatus;
  importance?: MarkerImportance;
  preview?: boolean;
}

export interface AnalysisPreview {
  video_id: number;
  revision: number;
  active_mode: MarkerMode | null;
  modes: Partial<Record<MarkerMode, {
    completed_chunks: number;
    total_chunks: number;
    current_chunk: number | null;
    candidate_count: number;
    truncated: boolean;
    markers: Marker[];
  }>>;
}

export interface AnalysisRun {
  analysis_mode: MarkerMode;
  status: string;
  candidate_count: number;
  updated_at?: string;
  scheme_name?: string | null;
  scheme_revision?: number;
  latest_scheme_revision?: number | null;
  archived?: boolean | null;
}

export interface StudyDataSource {
  video: "api" | "mock";
  subtitles: "api" | "mock";
  markers: "api" | "mock";
}

export interface LibraryStatus {
  library_id?: string;
  library_scope?: string;
  data_root: string;
  database_path: string;
  temp_audio_dir: string;
  video_count: number;
  active_job_id?: number | string | null;
  processing?: boolean;
}

export interface StorageDirectories {
  application_data?: ApplicationDataStatus;
  media_storage_root?: string | null;
  media_storage_bound?: boolean;
  media_storage_default_root?: string;
  library_id: string;
  data_root: string;
  asr_storage_root: string;
  audio_cache_dir: string;
  audio_cache_default_dir: string;
  video_library_dir: string;
  video_library_default_dir: string;
  delete_source_after_import?: boolean;
  log_dir: string;
  model_dir: string;
  runtime_dir: string;
  package_cache_dir: string;
  active_asr_storage_root: string;
  active_log_dir: string;
  restart_required: boolean;
  busy: boolean;
  has_paused_jobs: boolean;
  config_dir: string;
  migration_job?: Job | null;
}

export interface ApplicationDataStatus {
  root: string | null;
  database_dir: string;
  config_dir: string;
  database_bytes: number;
  config_bytes: number;
  migration: { id: string; status: string; message: string; destination: string } | null;
}

export interface ApplicationDataPlan {
  destination: string;
  database_dir: string;
  config_dir: string;
  files_total: number;
  bytes_total: number;
  free_bytes: number;
  plan_id: string;
}

export type StorageDirectoryValues = Pick<StorageDirectories, "asr_storage_root" | "log_dir" | "delete_source_after_import">
  & Partial<Pick<StorageDirectories, "media_storage_root" | "audio_cache_dir" | "video_library_dir">>;

export interface StorageMigrationPlan {
  plan_id: string;
  files_total: number;
  bytes_total: number;
  sections: Array<{ label: string; source: string; target: string; files: number; bytes: number }>;
  disks: Array<{ path: string; required_bytes: number; free_bytes: number }>;
  warnings: string[];
}

export interface StorageMigrationProgress {
  phase: "copying" | "verifying" | "committing" | "cleaning" | "completed";
  files_total: number;
  files_completed: number;
  bytes_total: number;
  bytes_completed: number;
  current_file?: string | null;
  warnings: string[];
}

export interface AsrModel {
  id: string;
  name: string;
  engine: string;
  selectable?: boolean;
  device?: "gpu" | "cpu";
  gpu_available?: boolean;
  gpu_runtime_ready?: boolean;
  gpu_prepare_available?: boolean;
  gpu_unavailable_reason?: string;
  gpu_name?: string;
  gpu_total_memory_mib?: number | null;
  gpu_batch_size?: number | null;
  cpu_notice?: string;
  cpu_available?: boolean | null;
  downloaded: boolean | null;
  cache_size_bytes: number | null;
  runtime_size_bytes?: number | null;
  inspection_status?: "unknown" | "cached" | "ready";
  status_checked_at?: number | null;
  qwen_ready?: boolean | null;
  /** FireRedASR2-AED readiness, when reported by the backend. */
  aed_ready?: boolean | null;
  /** Whether this model includes the AED decoder/capability. */
  supports_aed?: boolean;
  /** Optional backend-provided capability labels for richer model cards. */
  capabilities?: string[];
  model_family?: string;
  recommendation?: string;
  selected: boolean;
  status?: string;
  error_message?: string | null;
  active_job_id?: string | number | null;
  active_job_kind?: "preparing" | "deleting" | "cleaning_partial" | "cleaning_runtime" | null;
  download_state?: ModelDownloadState | null;
}

export interface ModelDownloadState {
  status: "preparing" | "paused" | "interrupted";
  retained_bytes: number;
  retained_bytes_estimated?: boolean;
  resumable?: boolean;
  completed_bytes: number;
  partial_bytes: number;
  temporary_files: number;
  can_cleanup: boolean;
  bytes_total?: number | null;
  current_file?: string | null;
  device?: "cpu" | "gpu" | null;
}

export interface AiSettings {
  connection_id?: string;
  connection_name?: string;
  connections?: { id: string; name: string; base_url: string; model: string; has_api_key: boolean }[];
  provider: "deepseek" | "openai_compatible" | string;
  base_url: string;
  model: string;
  output_tokens: number;
  token_parameter: string;
  json_mode: boolean;
  timeout_seconds: number;
  max_retries: number;
  request_concurrency?: number;
  reasoning_effort?: "low" | "medium" | "high" | null;
  has_api_key: boolean;
}

export interface JobStatus {
  id: number | string;
  status: "queued" | "running" | "completed" | "failed" | "interrupted" | string;
  stage?: string;
  progress?: number;
  message?: string;
  error_message?: string | null;
  resource?: FireRedResource | null;
  preparation?: PreparationProgress | null;
  cleanup?: CleanupProgress | null;
  migration?: StorageMigrationProgress | null;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface CleanupProgress {
  phase: "checking" | "scanning" | "deleting" | "completed";
  scope: string;
  message: string;
  current_target?: string;
  current_path?: string | null;
  files_scanned: number;
  directories_scanned: number;
  files_deleted: number;
  directories_deleted: number;
  files_total: number | null;
  directories_total: number | null;
  bytes_deleted: number;
  bytes_total: number | null;
  phase_progress: number | null;
  eta_seconds: number | null;
  elapsed_seconds: number;
  updated_at: number;
}

export interface PreparationProgress {
  active?: boolean;
  phase: "checking" | "installing" | "metadata" | "downloading" | "verifying" | "finalizing";
  label: string;
  message: string;
  phase_progress?: number | null;
  component?: string;
  repository?: string;
  source?: string;
  current_file?: string;
  current_package?: string;
  bytes_completed?: number;
  bytes_total?: number;
  file_bytes_completed?: number;
  file_bytes_total?: number;
  checked_bytes?: number;
  check_total_bytes?: number;
  speed_bytes_per_second?: number | null;
  eta_seconds?: number | null;
  elapsed_seconds?: number;
  updated_at: number;
  waiting_for_network?: boolean;
  step?: number;
  steps?: number;
}

export interface FireRedResource {
  engine: "firered";
  device: string;
  gpu_total_mib?: number | null;
  installed_ram_mib?: number | null;
  initial_batch_size: number;
  current_batch_size?: number;
  completed_segments?: number;
  total_segments?: number;
  recovery_count?: number;
  recovery_reason?: string | null;
  cuda_available?: boolean | null;
  warning?: string;
  status?: string;
  cache_hit?: boolean;
}

export interface PickerResult {
  id?: string | number;
  picker_id?: string | number;
  status?: string;
  path?: string;
  message?: string;
  error?: string;
}

export interface ProcessingOptions {
  video_id?: number;
  asr_model?: AsrModel | string;
  asr_model_name?: string;
  ai_service?: unknown;
  ai_service_name?: string;
  active_job?: Job | null;
  [key: string]: unknown;
}

export interface Job extends JobStatus {
  model_id?: string | null;
  job_id?: string | number;
  video_id?: number;
  job_type?: string;
  import_only?: boolean;
  analysis_modes?: MarkerMode[];
  analysis_schemes?: Record<string, Pick<RecallScheme, "id" | "name" | "base_mode" | "revision">>;
  started_at?: string | null;
  finished_at?: string | null;
  batch_summary?: {
    total: number;
    completed: number;
    skipped: number;
    failed: number;
    awaiting_ai: number;
    pending_asr: number;
    retryable?: number;
  };
  batch_entries?: Array<{
    video_id: number | null;
    source_path?: string;
    label?: string;
    status: string;
    error?: string | null;
    warning?: string | null;
  }>;
  folders?: Array<{ folder_id: number; name: string; path: string }>;
}
