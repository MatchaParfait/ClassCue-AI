import type { Marker, Subtitle } from "../types";
import type { SpacePhase } from "./taskState";
export interface SpaceSnapshot { subtitles: Subtitle[]; markers: Marker[]; phase: SpacePhase; reduced: boolean; resultScope?: string; paused?: boolean }
export interface TaskSpace { update(snapshot: SpaceSnapshot): void; select(id: number | null, options?: { travel?: boolean }): void; resetView(): void; dispose(): void }
export interface EvidenceCue { id: number; serial: number; phase: "travel" | "arrival" | "still" }
export function createTaskSpace(canvas: HTMLCanvasElement, options: {
  analysisQuality?: "low" | "high";
  analysisVariant?: "gravity" | "nailong" | "dafeiyu" | "dafeiyu31" | "dafeiyu-rice" | "gpt" | "particle";
  /** Presentation speed for the isolated settings demo. Real tasks default to 1. */
  analysisPlaybackRate?: number;
  key: string; onSelect(id: number): void; onError(message: string): void; onQuality(message: string): void;
  onReady?(animatedEntrance: boolean): void;
  onEvidence?(cue: EvidenceCue | null): void;
}): TaskSpace;
