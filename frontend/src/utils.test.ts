import { describe, expect, it } from "vitest";
import { canOpenReview, filterMarkers, formatTime, markerJumpTime, parseTimeInput } from "./utils";
import type { Marker } from "./types";

const markers: Marker[] = [
  { id: 2, video_id: 1, start_ms: 20_000, end_ms: 30_000, title: "二", note: "", source_quote: "", analysis_mode: "strict_exam_intent", review_status: "mastered" },
  { id: 1, video_id: 1, start_ms: 10_000, end_ms: 20_000, title: "一", note: "", source_quote: "", analysis_mode: "strict_exam_intent", review_status: "active" },
  { id: 3, video_id: 1, start_ms: 30_000, end_ms: 40_000, title: "三", note: "", source_quote: "", analysis_mode: "high_recall", review_status: "active" },
];

describe("study helpers", () => {
  it("allows saved subtitles to be inspected after AI failure without claiming AI readiness", () => {
    expect(canOpenReview("failed", true)).toBe(true);
    expect(canOpenReview("transcribed", true)).toBe(true);
    expect(canOpenReview("failed", false)).toBe(false);
    expect(canOpenReview("uploaded", false)).toBe(false);
    expect(canOpenReview("processing", true)).toBe(false);
    expect(canOpenReview("review_ready", false)).toBe(true);
  });
  it("formats timestamps for the review UI", () => {
    expect(formatTime(65_000)).toBe("1:05");
    expect(formatTime(3_661_000)).toBe("1:01:01");
  });

  it("prefers the custom jump time including zero and falls back to the original time", () => {
    expect(markerJumpTime(markers[0])).toBe(20_000);
    expect(markerJumpTime({ ...markers[0], user_start_ms: null })).toBe(20_000);
    expect(markerJumpTime({ ...markers[0], user_start_ms: 0 })).toBe(0);
    expect(markerJumpTime({ ...markers[0], user_start_ms: 7_000 })).toBe(7_000);
  });

  it.each([
    ["7:30", 450_000], ["1:07:30", 4_050_000], ["0:00", 0], [" 75:30 ", 4_530_000],
    ["7.30", null], ["-1:30", null], ["7:60", null], ["1:99:00", null],
    ["", null], ["7:3", null], ["7:30:99", null], ["abc", null],
    ["9999999999999999999999:00", null],
  ])("parses or rejects the timestamp %s", (value, expected) => {
    expect(parseTimeInput(value as string)).toBe(expected);
  });

  it("keeps the original order even when jump times cross", () => {
    const items = markers.map((marker) => ({ ...marker, user_start_ms: marker.id === 2 ? 0 : 40_000 }));
    expect(filterMarkers(items, "strict_exam_intent", "all", new Set()).map((marker) => marker.id)).toEqual([1, 2]);
  });

  it("filters by this visit's viewed IDs, independently of saved mastery", () => {
    expect(filterMarkers(markers, "strict_exam_intent", "unviewed", new Set()).map((marker) => marker.id)).toEqual([1, 2]);
    expect(filterMarkers(markers, "strict_exam_intent", "unviewed", new Set([1])).map((marker) => marker.id)).toEqual([2]);
    expect(filterMarkers(markers, "strict_exam_intent", "viewed", new Set([1])).map((marker) => marker.id)).toEqual([1]);
    expect(filterMarkers(markers, "strict_exam_intent", "all", new Set([1])).map((marker) => marker.id)).toEqual([1, 2]);
  });

  it("keeps excluded items out of every view filter", () => {
    const items = [...markers, { ...markers[0], id: 4, review_status: "excluded" as const }];
    for (const filter of ["all", "unviewed", "viewed"] as const) {
      expect(filterMarkers(items, "strict_exam_intent", filter, new Set([4])).some((marker) => marker.id === 4)).toBe(false);
    }
  });
});
