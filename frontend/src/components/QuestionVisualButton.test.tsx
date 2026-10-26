import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { QuestionVisualButton } from "./QuestionVisualButton";
import { autoQuestionImage, getQuestionVisual, verifyQuestionVisual, type QuestionVisual } from "../api/questionVisuals";
import type { Marker } from "../types";

vi.mock("../api/questionVisuals", async original => ({
  ...await original<typeof import("../api/questionVisuals")>(),
  getQuestionVisual: vi.fn(), autoQuestionImage: vi.fn(), verifyQuestionVisual: vi.fn(),
}));
const marker: Marker = { id: 8, video_id: 1, start_ms: 1000, end_ms: 5000, title: "Example",
  note: "", source_quote: "", analysis_mode: "question_extraction", review_status: "active" };
const saved: QuestionVisual = { marker_id: 8, question_id: "stable", status: "completed",
  algorithm: "question-image-time-v1", verdict: "uncertain", reason: "按时间自动配图", binding_mode: "auto",
  binding: { slide_id: 10, capture_id: "capture", representative_ms: 1500, source: "preferred" } };
let container: HTMLDivElement, root: Root, client: QueryClient;
const capture = vi.fn().mockResolvedValue(undefined);
const remove = vi.fn().mockResolvedValue(undefined);
async function render(busy = false) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <QuestionVisualButton marker={marker} state={saved} busy={busy} onCapture={capture} onRemove={remove} />
  </QueryClientProvider>));
}
async function open() {
  await act(async () => container.querySelector<HTMLButtonElement>(".question-visual-button")!.click());
  await vi.waitFor(() => expect(document.querySelector('img[alt="关联题图"]')).not.toBeNull());
}
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  vi.mocked(getQuestionVisual).mockResolvedValue(saved);
  vi.mocked(autoQuestionImage).mockResolvedValue(saved);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => { act(() => root.unmount()); client.clear(); container.remove(); });

it("captures in one click without opening a dialog or reading candidates", async () => {
  await render();
  await act(async () => container.querySelector<HTMLButtonElement>(".question-capture-current")!.click());
  expect(capture).toHaveBeenCalledTimes(1);
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(getQuestionVisual).not.toHaveBeenCalled();
  expect(verifyQuestionVisual).not.toHaveBeenCalled();
});

it("views the saved image without AI and restores focus on close", async () => {
  await render();
  const trigger = container.querySelector<HTMLButtonElement>(".question-visual-button")!;
  trigger.focus();
  await open();
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain("自动配图");
  expect(document.querySelector('img[alt="关联题图"]')?.getAttribute("src")).toContain("/captures/capture");
  act(() => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(document.activeElement).toBe(trigger);
  expect(autoQuestionImage).not.toHaveBeenCalled();
  expect(verifyQuestionVisual).not.toHaveBeenCalled();
});

it("runs local reassignment and cancels the binding only after explicit clicks", async () => {
  await render();
  await open();
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="按时间自动配图"]')!.click());
  expect(autoQuestionImage).toHaveBeenCalledWith(8);
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="取消题图"]')!.click());
  expect(remove).toHaveBeenCalledTimes(1);
  expect(verifyQuestionVisual).not.toHaveBeenCalled();
});

it("keeps image viewing available while writes are busy", async () => {
  await render(true);
  expect(container.querySelector<HTMLButtonElement>(".question-capture-current")!.disabled).toBe(true);
  await open();
  expect(document.querySelector<HTMLButtonElement>('[aria-label="按时间自动配图"]')!.disabled).toBe(true);
});
