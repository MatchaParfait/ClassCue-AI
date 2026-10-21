import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RecallSchemeSettings } from "./RecallSchemeSettings";
import { UnsavedChangesProvider } from "./UnsavedChanges";
import { recallSchemesKey } from "../hooks/useRecallSchemes";
import * as api from "../api/client";
import type { RecallScheme } from "../types";

vi.mock("../api/client", () => ({
  getRecallSchemes: vi.fn(), saveRecallScheme: vi.fn(), archiveRecallScheme: vi.fn(), previewRecallPrompt: vi.fn(),
}));
const first: RecallScheme = {
  id: `custom_${"a".repeat(32)}`, name: "张老师", base_mode: "medium_recall", revision: 1, archived: false,
  rules: [{ phrase: "拿笔画下来", meaning: "emphasis" }], notes: "",
};
let host: HTMLDivElement, root: Root, client: QueryClient;
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  vi.mocked(api.getRecallSchemes).mockResolvedValue([first]);
  vi.mocked(api.saveRecallScheme).mockImplementation(async (value, id) => ({
    ...value, id: (id ?? `custom_${"b".repeat(32)}`) as RecallScheme["id"], revision: 1, archived: false,
  }));
  vi.mocked(api.archiveRecallScheme).mockResolvedValue(undefined);
  vi.mocked(api.previewRecallPrompt).mockImplementation(async value => ({
    prompt: value.advanced_prompt ?? `${value.base_mode} 完整规则\n${value.rules.map(rule => rule.phrase).join("\n")}\n${value.notes}\n任务指令与正反例`,
    protocol: '固定 JSON 格式 {"candidates":[]}，字幕证据编号必须真实。',
    messages: [{ role: "system", content: "固定协议" }, { role: "user", content: value.advanced_prompt ?? "基础规则、示例及实际字幕占位" }],
    repair: "格式修正指令", evidence_repair: "证据修正指令", advanced: value.advanced_prompt != null,
  }));
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => { act(() => { root.unmount(); client.clear(); }); host.remove(); vi.restoreAllMocks(); });
async function mount(active = true) {
  await act(async () => root.render(<QueryClientProvider client={client}><UnsavedChangesProvider>
    <RecallSchemeSettings active={active} /></UnsavedChangesProvider></QueryClientProvider>));
  if (active) await vi.waitFor(() => expect(host.textContent).toContain("张老师"));
}
function button(text: string) {
  const found = [...host.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent?.trim() === text);
  expect(found).toBeDefined(); return found!;
}
async function click(text: string) { await act(async () => button(text).click()); }
function field(selector: string, value: string) {
  const node = host.querySelector<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(selector)!;
  const prototype = node.tagName === "SELECT" ? HTMLSelectElement.prototype : node.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  act(() => {
    Object.getOwnPropertyDescriptor(prototype, "value")!.set!.call(node, value);
    node.dispatchEvent(new Event(node.tagName === "SELECT" ? "change" : "input", { bubbles: true }));
  });
}
function unload() { const event = new Event("beforeunload", { cancelable: true }); window.dispatchEvent(event); return event.defaultPrevented; }

it("does not load the catalogue in inactive settings or call AI when editing", async () => {
  await mount(false);
  expect(api.getRecallSchemes).not.toHaveBeenCalled();
  expect(api.saveRecallScheme).not.toHaveBeenCalled();
});

it("creates a named derived scheme with bounded teacher expressions and preferences", async () => {
  await mount(); await click("新建方案");
  field(".recall-scheme-form .field-group input", "期末精简版");
  field(".recall-scheme-form .field-group select", "low_recall");
  await click("添加正向表达");
  field('[aria-label="教师表达 1"]', "拿笔画下来");
  field("textarea", "只关注有明确对象的内容");
  expect(unload()).toBe(true);
  expect(api.saveRecallScheme).not.toHaveBeenCalled();
  await click("保存方案");
  expect(api.saveRecallScheme).toHaveBeenCalledWith({
    name: "期末精简版", base_mode: "low_recall", rules: [{ phrase: "拿笔画下来", meaning: "emphasis" }],
    notes: "只关注有明确对象的内容",
  }, undefined);
  expect(host.textContent).toContain("方案已保存");
  expect(unload()).toBe(false);
  expect(client.getQueryData<RecallScheme[]>(recallSchemesKey)).toHaveLength(2);
});

