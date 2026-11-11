import { request } from "./client";

export type SlideBackend = "auto" | "cpu" | "cuda";

export interface SlideAcceleration {
  ready: boolean;
  runtime_ready: boolean;
  gpu_detected: boolean;
  gpu_prepare_available: boolean;
  gpu_name: string | null;
  gpu_unavailable_reason: string | null;
  message: string;
}

export interface SlideResources extends SlideAcceleration {
  runtime_size_bytes: number;
  download_cache_size_bytes: number;
  shared_download_cache_bytes?: number;
  busy: boolean;
  active_job_id: string | null;
  active_job_kind: "slide_prepare" | "slide_cache_cleanup" | null;
}

export function getSlideResources(refresh = false) {
  return request<SlideResources>(`/api/slides/resources${refresh ? "?refresh=true" : ""}`);
}

export function clearSlideResources(scope: "runtime" | "downloads" | "all") {
  return request<{ job_id: string; job_type: string }>("/api/slides/cache/cleanup", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scope }),
  });
}

export function getSlideAcceleration(refresh = false) {
  return request<SlideAcceleration>(`/api/slides/acceleration${refresh ? "?refresh=true" : ""}`);
}

export function getSlideSelection() {
  return request<{ backend: SlideBackend }>("/api/slides/selection");
}

export function saveSlideSelection(backend: SlideBackend) {
  return request<{ backend: SlideBackend }>("/api/slides/selection", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ backend }),
  });
}

export function prepareSlideAcceleration() {
  return request<{ job_id: string }>("/api/slides/acceleration/prepare", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
  });
}
