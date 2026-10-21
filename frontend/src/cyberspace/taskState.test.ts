import { describe, expect, it } from "vitest";
import { canReadSavedText, savedMarkers, spacePhase } from "./taskState";
import type { Job, Marker } from "../types";

describe("real task visualization state", () => {
  it.each([
    ["running", "transcribing", "transcribing"], ["running", "analyzing", "analyzing"],
    ["running", "saving_markers", "analyzing"], ["queued", "analyzing", "preparing"],
    ["completed", "transcribing", "completed"], ["resource_paused", "analyzing", "paused"],
    ["awaiting_api", "analyzing", "waiting"], ["failed", "analyzing", "failed"],
    ["interrupted", "transcribing", "stopped"], ["cancelled", "analyzing", "stopped"],
  ])("maps %s / %s without guessing progress", (status, stage, expected) => {
    expect(spacePhase({ id: 1, status, stage, progress: 100 })).toBe(expected);
  });
  it("does not interpret an old transcript as fresh ASR output", () => {
    expect(canReadSavedText("transcribing")).toBe(false);
    expect(canReadSavedText("preparing")).toBe(false);
    expect(canReadSavedText("analyzing")).toBe(true);
    expect(spacePhase({ id: 1, status: "running", stage: "transcribing", preparation: { active: true } } as Job)).toBe("preparing");
  });
  it("separates recall modes without mutating importance or adjusted timestamps", () => {
    const markers = [
      { id: 1, analysis_mode: "medium_recall", review_status: "active", importance: "critical", user_start_ms: 4000 },
      { id: 2, analysis_mode: "high_recall", review_status: "active" },
      { id: 3, analysis_mode: "medium_recall", review_status: "excluded" },
    ] as Marker[];
    expect(savedMarkers(markers, "medium_recall")).toEqual([markers[0]]);
    expect(markers[0].importance).toBe("critical");
    expect(markers[0].user_start_ms).toBe(4000);
  });
});
