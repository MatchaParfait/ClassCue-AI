import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { StudyMaterialsPage } from "./StudyMaterialsPage";
import * as api from "../api/studyMaterials";
import { StudyMarkdown } from "../components/StudyMarkdown";
import { rectangle, translateBox } from "../components/StudyPageCanvas";
import { StudyQuestionContent } from "../components/StudyQuestionPreview";
import { expandToTextWidth } from "../hooks/useStudyPageBoxSelection";
import { UnsavedChangesProvider } from "../components/UnsavedChanges";

vi.mock("../api/studyMaterials", async original => ({
  ...await original<typeof import("../api/studyMaterials")>(),
  getStudyDocuments: vi.fn(), getStudyBlocks: vi.fn(), getStudyQuestions: vi.fn(), getStudyTextBounds: vi.fn(), saveStudyQuestion: vi.fn(),
  saveStudyReading: vi.fn(), uploadStudyDocument: vi.fn(), studyWrite: vi.fn(), uploadStudyAttachment: vi.fn(),
}));

let root: Root, container: HTMLDivElement, client: QueryClient;
const blocks: api.StudyBlock[] = [
  { id: "b1", document_id: "d1", ordinal: 0, kind: "markdown", text: "# 第三题\n", line_start: 1, line_end: 1 },
  { id: "b2", document_id: "d1", ordinal: 1, kind: "markdown", text: "1. （1）计算。\n   （2）画图。\n", line_start: 3, line_end: 4 },
  { id: "b3", document_id: "d1", ordinal: 2, kind: "markdown", text: "$$\nx^2\n$$\n", line_start: 6, line_end: 8 },
];
const document: api.StudyDocument = { id: "d1", name: "模拟卷.md", format: "markdown", state: "ready", reason: "",
  page_count: 0, question_count: 0, deleted: false, reading: {} };
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  localStorage.clear(); vi.clearAllMocks();
  vi.mocked(api.getStudyDocuments).mockResolvedValue([document]);
  vi.mocked(api.getStudyBlocks).mockResolvedValue(blocks);
  vi.mocked(api.getStudyQuestions).mockResolvedValue([]);
  vi.mocked(api.getStudyTextBounds).mockResolvedValue({ status: "no_text", bbox: null });
  vi.mocked(api.saveStudyReading).mockResolvedValue({ ok: true });
  vi.mocked(api.studyWrite).mockResolvedValue({ ok: true });
  vi.mocked(api.saveStudyQuestion).mockImplementation(async value => ({ ...value, id: "q1", version: 1, deleted: false,
    regions: value.regions.map(region => ({ ...region, block: blocks.find(block => block.id === region.block_id)! })) }));
  container = window.document.createElement("div"); window.document.body.append(container);
  root = createRoot(container); client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
});
afterEach(async () => { await act(async () => root.unmount()); client.clear(); container.remove(); });
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 40)); }); }
async function mount() {
  await act(async () => root.render(<QueryClientProvider client={client}><StudyMaterialsPage scope="test" /></QueryClientProvider>));
  await settle(); await settle();
  const surface = container.querySelector<HTMLDivElement>(".study-document-content");
  if (surface) { surface.setPointerCapture = vi.fn(); surface.hasPointerCapture = () => false; }
}
function button(label: string) { return [...container.querySelectorAll<HTMLButtonElement>("button")].find(node => node.textContent?.trim() === label || node.getAttribute("aria-label") === label)!; }
async function click(node: Element) { expect(node).toBeTruthy(); await act(async () => (node as HTMLElement).click()); await settle(); }
function markdownSurface() {
  const surface = container.querySelector<HTMLDivElement>(".study-markdown-surface")!;
  surface.getBoundingClientRect = () => ({ left: 0, top: 0, right: 600, bottom: 600, width: 600, height: 600, x: 0, y: 0, toJSON: () => ({}) });
  surface.setPointerCapture = vi.fn(); surface.hasPointerCapture = () => false;
  container.querySelectorAll<HTMLElement>(".study-markdown-block").forEach((block, index) => {
    block.getBoundingClientRect = () => ({ left: 20, top: index * 100, right: 580, bottom: (index + 1) * 100,
      width: 560, height: 100, x: 20, y: index * 100, toJSON: () => ({}) });
  });
  return surface;
}
async function pointer(node: Element, type: string, x: number, y: number) {
  const event = new MouseEvent(type, { bubbles: true, clientX: x, clientY: y, button: 0 });
  Object.defineProperties(event, { pointerId: { value: 1 }, isPrimary: { value: true } });
  await act(async () => node.dispatchEvent(event)); await settle();
}

