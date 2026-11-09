import { act, useState, type ComponentProps, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { SecondaryViewControl } from "./SecondaryViewControl";
import type { SecondaryView, Video } from "../types";
import { saveAutoAlignSecondary } from "../secondaryViewPreferences";

vi.mock("./Motion", () => ({
  AnimatePresence: ({ children }: { children: ReactNode }) => children,
  AnimatedPopup: ({ children, ...props }: ComponentProps<"div">) => <div {...props}>{children}</div>,
}));

let container: HTMLDivElement;
let root: Root;
const onChange = vi.fn();
const onSwitch = vi.fn();
const secondary: SecondaryView = {
  video_id: 2, filename: "camera.mp4", duration_ms: 1000, offset_ms: 0,
};
const video = { id: 2, filename: "camera.mp4", duration_ms: 1000 } as Video;
const bridge = () => ({
  describe: vi.fn(), respond: vi.fn(), retry: vi.fn(), set_hotkey: vi.fn(), set_close_behavior: vi.fn(),
});

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => storage.set(key, value),
  });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  onChange.mockClear();
  onSwitch.mockClear();
  vi.spyOn(api, "getSecondaryView").mockResolvedValue(null);
  vi.spyOn(api, "createPicker").mockResolvedValue("camera-picker");
  vi.spyOn(api, "pollPicker").mockResolvedValue("D:/synthetic/camera.mp4");
  vi.spyOn(api, "importSecondaryView").mockResolvedValue({ secondary_view: secondary, video });
  vi.spyOn(api, "uploadSecondaryView");
  vi.spyOn(api, "startSecondaryAlignment").mockResolvedValue({ job_id: "alignment", status: "queued" });
  vi.spyOn(api, "getJob").mockResolvedValue({ id: "alignment", status: "interrupted" });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function mount(view: SecondaryView | null = null, videoId = 1) {
  await act(async () => root.render(
    <SecondaryViewControl videoId={videoId} demo={false} secondary={view} active={false} switching={false}
      onChange={onChange} onSwitch={onSwitch} />,
  ));
  onChange.mockClear();
}

function importButton() {
  return container.querySelector<HTMLButtonElement>('[aria-label="导入副视角"]')!;
}

it("uses the desktop picker and sends only the local path", async () => {
  vi.stubGlobal("pywebview", { api: bridge() });
  await mount();
  const pickerClick = vi.spyOn(container.querySelector<HTMLInputElement>('input[type="file"]')!, "click");
  await act(async () => importButton().click());
  expect(api.createPicker).toHaveBeenCalledWith("file");
  expect(api.pollPicker).toHaveBeenCalledWith("camera-picker", 15 * 60_000);
  expect(api.importSecondaryView).toHaveBeenCalledWith(1, "D:/synthetic/camera.mp4");
  expect(onChange).toHaveBeenCalledWith(secondary);
  expect(pickerClick).not.toHaveBeenCalled();
  expect(api.uploadSecondaryView).not.toHaveBeenCalled();
  expect(api.startSecondaryAlignment).not.toHaveBeenCalled();
});

it("handles a desktop bridge that becomes ready after mounting", async () => {
  await mount();
  vi.stubGlobal("pywebview", { api: bridge() });
  act(() => window.dispatchEvent(new Event("pywebviewready")));
  await act(async () => importButton().click());
  expect(api.importSecondaryView).toHaveBeenCalledOnce();
});

it("does not import after picker cancellation and allows another attempt", async () => {
  vi.stubGlobal("pywebview", { api: bridge() });
  vi.mocked(api.pollPicker).mockResolvedValueOnce(null);
  await mount();
  await act(async () => importButton().click());
  expect(api.importSecondaryView).not.toHaveBeenCalled();
  expect(importButton().disabled).toBe(false);
  expect(container.querySelector('[role="alert"]')).toBeNull();
  await act(async () => importButton().click());
  expect(api.importSecondaryView).toHaveBeenCalledOnce();
});

it("deduplicates clicks while choosing and importing a file", async () => {
  vi.stubGlobal("pywebview", { api: bridge() });
  let resolvePicker!: (path: string) => void;
  vi.mocked(api.pollPicker).mockReturnValue(new Promise(resolve => { resolvePicker = resolve; }));
  await mount();
  await act(async () => {
    importButton().click();
    importButton().click();
  });
  expect(api.createPicker).toHaveBeenCalledOnce();
  expect(importButton().disabled).toBe(true);
  let resolveImport!: (value: { secondary_view: SecondaryView; video: Video }) => void;
  vi.mocked(api.importSecondaryView).mockReturnValue(new Promise(resolve => { resolveImport = resolve; }));
  await act(async () => resolvePicker("D:/synthetic/camera.mp4"));
  expect(importButton().disabled).toBe(true);
  await act(async () => resolveImport({ secondary_view: secondary, video }));
  expect(importButton().disabled).toBe(false);
  expect(api.importSecondaryView).toHaveBeenCalledOnce();
});

