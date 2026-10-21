import { QueryClient, QueryClientProvider, focusManager } from "@tanstack/react-query";
import { act, type RefObject } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { ReviewPage } from "./ReviewPage";

vi.mock("../api/client", () => ({
  getVideo: vi.fn(), getSubtitles: vi.fn(), getMarkers: vi.fn(), getAnalysisRuns: vi.fn(),
  getApiBaseUrl: () => "test-api",
  updateMarkerImportance: vi.fn(), updateMarkerJumpTime: vi.fn(), updateMarkerStatus: vi.fn(),
}));
vi.mock("../components/TopBar", () => ({ TopBar: () => null }));
vi.mock("../components/VideoPanel", () => ({
  VideoPanel: ({ videoRef }: { videoRef: RefObject<HTMLVideoElement> }) => <video ref={videoRef} />,
}));
// Existing markers mean the sidebar does not mount RecallAnalysisPanel.
vi.mock("../components/ReviewSidebar", () => ({
  ReviewSidebar: ({ markers }: { markers: { id: number; title: string }[] }) =>
    <aside>{markers.map(marker => <p key={marker.id}>{marker.title}</p>)}</aside>,
}));

let container: HTMLDivElement, root: Root, client: QueryClient;
let completed: boolean;
const originalVideo = {
  id: 51, display_name: "Lesson", filename: "lesson.mp4", duration_ms: 3600000,
  video_path: "lesson.mp4", library_id: "test-library", status: "failed",
};
const marker = {
  id: 1, video_id: 51, start_ms: 10000, end_ms: 20000, title: "已保存考点",
  note: "note", source_quote: "quote", analysis_mode: "medium_recall" as const,
  review_status: "active" as const,
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.resetAllMocks();
  focusManager.setFocused(true);
  completed = false;
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  vi.mocked(api.getVideo).mockImplementation(async () => ({
    value: { ...originalVideo, status: completed ? "review_ready" : "failed" }, source: "api",
  }));
  vi.mocked(api.getMarkers).mockImplementation(async () => ({
    value: [{ ...marker, title: completed ? "已更新考点" : marker.title }], source: "api",
  }));
  vi.mocked(api.getSubtitles).mockResolvedValue({
    value: [{ id: 1, start_ms: 0, end_ms: 30000, text: "saved subtitles" }], source: "api",
  });
  vi.mocked(api.getAnalysisRuns).mockImplementation(async () => [{
    video_id: 51, analysis_mode: "medium_recall",
    status: completed ? "completed" : "failed", candidate_count: 1,
  }]);
});
afterEach(() => {
  act(() => root.unmount());
  client.clear();
  container.remove();
  focusManager.setFocused(undefined);
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function render() {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={["/review/51"]}>
      <Routes><Route path="/review/:videoId" element={<ReviewPage />} /></Routes>
    </MemoryRouter>
  </QueryClientProvider>));
  await act(async () => { await vi.advanceTimersByTimeAsync(10); });
}
async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}

it("clears the warning after background completion without reopening the page or player", async () => {
  await render();
  expect(container.textContent).toContain("字幕已保存，AI 重点尚未完成。");
  const player = container.querySelector("video")!;
  player.currentTime = 125;
  completed = true;

  await advance(3000);
  await advance(10);

  expect(container.textContent).not.toContain("字幕已保存，AI 重点尚未完成。");
  expect(container.textContent).toContain("已更新考点");
  expect(container.querySelector("video")).toBe(player);
  expect(player.currentTime).toBe(125);
  expect(api.getAnalysisRuns).toHaveBeenCalledTimes(2);
  const calls = vi.mocked(api.getVideo).mock.calls.length;
  await advance(10000);
  expect(api.getVideo).toHaveBeenCalledTimes(calls);
});

it("refreshes a fresh cached failure when returning to review without restarting the app", async () => {
  client.setQueryData(["study", "video", 51, "api"], { value: originalVideo, source: "api" });
  completed = true;
  await render();
  expect(api.getVideo).toHaveBeenCalledTimes(1);
  expect(container.textContent).not.toContain("字幕已保存，AI 重点尚未完成。");
});

it("keeps genuine failures visible, pauses background polling and refreshes on focus", async () => {
  await render();
  await advance(3000);
  expect(container.textContent).toContain("字幕已保存，AI 重点尚未完成。");
  act(() => focusManager.setFocused(false));
  const calls = vi.mocked(api.getVideo).mock.calls.length;
  completed = true;
  await advance(6000);
  expect(api.getVideo).toHaveBeenCalledTimes(calls);
  await act(async () => focusManager.setFocused(true));
  await advance(10);
  expect(container.textContent).not.toContain("字幕已保存，AI 重点尚未完成。");
});