it("opens imported Markdown without collections, categories, model requests or automatic questions", async () => {
  await mount();
  expect(container.querySelectorAll(".study-markdown-block")).toHaveLength(3);
  expect(container.textContent).toContain("（1）计算");
  expect(container.textContent).toContain("（2）画图");
  expect(api.saveStudyQuestion).not.toHaveBeenCalled();
  expect(api.studyWrite).not.toHaveBeenCalled();
  expect(container.textContent).not.toMatch(/整理连接|自动整理|分类目录|DSH/);
});

it("combines manually selected blocks, supports shift selection and only saves on command", async () => {
  await mount(); await click(button("选题"));
  const choices = [...container.querySelectorAll(".study-block-check")];
  await click(choices[0]);
  await act(async () => choices[2].dispatchEvent(new MouseEvent("click", { bubbles: true, shiftKey: true })));
  await settle();
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(3);
  expect(api.saveStudyQuestion).not.toHaveBeenCalled();
  await click(button("保存题目"));
  expect(api.saveStudyQuestion).toHaveBeenCalledWith({ document_id: "d1", title: "", number: "", regions: blocks.map(block => ({ block_id: block.id, start: 0, end: block.text!.length })) }, undefined);
  expect(container.textContent).toContain("题目已保存");
});

it("undoes block selection and cancelling does not save", async () => {
  await mount(); await click(button("选题"));
  const choices = [...container.querySelectorAll(".study-block-check")];
  await click(choices[0]); await click(choices[1]);
  await click(button("撤销选区操作"));
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(1);
  await click(button("取消"));
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(0);
  expect(api.saveStudyQuestion).not.toHaveBeenCalled();
});

it("selects a complete Markdown question by dragging once and undoes the entire gesture", async () => {
  await mount(); await click(button("选题"));
  const surface = markdownSurface();
  await pointer(surface, "pointerdown", 30, 20);
  await pointer(surface, "pointermove", 550, 280);
  expect(container.querySelectorAll(".study-block-check input:checked")).toHaveLength(3);
  expect(container.querySelector(".study-markdown-selection-box")).not.toBeNull();
  expect(api.saveStudyQuestion).not.toHaveBeenCalled();
  await pointer(surface, "pointerup", 550, 280);
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(3);
  expect(container.querySelector(".study-markdown-selection-box")).toBeNull();
  await click(button("撤销选区操作"));
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(0);
});

it("adds reverse-dragged Markdown blocks to existing selection and saves original block ranges", async () => {
  await mount(); await click(button("选题"));
  await click(container.querySelector(".study-block-check")!);
  await click(button("添加另一区域"));
  const surface = markdownSurface();
  await pointer(surface, "pointerdown", 550, 280);
  await pointer(surface, "pointermove", 30, 120);
  await pointer(surface, "pointerup", 30, 120);
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(3);
  await click(button("保存题目"));
  expect(api.saveStudyQuestion).toHaveBeenCalledWith({ document_id: "d1", title: "", number: "",
    regions: blocks.map(block => ({ block_id: block.id, start: 0, end: block.text!.length })) }, undefined);
});

