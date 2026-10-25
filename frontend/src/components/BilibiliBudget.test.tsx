import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { BilibiliBudget, budgetLimitReason } from "./BilibiliBudget";
import type { BiliRun } from "../api/bilibili";

let host: HTMLDivElement, root: ReturnType<typeof createRoot>;
const limits = { platform: 60, llm: 48, videos: 10, attempts: 15, verified: 5, seconds: 1800, tokens: 600000 };

function run(overrides: Partial<BiliRun> = {}) {
  return {
    id: "synthetic", status: "checking", stop_reason: "", budget: {
      platform: 24, llm: 3, videos: 10, attempts: 0, verified: 0, seconds: 125, tokens: 10000,
    }, limits: { ...limits }, ...overrides,
  } as BiliRun;
}
function render(value: BiliRun) { act(() => root.render(<BilibiliBudget run={value} />)); }

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); });

describe("Bilibili budget display", () => {
  it("shows all seven balances instead of only requests and verification slots", () => {
    render(run());
    expect(host.querySelectorAll("tbody tr")).toHaveLength(7);
    expect(host.querySelector('[data-budget-resource="platform"]')?.textContent).toBe("B 站接口请求246036");
    expect(host.querySelector('[data-budget-resource="llm"]')?.textContent).toBe("AI 请求34845");
    expect(host.querySelector('[data-budget-resource="videos"]')?.textContent).toBe("视频详情尝试10100");
    expect(host.querySelector('[data-budget-resource="seconds"]')?.textContent).toContain("2:05");
    expect(host.querySelector('[data-budget-resource="seconds"]')?.textContent).toContain("30:00");
    expect(host.textContent).toContain("不代表实际 Token 或费用");
    expect(host.querySelector('[role="status"]')).toBeNull();
  });

  it("names the exhausted resource even when requests and verification slots remain", () => {
    render(run({ status: "budget_exhausted", stop_reason: "budget", budget_stop: {
      scope: "task", phase: "course", resource: "videos", used: 10, limit: 10, requested: 1,
    } }));
    expect(host.querySelector('[role="status"]')?.textContent).toContain("视频详情尝试已达到本次任务上限");
    expect(host.querySelector('[role="status"]')?.textContent).toContain("已用 10 / 上限 10");
    expect(host.querySelector('[data-budget-resource="videos"]')?.className).toBe("bili-budget-hit");
    expect(host.querySelector('[data-budget-resource="platform"]')?.textContent).toContain("36");
    expect(host.textContent).not.toContain("本次预算已用完");
  });

  it("explains predictive limits and the synthesis reserve", () => {
    render(run({ status: "budget_exhausted", stop_reason: "budget", budget_stop: {
      scope: "task", resource: "llm", used: 47, limit: 48, requested: 2, reserve: 1,
    } }));
    const message = host.querySelector('[role="status"]')?.textContent;
    expect(message).toContain("AI 请求下一步预计超出");
    expect(message).toContain("下一步需 2，含综合核验预留 1");
    expect(message).not.toContain("AI 请求已达到");
  });

  it("does not invent a cause for old records or unknown transport limits", () => {
    render(run({ status: "budget_exhausted", stop_reason: "budget" }));
    expect(host.querySelector('[role="status"]')?.textContent).toContain("未保存具体触发项");
    expect(host.querySelector(".bili-budget-hit")).toBeNull();
    expect(budgetLimitReason({ scope: "transport", resource: null, used: null, limit: null, requested: null }))
      .toContain("未保存具体触发项");
  });

  it("shows exact-phase caps separately without treating fallback as task failure", () => {
    render(run({ phase: "course", phase_limits: { exact: { videos: 6, platform: 30 } } }));
    expect(host.querySelector(".bili-phase-budget summary")?.textContent).toBe("精准阶段上限");
    expect(host.querySelectorAll(".bili-phase-budget dl > div")).toHaveLength(2);
    expect(host.querySelector(".bili-phase-budget")?.textContent).toContain("视频详情尝试6");
    expect(host.querySelector('[data-budget-resource="videos"]')?.textContent).toBe("视频详情尝试10100");
    expect(host.querySelector('[role="status"]')).toBeNull();
    expect(budgetLimitReason({ scope: "exact", resource: "videos", used: 6, limit: 6, requested: 1 }))
      .toContain("精准阶段上限");
  });

  it("shows missing counters as unknown and never displays NaN or a negative balance", () => {
    render(run({ budget: { platform: 61 } }));
    expect(host.querySelector('[data-budget-resource="platform"]')?.textContent).toBe("B 站接口请求61600");
    expect(host.querySelector('[data-budget-resource="llm"]')?.textContent).toBe("AI 请求-48-");
    expect(host.textContent).not.toContain("NaN");
    expect(host.querySelector(".bili-phase-budget")).toBeNull();
  });

  it("removes a previous stop message after resuming while keeping usage visible", () => {
    render(run({ status: "budget_exhausted", budget_stop: {
      scope: "task", resource: "attempts", used: 15, limit: 15, requested: 1,
    } }));
    expect(host.querySelector('[role="status"]')).not.toBeNull();
    render(run({ limits: { ...limits, attempts: 30 } }));
    expect(host.querySelector('[role="status"]')).toBeNull();
    expect(host.querySelector('[data-budget-resource="attempts"]')?.textContent).toBe("字幕读取尝试03030");
  });
});