it("renames in place and keeps full prompt editing collapsed until opened", async () => {
  await mount(); await click("张老师基于中召回 · 版本 1");
  field(".recall-scheme-form .field-group input", "张老师的算法课");
  await click("保存方案");
  expect(api.saveRecallScheme).toHaveBeenCalledWith(expect.objectContaining({ name: "张老师的算法课", base_mode: "medium_recall" }), first.id);
  expect(host.querySelectorAll("textarea")).toHaveLength(1);
  expect(api.previewRecallPrompt).not.toHaveBeenCalled();
  expect(host.textContent).toContain("字幕证据、时间定位与输出格式使用固定校验");
});

async function expandPrompt() {
  const toggle = host.querySelector<HTMLButtonElement>(".recall-advanced-toggle")!;
  await act(async () => toggle.click());
  await vi.waitFor(() => expect(button("启用完整编辑").disabled).toBe(false));
}

it("derives a full prompt only on request and preserves edits after collapse and save", async () => {
  await mount(); await click("张老师基于中召回 · 版本 1");
  expect(host.querySelector(".recall-advanced-toggle")?.getAttribute("aria-expanded")).toBe("false");
  expect(api.previewRecallPrompt).not.toHaveBeenCalled();
  await expandPrompt();
  expect(unload()).toBe(false);
  const initial = host.querySelector<HTMLTextAreaElement>(".recall-prompt-text")!;
  expect(initial.readOnly).toBe(true);
  expect(initial.value).toContain("拿笔画下来");
  await click("启用完整编辑");
  expect(initial.readOnly).toBe(false);
  expect(host.querySelector<HTMLFieldSetElement>(".recall-basic-fields")?.disabled).toBe(true);
  field(".recall-prompt-text", "新的完整规则：普通概念定义也提取。\n新示例和任务指令。");
  expect(unload()).toBe(true);
  await act(async () => host.querySelector<HTMLButtonElement>(".recall-advanced-toggle")!.click());
  expect(host.querySelector(".recall-prompt-text")).toBeNull();
  expect(host.textContent).toContain("已启用");
  await click("保存方案");
  expect(api.saveRecallScheme).toHaveBeenCalledWith(expect.objectContaining({
    advanced_prompt: "新的完整规则：普通概念定义也提取。\n新示例和任务指令。",
    rules: first.rules, notes: "",
  }), first.id);
  expect(unload()).toBe(false);
});

it("rejects an empty full prompt, retains failed saves and restores ordinary fields after confirmation", async () => {
  await mount(); await click("张老师基于中召回 · 版本 1"); await expandPrompt();
  await click("启用完整编辑");
  field(".recall-prompt-text", "");
  await click("保存方案");
  expect(api.saveRecallScheme).not.toHaveBeenCalled();
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("完整提示词不能为空");
  field(".recall-prompt-text", "保留这段自定义规则");
  vi.mocked(api.saveRecallScheme).mockRejectedValueOnce(new Error("本次保存失败"));
  await click("保存方案");
  expect(host.querySelector<HTMLTextAreaElement>(".recall-prompt-text")?.value).toBe("保留这段自定义规则");
  expect(unload()).toBe(true);
  await click("恢复基础编辑");
  await click("取消");
  expect(host.querySelector<HTMLFieldSetElement>(".recall-basic-fields")?.disabled).toBe(true);
  await click("恢复基础编辑"); await click("确认恢复");
  expect(host.querySelector<HTMLFieldSetElement>(".recall-basic-fields")?.disabled).toBe(false);
  expect(host.querySelector<HTMLInputElement>('[aria-label="教师表达 1"]')?.value).toBe("拿笔画下来");
});

