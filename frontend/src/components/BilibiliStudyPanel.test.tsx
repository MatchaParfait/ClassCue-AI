import { act } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/bilibili";
import * as questions from "../api/questions";
import type { QuestionRun, QuestionState } from "../api/questions";
import { BilibiliStudyPanel } from "./BilibiliStudyPanel";
import type { BiliRun, BiliState } from "../api/bilibili";
import type { Marker, VideoSlide } from "../types";
import { cancelJob, getVideoSlides } from "../api/client";

const taskState = vi.hoisted(() => ({ status: "completed", progress: 100 }));
const questionTask = vi.hoisted(() => ({ status: "completed", progress: 100 }));

vi.mock("../api/questions", () => ({ getQuestionState: vi.fn(), saveQuestionMaterial: vi.fn(), analyzeQuestion: vi.fn(),
  uploadQuestionCapture: vi.fn(), deleteQuestionCapture: vi.fn(), questionCaptureUrl: (_: number, id: string) => `/captures/${id}`,
}));

vi.mock("../api/bilibili", () => ({
  getBiliState: vi.fn(), saveBiliContext: vi.fn(), planBiliSearch: vi.fn(), startBiliSearch: vi.fn(),
  getBiliAccount: vi.fn(), startBiliLogin: vi.fn(), pollBiliLogin: vi.fn(), cancelBiliLogin: vi.fn(),
  deleteBiliAccount: vi.fn(), resumeBiliReading: vi.fn(),
  continueBiliSearch: vi.fn(),
  biliFrameUrl: (marker: number, run: string, frame: string) => `/frames/${marker}/${run}/${frame}`,
}));
vi.mock("../hooks/useTaskStatus", () => ({
  useTaskStatus: (id: string | null) => ({ data: id ? { id, ...(id.startsWith("question-job") ? questionTask : taskState) } : undefined, isError: false, refetch: vi.fn() }),
  isTaskTerminal: (job: { status: string }) => ["completed", "failed", "interrupted"].includes(job.status),
}));
vi.mock("../api/client", () => ({ cancelJob: vi.fn().mockResolvedValue({ status: "interrupted" }), getVideoSlides: vi.fn(),
  getSlideImageUrl: (slide: VideoSlide) => `/api/videos/${slide.video_id}/slides/${slide.id}/image`,
}));

const marker = {
  id: 42, video_id: 1, title: "单调性", note: "分类讨论", source_quote: "这类题需要分类讨论",
  start_ms: 1000, end_ms: 5000, analysis_mode: "medium_recall", review_status: "active",
} as Marker;
const context = { course: "数学", notes: "" };
const sampleSlide = { id: 11, video_id: 1, start_ms: 0, end_ms: 5000, representative_ms: 1000 } as VideoSlide;
const run = {
  id: "run1", job_id: null, status: "ready", stale: false, error: "",
  queries: [{ text: "单调性 例题", purpose: "分类讨论" }], candidates: [],
  target: { course: "数学", keywords: ["单调性"], unknowns: [], requirements: [
    { id: "r1", text: "单调性例题", required: true, depth: "example", sources: ["marker.title"] }] },
  course_queries: [{ text: "高等数学课程", purpose: "课程补搜" }],
} as BiliRun;
let host: HTMLDivElement, background: HTMLElement, root: ReturnType<typeof createRoot>, client: QueryClient;
let state: BiliState;
let questionState: QuestionState;
const analysisRun: QuestionRun = {
  id: "analysis1", job_id: "question-job1", version: "question-analysis-v1", input_hash: "synthetic-hash",
  status: "completed", stale: false, error: "", target: run.target!, profile: { model: "synthetic-vision" },
  analysis: { kind: "exercise", facts: [
    { id: "f1", kind: "condition", text: "函数含参数 a", sources: ["question.text", "slide:11"] },
    { id: "f2", kind: "task", text: "求单调区间", sources: ["marker.source_quote", "classroom:1"] },
  ], knowledge_points: [{ id: "k1", text: "参数分类讨论", basis: ["f1"] }],
  methods: [{ id: "m1", text: "求导并分析符号", basis: ["f1", "f2"] }],
  criteria: [{ id: "r1", text: "演示参数分类例题", required: true, depth: "example", evidence_needed: "给出条件和分类步骤", basis: ["m1"] }],
  allowed_variations: ["允许不同系数"], unknowns: [], keywords: ["单调性"] },
  input: { classroom: { marker: { title: "单调性", note: "分类讨论", source_quote: "这类题需要分类讨论" },
    context: { course: "数学", notes: "寻找完整例题" }, classroom_segments: [{ id: 1, text: "合成课堂字幕", start_ms: 1000, end_ms: 5000 }] },
    question: { text: "函数含参数 a，求单调区间", images: [{ source: "slide:11", slide_id: 11, start_ms: 1000, end_ms: 5000, representative_ms: 2000 }] } },
};
const closed = vi.fn();

