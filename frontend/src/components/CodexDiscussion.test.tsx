import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { DshQuestionActions } from "./DshQuestionActions";
import { DiscussionSettingsSection } from "./DiscussionSettings";
import * as api from "../api/codexBridge";
import * as pickers from "../api/client";
import { setDiscussionProvider } from "../hooks/useDiscussionProvider";

const desktop = vi.hoisted(() => ({ enabled: true }));
vi.mock("../hooks/useDesktopAPI", () => ({ useDesktopAPI: () => desktop.enabled ? {} : null }));
vi.mock("../api/codexBridge", () => ({
  getCodexSettings: vi.fn(), setCodexSettings: vi.fn(), getCodexDiscussions: vi.fn(),
  prepareCodexDiscussion: vi.fn(), openCodexDiscussion: vi.fn(), copyCodexImages: vi.fn(),
  getCodexDiscussion: vi.fn(), attachCodexDiscussion: vi.fn(),
}));
vi.mock("../api/client", () => ({ createPicker: vi.fn(), pollPicker: vi.fn() }));
const saved = { root: "synthetic-root", executable: "synthetic-codex", home: "synthetic-home", version: "test" };
const discussion: api.CodexDiscussion = { request_id: "r", question_id: "q", content_version: 2,
  thread_id: "t", status: "ready", title: "试卷1 · 第1题", source_path: "paper", course_path: "course",
  context_file: "context.json" };
let root: Root, host: HTMLDivElement, cache: QueryClient;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
  });
  desktop.enabled = true;
  vi.mocked(api.getCodexSettings).mockResolvedValue(null);
  vi.mocked(api.setCodexSettings).mockResolvedValue(saved);
  vi.mocked(api.getCodexDiscussions).mockResolvedValue([]);
  vi.mocked(api.prepareCodexDiscussion).mockResolvedValue(discussion);
  vi.mocked(api.openCodexDiscussion).mockResolvedValue(discussion);
  vi.mocked(api.copyCodexImages).mockResolvedValue({ ...discussion,
    image_transfer: { status: "paste_required", image_count: 2 } });
  vi.mocked(pickers.createPicker).mockResolvedValue("picker");
  vi.mocked(pickers.pollPicker).mockResolvedValue(saved.root);
  host = document.createElement("div"); document.body.append(host);
  root = createRoot(host);
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(async () => {
  await act(async () => root.unmount());
  host.remove(); cache.clear(); vi.unstubAllGlobals();
});
const button = (label: string) => [...host.querySelectorAll("button")].find(b => b.textContent?.trim() === label)!;
async function mount(child: React.ReactNode) {
  await act(async () => root.render(<QueryClientProvider client={cache}>
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>{child}</MemoryRouter>
  </QueryClientProvider>));
}

it("saves the chosen Codex root and uses the selected tool at discussion entry points", async () => {
  setDiscussionProvider("codex");
  await mount(<DiscussionSettingsSection scope="library" />);
  await act(async () => button("选择默认工作区").click());
  expect(api.setCodexSettings).toHaveBeenCalledWith({ root: saved.root });
  expect(pickers.createPicker).toHaveBeenCalledWith("directory");
  expect(host.textContent).toContain("Codex 工作区已保存");
  await mount(<DshQuestionActions questionId="q" contentVersion={2} scope="library" />);
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  await act(async () => button("在 Codex 中讨论").click());
  expect(api.prepareCodexDiscussion).toHaveBeenCalledWith("q", false);
  expect(host.textContent).not.toContain("在 DSH 中讨论");
});

it("cancelling root selection preserves the settings and does not prepare discussions", async () => {
  setDiscussionProvider("codex");
  vi.mocked(api.getCodexSettings).mockResolvedValue(saved);
  vi.mocked(pickers.pollPicker).mockResolvedValue(null);
  await mount(<DiscussionSettingsSection />);
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  await act(async () => button("更改默认工作区").click());
  expect(host.textContent).toContain(saved.root);
  expect(api.setCodexSettings).not.toHaveBeenCalled();
  expect(api.prepareCodexDiscussion).not.toHaveBeenCalled();
});

it("opens historical bindings and explicitly starts an independent discussion", async () => {
  setDiscussionProvider("codex");
  vi.mocked(api.getCodexDiscussions).mockResolvedValue([discussion]);
  await mount(<DshQuestionActions questionId="q" contentVersion={2} />);
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  await act(async () => button("讨论 1 · 题目版本 2").click());
  expect(api.openCodexDiscussion).toHaveBeenCalledWith("r", expect.anything());
  await act(async () => (host.querySelector('[aria-label="新建独立讨论"]') as HTMLButtonElement).click());
  expect(api.prepareCodexDiscussion).toHaveBeenCalledWith("q", true);
});

it("shows the paste step only after copying succeeds and can repair historical discussions", async () => {
  setDiscussionProvider("codex");
  vi.mocked(api.getCodexDiscussions).mockResolvedValue([discussion]);
  await mount(<DshQuestionActions questionId="q" contentVersion={2} />);
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  await act(async () => (host.querySelector('[aria-label="复制讨论 1 的题图并打开 Codex"]') as HTMLButtonElement).click());
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(api.copyCodexImages).toHaveBeenCalledWith("r", expect.anything());
  expect(host.textContent).toContain("已复制 2 张题图");
  expect(host.textContent).toContain("Ctrl+V");
  expect(host.textContent).not.toContain("图片已附上");
  await act(async () => button("讨论 1 · 题目版本 2").click());
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(host.textContent).not.toContain("Ctrl+V");
  expect(host.textContent).toContain("已打开原 Codex 讨论");
});

