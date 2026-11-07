import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ReviewTabs, ReviewTabsProvider, useReviewTabs } from "../components/ReviewTabs";
import { UnsavedChangesProvider } from "../components/UnsavedChanges";
import { StudyCoursesPage } from "./StudyCoursesPage";
import * as api from "../api/studyMaterials";
import type { Video } from "../types";

vi.mock("../api/studyMaterials", async original => ({
  ...await original<typeof import("../api/studyMaterials")>(),
  getStudyCourses: vi.fn(), getStudyCourseFolders: vi.fn(), getStudyDocuments: vi.fn(),
  getStudyBlocks: vi.fn(), getStudyQuestions: vi.fn(), saveStudyReading: vi.fn(),
}));
vi.mock("../components/DshQuestionActions", () => ({ DshQuestionActions: () => null }));

let root: Root, container: HTMLDivElement, cache: QueryClient;
let router: ReturnType<typeof createMemoryRouter>;
const documents: api.StudyDocument[] = [
  { id: "a", course_id: "course", name: "练习一.md", format: "markdown", state: "ready", reason: "",
    page_count: 0, question_count: 0, deleted: false, reading: {} },
  { id: "b", course_id: "course", name: "练习二.md", format: "markdown", state: "ready", reason: "",
    page_count: 0, question_count: 0, deleted: false, reading: {} },
];
function Fixture() {
  const location = useLocation();
  const tabs = useReviewTabs()!;
  const active = location.pathname === "/study-materials";
  useEffect(() => {
    if (location.pathname === "/review/1") tabs.register({ id: 1, library_id: "library", display_name: "第一讲" } as Video);
  }, [location.pathname, tabs.register]);
  return <>
    <ReviewTabs />
    <div hidden={!active}><StudyCoursesPage scope="test" libraryId="library" active={active} /></div>
  </>;
}
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear(); vi.clearAllMocks();
  vi.mocked(api.getStudyCourses).mockResolvedValue([
    { id: "course", name: "合成课程", folder_id: 1, document_count: 2, question_count: 0 },
  ]);
  vi.mocked(api.getStudyCourseFolders).mockResolvedValue([]);
  vi.mocked(api.getStudyDocuments).mockImplementation(async (_course, unassigned) => unassigned ? [] : documents);
  vi.mocked(api.getStudyBlocks).mockImplementation(async id => [
    { id: `block-${id}`, document_id: id, ordinal: 0, kind: "markdown", text: `# ${id === "a" ? "第一份原文" : "第二份原文"}`,
      line_start: 1, line_end: 1 },
  ]);
  vi.mocked(api.getStudyQuestions).mockResolvedValue([]);
  vi.mocked(api.saveStudyReading).mockResolvedValue({ ok: true });
  container = document.createElement("div"); document.body.append(container);
  root = createRoot(container);
  cache = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
});
afterEach(async () => {
  await act(async () => root.unmount());
  router.dispose(); cache.clear(); container.remove(); vi.restoreAllMocks();
});
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)); }); }
async function mount(path = "/study-materials?course=course&document=b") {
  router = createMemoryRouter([{ path: "*", element:
    <UnsavedChangesProvider><ReviewTabsProvider><Fixture /></ReviewTabsProvider></UnsavedChangesProvider>,
  }], { initialEntries: [path] });
  await act(async () => root.render(<QueryClientProvider client={cache}><RouterProvider router={router} /></QueryClientProvider>));
  await settle(); await settle(); await settle();
}
async function click(node: Element) { expect(node).toBeTruthy(); await act(async () => (node as HTMLElement).click()); await settle(); }
const tabs = () => [...container.querySelectorAll<HTMLAnchorElement>(".review-tab-link")];
const selectedName = () => container.querySelector(".study-current-name")?.textContent;
async function go(path: string | number) { await act(async () => { await router.navigate(path as string); }); await settle(); }