async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)); }); }
function button(label: string) {
  return [...host.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent?.trim() === label)!;
}
function setInput(input: HTMLInputElement, value: string) {
  act(() => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function render(onCaptureFrame?: () => Promise<{ blob: Blob; captured_at_ms: number }>) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <BilibiliStudyPanel marker={marker} backgroundRef={{ current: background }} onClose={closed} onCaptureFrame={onCaptureFrame} />
  </QueryClientProvider>));
  await settle();
  await settle();
}

beforeEach(() => {
  vi.clearAllMocks();
  taskState.status = "completed"; taskState.progress = 100;
  questionTask.status = "completed"; questionTask.progress = 100;
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  background = document.createElement("main");
  const opener = document.createElement("button");
  opener.textContent = "打开考点讲解";
  const timeline = document.createElement("input");
  timeline.type = "range"; timeline.max = "100"; timeline.setAttribute("aria-label", "视频进度");
  background.append(opener, timeline); document.body.append(background); opener.focus();
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  state = { context: { ...context }, runs: [] };
  questionState = { material: { text: "", slide_ids: [] }, runs: [] };
  vi.mocked(questions.getQuestionState).mockImplementation(async () => structuredClone(questionState));
  vi.mocked(questions.saveQuestionMaterial).mockImplementation(async (_, value) => {
    questionState.material = value;
    questionState.runs = questionState.runs.map(item => ({ ...item, stale: true }));
    return value;
  });
  vi.mocked(getVideoSlides).mockResolvedValue([]);
  vi.mocked(api.getBiliState).mockImplementation(async () => structuredClone(state));
  vi.mocked(api.getBiliAccount).mockResolvedValue({ configured: false, pause: null });
  vi.mocked(api.saveBiliContext).mockImplementation(async (_, value) => {
    state.context = value; return value;
  });
});

