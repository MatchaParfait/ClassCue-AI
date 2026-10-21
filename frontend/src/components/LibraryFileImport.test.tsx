import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { LibraryFileImport } from "./LibraryFileImport";
import { uploadVideo } from "../api/client";
import type { Video } from "../types";

vi.mock("../api/client", () => ({ uploadVideo: vi.fn() }));
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
afterEach(() => { act(() => root.unmount()); container.remove(); client.clear(); });

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
  await settle(() => expect(container.textContent).toContain("未导入视频"));
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
  expect(uploadVideo).toHaveBeenLastCalledWith(second);
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
