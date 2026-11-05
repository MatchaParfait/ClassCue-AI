import { act, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FolderStudyHints, StudyBookmarksDialog, type BookmarkScope } from "./StudyBookmarks";
import * as api from "../api/studyBookmarks";
import { AnimatePresence } from "./Motion";
import { waitForExit } from "../test/waitForExit";

vi.mock("../api/studyBookmarks", async original => ({
  ...await original<typeof import("../api/studyBookmarks")>(),
  getStudyBookmarks: vi.fn(), createStudyBookmark: vi.fn(), editStudyBookmark: vi.fn(), deleteStudyBookmark: vi.fn(),
}));

let root: Root;
let container: HTMLDivElement;
let queryClient: QueryClient;
const close = vi.fn();
const seek = vi.fn();
const record: api.StudyBookmark = {
  id: 1, video_id: 9, position_ms: 23_180, note: "下次再看", display_name: "Lesson", filename: "lesson.mp4",
  path: [{ id: 1, name: "Course" }, { id: 2, name: "Week" }],
};

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  vi.mocked(api.getStudyBookmarks).mockResolvedValue([record]);
  vi.mocked(api.createStudyBookmark).mockResolvedValue(record);
  vi.mocked(api.editStudyBookmark).mockResolvedValue({ ...record, note: "changed" });
  vi.mocked(api.deleteStudyBookmark).mockResolvedValue({ deleted: true });
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  queryClient.clear(); container.remove();
  vi.unstubAllGlobals();
});
async function mount(scope: BookmarkScope) {
  await act(async () => root.render(<QueryClientProvider client={queryClient}><MemoryRouter>
    <StudyBookmarksDialog libraryId="test" scope={scope} onClose={close} onSeek={seek} />
  </MemoryRouter></QueryClientProvider>));
  await vi.waitFor(() => expect(document.querySelector(".study-bookmark-open")).not.toBeNull());
}
function button(label: string) {
  return [...document.querySelectorAll<HTMLButtonElement>("button")].find(element =>
    element.textContent?.trim() === label || element.getAttribute("aria-label") === label)!;
}
async function click(label: string) { await act(async () => button(label).click()); }
async function setNote(value: string) {
  await act(async () => {
    const input = document.querySelector("textarea")!;
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

it("loads a recursive folder scope and shows the path, reminder and exact jump time", async () => {
  await mount({ title: "Course", folderId: 1 });
  expect(api.getStudyBookmarks).toHaveBeenCalledWith("test", expect.objectContaining({ folderId: 1 }));
  expect(document.querySelector(".study-bookmark-path")?.textContent).toBe("Course › Week");
  expect(document.querySelector(".study-bookmark-note")?.textContent).toBe("下次再看");
  expect(document.querySelector(".study-bookmark-time")?.textContent).toContain("0:23");
  expect(document.querySelector(".study-bookmark-open")?.getAttribute("title")).toBe("跳转到 0:23");
  expect(button("添加书签")).toBeUndefined();
});

it("creates a timestamped reminder and keeps the supplied player position", async () => {
  await mount({ title: "Lesson", videoId: 9, positionMs: 23_180, create: true });
  await setNote("这里没看懂");
  await click("保存书签");
  expect(api.createStudyBookmark).toHaveBeenCalledWith("test", 9, 23_180, "这里没看懂");
  await vi.waitFor(() => expect(document.querySelector("textarea")).toBeNull());
});

it("creates a whole-video bookmark when no timestamp is supplied", async () => {
  await mount({ title: "Lesson", videoId: 9, create: true });
  expect(document.querySelector("form label")?.textContent).toContain("整个视频");
  await click("保存书签");
  expect(api.createStudyBookmark).toHaveBeenCalledWith("test", 9, null, "");
});

it("retains the draft after a failed save and lets the user retry", async () => {
  vi.mocked(api.createStudyBookmark).mockRejectedValueOnce(new Error("保存失败"));
  await mount({ title: "Lesson", videoId: 9, positionMs: 0, create: true });
  await setNote("保留草稿");
  await click("保存书签");
  expect(document.querySelector("textarea")?.value).toBe("保留草稿");
  expect(document.querySelector('[role="alert"]')?.textContent).toBe("保存失败");
  await click("保存书签");
  expect(api.createStudyBookmark).toHaveBeenCalledTimes(2);
});

it("edits a reminder, deletes it, and can undo the deletion", async () => {
  await mount({ title: "Lesson", videoId: 9 });
  await click("编辑书签：下次再看");
  await setNote("changed");
  await click("保存书签");
  expect(api.editStudyBookmark).toHaveBeenCalledWith("test", 1, "changed");
  await click("删除书签：下次再看");
  expect(api.deleteStudyBookmark).toHaveBeenCalledWith("test", 1);
  await click("撤销");
  expect(api.createStudyBookmark).toHaveBeenCalledWith("test", 9, 23_180, "下次再看");
});

it("seeks on the primary player timeline and closes the list", async () => {
  await mount({ title: "Lesson", videoId: 9 });
  await act(async () => document.querySelector<HTMLButtonElement>(".study-bookmark-open")!.click());
  expect(seek).toHaveBeenCalledWith(23_180);
  expect(close).toHaveBeenCalledOnce();
});

it("releases focus and disables the portal while its closing animation finishes", async () => {
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })));
  function DialogHost() {
    const [open, setOpen] = useState(false);
    return <><button onClick={() => setOpen(true)}>打开书签</button>
      <AnimatePresence>{open && <StudyBookmarksDialog libraryId="test" scope={{ title: "Lesson", videoId: 9 }}
        onClose={() => setOpen(false)} />}</AnimatePresence></>;
  }
  await act(async () => root.render(<QueryClientProvider client={queryClient}><MemoryRouter>
    <DialogHost />
  </MemoryRouter></QueryClientProvider>));
  await act(async () => { button("打开书签").focus(); button("打开书签").click(); });
  await vi.waitFor(() => expect(document.querySelector(".study-bookmark-open")).not.toBeNull());
  expect(document.activeElement).toBe(button("关闭书签"));
  await act(async () => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); });
  const exiting = document.querySelector<HTMLElement>(".study-bookmarks-backdrop")!;
  expect(exiting).not.toBeNull();
  expect(exiting.inert).toBe(true);
  expect(exiting.getAttribute("aria-hidden")).toBe("true");
  expect(document.activeElement).toBe(button("打开书签"));
  expect(document.body.getAttribute("data-modal-locked")).toBeNull();
  await waitForExit(document.body, ".study-bookmarks-backdrop");
});

it("shows both the nested continue link and bookmark count on a folder", () => {
  const resume = vi.fn(), bookmarks = vi.fn();
  const latest: api.StudyVideo = {
    video_id: 9, display_name: "Lesson", filename: "lesson.mp4", folder_id: 2, position_ms: 23_180,
    duration_ms: 60_000, observed_at: 100, bookmark_count: 3, bookmark_note: null, completed: false, path: record.path,
  };
  act(() => root.render(<FolderStudyHints summary={{ folder_id: 1, bookmark_count: 3, latest }}
    onResume={resume} onBookmarks={bookmarks} />));
  expect(container.textContent).toContain("上次Lesson0:23");
  act(() => container.querySelector<HTMLButtonElement>(".study-resume")!.click());
  expect(resume).toHaveBeenCalledWith(latest);
  act(() => container.querySelector<HTMLButtonElement>(".study-bookmark-count")!.click());
  expect(bookmarks).toHaveBeenCalledOnce();
});