it("opens the requested document instead of the remembered one and mixes it with video tabs", async () => {
  localStorage.setItem("classcue-study-document:test:course", "a");
  await mount();
  expect(selectedName()).toBe("练习二.md");
  expect(tabs().map(tab => tab.textContent)).toEqual(["练习二.md"]);
  await go("/review/1");
  expect(tabs().map(tab => tab.textContent)).toEqual(["练习二.md", "第一讲"]);
  await click(tabs()[0]);
  await settle();
  expect(selectedName()).toBe("练习二.md");
  expect(container.querySelector(".study-markdown-block")?.textContent).toContain("第二份原文");
  expect(tabs()[0].getAttribute("aria-current")).toBe("page");
});

it("registers the default document and switches same-course documents by tab and browser history", async () => {
  await mount("/study-materials?course=course");
  expect(new URLSearchParams(router.state.location.search).get("document")).toBe("a");
  await click([...container.querySelectorAll(".study-file-open")][1]);
  expect(selectedName()).toBe("练习二.md");
  expect(tabs().map(tab => tab.textContent)).toEqual(["练习一.md", "练习二.md"]);
  await click(tabs()[0]);
  expect(selectedName()).toBe("练习一.md");
  await go(-1);
  expect(selectedName()).toBe("练习二.md");
  await go(1);
  expect(selectedName()).toBe("练习一.md");
});

it("restores each document's zoom and closes into the neighboring document", async () => {
  await mount("/study-materials?course=course&document=a");
  await click(container.querySelector('[aria-label="放大"]')!);
  expect(container.querySelector(".study-zoom")?.textContent).toBe("110%");
  await click([...container.querySelectorAll(".study-file-open")][1]);
  expect(container.querySelector(".study-zoom")?.textContent).toBe("100%");
  await click(tabs()[0]);
  expect(container.querySelector(".study-zoom")?.textContent).toBe("110%");
  await click(container.querySelector('[aria-label="关闭资料标签：练习一.md"]')!);
  expect(selectedName()).toBe("练习二.md");
  expect(tabs().map(tab => tab.textContent)).toEqual(["练习二.md"]);
  expect(tabs()[0].getAttribute("aria-current")).toBe("page");
});

it("keeps the current document tab and draft when closing is cancelled", async () => {
  await mount();
  await click([...container.querySelectorAll("button")].find(node => node.textContent === "选题")!);
  await click(container.querySelector(".study-block-check")!);
  await click(container.querySelector('[aria-label="关闭资料标签：练习二.md"]')!);
  expect(document.querySelector(".unsaved-dialog")).not.toBeNull();
  expect(tabs()).toHaveLength(1);
  await click([...document.querySelectorAll(".unsaved-actions button")].find(node => node.textContent === "继续编辑")!);
  expect(tabs()).toHaveLength(1);
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(1);
  await click(container.querySelector('[aria-label="关闭资料标签：练习二.md"]')!);
  await click([...document.querySelectorAll(".unsaved-actions button")].find(node => node.textContent === "放弃修改")!);
  expect(router.state.location.pathname).toBe("/");
  expect(tabs()).toHaveLength(0);
});

it("guards switching to another document and selects it only after discarding the draft", async () => {
  await mount("/study-materials?course=course&document=a");
  await click([...container.querySelectorAll(".study-file-open")][1]);
  await click([...container.querySelectorAll("button")].find(node => node.textContent === "选题")!);
  await click(container.querySelector(".study-block-check")!);
  await click(tabs()[0]);
  expect(document.querySelector(".unsaved-dialog")).not.toBeNull();
  expect(selectedName()).toBe("练习二.md");
  await click([...document.querySelectorAll(".unsaved-actions button")].find(node => node.textContent === "放弃修改")!);
  expect(selectedName()).toBe("练习一.md");
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(0);
});

it("does not substitute a different document when a document URL no longer exists", async () => {
  await mount("/study-materials?course=course&document=missing");
  expect(tabs()).toHaveLength(0);
  expect(selectedName()).toBe("资料原文");
  expect(container.textContent).toContain("这份资料已移除或已不在当前课程中");
  await click(container.querySelector(".study-file-open")!);
  expect(selectedName()).toBe("练习一.md");
  expect(tabs()).toHaveLength(1);
});
