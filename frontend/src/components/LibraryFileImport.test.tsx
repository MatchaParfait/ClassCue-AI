import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LibraryFileImport } from "./LibraryFileImport";
import { importVideoPath, uploadVideo } from "../api/client";
import type { Video } from "../types";

vi.mock("../api/client", () => ({ uploadVideo: vi.fn(), importVideoPath: vi.fn() }));
let container: HTMLDivElement, root: Root, client: QueryClient;
const saved = { id: 3, filename: "lesson.mp4", video_path: "videos/lesson.mp4" } as Video;
const video = (name = "lesson.mp4") => new File(["video"], name, { type: "video/mp4" });

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  vi.mocked(uploadVideo).mockResolvedValue({ video: saved, existed: false });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render();
});
afterEach(() => { act(() => root.unmount()); container.remove(); client.clear(); delete window.pywebview; });

function render(enabled = true) {
  act(() => root.render(<QueryClientProvider client={client}><LibraryFileImport enabled={enabled}>
    <div className="empty-library"><strong>Empty</strong><button>Import</button></div>
  </LibraryFileImport></QueryClientProvider>));
}
function drag(type: string, files: File[] = [], target = container.querySelector(".empty-library")!, types = ["Files"], relatedTarget: Node | null = null) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, relatedTarget });
  const transfer = { types, files, dropEffect: "none" };
  Object.defineProperty(event, "dataTransfer", { value: transfer });
  act(() => target.dispatchEvent(event));
  return { event, transfer };
}
async function settle(check: () => void) {
  await vi.waitFor(async () => { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); }); check(); });
}

it("highlights external files, keeps nested transitions stable, and clears on leaving or cancel", () => {
  expect(drag("dragenter").transfer.dropEffect).toBe("copy");
  expect(container.textContent).toContain("松开即可导入视频");
  drag("dragleave", [], container.querySelector("strong")!, ["Files"], container.querySelector("button")!);
  expect(container.querySelector(".library-file-drop-overlay")).not.toBeNull();
  drag("dragleave");
  expect(container.querySelector(".library-file-drop-overlay")).toBeNull();
  drag("dragover");
  act(() => window.dispatchEvent(new Event("blur")));
  expect(container.querySelector(".library-file-drop-overlay")).toBeNull();
});

it("ignores internal card and text drags", () => {
  expect(drag("dragover", [], undefined, []).event.defaultPrevented).toBe(false);
  expect(drag("drop", [], undefined, ["text/plain"]).event.defaultPrevented).toBe(false);
  expect(container.querySelector(".library-file-drop-overlay")).toBeNull();
  expect(uploadVideo).not.toHaveBeenCalled();
});

it("imports multiple files sequentially, blocks overlapping drops, and refreshes library data", async () => {
  let finish!: (result: { video: Video; existed: boolean }) => void;
  vi.mocked(uploadVideo).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  const invalidate = vi.spyOn(client, "invalidateQueries");
  drag("drop", [video("one.MP4"), video("two.mkv")]);
  expect(container.textContent).toContain("正在导入 1/2：one.MP4");
  expect(container.querySelector(".library-file-drop-overlay")).toBeNull();
  drag("drop", [video("ignored.mp4")]);
  expect(uploadVideo).toHaveBeenCalledTimes(1);
  await act(async () => finish({ video: saved, existed: false }));
  await settle(() => expect(container.textContent).toContain("已导入 2 个视频"));
  expect(uploadVideo).toHaveBeenCalledTimes(2);
  expect(invalidate.mock.calls.map(([filter]) => filter?.queryKey)).toEqual([["videos"], ["library"], ["library-status"]]);
});

