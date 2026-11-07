import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import type { Job, SecondaryView } from "../types";
import { SecondaryAlignmentControl, useSecondaryAlignment } from "./SecondaryAlignmentControl";

let root: Root;
let container: HTMLDivElement;
const onChange = vi.fn();
const secondary: SecondaryView = { video_id: 2, filename: "camera.mp4", duration_ms: 60000, offset_ms: 0 };
const candidate: Job = {
  id: "alignment", status: "completed",
  alignment: { outcome: "candidate", offset_ms: 8000, message: "找到一处可靠对应",
    evidence: [{ primary_text: "主视频中的一段合成长句", secondary_text: "副视频中的一段合成长句",
      primary_ms: 10000, secondary_ms: 18000, offset_ms: 8000, score: 1 }] },
};

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  onChange.mockReset();
  vi.spyOn(api, "startSecondaryAlignment").mockResolvedValue({ job_id: "alignment", status: "queued" });
  vi.spyOn(api, "getJob").mockResolvedValue(candidate);
  vi.spyOn(api, "getSecondaryView").mockResolvedValue({ ...secondary, alignment_job_id: "alignment", offset_ms: 8000 });
  vi.spyOn(api, "cancelJob").mockResolvedValue({ id: "alignment", status: "interrupted" });
  vi.spyOn(api, "changeSecondaryAlignment").mockResolvedValue({ ...secondary, offset_ms: 8000 });
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

async function mount(view = secondary, disabled = false, videoId = 1) {
  await act(async () => root.render(<AlignmentHarness
    videoId={videoId} secondary={view} disabled={disabled} onChange={onChange} />));
}
function AlignmentHarness(props: {
  videoId: number; secondary: SecondaryView; disabled: boolean; onChange: (view: SecondaryView) => void;
}) {
  const alignment = useSecondaryAlignment(props);
  return <SecondaryAlignmentControl alignment={alignment} disabled={props.disabled} />;
}
function button(label: string) {
  return [...container.querySelectorAll<HTMLButtonElement>("button")]
    .find(button => button.getAttribute("aria-label") === label || button.textContent === label)!;
}

it("starts once, shows a candidate and applies it only on explicit selection", async () => {
  await mount();
  await act(async () => { button("自动对齐").click(); button("自动对齐").click(); });
  expect(api.startSecondaryAlignment).toHaveBeenCalledOnce();
  expect(api.changeSecondaryAlignment).not.toHaveBeenCalled();
  expect(container.textContent).toContain("+8.0 秒");
  expect(container.textContent).toContain("副视频中的一段合成长句");
  await act(async () => button("应用候选").click());
  expect(api.changeSecondaryAlignment).toHaveBeenCalledWith(1, "alignment", "apply");
  expect(onChange).toHaveBeenLastCalledWith({ ...secondary, offset_ms: 8000 });
});

it("restores a running task, cancels it and stops polling after completion", async () => {
  vi.useFakeTimers();
  vi.mocked(api.getJob).mockResolvedValueOnce({ id: "alignment", status: "queued" })
    .mockResolvedValue({ id: "alignment", status: "interrupted" });
  await mount({ ...secondary, alignment_job_id: "alignment" });
  expect(container.textContent).toContain("等待 ASR");
  expect(button("自动对齐").disabled).toBe(true);
  await act(async () => button("取消自动对齐").click());
  await act(async () => vi.advanceTimersByTime(1000));
  expect(api.cancelJob).toHaveBeenCalledWith("alignment");
  expect(container.textContent).toContain("已停止");
  const count = vi.mocked(api.getJob).mock.calls.length;
  await act(async () => vi.advanceTimersByTime(5000));
  expect(api.getJob).toHaveBeenCalledTimes(count);
});

it("fetches the current association after automatic application and exposes undo", async () => {
  vi.mocked(api.getJob).mockResolvedValue({ ...candidate,
    alignment: { ...candidate.alignment!, outcome: "applied", message: "已自动对齐" } });
  await mount({ ...secondary, alignment_job_id: "alignment" });
  expect(api.getSecondaryView).toHaveBeenCalledWith(1);
  expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ offset_ms: 8000 }));
  await act(async () => button("撤销自动对齐").click());
  expect(api.changeSecondaryAlignment).toHaveBeenCalledWith(1, "alignment", "undo");
});

it("preserves settings on failure and allows a retry", async () => {
  vi.mocked(api.startSecondaryAlignment).mockRejectedValueOnce(new Error("模型尚未准备"));
  await mount();
  await act(async () => button("自动对齐").click());
  expect(container.querySelector('[role="alert"]')?.textContent).toBe("模型尚未准备");
  expect(onChange).not.toHaveBeenCalled();
  await act(async () => button("自动对齐").click());
  expect(api.startSecondaryAlignment).toHaveBeenCalledTimes(2);
});

it("does not start when manual settings have unsaved changes", async () => {
  await mount(secondary, true);
  expect(button("自动对齐").disabled).toBe(true);
  await act(async () => button("自动对齐").click());
  expect(api.startSecondaryAlignment).not.toHaveBeenCalled();
});

it("discards late results after changing videos", async () => {
  let resolve!: (job: Job) => void;
  vi.mocked(api.getJob).mockReturnValueOnce(new Promise(done => { resolve = done; }));
  await mount({ ...secondary, alignment_job_id: "old-job" });
  await mount({ ...secondary, video_id: 4 }, false, 3);
  await act(async () => resolve({ ...candidate, alignment: { ...candidate.alignment!, outcome: "applied" } }));
  expect(api.getSecondaryView).not.toHaveBeenCalled();
  expect(onChange).not.toHaveBeenCalled();
  expect(container.textContent).not.toContain("对应句");
});

it("shows fallback usage and identifies a different primary subtitle model", async () => {
  vi.mocked(api.getJob).mockResolvedValue({ ...candidate, alignment: {
    ...candidate.alignment!, phase: "fallback", model_id: "fireredasr2-aed",
    primary_model_id: "whisper-turbo", asr_seconds: 123.4,
  } });
  await mount({ ...secondary, alignment_job_id: "alignment" });
  expect(container.textContent).toContain("ASR：fireredasr2-aed");
  expect(container.textContent).toContain("124 / 900 秒");
  expect(container.textContent).toContain("已使用扩大搜索");
  expect(container.textContent).toContain("主字幕模型：whisper-turbo（与本次 ASR 不同）");
});