it("cancels an unfinished Markdown box with Escape without changing the draft", async () => {
  await mount(); await click(button("选题"));
  const surface = markdownSurface();
  await pointer(surface, "pointerdown", 30, 20);
  await pointer(surface, "pointermove", 550, 280);
  await act(async () => window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  await settle();
  expect(container.querySelectorAll(".study-block-check input:checked")).toHaveLength(0);
  await pointer(surface, "pointerup", 550, 280);
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(0);
  expect(api.saveStudyQuestion).not.toHaveBeenCalled();
});

it("creates independent questions continuously, retains edits when switching and adds to only the armed question", async () => {
  await mount(); await click(button("选题"));
  const surface = markdownSurface();
  async function draw(top: number, bottom: number) {
    await pointer(surface, "pointerdown", 30, top);
    await pointer(surface, "pointerup", 550, bottom);
  }
  await draw(20, 80);
  const title = container.querySelector<HTMLInputElement>('[aria-label="题目标题"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(title, "第一题");
    title.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await draw(120, 180);
  expect(button("选题").getAttribute("aria-pressed")).toBe("true");
  expect(container.querySelectorAll(".study-question-draft-row")).toHaveLength(2);
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(1);
  await click(container.querySelectorAll(".study-question-draft-row .study-question-open")[0]);
  expect(container.querySelector<HTMLInputElement>('[aria-label="题目标题"]')!.value).toBe("第一题");
  await click(button("添加另一区域"));
  await draw(220, 280);
  expect(container.querySelectorAll(".study-question-draft-row")).toHaveLength(2);
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(2);
  await draw(120, 180);
  expect(container.querySelectorAll(".study-question-draft-row")).toHaveLength(3);
  await click(container.querySelectorAll(".study-question-draft-row .study-question-open")[0]);
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(2);
  await click(button("阅读"));
  expect(container.querySelectorAll(".study-markdown-block.is-selected,.study-markdown-block.has-question")).toHaveLength(0);
  await click(button("选题"));
  expect(container.querySelectorAll(".study-markdown-block.is-selected")).toHaveLength(2);
  expect(api.saveStudyQuestion).not.toHaveBeenCalled();
});

it("retains successful batch saves and retries only remaining drafts after a partial failure", async () => {
  let attempt = 0;
  vi.mocked(api.saveStudyQuestion).mockImplementation(async value => {
    attempt++;
    if (attempt === 2) throw new Error("第二题保存失败");
    return { ...value, id: `saved${attempt}`, version: 1, deleted: false,
      regions: value.regions.map(region => ({ ...region, block: blocks.find(block => block.id === region.block_id)! })) };
  });
  await mount(); await click(button("选题"));
  const surface = markdownSurface();
  await pointer(surface, "pointerdown", 30, 20); await pointer(surface, "pointerup", 550, 80);
  await pointer(surface, "pointerdown", 30, 120); await pointer(surface, "pointerup", 550, 180);
  await click(button("全部保存"));
  expect(container.textContent).toContain("第二题保存失败");
  expect(container.querySelectorAll(".study-question-draft-row")).toHaveLength(1);
  await click(button("保存题目"));
  expect(api.saveStudyQuestion).toHaveBeenCalledTimes(3);
  const calls = vi.mocked(api.saveStudyQuestion).mock.calls;
  expect(calls.map(([value]) => value.regions[0].block_id)).toEqual(["b1", "b2", "b2"]);
  expect(calls.every(([, id]) => id === undefined)).toBe(true);
});

it("keeps a titled question without regions accessible when switching drafts so batch saving can recover", async () => {
  let serial = 0;
  vi.mocked(api.saveStudyQuestion).mockImplementation(async value => ({ ...value, id: `saved${++serial}`,
    version: 1, deleted: false,
    regions: value.regions.map(region => ({ ...region, block: blocks.find(block => block.id === region.block_id)! })) }));
  await mount(); await click(button("选题"));
  const title = container.querySelector<HTMLInputElement>('[aria-label="题目标题"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(title, "尚未框选的题目");
    title.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await click(button("新建题目"));
  const surface = markdownSurface();
  await pointer(surface, "pointerdown", 30, 120); await pointer(surface, "pointerup", 550, 180);
  expect(container.querySelectorAll(".study-question-draft-row")).toHaveLength(2);
  await click(button("全部保存"));
  expect(api.saveStudyQuestion).not.toHaveBeenCalled();
  await click(button("阅读"));
  await click(container.querySelectorAll(".study-question-draft-row .study-question-open")[0]);
  expect(container.querySelector<HTMLInputElement>('[aria-label="题目标题"]')!.value).toBe("尚未框选的题目");
  await click(button("选题"));
  await pointer(surface, "pointerdown", 30, 20); await pointer(surface, "pointerup", 550, 80);
  await click(button("全部保存"));
  expect(api.saveStudyQuestion).toHaveBeenCalledTimes(2);
  expect(vi.mocked(api.saveStudyQuestion).mock.calls[0][0].title).toBe("尚未框选的题目");
  expect(container.querySelectorAll(".study-question-draft-row")).toHaveLength(0);
});

it("guards all drafts when leaving even if the current question was discarded", async () => {
  vi.mocked(api.getStudyDocuments).mockResolvedValue([document, { ...document, id: "d2", name: "第二份.md" }]);
  await act(async () => root.render(<QueryClientProvider client={client}><UnsavedChangesProvider>
    <StudyMaterialsPage scope="test" /></UnsavedChangesProvider></QueryClientProvider>));
  await settle(); await settle(); await click(button("选题"));
  const surface = markdownSurface();
  await pointer(surface, "pointerdown", 30, 20); await pointer(surface, "pointerup", 550, 80);
  await pointer(surface, "pointerdown", 30, 120); await pointer(surface, "pointerup", 550, 180);
  await click(button("取消"));
  await click(container.querySelectorAll(".study-file-open")[1]);
  expect(window.document.querySelector(".unsaved-dialog")?.textContent).toContain("题目草稿");
  expect(container.querySelectorAll(".study-question-draft-row")).toHaveLength(1);
  await click(window.document.querySelector(".unsaved-actions button:last-child")!);
});

it("keeps restored and newly saved questions separately selectable after removing the current question", async () => {
  const questionButton = (title: string) => [...container.querySelectorAll(".study-question-open")]
    .find(node => node.querySelector("strong")?.textContent === title)!;
  const original: api.StudyQuestion = { id: "original", document_id: "d1", title: "原题A", number: "",
    version: 1, deleted: false,
    regions: [{ block_id: "b1", start: 0, end: blocks[0].text!.length, block: blocks[0] }] };
  let records = [original];
  vi.mocked(api.getStudyDocuments).mockResolvedValue([{ ...document, reading: { question_id: original.id } }]);
  vi.mocked(api.getStudyQuestions).mockImplementation(async () => records);
  vi.mocked(api.saveStudyQuestion).mockImplementation(async (value, id) => {
    const result = { ...value, id: id || "new", title: value.title || "新题B", version: 1, deleted: false,
      regions: value.regions.map(region => ({ ...region, block: blocks.find(block => block.id === region.block_id)! })) };
    records = [...records.filter(question => question.id !== result.id), result];
    return result;
  });
  await mount();
  records = [{ ...original, deleted: true }];
  await click(button("移除题目 1"));
  await click(button("选题"));
  await click(container.querySelectorAll(".study-block-check")[1]);
  await click(button("保存题目")); await click(button("阅读"));
  expect(container.querySelector(".study-question-title")?.textContent).toBe("新题B");
  await click(container.querySelector(".study-recycle input")!);
  records = records.map(question => ({ ...question, deleted: false }));
  await click(button("恢复题目 1"));
  await click(container.querySelector(".study-recycle input")!);
  await click(questionButton("原题A"));
  expect(container.querySelector(".study-question-title")?.textContent).toBe("原题A");
  await click(questionButton("新题B"));
  expect(container.querySelector(".study-question-title")?.textContent).toBe("新题B");
  await click(button("编辑题目"));
  const title = container.querySelector<HTMLInputElement>('[aria-label="题目标题"]')!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(title, "新题B修改");
    title.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await click(questionButton("原题A")); await click(questionButton("新题B修改 · 未保存"));
  expect(container.querySelector<HTMLInputElement>('[aria-label="题目标题"]')!.value).toBe("新题B修改");
  await click(button("保存题目"));
  expect(vi.mocked(api.saveStudyQuestion).mock.calls.at(-1)?.[1]).toBe("new");
});

it("keeps the draft after a failed save", async () => {
  vi.mocked(api.saveStudyQuestion).mockRejectedValue(new Error("Synthetic save failure"));
  await mount(); await click(button("选题")); await click(container.querySelector(".study-block-check")!);
  await click(button("保存题目"));
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("Synthetic save failure");
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(1);
});

it("restores each document's own question and zoom without writing another document's identity", async () => {
  const second: api.StudyDocument = { ...document, id: "d2", name: "第二份.md", reading: { zoom: .8, question_id: "q2" } };
  vi.mocked(api.getStudyDocuments).mockResolvedValue([{ ...document, reading: { zoom: 1.6, question_id: "q1" } }, second]);
  vi.mocked(api.getStudyBlocks).mockImplementation(async id => blocks.map(block => ({ ...block, id: `${id}-${block.id}`, document_id: id })));
  vi.mocked(api.getStudyQuestions).mockImplementation(async id => [{
    id: id === "d1" ? "q1" : "q2", document_id: id, title: id, number: "", version: 1, deleted: false,
    regions: [{ block_id: `${id}-b1`, start: 0, end: blocks[0].text!.length, block: { ...blocks[0], id: `${id}-b1`, document_id: id } }],
  }]);
  await mount();
  expect(container.querySelector(".study-zoom")?.textContent).toBe("160%");
  expect(container.querySelector(".study-question-title")?.textContent).toBe("d1");
  const secondFile = [...container.querySelectorAll(".study-file-open")].find(node => node.textContent?.includes("第二份.md"))!;
  await click(secondFile);
  expect(container.querySelector(".study-zoom")?.textContent).toBe("80%");
  expect(container.querySelector(".study-question-title")?.textContent).toBe("d2");
  await click(container.querySelector(".study-file-open")!);
  expect(container.querySelector(".study-zoom")?.textContent).toBe("160%");
  expect(container.querySelector(".study-question-title")?.textContent).toBe("d1");
  for (const [id, reading] of vi.mocked(api.saveStudyReading).mock.calls) {
    expect(reading.question_id).toBe(id === "d1" ? "q1" : "q2");
  }
});

it("separates saved reading from editing and keeps the question index outside long previews", async () => {
  const saved = { id: "q1", document_id: "d1", title: "完整情景题", number: "三", version: 1, deleted: false,
    regions: blocks.map(block => ({ block_id: block.id, start: 0, end: block.text!.length, block })) };
  vi.mocked(api.getStudyQuestions).mockResolvedValue([saved]);
  vi.mocked(api.getStudyDocuments).mockResolvedValue([{ ...document, reading: { question_id: "q1" } }]);
  await mount();
  expect(container.querySelector(".study-question-title")?.textContent).toBe("完整情景题");
  expect(container.querySelector('[aria-label="题目标题"]')).toBeNull();
  expect(container.querySelector(".study-question-scroll .study-question-index")).toBeNull();
  await click(button("编辑题目"));
  expect(container.querySelector('[aria-label="题目标题"]')).not.toBeNull();
  expect(container.querySelector(".study-question-scroll .study-draft-actions")).toBeNull();
  expect(container.querySelector(".study-question-pane > .study-draft-actions")).not.toBeNull();
  await click(button("取消"));
  expect(container.querySelector(".study-question-title")?.textContent).toBe("完整情景题");
});

it("opens the entire selected question in a zoomable preview", async () => {
  await mount(); await click(button("选题")); await click(container.querySelector(".study-block-check")!);
  await click(button("完整预览"));
  expect(window.document.querySelector('[role="dialog"] .study-markdown')?.textContent).toContain("第三题");
  const enlarge = window.document.querySelector('[aria-label="放大预览"]')!;
  await click(enlarge);
  expect(window.document.querySelector(".study-preview-tools")?.textContent).toContain("125%");
  await click(window.document.querySelector('[aria-label="关闭预览"]')!);
  expect(window.document.querySelector(".study-preview-dialog")).toBeNull();
  expect(api.saveStudyQuestion).not.toHaveBeenCalled();
});

it("renders selected Markdown as one continuous question with source controls folded away", async () => {
  await mount(); await click(button("选题"));
  await click(container.querySelectorAll(".study-block-check")[0]);
  const last = container.querySelectorAll(".study-block-check")[2];
  await act(async () => last.dispatchEvent(new MouseEvent("click", { bubbles: true, shiftKey: true })));
  await settle();
  const content = container.querySelector(".study-draft > .study-question-content")!;
  expect(content.querySelectorAll(".study-markdown")).toHaveLength(1);
  expect(content.textContent).toContain("第三题");
  expect(content.textContent).toContain("（2）画图");
  expect(content.querySelector(".katex")).not.toBeNull();
  expect(content.textContent).not.toMatch(/第.*行/);
  expect(content.querySelector("header,.study-region-preview")).toBeNull();
  expect(container.querySelector<HTMLDetailsElement>(".study-source-management")?.open).toBe(false);
  await click(button("完整预览"));
  expect(window.document.querySelectorAll('[role="dialog"] .study-markdown')).toHaveLength(1);
  expect(window.document.querySelector('[role="dialog"] .study-preview-source')).toBeNull();
});

it("keeps source ranges and inserted images in their selected order when composing a question", async () => {
  const markdown = { ...blocks[0], text: "prefix Visible suffix" };
  const image: api.StudyBlock = { id: "image", document_id: "d1", ordinal: 3, kind: "image", supplement: true };
  await act(async () => root.render(<StudyQuestionContent documentId="d1" blocks={[markdown, blocks[2], image]}
    regions={[{ block_id: markdown.id, start: 7, end: 14 }, { block_id: image.id, bbox: [0, 0, 1, 1] },
      { block_id: blocks[2].id, start: 0, end: blocks[2].text!.length }]} />));
  const parts = container.querySelector(".study-question-content")!.children;
  expect(parts).toHaveLength(3);
  expect(parts[0].textContent).toBe("Visible");
  expect(parts[1].tagName).toBe("IMG");
  expect(parts[2].querySelector(".katex")).not.toBeNull();
});

it("keeps PDF selection editable and appends a region only after an explicit add action", async () => {
  vi.mocked(api.getStudyDocuments).mockResolvedValue([{ ...document, format: "pdf", name: "模拟卷.pdf", page_count: 1 }]);
  vi.mocked(api.getStudyBlocks).mockResolvedValue([{ id: "page1", document_id: "d1", ordinal: 0, kind: "pdf_page",
    page: 1, width: 600, height: 800 }]);
  await mount(); await click(button("选题"));
  const canvas = container.querySelector<HTMLDivElement>(".study-page-canvas")!;
  canvas.getBoundingClientRect = () => ({ left: 0, top: 0, right: 600, bottom: 800, width: 600, height: 800, x: 0, y: 0, toJSON: () => ({}) });
  canvas.setPointerCapture = vi.fn(); canvas.hasPointerCapture = () => false;
  await act(async () => canvas.querySelector("img")!.dispatchEvent(new Event("load")));
  await pointer(canvas, "pointerdown", 60, 80);
  await pointer(canvas, "pointermove", 540, 320);
  await pointer(canvas, "pointerup", 540, 320);
  const first = canvas.querySelector<HTMLElement>(".study-selection-box")!;
  const initial = first.getAttribute("style");
  expect(first.classList.contains("locked")).toBe(false);
  expect(first.tabIndex).toBe(0);
  expect(canvas.querySelectorAll("[data-handle]")).toHaveLength(4);
  const editable = container.querySelector<HTMLDivElement>(".study-page-canvas")!;
  const editableFirst = editable.querySelector<HTMLElement>(".study-selection-box")!;
  expect(editableFirst.classList.contains("locked")).toBe(false);
  expect(editable.querySelectorAll("[data-handle]")).toHaveLength(4);
  await pointer(editableFirst, "pointerdown", 120, 100);
  await pointer(editable, "pointermove", 180, 180);
  await pointer(editable, "pointerup", 180, 180);
  expect(editableFirst.getAttribute("style")).not.toBe(initial);
  expect(editableFirst.classList.contains("locked")).toBe(false);
  expect(editable.querySelectorAll("[data-handle]")).toHaveLength(4);
  const moved = editableFirst.getAttribute("style");
  await pointer(editableFirst.querySelector('[data-handle="se"]')!, "pointerdown", 600, 400);
  await pointer(editable, "pointermove", 550, 450);
  await pointer(editable, "pointerup", 550, 450);
  expect(editableFirst.getAttribute("style")).not.toBe(moved);
  const adjusted = editableFirst.getAttribute("style");
  await click(button("添加另一区域"));
  const drawingCanvas = container.querySelector<HTMLDivElement>(".study-page-canvas")!;
  const drawingFirst = drawingCanvas.querySelector<HTMLElement>(".study-selection-box")!;
  // Cancelling or making a tiny second box leaves the first region unchanged.
  await pointer(drawingCanvas, "pointerdown", 60, 500);
  await pointer(drawingCanvas, "pointermove", 500, 650);
  expect(drawingFirst.classList.contains("locked")).toBe(true);
  await pointer(drawingCanvas, "pointercancel", 500, 650);
  await pointer(drawingCanvas, "pointerdown", 60, 500);
  await pointer(drawingCanvas, "pointerup", 61, 501);
  await pointer(drawingCanvas, "pointerdown", 60, 500);
  await pointer(drawingCanvas, "pointermove", 500, 650);
  await pointer(drawingCanvas, "pointerup", 500, 650);
  expect(drawingCanvas.querySelectorAll(".study-selection-box")).toHaveLength(2);
  expect(drawingCanvas.querySelector(".study-selection-box")?.getAttribute("style")).toBe(adjusted);
  expect(drawingCanvas.querySelector(".study-selection-box")?.classList.contains("locked")).toBe(false);
  expect(drawingCanvas.querySelector(".study-selection-box [data-handle]")).not.toBeNull();
  const second = drawingCanvas.querySelectorAll(".study-selection-box")[1];
  expect(second.classList.contains("locked")).toBe(false);
  await click(button("撤销选区操作"));
  expect(container.querySelectorAll(".study-selection-box")).toHaveLength(1);
  expect(container.querySelector(".study-selection-box")?.classList.contains("locked")).toBe(false);
  expect(container.querySelector(".study-selection-box")?.getAttribute("style")).toBe(adjusted);
  await click(button("撤销选区操作"));
  expect(container.querySelector(".study-selection-box")?.getAttribute("style")).toBe(moved);
  await click(button("撤销选区操作"));
  expect(container.querySelector(".study-selection-box")?.getAttribute("style")).toBe(initial);
  expect(api.saveStudyQuestion).not.toHaveBeenCalled();
});

it("selects across PDF pages as one completed question and expands to each page's text width", async () => {
  const pages: api.StudyBlock[] = [0, 1, 2].map(index => ({ id: `page${index}`, document_id: "d1",
    ordinal: index, kind: "pdf_page", page: index + 1, width: 600, height: 800 }));
  vi.mocked(api.getStudyDocuments).mockResolvedValue([{ ...document, format: "pdf", page_count: 3 }]);
  vi.mocked(api.getStudyBlocks).mockResolvedValue(pages);
  vi.mocked(api.getStudyTextBounds).mockImplementation(async (_, id) => ({ status: "ready",
    bbox: id === "page1" ? [.05, .02, .98, .95] : [.1, .02, .9, .95] }));
  await mount(); await click(button("选题"));
  const surface = container.querySelector(".study-document-content")!;
  const canvases = [...container.querySelectorAll<HTMLDivElement>(".study-page-canvas")];
  for (const [index, canvas] of canvases.entries()) {
    canvas.getBoundingClientRect = () => ({ left: 0, top: index * 840, right: 600, bottom: index * 840 + 800,
      width: 600, height: 800, x: 0, y: index * 840, toJSON: () => ({}) });
    canvas.setPointerCapture = vi.fn(); canvas.hasPointerCapture = () => false;
    await act(async () => canvas.querySelector("img")!.dispatchEvent(new Event("load")));
  }
  await settle();
  await pointer(canvases[0], "pointerdown", 150, 600);
  await pointer(surface, "pointermove", 450, 1040);
  expect(container.querySelectorAll(".study-selection-box.is-drawing")).toHaveLength(2);
  await pointer(surface, "pointerup", 450, 1040);
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(2);
  expect(container.querySelectorAll(".study-selection-box")).toHaveLength(2);
  expect(container.querySelectorAll("[data-handle]")).toHaveLength(8);
  expect(container.querySelectorAll(".study-question-draft-row")).toHaveLength(1);
  const boxes = [...container.querySelectorAll<HTMLElement>(".study-selection-box")];
  expect(parseFloat(boxes[0].style.left)).toBeCloseTo(9);
  expect(parseFloat(boxes[0].style.width)).toBeCloseTo(82);
  expect(parseFloat(boxes[1].style.left)).toBeCloseTo(4);
  expect(parseFloat(boxes[1].style.width)).toBeCloseTo(95);
  await click(button("保存题目"));
  const values = vi.mocked(api.saveStudyQuestion).mock.calls[0][0].regions;
  expect(values).toHaveLength(2);
  expect(values[0].selection_group).toBeTruthy();
  expect(values[0].selection_group).toBe(values[1].selection_group);
  expect(values[0].bbox).toEqual([expect.closeTo(.09), .75, expect.closeTo(.91), 1]);
  expect(values[1].bbox).toEqual([expect.closeTo(.04), 0, expect.closeTo(.99), .25]);
});

it("creates separate PDF questions on consecutive gestures and restores all boxes after reading", async () => {
  vi.mocked(api.getStudyDocuments).mockResolvedValue([{ ...document, format: "pdf", page_count: 1 }]);
  vi.mocked(api.getStudyBlocks).mockResolvedValue([{ id: "page1", document_id: "d1", ordinal: 0, kind: "pdf_page",
    page: 1, width: 600, height: 800 }]);
  await mount(); await click(button("选题"));
  const canvas = container.querySelector<HTMLDivElement>(".study-page-canvas")!;
  canvas.getBoundingClientRect = () => ({ left: 0, top: 0, right: 600, bottom: 800, width: 600, height: 800, x: 0, y: 0, toJSON: () => ({}) });
  canvas.setPointerCapture = vi.fn(); canvas.hasPointerCapture = () => false;
  await act(async () => canvas.querySelector("img")!.dispatchEvent(new Event("load")));
  await pointer(canvas, "pointerdown", 60, 80); await pointer(canvas, "pointerup", 540, 320);
  await pointer(canvas, "pointerdown", 60, 400); await pointer(canvas, "pointerup", 540, 600);
  expect(button("选题").getAttribute("aria-pressed")).toBe("true");
  expect(container.querySelectorAll(".study-question-draft-row")).toHaveLength(2);
  expect(canvas.querySelectorAll(".study-selection-box")).toHaveLength(1);
  expect(canvas.querySelectorAll(".study-saved-box")).toHaveLength(1);
  expect(canvas.querySelector(".study-selection-number")!.textContent).toContain("题目 1");
  await click(button("阅读"));
  expect(canvas.querySelectorAll(".study-selection-box,.study-saved-box,[data-handle]")).toHaveLength(0);
  await click(button("选题"));
  expect(canvas.querySelectorAll(".study-selection-box,.study-saved-box")).toHaveLength(2);
  await pointer(canvas.querySelector(".study-saved-box")!, "pointerdown", 80, 100);
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(1);
  expect(canvas.querySelector(".study-selection-box .study-selection-number")!.textContent).toBe("题目 1");
  await pointer(canvas, "pointerdown", 60, 650); await pointer(canvas, "pointerup", 540, 750);
  expect(container.querySelectorAll(".study-question-draft-row")).toHaveLength(3);
  expect(api.saveStudyQuestion).not.toHaveBeenCalled();
});

it("allows reverse cross-page PDF selection without text and undoes all pages together", async () => {
  vi.mocked(api.getStudyDocuments).mockResolvedValue([{ ...document, format: "pdf", page_count: 2 }]);
  vi.mocked(api.getStudyBlocks).mockResolvedValue([0, 1].map(index => ({ id: `scan${index}`, document_id: "d1",
    ordinal: index, kind: "pdf_page", page: index + 1, width: 600, height: 800 })));
  await mount(); await click(button("选题"));
  const surface = container.querySelector(".study-document-content")!;
  const canvases = [...container.querySelectorAll<HTMLDivElement>(".study-page-canvas")];
  for (const [index, canvas] of canvases.entries()) {
    canvas.getBoundingClientRect = () => ({ left: 0, top: index * 840, right: 600, bottom: index * 840 + 800,
      width: 600, height: 800, x: 0, y: index * 840, toJSON: () => ({}) });
    await act(async () => canvas.querySelector("img")!.dispatchEvent(new Event("load")));
  }
  await pointer(canvases[1], "pointerdown", 540, 1040);
  await pointer(surface, "pointerup", 60, 600);
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(2);
  const boxes = [...container.querySelectorAll<HTMLElement>(".study-selection-box")];
  expect(boxes[0].style.top).toBe("75%");
  expect(boxes[1].style.top).toBe("0%");
  expect(boxes[0].style.left).toBe("10%");
  await click(button("撤销选区操作"));
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(0);
});

it("extends an existing PDF selection into the previous page by dragging its top corner", async () => {
  const pages: api.StudyBlock[] = [0, 1].map(index => ({ id: `page${index}`, document_id: "d1",
    ordinal: index, kind: "pdf_page", page: index + 1, width: 600, height: 800 }));
  vi.mocked(api.getStudyDocuments).mockResolvedValue([{ ...document, format: "pdf", page_count: 2 }]);
  vi.mocked(api.getStudyBlocks).mockResolvedValue(pages);
  await mount(); await click(button("选题"));
  const surface = container.querySelector(".study-document-content")!;
  const canvases = [...container.querySelectorAll<HTMLDivElement>(".study-page-canvas")];
  for (const [index, canvas] of canvases.entries()) {
    canvas.getBoundingClientRect = () => ({ left: 0, top: index * 840, right: 600, bottom: index * 840 + 800,
      width: 600, height: 800, x: 0, y: index * 840, toJSON: () => ({}) });
    canvas.setPointerCapture = vi.fn(); canvas.hasPointerCapture = () => false;
    await act(async () => canvas.querySelector("img")!.dispatchEvent(new Event("load")));
  }
  await pointer(canvases[1], "pointerdown", 120, 1000);
  await pointer(surface, "pointerup", 480, 1160);
  const original = canvases[1].querySelector<HTMLElement>(".study-selection-box")!.getAttribute("style");
  await click(button("调整选框 1"));
  const currentCanvases = () => [...container.querySelectorAll<HTMLDivElement>(".study-page-canvas")];
  const corner = () => currentCanvases()[1].querySelector('[data-handle="nw"]')!;
  // A cancelled cross-page resize keeps the original rectangle and adds no undo step.
  await pointer(corner(), "pointerdown", 120, 1000);
  await pointer(currentCanvases()[1], "pointermove", 60, 790);
  await pointer(surface, "pointercancel", 60, 790);
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(1);
  expect(currentCanvases()[1].querySelector(".study-selection-box")!.getAttribute("style")).toBe(original);
  await pointer(corner(), "pointerdown", 120, 1000);
  await pointer(currentCanvases()[1], "pointermove", 60, 790);
  await pointer(surface, "pointermove", 60, 600);
  await pointer(surface, "pointerup", 60, 600);
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(2);
  expect(container.querySelectorAll(".study-selection-box")).toHaveLength(2);
  await click(button("撤销选区操作"));
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(1);
  expect(currentCanvases()[1].querySelector(".study-selection-box")!.getAttribute("style")).toBe(original);
  await click(button("撤销选区操作"));
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(0);
});

it("only expands to the maximum text width and leaves height, larger crops and scanned pages alone", () => {
  const bounds: api.StudyTextBounds = { status: "ready", bbox: [.1, .1, .9, .9] };
  expect(expandToTextWidth([.3, .2, .6, .4], bounds, 600)).toEqual([expect.closeTo(.09), .2, expect.closeTo(.91), .4]);
  expect(expandToTextWidth([0, .2, 1, .4], bounds, 600)).toEqual([0, .2, 1, .4]);
  expect(expandToTextWidth([.3, .2, .6, .4], { status: "no_text", bbox: null }, 600)).toEqual([.3, .2, .6, .4]);
});

it("resets the Markdown range anchor when starting another question", async () => {
  await mount(); await click(button("选题"));
  await click(container.querySelectorAll(".study-block-check")[2]);
  await click(button("取消")); await click(button("新建题目"));
  const first = container.querySelector(".study-block-check")!;
  await act(async () => first.dispatchEvent(new MouseEvent("click", { bubbles: true, shiftKey: true })));
  await settle();
  expect(container.querySelectorAll(".study-region-preview")).toHaveLength(1);
});

it("does not count removed questions in the visible default question number", async () => {
  vi.mocked(api.getStudyQuestions).mockResolvedValue([{ id: "removed", document_id: "d1", title: "",
    number: "", version: 1, deleted: true, regions: [] }]);
  await mount(); await click(button("选题"));
  expect(container.querySelector<HTMLInputElement>('[aria-label="题目标题"]')?.placeholder).toBe("题目 1");
});

it("preserves visible content while refusing external images, links and HTML", async () => {
  await act(async () => root.render(<StudyMarkdown text={'| A | B |\n|---|---|\n| 1 | 2 |\n\n$x^2$\n\n![图](https://example.invalid/x.png)\n\n<script>alert(1)</script>\n\n[链接](https://example.invalid)'} />));
  expect(container.querySelector("table")).not.toBeNull();
  expect(container.querySelector(".katex")).not.toBeNull();
  expect(container.querySelector("img,script,a[href]")).toBeNull();
  expect(container.textContent).toContain("缺少图片");
});

it("clamps pointer rectangles and moves selections without changing their dimensions", () => {
  expect(rectangle({ x: .8, y: .9 }, { x: .1, y: .2 })).toEqual([.1, .2, .8, .9]);
  expect(translateBox([.1, .2, .6, .7], -.5, .9)).toEqual([0, .5, .5, 1]);
});
