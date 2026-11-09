import { afterEach, expect, it, vi } from "vitest";

afterEach(() => { vi.unstubAllEnvs(); vi.resetModules(); });

it.each([undefined, "", "false", "1", "TRUE", "true"])("requires an explicit true to enable materials (%s)", async value => {
  vi.resetModules();
  vi.stubEnv("VITE_ENABLE_MATERIALS", value);
  const { MATERIALS_ENABLED } = await import("./featureFlags");
  expect(MATERIALS_ENABLED).toBe(value === "true");
});

it.each([undefined, "", "false", "1", "TRUE", "true"])("hides unfinished Bilibili study unless explicitly enabled (%s)", async value => {
  vi.resetModules();
  vi.stubEnv("VITE_ENABLE_BILIBILI_STUDY", value);
  const { BILIBILI_STUDY_ENABLED } = await import("./featureFlags");
  expect(BILIBILI_STUDY_ENABLED).toBe(value === "true");
});
