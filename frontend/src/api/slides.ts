import { request } from "./client";

export type SlideBackend = "auto" | "cpu" | "cuda";

export function getSlideAcceleration() {
  return request<{ ready: boolean; gpu_name: string | null; message: string }>("/api/slides/acceleration");
}

export function prepareSlideAcceleration() {
  return request<{ job_id: string }>("/api/slides/acceleration/prepare", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
  });
}
