import { describe, expect, it } from "vitest";
import { buildProcessPayload, RECALL_SELECTION_REQUIRED, type ProcessForm } from "./processOptions";

const form: ProcessForm = {
  displayName: "",
  recallModes: ["medium_recall"],
  keywords: "",
  filterFormula: "",
  filterLowInformation: true,
};

describe("processing request options", () => {
  it("rejects an explicitly cleared recall selection before building a request", () => {
    expect(() => buildProcessPayload({ ...form, recallModes: [] })).toThrow(RECALL_SELECTION_REQUIRED);
  });

  it("disables low-information filtering even for retained legacy form state", () => {
    expect(buildProcessPayload({ ...form, filterLowInformation: true }).filter_formula_noise).toBe(false);
    expect(buildProcessPayload({ ...form, filterLowInformation: false }).filter_formula_noise).toBe(false);
  });

  it("replaces subtitles only when explicitly requested", () => {
    expect(buildProcessPayload(form).force_retranscribe).toBe(false);
    expect(buildProcessPayload({ ...form, forceRetranscribe: true }).force_retranscribe).toBe(true);
  });
  it("sends only the selected recall modes", () => {
    for (const recallMode of ["high_recall", "medium_recall", "low_recall"] as const) {
      const payload = buildProcessPayload({ ...form, recallModes: [recallMode] });
      expect(payload.analysis_mode).toBe(recallMode);
      expect(payload.analysis_modes).toEqual([recallMode]);
      expect(payload).not.toHaveProperty("exam_focus");
      expect(payload).not.toHaveProperty("extension_focus");
    }
  });
  it("sends several recall modes without duplicates", () => {
    expect(buildProcessPayload({ ...form, recallModes: ["medium_recall", "high_recall", "medium_recall", "low_recall"] }).analysis_modes)
      .toEqual(["medium_recall", "high_recall", "low_recall"]);
  });

  it("sends normalized keywords and the actual formula filter", () => {
    const payload = buildProcessPayload({
      ...form, keywords: " KKT，单纯形法;KKT\n最优解 ", filterFormula: " x+y，a+b ",
    });
    expect(payload.keywords).toEqual(["KKT", "单纯形法", "最优解"]);
    expect(payload.use_keyword_prefilter).toBe(true);
    expect(payload.filter_formula).toBe("x+y，a+b");
  });

  it("does not enable prefiltering for separators alone", () => {
    const payload = buildProcessPayload({ ...form, keywords: "，,;\n" });
    expect(payload.use_keyword_prefilter).toBe(false);
    expect(payload.keywords).toEqual([]);
    expect(payload.filter_formula).toBeNull();
  });
});