it.each(["picker", "import"])("shows a %s failure and permits retry", async phase => {
  vi.stubGlobal("pywebview", { api: bridge() });
  const operation = phase === "picker" ? vi.mocked(api.createPicker) : vi.mocked(api.importSecondaryView);
  operation.mockRejectedValueOnce(new Error("synthetic failure"));
  await mount();
  await act(async () => importButton().click());
  expect(container.querySelector('[role="alert"]')?.textContent).toBe("synthetic failure");
  expect(importButton().disabled).toBe(false);
  await act(async () => importButton().click());
  expect(onChange).toHaveBeenCalledWith(secondary);
});

it("does not start an import after leaving the player during file selection", async () => {
  vi.stubGlobal("pywebview", { api: bridge() });
  let resolvePicker!: (path: string) => void;
  vi.mocked(api.pollPicker).mockReturnValue(new Promise(resolve => { resolvePicker = resolve; }));
  await mount();
  await act(async () => importButton().click());
  act(() => root.render(null));
  await act(async () => resolvePicker("D:/synthetic/camera.mp4"));
  expect(api.importSecondaryView).not.toHaveBeenCalled();
  expect(onChange).not.toHaveBeenCalled();
});

it("discards a picker selection after changing to another video", async () => {
  vi.stubGlobal("pywebview", { api: bridge() });
  let resolvePicker!: (path: string) => void;
  vi.mocked(api.pollPicker).mockReturnValue(new Promise(resolve => { resolvePicker = resolve; }));
  await mount();
  await act(async () => importButton().click());
  await mount(null, 3);
  await act(async () => resolvePicker("D:/synthetic/camera.mp4"));
  expect(api.importSecondaryView).not.toHaveBeenCalled();
  expect(onChange).not.toHaveBeenCalled();
});

it("aligns the imported video after changing videos without updating the new player", async () => {
  saveAutoAlignSecondary(true);
  vi.stubGlobal("pywebview", { api: bridge() });
  let resolveImport!: (value: { secondary_view: SecondaryView; video: Video }) => void;
  vi.mocked(api.importSecondaryView).mockReturnValue(new Promise(resolve => { resolveImport = resolve; }));
  await mount();
  await act(async () => importButton().click());
  await mount(null, 3);
  await act(async () => resolveImport({ secondary_view: secondary, video }));
  expect(onChange).not.toHaveBeenCalled();
  expect(api.startSecondaryAlignment).toHaveBeenCalledOnce();
  expect(api.startSecondaryAlignment).toHaveBeenCalledWith(1, secondary);
});