it("reports copy failures without claiming an attachment or a successful copy", async () => {
  setDiscussionProvider("codex");
  vi.mocked(api.prepareCodexDiscussion).mockRejectedValue(new Error("复制题图失败"));
  await mount(<DshQuestionActions questionId="q" contentVersion={2} />);
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  await act(async () => button("在 Codex 中讨论").click());
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(host.textContent).toContain("复制题图失败");
  expect(host.querySelector('[role="status"]')).toBeNull();
});

it("reports questions without images without asking the user to paste", async () => {
  setDiscussionProvider("codex");
  vi.mocked(api.prepareCodexDiscussion).mockResolvedValue({ ...discussion,
    image_transfer: { status: "no_images", image_count: 0 } });
  await mount(<DshQuestionActions questionId="q" contentVersion={2} />);
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  await act(async () => button("在 Codex 中讨论").click());
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(host.textContent).toContain("此题没有题图");
  expect(host.textContent).not.toContain("Ctrl+V");
});
it("explicitly identifies text-only snapshots without promising image attachments", async () => {
  setDiscussionProvider("codex");
  vi.mocked(api.prepareCodexDiscussion).mockResolvedValue({ ...discussion, image_count: 0,
    draft_transfer: { status: "pending" } });
  vi.mocked(api.getCodexDiscussion).mockResolvedValue({ ...discussion, image_count: 0,
    draft_transfer: { status: "pending" } });
  await mount(<DshQuestionActions questionId="q" contentVersion={2} />);
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  await act(async () => button("在 Codex 中讨论").click());
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(host.textContent).toContain("纯文字题，没有图片附件");
  expect(host.textContent).not.toContain("更多工具");
});
it("shows plugin instructions while pending and only reports insertion after a persisted receipt", async () => {
  setDiscussionProvider("codex");
  vi.mocked(api.prepareCodexDiscussion).mockResolvedValue({ ...discussion, draft_transfer: { status: "pending" } });
  vi.mocked(api.getCodexDiscussion).mockResolvedValue({ ...discussion, draft_transfer: { status: "pending" } });
  await mount(<DshQuestionActions questionId="q" contentVersion={2} />);
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  await act(async () => button("在 Codex 中讨论").click());
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(host.textContent).not.toContain("更多工具");
  expect(host.textContent).not.toContain("已确认加入");
  vi.mocked(api.getCodexDiscussion).mockResolvedValue({ ...discussion, draft_transfer: { status: "attached" } });
  await act(async () => { await cache.invalidateQueries({ queryKey: ["codex-draft"] }); });
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(host.textContent).toContain("插件已确认加入过");
});
it("does not query Codex from a browser or an inactive question", async () => {
  setDiscussionProvider("codex");
  desktop.enabled = false;
  await mount(<DshQuestionActions questionId="q" contentVersion={2} />);
  expect(api.getCodexDiscussions).not.toHaveBeenCalled();
  desktop.enabled = true;
  await mount(<DshQuestionActions questionId="q" contentVersion={2} active={false} />);
  expect(api.getCodexDiscussions).not.toHaveBeenCalled();
});

it("primary action reports native attachment without requesting a plugin panel or polling MCP", async () => {
  setDiscussionProvider("codex");
  vi.mocked(api.prepareCodexDiscussion).mockResolvedValue({ ...discussion, image_count: 2,
    native_transfer: { status: "pasted", image_count: 2 }, draft_transfer: { status: "pasted" } });
  await mount(<DshQuestionActions questionId="q" contentVersion={2} />);
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  await act(async () => button("在 Codex 中讨论").click());
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(host.textContent).toContain("Codex 原生输入框");
  expect(host.textContent).toContain("请检查后发送");
  expect(host.textContent).not.toContain("已确认");
  expect(host.textContent).not.toContain("更多工具");
  expect(host.textContent).not.toContain("Ctrl+V");
  expect(api.getCodexDiscussion).not.toHaveBeenCalled();
});

it("unknown native result offers explicit repair on the same binding", async () => {
  setDiscussionProvider("codex");
  vi.mocked(api.prepareCodexDiscussion).mockResolvedValue({ ...discussion,
    native_transfer: { status: "unknown", error: "请先检查 Codex 输入框" } });
  vi.mocked(api.attachCodexDiscussion).mockResolvedValue({ ...discussion,
    native_transfer: { status: "attached", image_count: 2 } });
  await mount(<DshQuestionActions questionId="q" contentVersion={2} />);
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  await act(async () => button("在 Codex 中讨论").click());
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(host.textContent).toContain("请先检查 Codex 输入框");
  await act(async () => button("重新附上原题").click());
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
  expect(api.attachCodexDiscussion).toHaveBeenCalledWith("r", expect.anything());
  expect(api.prepareCodexDiscussion).toHaveBeenCalledTimes(1);
  expect(host.textContent).toContain("Codex 原生输入框");
});
