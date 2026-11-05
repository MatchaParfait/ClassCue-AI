import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "./App";
import { getLibrary, type LibraryFolder, type LibraryResponse } from "./api/client";
import { getStudyBookmarks, getStudyNavigation, type StudyNavigation, type StudyVideo } from "./api/studyBookmarks";
import type { Video } from "./types";

const folders: LibraryFolder[] = [
  { id: 1, name: "Course", parent_id: null, video_count: 1 },
  { id: 2, name: "Week", parent_id: 1, video_count: 1 },
  { id: 3, name: "Unit", parent_id: 2, video_count: 1 },
];
const video: Video = {
  id: 9, display_name: "Lesson", filename: "lesson.mp4", video_path: "lesson.mp4", duration_ms: 60_000,
  folder_id: 3, status: "review_ready", has_transcript: true, library_id: "synthetic",
};
const recent: StudyVideo = {
  video_id: 9, display_name: "Lesson", filename: "lesson.mp4", folder_id: 3, position_ms: 23_180,
  duration_ms: 60_000, observed_at: 100, bookmark_count: 1, bookmark_note: "下次再看", completed: false,
  path: folders.map(folder => ({ id: folder.id, name: folder.name })),
};
const navigation: StudyNavigation = {
  library_id: "synthetic", videos: [recent], latest: recent,
  folders: folders.map(folder => ({ folder_id: folder.id, bookmark_count: 1, latest: recent })),
};

vi.mock("./api/client", async original => ({
  ...await original<typeof import("./api/client")>(),
  getLibrary: vi.fn(),
  getVideos: vi.fn(async () => [video]),
  getLibraryStatus: vi.fn(async () => ({ library_id: "synthetic", data_root: "synthetic", video_count: 1 })),
  getLatestBatch: vi.fn(async () => null), getPausedJobs: vi.fn(async () => []), getRecentJobs: vi.fn(async () => []),
  getLatestLibraryUndo: vi.fn(async () => null), getLatestLibraryRedo: vi.fn(async () => null),
}));
vi.mock("./api/studyBookmarks", async original => ({
  ...await original<typeof import("./api/studyBookmarks")>(),
  getStudyNavigation: vi.fn(), getStudyBookmarks: vi.fn(),
}));

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  vi.stubGlobal("matchMedia", vi.fn(() => ({ matches: true })));
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => values.set(key, value) });
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  window.sessionStorage.clear();
  window.history.replaceState({}, "", "/");
  vi.mocked(getLibrary).mockImplementation(async folderId => {
    const parent = folderId ?? null;
    const response: LibraryResponse = {
      folder: folders.find(folder => folder.id === parent) ?? null, parent_id: parent, sort: "manual", order: "asc",
      ancestors: parent === null ? [] : folders.slice(0, parent),
      items: [
        ...folders.filter(folder => folder.parent_id === parent).map(folder => ({ type: "folder" as const, folder })),
        ...(parent === 3 ? [{ type: "video" as const, video }] : []),
      ],
    };
    return response;
  });
  vi.mocked(getStudyNavigation).mockResolvedValue(navigation);
  vi.mocked(getStudyBookmarks).mockResolvedValue([{
    id: 1, video_id: 9, position_ms: 23_180, note: "下次再看", display_name: "Lesson", filename: "lesson.mp4", path: recent.path,
  }]);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<App />));
  await vi.waitFor(async () => {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.querySelector('[data-library-key="folder-1"] .folder-study-hints')).not.toBeNull();
  });
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks(); vi.unstubAllGlobals();
});

async function openFolder(id: number) {
  await act(async () => container.querySelector<HTMLButtonElement>(`[data-library-key="folder-${id}"] .folder-card-link`)!.click());
  await vi.waitFor(async () => {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
    expect(container.querySelector("#folder-window-title")?.textContent).toBe(folders[id - 1].name);
  });
}

it("shows recursive study guidance before any descendants have been opened, then guides through every folder to the video", async () => {
  expect(container.querySelector('[data-library-key="folder-1"]')?.textContent).toContain("书签 1");
  expect(getLibrary).not.toHaveBeenCalledWith(3);
  await openFolder(1);
  await vi.waitFor(() => expect(container.querySelector('[data-library-key="folder-2"] .folder-study-hints')?.textContent).toContain("Lesson"));
  await openFolder(2);
  await vi.waitFor(() => expect(container.querySelector('[data-library-key="folder-3"] .folder-study-hints')?.textContent).toContain("0:23"));
  await openFolder(3);
  await vi.waitFor(() => expect(container.querySelector(".video-study-hints")?.textContent).toContain("下次再看"));
  expect(container.querySelector('.video-watch-progress')?.getAttribute("aria-valuetext")).toBe("上次看到 0:23 / 1:00");
});

it("opens a folder bookmark list while keeping its scope and nested path", async () => {
  await openFolder(1);
  await vi.waitFor(() => expect(container.querySelector('[data-library-key="folder-2"] .study-bookmark-count')).not.toBeNull());
  await act(async () => container.querySelector<HTMLButtonElement>('[data-library-key="folder-2"] .study-bookmark-count')!.click());
  await vi.waitFor(() => expect(document.querySelector(".study-bookmark-note")?.textContent).toBe("下次再看"));
  expect(getStudyBookmarks).toHaveBeenCalledWith("synthetic", expect.objectContaining({ folderId: 2 }));
  expect(document.querySelector(".study-bookmark-path")?.textContent).toBe("Course › Week › Unit");
  await act(async () => document.querySelector<HTMLButtonElement>(".study-bookmark-open")!.click());
  expect(window.location.pathname).toBe("/review/9");
  expect(new URLSearchParams(window.location.search).get("start_ms")).toBe("23180");
  expect(new URLSearchParams(window.location.search).get("library_id")).toBe("synthetic");
});

it("continues directly from the top-level folder using the exact saved timestamp", async () => {
  await act(async () => container.querySelector<HTMLButtonElement>('[data-library-key="folder-1"] .study-resume')!.click());
  expect(window.location.pathname).toBe("/review/9");
  expect(new URLSearchParams(window.location.search).get("start_ms")).toBe("23180");
});
