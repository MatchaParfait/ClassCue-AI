import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { StudyMaterialsPage } from "./StudyMaterialsPage";
import * as study from "../api/studyMaterials";
import * as dsh from "../api/dshBridge";

vi.mock("../hooks/useDesktopAPI", () => ({ useDesktopAPI: () => ({}) }));
vi.mock("../api/studyMaterials", async original => ({
  ...await original<typeof import("../api/studyMaterials")>(),
  getStudyDocuments: vi.fn(), getStudyBlocks: vi.fn(), getStudyQuestions: vi.fn(),
  saveStudyQuestion: vi.fn(), saveStudyReading: vi.fn(), studyWrite: vi.fn(),
}));
vi.mock("../api/dshBridge", () => ({
  getDshStatus: vi.fn(async () => ({ client_connected: true })),
  getDshDiscussions: vi.fn(async () => []),
  prepareDshQuestion: vi.fn(), openDshDiscussion: vi.fn(), getDshRequest: vi.fn(), connectDsh: vi.fn(),
}));

const block: study.StudyBlock = { id: "b", document_id: "doc", ordinal: 0, kind: "markdown",
  text: "Shared scenario.\n(1) Draw the diagram.\n(2) Explain it.\n" };
const saved: study.StudyQuestion = { id: "question", document_id: "doc", title: "Whole scenario",
  number: "3", version: 1, deleted: false,
  regions: [{ block_id: "b", start: 0, end: block.text!.length, block }] };
let root: Root, host: HTMLDivElement, cache: QueryClient;
const button = (label: string) => [...host.querySelectorAll("button")].find(
  item => item.textContent?.trim() === label || item.getAttribute("aria-label") === label)!;
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 40)); }); }
async function click(label: string) {
  await vi.waitFor(() => expect(button(label)?.disabled).toBe(false));
  await act(async () => button(label).click()); await settle();
}
async function mount(active = true) {
  await act(async () => root.render(<QueryClientProvider client={cache}>
    <StudyMaterialsPage scope="library" active={active} />
  </QueryClientProvider>));
  await settle(); await settle();
}
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear(); vi.clearAllMocks();
  vi.mocked(study.getStudyDocuments).mockResolvedValue([{ id: "doc", name: "paper.md", format: "markdown",
    state: "ready", reason: "", page_count: 0, question_count: 1, deleted: false, reading: { question_id: saved.id } }]);
  vi.mocked(study.getStudyBlocks).mockResolvedValue([block]);
  vi.mocked(study.getStudyQuestions).mockResolvedValue([saved]);
  vi.mocked(study.saveStudyReading).mockResolvedValue({ ok: true });
  vi.mocked(study.studyWrite).mockResolvedValue({ ok: true });
  vi.mocked(study.saveStudyQuestion).mockImplementation(async value => ({
    ...saved, ...value, version: 2, regions: value.regions.map(region => ({ ...region, block })),
  }));
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  cache = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
});
afterEach(async () => { await act(async () => root.unmount()); cache.clear(); host.remove(); });

it("opens the saved whole question from the reader without sending content from the frontend", async () => {
  localStorage.setItem("classcue-study-document:library", "doc");
  const receipt = { request_id: "r", question_id: saved.id, content_version: 1, instance_id: "i",
    workspace_id: null, cwd: null, session_id: "s", status: "draft_ready" as const, error: null };
  vi.mocked(dsh.prepareDshQuestion).mockResolvedValue(receipt);
  vi.mocked(dsh.getDshRequest).mockResolvedValue(receipt);
  await mount();
  expect(host.querySelector(".study-question-pane > .dsh-question-actions")).not.toBeNull();
  expect(host.textContent).toContain("(2) Explain it.");
  await click("在 DSH 中讨论");
  expect(dsh.prepareDshQuestion).toHaveBeenCalledWith(saved.id, false);
});

it("hides the bridge while editing or creating an unsaved question and restores it on cancel", async () => {
  await mount(); await click("编辑题目");
  expect(host.querySelector(".dsh-question-actions")).toBeNull();
  await click("取消");
  expect(host.querySelector(".dsh-question-actions")).not.toBeNull();
  await click("新建题目");
  expect(host.querySelector(".dsh-question-actions")).toBeNull();
});

it("uses the saved content version after changing selected regions", async () => {
  const second = { ...block, id: "extra", ordinal: 1, text: "Additional condition.\n" };
  vi.mocked(study.getStudyBlocks).mockResolvedValue([block, second]);
  vi.mocked(study.saveStudyQuestion).mockImplementation(async value => {
    const revised = { ...saved, ...value, version: 2,
      regions: value.regions.map(region => ({ ...region, block: region.block_id === "extra" ? second : block })) };
    vi.mocked(study.getStudyQuestions).mockResolvedValue([revised]);
    return revised;
  });
  vi.mocked(dsh.getDshDiscussions).mockResolvedValue([
    { request_id: "previous", content_version: 1, instance_id: "i", receipt: null, created_at: "" },
  ]);
  await mount();
  await vi.waitFor(() => expect(button("继续 DSH 讨论")).toBeTruthy());
  await click("编辑题目");
  await act(async () => host.querySelectorAll<HTMLElement>(".study-block-check")[1].click());
  await click("保存题目");
  expect(study.saveStudyQuestion).toHaveBeenCalled();
  await vi.waitFor(() => expect(button("在 DSH 中讨论")).toBeTruthy());
  expect(host.textContent).toContain("题目版本 1");
});

it("stops bridge queries for an inactive reader and hides removed question actions", async () => {
  await mount();
  await mount(false);
  expect(host.querySelector(".dsh-question-actions")).toBeNull();
  expect(cache.getQueryCache().find({ queryKey: ["dsh-status", "library"] })?.getObserversCount() || 0).toBe(0);
  await mount();
  await click("移除题目 1");
  expect(host.querySelector(".dsh-question-actions")).toBeNull();
});