it.each(["desktop", "browser"] as const)("aligns a successful %s import after leaving the player without updating it", async entry => {
  saveAutoAlignSecondary(true);
  let resolveImport!: (value: { secondary_view: SecondaryView; video: Video }) => void;
  const pending = new Promise<{ secondary_view: SecondaryView; video: Video }>(resolve => { resolveImport = resolve; });
  if (entry === "desktop") {
    vi.stubGlobal("pywebview", { api: bridge() });
    vi.mocked(api.importSecondaryView).mockReturnValue(pending);
  } else {
    vi.mocked(api.uploadSecondaryView).mockReturnValue(pending);
  }
  await mount();
  await act(async () => {
    if (entry === "desktop") importButton().click();
    else {
      const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
      Object.defineProperty(input, "files", { value: [new File(["synthetic camera"], "camera.mp4")] });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
  act(() => root.render(null));
  await act(async () => resolveImport({ secondary_view: secondary, video }));
  expect(api.startSecondaryAlignment).toHaveBeenCalledOnce();
  expect(api.startSecondaryAlignment).toHaveBeenCalledWith(1, secondary);
  expect(onChange).not.toHaveBeenCalled();
});

it("keeps switching an existing secondary view independent of file selection", async () => {
  vi.stubGlobal("pywebview", { api: bridge() });
  await mount(secondary);
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="切换到副视角"]')!.click());
  expect(onSwitch).toHaveBeenCalledOnce();
  expect(api.createPicker).not.toHaveBeenCalled();
});

it("saves the main-audio option without changing the saved offset", async () => {
  const view = { ...secondary, offset_ms: 5000 };
  vi.spyOn(api, "setSecondaryViewAudio").mockResolvedValue({ ...view, use_primary_audio: true });
  await mount(view);
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="副视角设置"]')!.click());
  await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
  expect(api.setSecondaryViewAudio).toHaveBeenCalledWith(1, true);
  expect(onChange).toHaveBeenCalledWith({ ...view, use_primary_audio: true });
});

function AlignmentPlayer({ initialView = secondary }: { initialView?: SecondaryView | null }) {
  const [view, setView] = useState<SecondaryView | null>(initialView);
  return <SecondaryViewControl videoId={1} demo={false} secondary={view} active={false} switching={false}
    onChange={next => { onChange(next); setView(next); }} onSwitch={onSwitch} />;
}

it.each(["desktop", "browser"] as const)("automatically aligns a successful %s import when enabled", async entry => {
  saveAutoAlignSecondary(true);
  if (entry === "desktop") vi.stubGlobal("pywebview", { api: bridge() });
  else vi.mocked(api.uploadSecondaryView).mockResolvedValue({ secondary_view: secondary, video });
  await mount();
  await act(async () => {
    if (entry === "desktop") {
      importButton().click();
      importButton().click();
    } else {
      const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
      Object.defineProperty(input, "files", { value: [new File(["synthetic camera"], "camera.mp4")] });
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
  });
  expect(api.startSecondaryAlignment).toHaveBeenCalledOnce();
  expect(api.startSecondaryAlignment).toHaveBeenCalledWith(1, secondary);
  expect(onChange).toHaveBeenLastCalledWith({ ...secondary, alignment_job_id: "alignment" });
});

it("keeps browser imports manual when automatic alignment is disabled", async () => {
  vi.mocked(api.uploadSecondaryView).mockResolvedValue({ secondary_view: secondary, video });
  await mount();
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
  Object.defineProperty(input, "files", { value: [new File(["synthetic camera"], "camera.mp4")] });
  await act(async () => input.dispatchEvent(new Event("change", { bubbles: true })));
  expect(onChange).toHaveBeenCalledWith(secondary);
  expect(api.startSecondaryAlignment).not.toHaveBeenCalled();
});

it("does not align existing views on reopening or after changing the preference", async () => {
  saveAutoAlignSecondary(true);
  await mount(secondary);
  act(() => { saveAutoAlignSecondary(false); saveAutoAlignSecondary(true); });
  expect(api.startSecondaryAlignment).not.toHaveBeenCalled();
});

it("preserves an imported view and its storage warning when automatic alignment fails, allowing manual retry", async () => {
  saveAutoAlignSecondary(true);
  vi.stubGlobal("pywebview", { api: bridge() });
  vi.mocked(api.importSecondaryView).mockResolvedValue({
    secondary_view: secondary, video: { ...video, storage_warning: "来源文件已保留" },
  });
  vi.mocked(api.startSecondaryAlignment).mockRejectedValueOnce(new Error("主字幕缺少可对齐的长句"));
  await act(async () => root.render(<AlignmentPlayer initialView={null} />));
  await act(async () => importButton().click());
  expect(onChange).toHaveBeenLastCalledWith(secondary);
  expect(container.querySelector('[role="alert"]')?.textContent)
    .toBe("来源文件已保留；副视角已导入，自动对齐未启动：主字幕缺少可对齐的长句");
  expect(container.querySelector('[aria-label="切换到副视角"]')).not.toBeNull();
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="自动对齐"]')!.click());
  expect(api.startSecondaryAlignment).toHaveBeenCalledTimes(2);
  expect(onChange).toHaveBeenLastCalledWith({ ...secondary, alignment_job_id: "alignment" });
  expect(container.querySelector('[role="alert"]')).toBeNull();
});

it("polls an automatically started task and applies its result to the player", async () => {
  saveAutoAlignSecondary(true);
  vi.stubGlobal("pywebview", { api: bridge() });
  vi.mocked(api.getJob).mockResolvedValue({ id: "alignment", status: "completed",
    alignment: { outcome: "applied", offset_ms: 8000, message: "已自动对齐", evidence: [] } });
  const aligned = { ...secondary, alignment_job_id: "alignment", offset_ms: 8000 };
  vi.mocked(api.getSecondaryView).mockResolvedValueOnce(null).mockResolvedValue(aligned);
  await act(async () => root.render(<AlignmentPlayer initialView={null} />));
  await act(async () => importButton().click());
  expect(api.startSecondaryAlignment).toHaveBeenCalledOnce();
  expect(api.getJob).toHaveBeenCalledWith("alignment");
  expect(onChange).toHaveBeenLastCalledWith(aligned);
  expect(container.querySelector('[aria-label="撤销自动对齐"]')).not.toBeNull();
});

it("does not align a cancelled selection or a failed import", async () => {
  saveAutoAlignSecondary(true);
  vi.stubGlobal("pywebview", { api: bridge() });
  vi.mocked(api.pollPicker).mockResolvedValueOnce(null);
  vi.mocked(api.importSecondaryView).mockRejectedValueOnce(new Error("导入失败"));
  await mount();
  await act(async () => importButton().click());
  await act(async () => importButton().click());
  expect(api.startSecondaryAlignment).not.toHaveBeenCalled();
  expect(onChange).not.toHaveBeenCalled();
});

it("discards a late automatic-start response after changing videos", async () => {
  saveAutoAlignSecondary(true);
  vi.stubGlobal("pywebview", { api: bridge() });
  let resolveStart!: (value: { job_id: string; status: string }) => void;
  vi.mocked(api.startSecondaryAlignment).mockReturnValue(new Promise(resolve => { resolveStart = resolve; }));
  await mount();
  await act(async () => importButton().click());
  await mount(null, 3);
  await act(async () => resolveStart({ job_id: "alignment", status: "queued" }));
  expect(onChange).not.toHaveBeenCalled();
});

it("respects disabling automatic alignment while a file is still importing", async () => {
  saveAutoAlignSecondary(true);
  vi.stubGlobal("pywebview", { api: bridge() });
  let resolveImport!: (value: { secondary_view: SecondaryView; video: Video }) => void;
  vi.mocked(api.importSecondaryView).mockReturnValue(new Promise(resolve => { resolveImport = resolve; }));
  await mount();
  await act(async () => importButton().click());
  saveAutoAlignSecondary(false);
  await act(async () => resolveImport({ secondary_view: secondary, video }));
  expect(onChange).toHaveBeenCalledWith(secondary);
  expect(api.startSecondaryAlignment).not.toHaveBeenCalled();
});

it("updates the player when alignment finishes with settings closed", async () => {
  vi.useFakeTimers();
  vi.mocked(api.getSecondaryView).mockResolvedValue(secondary);
  vi.spyOn(api, "startSecondaryAlignment").mockResolvedValue({ job_id: "alignment", status: "queued" });
  vi.spyOn(api, "getJob").mockResolvedValue({ id: "alignment", status: "running" });
  await act(async () => root.render(<AlignmentPlayer />));
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="副视角设置"]')!.click());
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="自动对齐"]')!.click());
  act(() => document.body.dispatchEvent(new Event("pointerdown", { bubbles: true })));
  await act(async () => vi.advanceTimersByTime(500));
  expect(container.querySelector('[aria-label="自动对齐"]')).toBeNull();

  const aligned = { ...secondary, offset_ms: 8000, alignment_job_id: "alignment" };
  vi.mocked(api.getSecondaryView).mockResolvedValue(aligned);
  vi.mocked(api.getJob).mockResolvedValue({ id: "alignment", status: "completed",
    alignment: { outcome: "applied", offset_ms: 8000, message: "已自动对齐", evidence: [] } });
  onChange.mockClear();
  await act(async () => vi.advanceTimersByTime(1000));
  expect(onChange).toHaveBeenLastCalledWith(aligned);
  const count = vi.mocked(api.getJob).mock.calls.length;
  await act(async () => vi.advanceTimersByTime(3000));
  expect(api.getJob).toHaveBeenCalledTimes(count);
});

it("keeps the running task and cancellation available after changing the audio option", async () => {
  vi.useFakeTimers();
  vi.mocked(api.getSecondaryView).mockResolvedValue(secondary);
  vi.spyOn(api, "startSecondaryAlignment").mockResolvedValue({ job_id: "alignment", status: "queued" });
  vi.spyOn(api, "getJob").mockResolvedValue({ id: "alignment", status: "running" });
  vi.spyOn(api, "setSecondaryViewAudio").mockResolvedValue({
    ...secondary, use_primary_audio: true, alignment_job_id: "alignment",
  });
  vi.spyOn(api, "cancelJob").mockResolvedValue({ id: "alignment", status: "interrupted" });
  await act(async () => root.render(<AlignmentPlayer />));
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="副视角设置"]')!.click());
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="自动对齐"]')!.click());
  await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')!.click());
  expect(container.querySelector<HTMLButtonElement>('[aria-label="自动对齐"]')!.disabled).toBe(true);
  await act(async () => container.querySelector<HTMLButtonElement>('[aria-label="取消自动对齐"]')!.click());
  expect(api.cancelJob).toHaveBeenCalledWith("alignment");
});