it("reports unsupported, empty, oversized files and directories without making requests", async () => {
  const huge = video("huge.mp4");
  Object.defineProperty(huge, "size", { value: 1024 * 1024 * 1024 + 1 });
  drag("drop", [video("notes.txt"), new File([], "empty.mp4"), huge]);
  await settle(() => expect(container.textContent).toContain("未确认导入成功"));
  expect(container.textContent).toContain("notes.txt：不支持的格式");
  expect(container.textContent).toContain("empty.mp4：文件为空");
  expect(container.textContent).toContain("huge.mp4：文件超过 1GB");
  expect(uploadVideo).not.toHaveBeenCalled();
  drag("drop", []);
  expect(container.textContent).toContain("不支持文件夹");
});

it("preserves partial successes and retries only failed files", async () => {
  vi.mocked(uploadVideo)
    .mockResolvedValueOnce({ video: saved, existed: false })
    .mockRejectedValueOnce(new Error("磁盘空间不足"));
  const second = video("second.mp4");
  drag("drop", [video(), second]);
  await settle(() => expect(container.textContent).toContain("second.mp4：磁盘空间不足"));
  expect(container.textContent).toContain("已导入 1 个视频");
  const retry = [...container.querySelectorAll("button")].find((button) => button.textContent === "重试失败项")!;
  act(() => retry.click());
  await settle(() => expect(uploadVideo).toHaveBeenCalledTimes(3));
  expect(uploadVideo).toHaveBeenLastCalledWith(second, expect.objectContaining({ signal: expect.any(AbortSignal), onProgress: expect.any(Function) }));
  expect(container.querySelector(".library-import-issues")).toBeNull();
});

it("reports existing files, keeps storage warnings visible and allows dismissing results", async () => {
  vi.mocked(uploadVideo).mockResolvedValue({ video: { ...saved, storage_warning: "存储提醒" }, existed: true });
  drag("drop", [video()]);
  await settle(() => expect(container.textContent).toContain("1 个视频已在库中"));
  expect(container.textContent).toContain("存储提醒");
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="关闭导入结果"]')!.click());
  expect(container.querySelector(".library-import-feedback")).toBeNull();
});

it("rejects file drops while hidden or behind a modal", () => {
  drag("dragover");
  render(false);
  expect(container.querySelector(".library-file-drop-overlay")).toBeNull();
  expect(drag("dragover").transfer.dropEffect).toBe("none");
  expect(drag("drop", [video()]).event.defaultPrevented).toBe(true);
  expect(uploadVideo).not.toHaveBeenCalled();
});

it("shows byte progress separately from server validation", async () => {
  let finish!: (result: { video: Video; existed: boolean }) => void;
  vi.mocked(uploadVideo).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  drag("drop", [video()]);
  expect(container.textContent).toContain("正在连接…");
  const options = vi.mocked(uploadVideo).mock.calls[0][1]!;
  act(() => options.onProgress(2, 5));
  expect(container.textContent).toContain("上传 40%");
  act(() => options.onProgress(5, 5));
  expect(container.textContent).toContain("已上传，正在校验入库…");
  await act(async () => finish({ video: saved, existed: false }));
  expect(container.textContent).toContain("已导入 1 个视频");
  expect(container.textContent).not.toContain("停止等待");
});

it("stops waiting, retains unstarted files and allows another drop after an uncertain result", async () => {
  vi.mocked(uploadVideo).mockImplementationOnce((_file, options) => new Promise((_resolve, reject) => {
    options!.signal.addEventListener("abort", () => reject(new Error("请刷新视频库核对结果")));
  }));
  const invalidate = vi.spyOn(client, "invalidateQueries");
  drag("drop", [video("first.mp4"), video("queued.mp4")]);
  const stop = [...container.querySelectorAll("button")].find(button => button.textContent === "停止等待")!;
  await act(async () => stop.click());
  expect(uploadVideo).toHaveBeenCalledTimes(1);
  expect(container.textContent).toContain("first.mp4：请刷新视频库核对结果");
  expect(container.textContent).toContain("queued.mp4：已停止，尚未开始上传");
  expect(container.textContent).toContain("未确认导入成功");
  expect(invalidate.mock.calls.map(([filter]) => filter?.queryKey)).toEqual([["videos"], ["library"], ["library-status"]]);
  drag("drop", [video("new.mp4")]);
  await settle(() => expect(container.textContent).toContain("已导入 1 个视频"));
  expect(uploadVideo).toHaveBeenCalledTimes(2);
});