it("copies advanced schemes without losing their full prompt and discards edits to the saved version", async () => {
  vi.mocked(api.getRecallSchemes).mockResolvedValueOnce([{ ...first, advanced_prompt: "原有完整提示词" }]);
  await mount();
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="复制方案“张老师”"]')!.click());
  expect(host.textContent).toContain("已启用");
  await act(async () => host.querySelector<HTMLButtonElement>(".recall-advanced-toggle")!.click());
  field(".recall-prompt-text", "副本中的新规则");
  await click("保存方案");
  expect(api.saveRecallScheme).toHaveBeenCalledWith(expect.objectContaining({
    name: "张老师（副本）", advanced_prompt: "副本中的新规则",
  }), undefined);
  await act(async () => host.querySelector<HTMLButtonElement>(".recall-advanced-toggle")!.click());
  field(".recall-prompt-text", "未保存的改动");
  await click("撤销修改");
  expect(host.querySelector<HTMLTextAreaElement>(".recall-prompt-text")?.value).toBe("副本中的新规则");
  expect(unload()).toBe(false);
});

it("handles preview failures with retry and never replaces the active editing draft with a preview", async () => {
  await mount(); await click("张老师基于中召回 · 版本 1");
  vi.mocked(api.previewRecallPrompt).mockRejectedValueOnce(new Error("模板读取失败"));
  await act(async () => host.querySelector<HTMLButtonElement>(".recall-advanced-toggle")!.click());
  await vi.waitFor(() => expect(host.querySelector('[role="alert"]')?.textContent).toContain("模板读取失败"));
  expect(button("启用完整编辑").disabled).toBe(true);
  await click("重试");
  await vi.waitFor(() => expect(button("启用完整编辑").disabled).toBe(false));
  await click("启用完整编辑");
  field(".recall-prompt-text", "快速输入的新草稿");
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 450)); });
  expect(host.querySelector<HTMLTextAreaElement>(".recall-prompt-text")?.value).toBe("快速输入的新草稿");
  expect(api.saveRecallScheme).not.toHaveBeenCalled();
});

it("retains drafts on failures and ignores refreshed server fields until the user discards", async () => {
  await mount(); await click("张老师基于中召回 · 版本 1");
  field("textarea", "尚未保存的偏好");
  act(() => client.setQueryData(recallSchemesKey, [{ ...first, name: "服务器名称" }]));
  expect(host.querySelector<HTMLTextAreaElement>("textarea")?.value).toBe("尚未保存的偏好");
  vi.mocked(api.saveRecallScheme).mockRejectedValueOnce(new Error("保存失败"));
  await click("保存方案");
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("保存失败");
  expect(unload()).toBe(true);
  await click("保存方案");
  expect(api.saveRecallScheme).toHaveBeenCalledTimes(2);
  expect(unload()).toBe(false);
});

it("requires valid nonduplicate expressions and prevents repeated requests while saving", async () => {
  await mount(); await click("张老师基于中召回 · 版本 1");
  await click("添加正向表达");
  await click("保存方案");
  expect(api.saveRecallScheme).not.toHaveBeenCalled();
  field('[aria-label="教师表达 2"]', "拿笔画下来");
  await click("保存方案");
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("不能重复");
  field('[aria-label="教师表达 2"]', "考试写清楚");
  let finish!: () => void;
  vi.mocked(api.saveRecallScheme).mockImplementationOnce(() => new Promise(resolve => {
    finish = () => resolve(first);
  }));
  const save = button("保存方案");
  act(() => { save.click(); save.click(); });
  await vi.waitFor(() => expect(api.saveRecallScheme).toHaveBeenCalledTimes(1));
  expect(button("正在保存…").disabled).toBe(true);
  await act(async () => finish());
});

it("separates positive and negative expressions, focuses new inputs and saves their meanings", async () => {
  await mount(); await click("张老师基于中召回 · 版本 1");
  expect(host.querySelector(".is-emphasis input")?.getAttribute("value")).toBe("拿笔画下来");
  expect(host.querySelector(".is-deemphasize input")).toBeNull();
  await click("添加负向表达");
  await vi.waitFor(() => expect(document.activeElement?.getAttribute("aria-label")).toBe("教师表达 2"));
  field('[aria-label="教师表达 2"]', "随便了解一下");
  await click("添加正向表达");
  field('[aria-label="教师表达 3"]', "这段要记住");
  expect(host.querySelectorAll(".is-emphasis input")).toHaveLength(2);
  expect(host.querySelectorAll(".is-deemphasize input")).toHaveLength(1);
  expect(host.querySelectorAll(".recall-rule-row select")).toHaveLength(0);
  await click("保存方案");
  expect(api.saveRecallScheme).toHaveBeenCalledWith(expect.objectContaining({
    rules: [
      { phrase: "拿笔画下来", meaning: "emphasis" },
      { phrase: "随便了解一下", meaning: "deemphasize" },
      { phrase: "这段要记住", meaning: "emphasis" },
    ],
  }), first.id);
});

