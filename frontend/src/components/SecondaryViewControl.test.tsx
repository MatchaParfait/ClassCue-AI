import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { SecondaryViewControl } from "./SecondaryViewControl";
import type { SecondaryView, Video } from "../types";

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
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
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

it("discards an import result after changing to another video", async () => {
  vi.stubGlobal("pywebview", { api: bridge() });
  let resolveImport!: (value: { secondary_view: SecondaryView; video: Video }) => void;
  vi.mocked(api.importSecondaryView).mockReturnValue(new Promise(resolve => { resolveImport = resolve; }));
  await mount();
  await act(async () => importButton().click());
  await mount(null, 3);
  await act(async () => resolveImport({ secondary_view: secondary, video }));
  expect(onChange).not.toHaveBeenCalled();
});

it("keeps switching an existing secondary view independent of file selection", async () => {
  vi.stubGlobal("pywebview", { api: bridge() });
  await mount(secondary);
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="切换到副视角"]')!.click());
  expect(onSwitch).toHaveBeenCalledOnce();
  expect(api.createPicker).not.toHaveBeenCalled();
});