it("aborts an outstanding upload when the importer unmounts", async () => {
  vi.mocked(uploadVideo).mockImplementationOnce((_file, options) => new Promise((_resolve, reject) => {
    options!.signal.addEventListener("abort", () => reject(new Error("stopped")));
  }));
  drag("drop", [video()]);
  const signal = vi.mocked(uploadVideo).mock.calls[0][1]!.signal;
  await act(async () => root.render(null));
  expect(signal.aborted).toBe(true);
});

async function nativeImporter() {
  window.pywebview = { api: {
    describe: vi.fn(), respond: vi.fn(), retry: vi.fn(), set_hotkey: vi.fn(), set_close_behavior: vi.fn(),
    bind_video_drop: vi.fn().mockResolvedValue("drop-token"), unbind_video_drop: vi.fn().mockResolvedValue(undefined),
  } as unknown as NonNullable<typeof window.pywebview>["api"] };
  act(() => window.dispatchEvent(new Event("pywebviewready")));
  await settle(() => expect(window.pywebview!.api.bind_video_drop).toHaveBeenCalled());
}
function nativeDrop(files: { name: string; path?: string }[]) {
  act(() => window.dispatchEvent(new CustomEvent("classcue-video-drop", {
    detail: { token: "drop-token", files },
  })));
}
it("imports native drop paths in order without uploading browser copies and retains duplicate notices", async () => {
  await nativeImporter();
  vi.mocked(importVideoPath).mockResolvedValueOnce({ video: saved, existed: false })
    .mockResolvedValueOnce({ video: { ...saved, storage_warning: "重复视频已复用，来源已保留" }, existed: true });
  expect(drag("drop", [video()]).event.defaultPrevented).toBe(true);
  expect(uploadVideo).not.toHaveBeenCalled();
  nativeDrop([{ name: "first.mp4", path: "D:/synthetic/first.mp4" }, { name: "second.mp4", path: "D:/synthetic/second.mp4" }]);
  await settle(() => expect(container.textContent).toContain("1 个视频已在库中"));
  expect(vi.mocked(importVideoPath).mock.calls.map(call => call[0])).toEqual(["D:/synthetic/first.mp4", "D:/synthetic/second.mp4"]);
  expect(uploadVideo).not.toHaveBeenCalled();
  expect(container.textContent).toContain("来源已保留");
});
it("rejects missing native paths and disabled native drops without silently uploading", async () => {
  await nativeImporter();
  nativeDrop([{ name: "lesson.mp4" }]);
  expect(container.textContent).toContain("未取得本机文件路径");
  expect(importVideoPath).not.toHaveBeenCalled();
  expect(uploadVideo).not.toHaveBeenCalled();
  render(false);
  nativeDrop([{ name: "lesson.mp4", path: "D:/synthetic/lesson.mp4" }]);
  expect(importVideoPath).not.toHaveBeenCalled();
});
it("keeps native file paths when retrying a failed import", async () => {
  await nativeImporter();
  vi.mocked(importVideoPath).mockRejectedValueOnce(new Error("磁盘空间不足"))
    .mockResolvedValueOnce({ video: saved, existed: false });
  nativeDrop([{ name: "lesson.mp4", path: "D:/synthetic/lesson.mp4" }]);
  await settle(() => expect(container.textContent).toContain("磁盘空间不足"));
  act(() => [...container.querySelectorAll("button")].find(button => button.textContent === "重试失败项")!.click());
  await settle(() => expect(container.textContent).toContain("已导入 1 个视频"));
  expect(importVideoPath).toHaveBeenCalledTimes(2);
  expect(uploadVideo).not.toHaveBeenCalled();
});
