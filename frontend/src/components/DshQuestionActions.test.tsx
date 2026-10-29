import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { DshQuestionActions } from "./DshQuestionActions";
import * as api from "../api/dshBridge";

const mocks = vi.hoisted(() => ({ desktop: false }));
vi.mock("../hooks/useDesktopAPI", () => ({ useDesktopAPI: () => mocks.desktop ? {} : null }));
vi.mock("../api/dshBridge", () => ({
  getDshStatus: vi.fn(async () => ({ client_connected: true })),
  getDshDiscussions: vi.fn(async () => []),
  prepareDshQuestion: vi.fn(),
  openDshDiscussion: vi.fn(),
  getDshRequest: vi.fn(),
  connectDsh: vi.fn(),
  connectDshAuto: vi.fn(),
}));

let root: Root | undefined;
let host: HTMLDivElement;
let cache: QueryClient;
async function mount(props: { scope?: string; active?: boolean } = {}) {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  await act(async () => root!.render(<QueryClientProvider client={cache}>
    <DshQuestionActions questionId="question1" contentVersion={2} {...props} />
  </QueryClientProvider>));
}
afterEach(() => {
  act(() => root?.unmount());
  host?.remove(); cache?.clear(); root = undefined;
  mocks.desktop = false;
  vi.clearAllMocks();
});
const button = (text: string) => [...host.querySelectorAll("button")].find(b => b.textContent?.includes(text))!;

it("does not expose desktop bridge or query it in an ordinary browser", async () => {
  await mount();
  expect(host.textContent).toBe("");
  expect(api.getDshStatus).not.toHaveBeenCalled();
});

it("continues the current version binding and preserves older discussion entries", async () => {
  mocks.desktop = true;
  vi.mocked(api.getDshDiscussions).mockResolvedValueOnce([
    { request_id: "old", content_version: 1, instance_id: "instance", receipt: null, created_at: "" },
    { request_id: "current", content_version: 2, instance_id: "instance", receipt: null, created_at: "" },
  ]);
  const receipt = { request_id: "current", question_id: "question1", content_version: 2, instance_id: "instance",
    workspace_id: "workspace", cwd: "synthetic", session_id: "session", status: "draft_ready" as const, error: null };
  vi.mocked(api.openDshDiscussion).mockResolvedValue(receipt);
  vi.mocked(api.getDshRequest).mockResolvedValue(receipt);
  await mount();
  await vi.waitFor(() => expect(button("继续 DSH 讨论")).toBeTruthy());
  await act(async () => button("继续 DSH 讨论").click());
  expect(api.openDshDiscussion).toHaveBeenCalledWith("current");
  expect(api.prepareDshQuestion).not.toHaveBeenCalled();
  expect(host.textContent).toContain("题目版本 1");
  await vi.waitFor(() => expect(host.textContent).toContain("等待你发送"));
});

it("first use creates a draft and shows the native Workspace selection state", async () => {
  mocks.desktop = true;
  const receipt = { request_id: "first", question_id: "question1", content_version: 2, instance_id: "instance",
    workspace_id: null, cwd: null, session_id: "session", status: "choose_workspace" as const, error: null };
  vi.mocked(api.prepareDshQuestion).mockResolvedValue(receipt);
  vi.mocked(api.getDshRequest).mockResolvedValue(receipt);
  await mount();
  await vi.waitFor(() => expect(button("在 DSH 中讨论").disabled).toBe(false));
  await act(async () => button("在 DSH 中讨论").click());
  expect(api.prepareDshQuestion).toHaveBeenCalledWith("question1", false);
  await vi.waitFor(() => expect(host.textContent).toContain("请在 DSH 中选择工作区"));
});

it("does not query a hidden desktop workbench", async () => {
  mocks.desktop = true;
  await mount({ active: false });
  expect(host.textContent).toBe("");
  expect(api.getDshStatus).not.toHaveBeenCalled();
  expect(api.getDshDiscussions).not.toHaveBeenCalled();
});

it("isolates bindings and connection status when switching libraries", async () => {
  mocks.desktop = true;
  vi.mocked(api.getDshDiscussions).mockResolvedValueOnce([
    { request_id: "library-a", content_version: 2, instance_id: "a", receipt: null, created_at: "" },
  ]);
  await mount({ scope: "a" });
  await vi.waitFor(() => expect(button("继续 DSH 讨论")).toBeTruthy());
  await act(async () => root!.render(<QueryClientProvider client={cache}>
    <DshQuestionActions questionId="question1" contentVersion={2} scope="b" />
  </QueryClientProvider>));
  await vi.waitFor(() => expect(api.getDshDiscussions).toHaveBeenCalledTimes(2));
  expect(button("在 DSH 中讨论")).toBeTruthy();
  expect(api.getDshStatus).toHaveBeenCalledTimes(2);
  expect(cache.getQueryData(["dsh-discussions", "a", "question1"])).toHaveLength(1);
  expect(cache.getQueryData(["dsh-discussions", "b", "question1"])).toEqual([]);
  await act(async () => root!.render(<QueryClientProvider client={cache}>
    <DshQuestionActions questionId="question1" contentVersion={2} scope="b" active={false} />
  </QueryClientProvider>));
  expect(cache.getQueryCache().find({ queryKey: ["dsh-status", "b"] })?.getObserversCount()).toBe(0);
});