it("offers an optional editable template and appends it without replacing existing preferences", async () => {
  await mount(); await click("张老师基于中召回 · 版本 1");
  const textarea = host.querySelector<HTMLTextAreaElement>("textarea")!;
  expect(textarea.placeholder).toContain("优先关注：");
  expect(textarea.value).toBe("");
  expect(unload()).toBe(false);
  field("textarea", "我原有的偏好");
  await click("插入模板");
  expect(textarea.value).toMatch(/^我原有的偏好\n\n优先关注：/);
  expect(textarea.value).toContain("减少纳入：");
  expect(textarea.value).toContain("范围限制：");
  expect(button("插入模板").disabled).toBe(true);
  expect(api.saveRecallScheme).not.toHaveBeenCalled();
  const value = textarea.value.replace("概念、方法和易错条件", "概念区别");
  field("textarea", value);
  await click("保存方案");
  expect(api.saveRecallScheme).toHaveBeenCalledWith(expect.objectContaining({ notes: value }), first.id);
});

it("inserts a template into an empty preference field and respects its total character limit", async () => {
  await mount(); await click("张老师基于中召回 · 版本 1");
  await click("插入模板");
  const textarea = host.querySelector<HTMLTextAreaElement>("textarea")!;
  expect(textarea.value).toBe(textarea.placeholder);
  expect(textarea.value.length).toBeLessThanOrEqual(500);
  field("textarea", "x".repeat(490));
  expect(button("插入模板").disabled).toBe(true);
  await click("插入模板");
  expect(textarea.value).toBe("x".repeat(490));
});

it("preserves legacy ordinary expressions and shares the twenty-expression limit across both groups", async () => {
  const rules = Array.from({ length: 20 }, (_, index) => ({
    phrase: `表达${index}`, meaning: index === 0 ? "ordinary" as const : index % 2 ? "emphasis" as const : "deemphasize" as const,
  }));
  vi.mocked(api.getRecallSchemes).mockResolvedValueOnce([{ ...first, rules }]);
  await mount(); await click("张老师基于中召回 · 版本 1");
  expect(host.querySelector(".is-ordinary input")?.getAttribute("value")).toBe("表达0");
  expect(button("添加正向表达").disabled).toBe(true);
  expect(button("添加负向表达").disabled).toBe(true);
  field(".is-ordinary input", "原有一般提示");
  await click("保存方案");
  expect(api.saveRecallScheme).toHaveBeenCalledWith(expect.objectContaining({
    rules: [{ phrase: "原有一般提示", meaning: "ordinary" }, ...rules.slice(1)],
  }), first.id);
});

it("copies to a new ID and archives only after explicit confirmation without deleting results", async () => {
  await mount();
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="复制方案“张老师”"]')!.click());
  expect(host.querySelector<HTMLInputElement>(".recall-scheme-form input")?.value).toBe("张老师（副本）");
  await click("保存方案");
  expect(api.saveRecallScheme).toHaveBeenCalledWith(expect.objectContaining({ name: "张老师（副本）" }), undefined);
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="停用方案“张老师”"]')!.click());
  expect(api.archiveRecallScheme).not.toHaveBeenCalled();
  await click("取消");
  expect(api.archiveRecallScheme).not.toHaveBeenCalled();
  await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="停用方案“张老师”"]')!.click());
  await click("确认停用");
  expect(vi.mocked(api.archiveRecallScheme).mock.calls[0][0]).toBe(first.id);
  expect(host.textContent).toContain("历史结果保留");
});
