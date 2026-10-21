import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Marker } from "../types";
import { MarkerTimeEditor } from "./MarkerTimeEditor";

const marker: Marker = {
  id: 1, video_id: 1, start_ms: 521_000, end_ms: 530_000, title: "Topic",
  note: "", source_quote: "", analysis_mode: "medium_recall", review_status: "active",
};
let container: HTMLDivElement;
let root: Root;
let props: React.ComponentProps<typeof MarkerTimeEditor>;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  props = {
    marker, durationMs: 600_000, currentTimeMs: 450_900, getCurrentTimeMs: vi.fn(() => 450_900),
    onSave: vi.fn().mockResolvedValue(undefined), onClose: vi.fn(),
  };
});

afterEach(() => { act(() => root.unmount()); container.remove(); });

function mount() { act(() => root.render(<MarkerTimeEditor {...props} />)); }
function input() { return container.querySelector<HTMLInputElement>("input")!; }
function click(label: string) {
  const button = [...container.querySelectorAll("button")].find((item) => item.getAttribute("aria-label") === label || item.textContent?.trim() === label)!;
  expect(button).toBeTruthy();
  act(() => button.click());
}
describe("marker jump time editor", () => {
  it("keeps a disabled preview with no manual adjustment or standalone save button", () => {
    mount();
    expect(input().disabled).toBe(true);
    expect(input().readOnly).toBe(true);
    expect(container.querySelector('button[type="submit"]')).toBeNull();
    expect(container.querySelector('[aria-label="前移 5 秒"]')).toBeNull();
    expect(container.querySelector('[aria-label="后移 5 秒"]')).toBeNull();
    expect([...container.querySelectorAll("button")].some((item) => item.textContent?.trim() === "保存")).toBe(false);
    expect(container.textContent).toContain("保存当前播放位置");
    expect(container.querySelector(".marker-editor-footer")?.textContent).toBe("取消");
  });

  it("saves the current player position immediately without a second confirmation", async () => {
    mount();
    expect(input().value).toBe("7:30");
    expect(container.querySelector('[aria-label="试听"]')).toBeNull();
    await act(async () => click("保存当前播放位置"));
    expect(input().value).toBe("7:30");
    expect(props.onSave).toHaveBeenCalledWith(450_000);
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("retries using the latest playback position after a failed save", async () => {
    props.onSave = vi.fn().mockRejectedValueOnce(new Error("保存失败")).mockResolvedValueOnce(undefined);
    mount();
    await act(async () => click("保存当前播放位置"));
    expect(input().value).toBe("7:30");
    expect(container.querySelector('[role="alert"]')?.textContent).toBe("保存失败");
    expect(props.onClose).not.toHaveBeenCalled();
    props.getCurrentTimeMs = () => 500_000;
    props.currentTimeMs = 500_000;
    act(() => root.render(<MarkerTimeEditor {...props} />));
    expect(input().value).toBe("8:20");
    await act(async () => click("保存当前播放位置"));
    expect(props.onSave).toHaveBeenNthCalledWith(2, 500_000);
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("reports an unreadable playback position without saving", async () => {
    props.getCurrentTimeMs = () => NaN;
    mount();
    await act(async () => click("保存当前播放位置"));
    expect(container.querySelector('[role="alert"]')?.textContent).toContain("无法读取当前播放位置");
    expect(props.onSave).not.toHaveBeenCalled();
  });

  it("saves the current position at zero immediately", async () => {
    props.getCurrentTimeMs = () => 0;
    mount();
    await act(async () => click("保存当前播放位置"));
    expect(props.onSave).toHaveBeenCalledWith(0);
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("updates the readonly preview when playback moves without saving", () => {
    mount();
    props.currentTimeMs = 125_000;
    mount();
    expect(input().value).toBe("2:05");
    expect(props.onSave).not.toHaveBeenCalled();
  });

  it.each([[-500, 0, "0:00"], [605_000, 600_000, "10:00"]])("clamps preview and capture at the video bounds (%s)", async (position, saved, preview) => {
    props.currentTimeMs = position as number;
    props.getCurrentTimeMs = () => position as number;
    mount();
    expect(input().value).toBe(preview);
    await act(async () => click("保存当前播放位置"));
    expect(props.onSave).toHaveBeenCalledWith(saved);
  });

  it("reads the live player on click instead of the last rendered preview", async () => {
    mount();
    props.getCurrentTimeMs = vi.fn(() => 455_300);
    mount();
    expect(input().value).toBe("7:30");
    await act(async () => click("保存当前播放位置"));
    expect(props.onSave).toHaveBeenCalledWith(455_000);
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("restores the original by explicitly saving null", async () => {
    props.marker = { ...marker, user_start_ms: 450_000 };
    mount();
    expect(input().value).toBe("7:30");
    expect(container.textContent).toContain("原始定位 8:41");
    await act(async () => click("恢复原始时间"));
    expect(props.onSave).toHaveBeenCalledWith(null);
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("disables all editor controls while saving", async () => {
    let finish!: () => void;
    props.onSave = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
    mount();
    await act(async () => click("保存当前播放位置"));
    expect(container.querySelector("fieldset")?.disabled).toBe(true);
    expect(container.textContent).toContain("保存中");
    await act(async () => finish());
    expect(props.onClose).toHaveBeenCalledOnce();
  });

  it("cancels on Escape without writing", () => {
    mount();
    act(() => container.querySelector("button")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    expect(props.onClose).toHaveBeenCalledOnce();
    expect(props.onSave).not.toHaveBeenCalled();
  });
});