function selectTab(name: "material" | "understanding" | "search") {
  act(() => host.querySelector<HTMLButtonElement>(`#bili-tab-${name}`)!.click());
}
describe("question workspace", () => {
  it("captures the current player frame, selects it, saves it, and deletes without AI calls", async () => {
    const frame = { blob: new Blob(["synthetic"], { type: "image/png" }), captured_at_ms: 12345 };
    const capture = { id: "a".repeat(32), marker_id: 42, video_id: 1, captured_at_ms: 12345,
      width: 1280, height: 720, image_hash: "synthetic", mime_type: "image/png" };
    const captureFrame = vi.fn().mockResolvedValue(frame);
    vi.mocked(questions.uploadQuestionCapture).mockImplementation(async () => { questionState.captures = [capture]; return capture; });
    vi.mocked(questions.deleteQuestionCapture).mockImplementation(async () => {
      questionState.captures = []; questionState.material.capture_ids = []; return { deleted: true };
    });
    await render(captureFrame);
    await act(async () => button("从视频截图").click());
    expect(host.querySelector(".bili-backdrop")?.hasAttribute("hidden")).toBe(true);
    expect(background.inert).toBe(false);
    expect(document.body.hasAttribute("data-modal-locked")).toBe(false);
    expect(captureFrame).not.toHaveBeenCalled();
    await act(async () => button("截取当前画面").click()); await settle();
    expect(captureFrame).toHaveBeenCalledTimes(1);
    expect(questions.uploadQuestionCapture).toHaveBeenCalledWith(42, frame);
    expect(host.querySelector<HTMLInputElement>('[aria-label="选择截图 0:12"]')?.checked).toBe(true);
    expect(host.querySelector(".question-capture-toolbar")).toBeNull();
    expect(host.querySelector(".bili-backdrop")?.hasAttribute("hidden")).toBe(false);
    expect(background.inert).toBe(true);
    expect(button("生成搜索词").disabled).toBe(true);
    await act(async () => button("保存材料").click());
    expect(questions.saveQuestionMaterial).toHaveBeenCalledWith(42, { text: "", slide_ids: [], capture_ids: [capture.id] });
    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="删除截图 0:12"]')!.click()); await settle();
    expect(questions.deleteQuestionCapture).toHaveBeenCalledWith(42, capture.id);
    expect(host.querySelector('[aria-label="选择截图 0:12"]')).toBeNull();
    expect(questions.analyzeQuestion).not.toHaveBeenCalled();
  });

  it("blocks capture when four PPTs are already selected and shows capture failure", async () => {
    questionState.material = { text: "", slide_ids: [1, 2, 3, 4] };
    const capture = vi.fn().mockRejectedValue(new Error("当前画面尚未就绪"));
    await render(capture);
    expect(button("从视频截图").disabled).toBe(true);
    expect(capture).not.toHaveBeenCalled();
    await act(async () => button("移除失效选择").click());
    await act(async () => button("从视频截图").click());
    await act(async () => button("截取当前画面").click());
    expect(host.querySelector(".question-capture-toolbar [role='alert']")?.textContent).toContain("当前画面尚未就绪");
    expect(background.inert).toBe(false);
    expect(button("截取当前画面").disabled).toBe(false);
    expect(questions.uploadQuestionCapture).not.toHaveBeenCalled();
    await act(async () => button("返回题目材料").click());
    expect(background.inert).toBe(true);
    expect(host.querySelector(".bili-dialog [role='alert']")?.textContent).toContain("当前画面尚未就绪");
  });

  it("unlocks the player while framing and restores unsaved fields, scroll, and modal focus on return", async () => {
    const captureFrame = vi.fn();
    vi.mocked(getVideoSlides).mockResolvedValue([sampleSlide]);
    await render(captureFrame);
    setInput(host.querySelector<HTMLInputElement>(".bili-fields input")!, "高等数学");
    act(() => host.querySelector<HTMLInputElement>('[aria-label="选择 PPT 11"]')!.click());
    const body = host.querySelector<HTMLDivElement>(".bili-body")!;
    body.scrollTop = 160;
    expect(background.inert).toBe(true);
    await act(async () => button("从视频截图").click());
    expect(background.inert).toBe(false);
    expect(document.body.style.overflow).not.toBe("hidden");
    const timeline = background.querySelector<HTMLInputElement>('input[type="range"]')!;
    act(() => { timeline.focus(); timeline.value = "65"; timeline.dispatchEvent(new Event("input", { bubbles: true })); });
    expect(document.activeElement).toBe(timeline);
    expect(timeline.value).toBe("65");
    await act(async () => button("返回题目材料").click());
    expect(host.querySelector<HTMLInputElement>(".bili-fields input")?.value).toBe("高等数学");
    expect(host.querySelector<HTMLInputElement>('[aria-label="选择 PPT 11"]')?.checked).toBe(true);
    expect(body.scrollTop).toBe(160);
    expect(background.inert).toBe(true);
    expect(document.body.style.overflow).toBe("hidden");
    expect(document.activeElement).toBe(host.querySelector('[aria-label="关闭 B 站讲解"]'));
    await act(async () => button("从视频截图").click());
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })));
    expect(host.querySelector(".question-capture-toolbar")).toBeNull();
    expect(background.inert).toBe(true);
    expect(closed).not.toHaveBeenCalled();
    expect(captureFrame).not.toHaveBeenCalled();
    expect(questions.saveQuestionMaterial).not.toHaveBeenCalled();
    expect(questions.analyzeQuestion).not.toHaveBeenCalled();
    expect(api.startBiliSearch).not.toHaveBeenCalled();
    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="关闭 B 站讲解"]')!.click());
    expect(closed).toHaveBeenCalledTimes(1);
    await act(async () => root.render(null));
    expect(document.activeElement).toBe(background.querySelector("button"));
    expect(background.inert).toBe(false);
  });

  it("prevents duplicate captures and keeps the framing toolbar available after an upload error", async () => {
    const frame = { blob: new Blob(["synthetic"], { type: "image/png" }), captured_at_ms: 12000 };
    const captureFrame = vi.fn().mockResolvedValue(frame);
    let rejectUpload!: (error: Error) => void;
    vi.mocked(questions.uploadQuestionCapture).mockImplementation(() => new Promise((_, reject) => { rejectUpload = reject; }));
    await render(captureFrame);
    await act(async () => button("从视频截图").click());
    await act(async () => button("截取当前画面").click());
    expect(button("正在保存截图").disabled).toBe(true);
    expect(button("返回题目材料").disabled).toBe(true);
    act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })));
    expect(host.querySelector(".question-capture-toolbar")).not.toBeNull();
    expect(captureFrame).toHaveBeenCalledTimes(1);
    await act(async () => rejectUpload(new Error("本地保存失败")));
    expect(host.querySelector(".question-capture-toolbar [role='alert']")?.textContent).toContain("本地保存失败");
    expect(button("截取当前画面").disabled).toBe(false);
    expect(button("返回题目材料").disabled).toBe(false);
    expect(background.inert).toBe(false);
  });
  it("opens materials without AI requests and supports keyboard tab navigation", async () => {
    await render();
    expect(questions.getQuestionState).toHaveBeenCalledWith(42);
    expect(questions.analyzeQuestion).not.toHaveBeenCalled();
    expect(api.planBiliSearch).not.toHaveBeenCalled();
    expect(host.querySelector("#bili-panel-search")?.hasAttribute("hidden")).toBe(true);
    const tab = host.querySelector<HTMLButtonElement>("#bili-tab-material")!;
    act(() => { tab.focus(); tab.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true })); });
    expect(document.activeElement?.id).toBe("bili-tab-understanding");
    expect(host.querySelector("#bili-panel-understanding")?.hasAttribute("hidden")).toBe(false);
  });

  it("selects nearby or all PPTs, previews inline, and enforces four manual selections", async () => {
    vi.mocked(getVideoSlides).mockResolvedValue(Array.from({ length: 6 }, (_, i) => ({
      id: 11 + i, video_id: 1, start_ms: i < 2 ? 0 : 60000, end_ms: i < 2 ? 10000 : 70000,
      representative_ms: i < 2 ? 5000 : 65000, is_llm_preferred: i === 0,
    } as VideoSlide)));
    await render();
    expect(host.querySelectorAll(".question-slide")).toHaveLength(2);
    expect(host.querySelectorAll('.question-slide input:checked')).toHaveLength(0);
    const range = host.querySelector<HTMLSelectElement>('[aria-label="PPT 范围"]')!;
    act(() => { range.value = "all"; range.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(host.querySelectorAll(".question-slide")).toHaveLength(6);
    const checkboxes = host.querySelectorAll<HTMLInputElement>(".question-slide input");
    for (let i = 0; i < 4; i++) act(() => checkboxes[i].click());
    expect(checkboxes[4].disabled).toBe(true);
    expect(button("生成搜索词").disabled).toBe(true);
    act(() => host.querySelector<HTMLButtonElement>('[aria-label="放大 PPT 11"]')!.click());
    expect(host.querySelector(".question-preview img")).not.toBeNull();
    expect(host.querySelectorAll('[role="dialog"]')).toHaveLength(1);
    act(() => host.querySelector<HTMLButtonElement>('[aria-label="收起预览"]')!.click());
    await act(async () => button("保存材料").click());
    expect(questions.saveQuestionMaterial).toHaveBeenCalledWith(42, { text: "", slide_ids: [11, 12, 13, 14] });
    expect(questions.analyzeQuestion).not.toHaveBeenCalled();
  });

  it("saves input before analyzing, shows grounded understanding, then explicitly generates searches", async () => {
    vi.mocked(getVideoSlides).mockResolvedValue([sampleSlide]);
    vi.mocked(questions.analyzeQuestion).mockImplementation(async () => {
      questionState.runs = [structuredClone(analysisRun)]; return { analysis_id: "analysis1", job_id: "question-job1" };
    });
    vi.mocked(api.planBiliSearch).mockImplementation(async () => { state.runs = [structuredClone(run)]; return { run_id: run.id, job_id: "search-job" }; });
    await render();
    act(() => host.querySelector<HTMLInputElement>('[aria-label="选择 PPT 11"]')!.click());
    setInput(host.querySelector<HTMLInputElement>(".bili-fields input")!, "高等数学");
    expect(button("生成搜索词").disabled).toBe(true);
    await act(async () => button("分析题目").click()); await settle();
    expect(questions.saveQuestionMaterial).toHaveBeenCalledWith(42, { text: "", slide_ids: [11] });
    expect(api.saveBiliContext).toHaveBeenCalledWith(42, { course: "高等数学", notes: "" });
    expect(vi.mocked(questions.saveQuestionMaterial).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(questions.analyzeQuestion).mock.invocationCallOrder[0]);
    expect(host.querySelector("#bili-tab-understanding")?.getAttribute("aria-selected")).toBe("true");
    expect(host.textContent).toContain("当前搜索依据");
    expect(host.textContent).toContain("可能的解法");
    expect(host.textContent).toContain("给出条件和分类步骤");
    expect(host.textContent).toContain("synthetic-vision");
    expect(host.textContent).toContain("合成课堂字幕");
    expect(host.textContent).toContain("课堂字幕 0:01：合成课堂字幕");
    expect(api.planBiliSearch).not.toHaveBeenCalled();
    await act(async () => button("生成搜索方案").click()); await settle();
    expect(api.planBiliSearch).toHaveBeenCalledWith(42, true);
    expect(host.querySelector("#bili-tab-search")?.getAttribute("aria-selected")).toBe("true");
    expect(api.startBiliSearch).not.toHaveBeenCalled();
  });

  it("blocks old targets immediately when materials or context change", async () => {
    questionState.runs = [structuredClone(analysisRun)]; state.runs = [structuredClone(run)];
    vi.mocked(getVideoSlides).mockResolvedValue([sampleSlide]);
    await render();
    expect(button("重新生成搜索词").disabled).toBe(false);
    act(() => host.querySelector<HTMLInputElement>('[aria-label="选择 PPT 11"]')!.click());
    expect(button("重新生成搜索词").disabled).toBe(true);
    expect(host.textContent).toContain("材料已变化");
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    expect(button("搜索并核验").disabled).toBe(true);
  });

  it("does not use an unclear analysis or allow a historical run to generate a new search", async () => {
    questionState.runs = [{ ...structuredClone(analysisRun), target: null,
      analysis: { ...analysisRun.analysis!, kind: "unclear", criteria: [], unknowns: ["缺少参数范围"] } }];
    await render(); selectTab("understanding");
    expect(host.textContent).toContain("缺少参数范围");
    expect(button("生成搜索方案").disabled).toBe(true);
    questionState.runs = [structuredClone(analysisRun), { ...structuredClone(analysisRun), id: "old", stale: true }];
    await act(async () => { await client.invalidateQueries({ queryKey: ["question", 42] }); });
    await settle();
    const history = host.querySelector<HTMLSelectElement>('[aria-label="题目分析记录"]')!;
    act(() => { history.value = "old"; history.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(button("生成搜索方案").disabled).toBe(true);
    expect(api.planBiliSearch).not.toHaveBeenCalled();
  });

  it("keeps question progress and cancellation available across tabs and histories", async () => {
    questionTask.status = "running"; questionTask.progress = 35;
    questionState.runs = [{ ...structuredClone(analysisRun), status: "analyzing", analysis: null, target: null },
      { ...structuredClone(analysisRun), id: "old", job_id: "old-job" }];
    await render(); selectTab("understanding");
    const history = host.querySelector<HTMLSelectElement>('[aria-label="题目分析记录"]')!;
    act(() => { history.value = "old"; history.dispatchEvent(new Event("change", { bubbles: true })); });
    selectTab("search");
    expect(host.querySelector<HTMLProgressElement>('[aria-label="题目分析进度"]')?.value).toBe(35);
    await act(async () => button("停止").click());
    expect(cancelJob).toHaveBeenCalledWith("question-job1");
    expect(questions.analyzeQuestion).not.toHaveBeenCalled();
    expect(api.planBiliSearch).not.toHaveBeenCalled();
  });

  it("restores saved material on reopen and resets drafts when changing markers", async () => {
    vi.mocked(getVideoSlides).mockResolvedValue([sampleSlide, { ...sampleSlide, id: 12 }]);
    questionState.material = { text: "已保存的题目", slide_ids: [11] };
    await render();
    expect(host.querySelector<HTMLInputElement>('[aria-label="选择 PPT 11"]')?.checked).toBe(true);
    act(() => host.querySelector<HTMLInputElement>('[aria-label="选择 PPT 12"]')!.click());
    questionState.material = { text: "另一个考点", slide_ids: [12] };
    await act(async () => root.render(<QueryClientProvider client={client}><BilibiliStudyPanel marker={{ ...marker, id: 43 }} backgroundRef={{ current: null }} onClose={closed} /></QueryClientProvider>));
    await settle();
    await settle();
    expect(questions.getQuestionState).toHaveBeenCalledWith(43);
    expect(host.querySelector<HTMLInputElement>('[aria-label="选择 PPT 11"]')?.checked).toBe(false);
    expect(host.querySelector<HTMLInputElement>('[aria-label="选择 PPT 12"]')?.checked).toBe(true);
    expect(host.querySelector("textarea")).toBeNull();
    expect(questions.saveQuestionMaterial).not.toHaveBeenCalled();
  });

  it("removes manual text inputs while preserving saved legacy data when course or images change", async () => {
    state.context.notes = "历史学习要求";
    questionState.material = { text: "历史题干", slide_ids: [] };
    vi.mocked(getVideoSlides).mockResolvedValue([sampleSlide]);
    await render();
    expect(host.querySelector("textarea")).toBeNull();
    expect(host.querySelector(".question-text")).toBeNull();
    expect(host.querySelector("#bili-panel-material")?.textContent).not.toContain("学习要求");
    expect(host.querySelector("#bili-panel-material")?.textContent).not.toContain("题目文字");
    setInput(host.querySelector<HTMLInputElement>(".bili-fields input")!, "高等数学");
    act(() => host.querySelector<HTMLInputElement>('[aria-label="选择 PPT 11"]')!.click());
    await act(async () => button("保存材料").click());
    expect(questions.saveQuestionMaterial).toHaveBeenCalledWith(42, { text: "历史题干", slide_ids: [11] });
    expect(api.saveBiliContext).toHaveBeenCalledWith(42, { course: "高等数学", notes: "历史学习要求" });
    expect(questions.analyzeQuestion).not.toHaveBeenCalled();
  });

  it("reports failed requests and permits retry without starting search", async () => {
    vi.mocked(questions.analyzeQuestion).mockRejectedValueOnce(new Error("模型不支持图片"));
    await render();
    await act(async () => button("分析题目").click());
    expect(host.textContent).toContain("模型不支持图片");
    expect(button("分析题目").disabled).toBe(false);
    expect(api.startBiliSearch).not.toHaveBeenCalled();
  });

  it("removes missing selected slides and displays broken image states", async () => {
    questionState.material = { text: "", slide_ids: [99] };
    vi.mocked(getVideoSlides).mockResolvedValue([{ id: 11, video_id: 1, start_ms: 0, end_ms: 5000, representative_ms: 1000 } as VideoSlide]);
    await render();
    expect(host.textContent).toContain("已选 PPT #99 已不存在");
    act(() => host.querySelector(".question-thumbnail img")!.dispatchEvent(new Event("error")));
    expect(host.textContent).toContain("图片不可用");
    await act(async () => button("移除失效选择").click());
    await act(async () => button("保存材料").click());
    expect(questions.saveQuestionMaterial).toHaveBeenCalledWith(42, { text: "", slide_ids: [] });
  });
});
afterEach(async () => {
  await act(async () => root.unmount());
  client.clear(); host.remove(); background.remove();
  vi.unstubAllGlobals();
});

describe("marker-scoped Bilibili panel", () => {
  it("loads and saves only the selected marker context without calling the model on open", async () => {
    await render();
    expect(api.getBiliState).toHaveBeenCalledWith(42);
    expect(button("生成搜索词").disabled).toBe(false);
    expect(host.textContent).not.toContain("同意将本考点");
    const inputs = host.querySelectorAll<HTMLInputElement>(".bili-fields input");
    expect(inputs).toHaveLength(1);
    expect(host.textContent).not.toContain("学段或考试类型");
    expect(host.textContent).not.toContain("教材版本");
    expect(host.querySelector("textarea")).toBeNull();
    expect(inputs[0].value).toBe("数学");
    setInput(inputs[0], "概率论");
    await act(async () => button("保存材料").click());
    await settle();
    expect(api.saveBiliContext).toHaveBeenCalledWith(42, { course: "概率论", notes: "" });
    expect(api.planBiliSearch).not.toHaveBeenCalled();
  });

  it("shows generated queries after the job changes state and sends edited queries only after confirmation", async () => {
    vi.mocked(api.planBiliSearch).mockImplementation(async () => {
      state.runs = [structuredClone(run)]; return { run_id: "run1", job_id: "job1" };
    });
    vi.mocked(api.startBiliSearch).mockImplementation(async () => {
      state.runs = [{ ...run, id: "run2", status: "completed" }, run];
      return { run_id: "run2", job_id: "job2" };
    });
    await render();
    await act(async () => button("生成搜索词").click());
    await settle();
    expect(api.planBiliSearch).toHaveBeenCalledWith(42, true);
    const query = host.querySelector<HTMLInputElement>(".bili-queries input")!;
    expect(query.value).toBe("单调性 例题");
    setInput(query, "单调性 参数 分类讨论");
    expect(button("搜索并核验").disabled).toBe(true);
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    await act(async () => button("搜索并核验").click());
    await settle();
    expect(api.startBiliSearch).toHaveBeenCalledWith(42, "run1",
      [{ text: "单调性 参数 分类讨论", purpose: "分类讨论" }], true,
      { target: run.target, course_queries: run.course_queries, auto_rewrite: false, visual_verification: true });
  });

  it("defaults to visual checking and allows explicit subtitle-only searches", async () => {
    state.runs = [structuredClone(run)];
    vi.mocked(api.startBiliSearch).mockResolvedValue({ run_id: "run2", job_id: "job2" });
    await render();
    const visual = [...host.querySelectorAll("label")].find(item => item.textContent?.includes("截图复核题型"))!
      .querySelector<HTMLInputElement>("input")!;
    expect(visual.checked).toBe(true);
    act(() => visual.click());
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    await act(async () => button("搜索并核验").click());
    expect(api.startBiliSearch).toHaveBeenCalledWith(42, run.id, run.queries, true, expect.objectContaining({
      visual_verification: false,
    }));
  });

  it("shows separate visual dimensions and candidate frames with inline preview and broken-image state", async () => {
    const dimension = { status: "yes" as const, reason: "合成图文依据", frame_ids: ["frame:" + "a".repeat(32)], segment_ids: [7] };
    state.runs = [{ ...run, status: "completed", visual_verification: true, candidates: [{
      bvid: "BV1xx411c7mD", title: "合成候选题型", author: "", status: "done", url: "https://www.bilibili.com/video/BV1xx411c7mD/",
      units: [{ id: "one", page: 2, cid: 9, part: "例题", status: "checked", verdict: "partial",
        verification_mode: "visual", url: "https://www.bilibili.com/video/BV1xx411c7mD/?p=2",
        dimensions: { knowledge: dimension, problem_type: { ...dimension, status: "partial" },
          explanation: { ...dimension, status: "unknown" } },
        visual: { status: "checked", rounds: 2 },
        frames: [{ id: "a".repeat(32), captured_ms: 12500, requested_ms: 12000, width: 960, height: 540,
          image_hash: "synthetic", time_source: "decoded_frame", url: "https://www.bilibili.com/video/BV1xx411c7mD/?p=2&t=12" }],
      }],
    }] }];
    await render(); selectTab("search");
    expect(host.querySelector(".bili-dimensions")?.textContent).toContain("同知识点已确认");
    expect(host.querySelector(".bili-dimensions")?.textContent).toContain("类似题型部分符合");
    expect(host.querySelector(".bili-other")?.textContent).toContain("合成候选题型");
    expect(host.textContent).toContain("已补拍");
    act(() => host.querySelector<HTMLButtonElement>('[aria-label="查看候选截图 0:12"]')!.click());
    expect(host.querySelector(".bili-visual .question-preview img")?.getAttribute("src")).toContain("/frames/42/run1/");
    act(() => host.querySelector<HTMLButtonElement>('[aria-label="收起候选题图"]')!.click());
    expect(host.querySelector(".bili-visual .question-preview")).toBeNull();
    act(() => host.querySelector(".bili-frame-grid img")!.dispatchEvent(new Event("error")));
    expect(host.textContent).toContain("截图不可用");
    expect(host.querySelector('[aria-label="打开候选视频 0:12"]')?.getAttribute("href")).toContain("p=2&t=12");
  });

  it("separates evidence-backed recommendations from unverified candidates and preserves exact timestamps", async () => {
    state.runs = [{ ...run, status: "completed", candidates: [
      { bvid: "BV1xx411c7mD", title: "例题讲解", author: "合成作者", url: "https://www.bilibili.com/video/BV1xx411c7mD/",
        status: "unverified", unchecked_pages: 2, units: [{
          id: "unit1", page: 1, cid: 1, part: "分类讨论", url: "https://www.bilibili.com/video/BV1xx411c7mD/?p=1",
          status: "checked", verdict: "partial", reason: "只覆盖部分方法",
          source: { language: "zh-CN", automatic: true },
          coverage: { partial: true, selected_segments: 20, total_segments: 100 },
          evidence: [{ id: 7, start_ms: 12500, end_ms: 15000, text: "先判断参数范围",
            url: "https://www.bilibili.com/video/BV1xx411c7mD/?p=1&t=12" }],
        }] },
      { bvid: "BV1yy411c7mD", title: "无字幕候选", author: "", status: "unverified", units: [],
        url: "https://www.bilibili.com/video/BV1yy411c7mD/" },
    ] }];
    await render();
    expect(host.textContent).toContain("部分相关");
    expect(host.textContent).toContain("20/100");
    expect(host.textContent).toContain("另有 2 个分 P 未核验");
    expect(host.querySelector(".bili-other")?.textContent).toContain("无字幕候选");
    expect(host.querySelector('a[href$="t=12"]')?.textContent).toContain("0:12");
    expect(host.textContent).not.toContain("原题匹配");
  });

  it("disables stale searches and replaces manual credentials with QR login", async () => {
    const storeSecret = vi.fn();
    vi.stubGlobal("localStorage", { setItem: storeSecret });
    state.runs = [{ ...run, stale: true }];
    await render();
    expect(host.textContent).toContain("以下为历史结果");
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    expect(button("搜索并核验").disabled).toBe(true);
    expect(host.querySelector('input[type="password"]')).toBeNull();
    expect(button("扫码登录")).toBeDefined();
    expect(api.startBiliLogin).not.toHaveBeenCalled();
    expect(storeSecret).not.toHaveBeenCalled();
  });

  it("closes without triggering searches", async () => {
    await render();
    await act(async () => host.querySelector<HTMLButtonElement>('[aria-label="关闭 B 站讲解"]')!.click());
    expect(closed).toHaveBeenCalledTimes(1);
    expect(api.startBiliSearch).not.toHaveBeenCalled();
  });

  it("edits course fallback and target depth before confirming", async () => {
    state.runs = [structuredClone(run)];
    vi.mocked(api.startBiliSearch).mockResolvedValue({ run_id: "run1", job_id: "job1" });
    await render();
    setInput(host.querySelector<HTMLInputElement>('[aria-label="课程搜索词 1"]')!, "大学数学课程");
    const depth = host.querySelector<HTMLSelectElement>('[aria-label="讲解深度 1"]')!;
    act(() => { depth.value = "method"; depth.dispatchEvent(new Event("change", { bubbles: true })); });
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    await act(async () => button("搜索并核验").click());
    expect(api.startBiliSearch).toHaveBeenCalledWith(42, "run1", run.queries, true, expect.objectContaining({
      course_queries: [{ text: "大学数学课程", purpose: "课程补搜" }],
      target: expect.objectContaining({ requirements: [expect.objectContaining({ depth: "method", sources: ["user.confirmed"] })] }),
    }));
  });

  it("continues the same interrupted run without creating a new search", async () => {
    state.runs = [{ ...run, status: "interrupted", phase: "course", stop_reason: "cancelled" }];
    vi.mocked(api.continueBiliSearch).mockResolvedValue({ run_id: "run1", job_id: "continued" });
    await render();
    expect(host.textContent).toContain("课程类别检索");
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    await act(async () => button("继续搜索与核验").click());
    expect(api.continueBiliSearch).toHaveBeenCalledWith(42, "run1", undefined, false);
    expect(api.startBiliSearch).not.toHaveBeenCalled();
  });

  it("shows the exact budget stop in the panel and trace before explicitly extending the same run", async () => {
    state.runs = [{ ...structuredClone(run), status: "budget_exhausted", phase: "course", stop_reason: "budget",
      budget: { platform: 24, llm: 3, videos: 10, attempts: 0, verified: 0, seconds: 60, tokens: 1000 },
      limits: { platform: 60, llm: 48, videos: 10, attempts: 15, verified: 5, seconds: 1800, tokens: 600000 },
      phase_limits: { exact: { videos: 6, platform: 30 } },
      budget_stop: { scope: "task", phase: "course", resource: "videos", used: 10, limit: 10, requested: 1 },
      trace: [{ seq: 1, action: "phase_budget", phase: "exact",
        budget_stop: { scope: "exact", resource: "videos", used: 6, limit: 6, requested: 1 } },
        { seq: 2, action: "stop", phase: "course", reason: "budget",
          budget_stop: { scope: "task", resource: "videos", used: 10, limit: 10, requested: 1 } }],
    }];
    vi.mocked(api.continueBiliSearch).mockResolvedValue({ run_id: "run1", job_id: "continued" });
    await render(); selectTab("search");
    expect(host.querySelector(".bili-budget [role='status']")?.textContent).toContain("视频详情尝试已达到本次任务上限");
    expect(host.querySelector(".bili-trace")?.textContent).toContain("视频详情尝试已达到精准阶段上限");
    expect(host.querySelector(".bili-trace")?.textContent).toContain("已用 10 / 上限 10");
    expect(host.querySelectorAll(".bili-budget tbody tr")).toHaveLength(7);
    expect(api.continueBiliSearch).not.toHaveBeenCalled();
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    expect(button("继续搜索与核验").disabled).toBe(true);
    const extension = [...host.querySelectorAll("label")].find(item => item.textContent?.includes("追加一份初始预算"))!;
    act(() => extension.querySelector<HTMLInputElement>("input")!.click());
    await act(async () => button("继续搜索与核验").click());
    expect(api.continueBiliSearch).toHaveBeenCalledWith(42, "run1", undefined, true);
    expect(api.startBiliSearch).not.toHaveBeenCalled();
  });

  it("waits for approval before submitting rewritten course queries", async () => {
    state.runs = [{ ...run, status: "awaiting_approval", pending_queries: [{ text: "数学习题课程", purpose: "缺少例题" }] }];
    vi.mocked(api.continueBiliSearch).mockResolvedValue({ run_id: "run1", job_id: "continued" });
    await render();
    expect(button("确认并补搜").disabled).toBe(true);
    expect(api.continueBiliSearch).not.toHaveBeenCalled();
    act(() => host.querySelector<HTMLInputElement>(".bili-consent input")!.click());
    await act(async () => button("确认并补搜").click());
    expect(api.continueBiliSearch).toHaveBeenCalledWith(42, "run1", state.runs[0].pending_queries, false);
  });

  it("keeps partial matches outside verified recommendations and labels plain videos", async () => {
    state.runs = [{ ...run, status: "completed", stop_reason: "partial", candidates: [{
      bvid: "BV1xx411c7mD", title: "普通视频", author: "", url: "https://www.bilibili.com/video/BV1xx411c7mD/",
      status: "done", units: [{ id: "one", page: 1, cid: 1, part: "概念解释", multipart: false,
        url: "https://www.bilibili.com/video/BV1xx411c7mD/", status: "checked", verdict: "partial", reason: "缺少例题" }],
    }] }];
    await render();
    expect(host.querySelector(".bili-other")?.textContent).toContain("普通视频");
    expect(host.textContent).not.toContain("P1");
    expect(host.textContent).toContain("仅找到部分相关讲解");
  });

  it("shows progress and cancels the active job without restarting search", async () => {
    taskState.status = "running"; taskState.progress = 42;
    state.runs = [{ ...run, status: "checking", job_id: "running-job" }];
    await render();
    expect(host.querySelector<HTMLProgressElement>("progress")?.value).toBe(42);
    await act(async () => button("停止").click());
    expect(cancelJob).toHaveBeenCalledWith("running-job");
    expect(api.startBiliSearch).not.toHaveBeenCalled();
    expect(api.continueBiliSearch).not.toHaveBeenCalled();
  });
});
