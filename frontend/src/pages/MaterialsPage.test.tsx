import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MaterialsPage } from "./MaterialsPage";
import * as api from "../api/materials";
import * as common from "../api/client";
import { MaterialText } from "../components/MaterialReader";

vi.mock("../api/materials", async original => ({
  ...await original<typeof import("../api/materials")>(),
  getCollections: vi.fn(), getDocuments: vi.fn(), getCategories: vi.fn(), getMaterialItems: vi.fn(),
  getMaterialItem: vi.fn(), getMaterialBlocks: vi.fn(), getMaterialRuns: vi.fn(), getMaterialFolders: vi.fn(),
  materialRequest: vi.fn(),
}));
vi.mock("../api/client", async original => ({
  ...await original<typeof import("../api/client")>(), getAiSettings: vi.fn(), getRecentJobs: vi.fn(),
}));

let container: HTMLDivElement, root: Root, client: QueryClient;
const onJob = vi.fn();
const first: api.MaterialDetail = {
  id: "q1", document_id: "d1", document_name: "模拟卷一.pdf", ordinal: 0, title: "绘制图书借阅 E-R 图",
  number: "三（1）", kind: "question", quality: "ready", reason: "", sources: [{ block_id: "b1", bbox: [0.1, 0.2, 0.9, 0.5] }],
  category_ids: ["type"], tags: ["绘图"], parent_id: null, references: [], correction: "", correction_image: null,
  answer_sources: [], version: 1, study_status: "unreviewed", origin: "automatic", deleted: 0,
  regions: [{ id: "b1", document_id: "d1", block_id: "b1", ordinal: 0, type: "page", text: "原题", image: "d1/page.png",
    coverage: "identified", reason: "", page: 1, bbox: [0.1, 0.2, 0.9, 0.5] }],
  shared: null, answer_regions: [], answers: [], versions: [],
};
const second: api.MaterialDetail = { ...first, id: "q2", document_id: "d2", document_name: "模拟卷二.md", title: "绘制课程选修 E-R 图" };
const categories: api.MaterialCategory[] = [
  { id: "topic", collection_id: "c1", parent_id: null, name: "数据库设计", aliases: [], position: 1, count: 2 },
  { id: "type", collection_id: "c1", parent_id: "topic", name: "绘制实体联系图（E-R 图）", aliases: ["ER 图"], position: 2, count: 2 },
];
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  window.localStorage.clear();
  vi.clearAllMocks();
  vi.mocked(api.getCollections).mockResolvedValue([{ id: "c1", name: "数据库原理", folder_ids: [], total: 2, mastered: 0 }]);
  vi.mocked(api.getDocuments).mockResolvedValue([
    { id: "d1", collection_id: "c1", name: "模拟卷一.pdf", format: "pdf", state: "completed", reason: "", deleted: 0, block_count: 1, remaining: 0, pending_count: 0 },
    { id: "d2", collection_id: "c1", name: "模拟卷二.md", format: "markdown", state: "completed", reason: "", deleted: 0, block_count: 1, remaining: 0, pending_count: 0 },
  ]);
  vi.mocked(api.getCategories).mockResolvedValue(categories);
  vi.mocked(api.getMaterialItems).mockResolvedValue([first, second]);
  vi.mocked(api.getMaterialItem).mockImplementation(async id => id === "q1" ? first : second);
  vi.mocked(api.getMaterialBlocks).mockResolvedValue(first.regions);
  vi.mocked(api.getMaterialRuns).mockResolvedValue([]);
  vi.mocked(api.getMaterialFolders).mockResolvedValue([]);
  vi.mocked(common.getAiSettings).mockResolvedValue({ connection_id: "active", connections: [
    { id: "active", name: "合成连接", model: "fake", has_api_key: false, base_url: "" },
  ] } as never);
  vi.mocked(common.getRecentJobs).mockResolvedValue([]);
  vi.mocked(api.materialRequest).mockResolvedValue({ ok: true });
  container = document.createElement("div"); document.body.append(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); client.clear(); container.remove(); });
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 40)); }); }
async function mount() {
  await act(async () => root.render(<QueryClientProvider client={client}><MaterialsPage scope="synthetic" onJob={onJob} /></QueryClientProvider>));
  await settle(); await settle();
}
async function click(button: Element | null | undefined) {
  expect(button).toBeTruthy();
  await act(async () => (button as HTMLElement).click()); await settle();
}

it("aggregates papers under the human-readable category without invoking a model or changing study state", async () => {
  await mount();
  await click([...container.querySelectorAll(".material-directory-row")].find(el => el.textContent?.includes("绘制实体联系图")));
  expect(container.querySelectorAll(".material-item-row")).toHaveLength(2);
  expect(container.textContent).toContain("模拟卷一.pdf");
  expect(container.textContent).toContain("模拟卷二.md");
  await click(container.querySelector(".material-item-row > button"));
  expect(container.querySelector(".material-reading-heading")?.textContent).toContain(first.title);
  expect(container.textContent).toContain("自动整理");
  expect(api.materialRequest).not.toHaveBeenCalled();
  const image = container.querySelector(".material-question-image") as HTMLImageElement;
  expect(image.src).toContain("left=0.1");
  expect(image.src).toContain("top=0.2");
});

it("locates the exact source and persists the selected question", async () => {
  await mount(); await click(container.querySelector(".material-item-row > button"));
  await click([...container.querySelectorAll("button")].find(el => el.textContent === "原文定位"));
  expect(container.querySelector('[data-source="b1"]')).toBeTruthy();
  expect((container.querySelector(".material-highlight") as HTMLElement).style.top).toBe("20%");
  expect(JSON.parse(window.localStorage.getItem("classcue-materials:synthetic")!).item).toBe("q1");
});

it("only explicit status controls mark progress and send the current content version", async () => {
  await mount(); await click(container.querySelector(".material-item-row > button"));
  await click([...container.querySelectorAll(".material-study-control button")].find(el => el.textContent?.includes("已掌握")));
  expect(api.materialRequest).toHaveBeenCalledWith("/api/material-items/q1/study", "PUT", { status: "mastered", version: 1 });
});

it("selects the active connection and exposes independent source and pending views", async () => {
  await mount();
  expect((container.querySelector('[aria-label="整理连接"]') as HTMLSelectElement).value).toBe("active");
  await click([...container.querySelectorAll('[role="tab"]')].find(el => el.textContent?.includes("按原始资料")));
  expect(container.querySelectorAll(".material-document-row")).toHaveLength(2);
  await click([...container.querySelectorAll('[role="tab"]')].find(el => el.textContent?.includes("待处理")));
  expect(container.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toContain("待处理");
  expect(api.materialRequest).not.toHaveBeenCalled();
});

it("renders tables and math while refusing external images, active HTML and links", async () => {
  await act(async () => root.render(<MaterialText text={'| A | B |\n|---|---|\n| 1 | 2 |\n\n$x^2$\n\n![photo](https://example.invalid/photo.png)\n\n<script>alert(1)</script>\n\n[external](https://example.invalid)'} />));
  expect(container.querySelector("table")).toBeTruthy();
  expect(container.querySelector(".katex")).toBeTruthy();
  expect(container.querySelector("img,script,a[href]")).toBeNull();
});
