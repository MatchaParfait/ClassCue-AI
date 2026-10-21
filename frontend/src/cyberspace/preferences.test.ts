import { expect, it } from "vitest";
import { resolveAnalysisVariant } from "./preferences";

it("matches only the model name, case-insensitively, with DeepSeek taking precedence", () => {
  for (const name of ["DeepSeek", " deepseek-chat ", "vendor/DEEPSEEK-R1", "deepseek-gpt"]) {
    expect(["dafeiyu", "dafeiyu31", "dafeiyu-rice"]).toContain(resolveAnalysisVariant("auto", name, "job-1"));
  }
  for (const name of ["GPT-5", "gpt-4.1-mini", "vendor/gPt-test"]) {
    expect(resolveAnalysisVariant("auto", name, "job-1")).toBe("gpt");
  }
  for (const name of [undefined, "", "Qwen", "o3"]) {
    expect(["gravity", "nailong"]).toContain(resolveAnalysisVariant("auto", name, "job-1"));
  }
});

it("keeps a task stable and distributes different tasks across all allowed variants", () => {
  for (const [model, count] of [["deepseek-chat", 3], ["other", 2]] as const) {
    const choices = new Set();
    for (let id = 0; id < 100; id++) {
      const key = `library:video:${id}`;
      const variant = resolveAnalysisVariant("auto", model, key);
      expect(resolveAnalysisVariant("auto", model, key)).toBe(variant);
      choices.add(variant);
    }
    expect(choices.size).toBe(count);
  }
  expect(resolveAnalysisVariant("nailong", "gpt-5", "job")).toBe("nailong");
  expect(resolveAnalysisVariant("gpt", "deepseek-chat", "job")).toBe("gpt");
});
