import { act } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { RecallAnalysisPanel } from "./RecallAnalysisPanel";
import { getAnalysisRuns, getVideoProcessingOptions } from "../api/client";

vi.mock("../api/client", () => ({
  getAnalysisRuns: vi.fn(),
  getVideoProcessingOptions: vi.fn(),
  getJob: vi.fn(),
  analyzeVideoRecall: vi.fn(),
}));

it("refreshes cached video and library status when a saved analysis completes elsewhere", async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const container = document.createElement("div");
  const root = createRoot(container);
  const videoKey = ["study", "video", 51, "api"];
  client.setQueryData(videoKey, "failed");
  const fetchVideo = vi.fn().mockResolvedValue("review_ready");
  const invalidate = vi.spyOn(client, "invalidateQueries");
  let completed = false;
  vi.mocked(getAnalysisRuns).mockImplementation(async () => [{
    video_id: 51, analysis_mode: "medium_recall",
    status: completed ? "completed" : "processing", candidate_count: 0,
    updated_at: completed ? "2026-10-20 15:17:48" : "2026-10-20 15:17:05",
  }]);
  vi.mocked(getVideoProcessingOptions).mockResolvedValue({
    active_job: null,
  } as Awaited<ReturnType<typeof getVideoProcessingOptions>>);
  function VideoStatus() {
    const video = useQuery({ queryKey: videoKey, queryFn: fetchVideo, staleTime: 60_000 });
    return <output>{video.data}</output>;
  }
  try {
    await act(async () => {
      root.render(<QueryClientProvider client={client}>
        <VideoStatus />
        <RecallAnalysisPanel videoId={51} mode="medium_recall" />
      </QueryClientProvider>);
    });
    await vi.waitFor(() => expect(container.querySelector("output")?.textContent).toBe("failed"));
    expect(fetchVideo).not.toHaveBeenCalled();
    completed = true;
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["study", "analysis-runs", 51] });
    });
    await vi.waitFor(async () => {
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
      expect(container.querySelector("output")?.textContent).toBe("review_ready");
    });
    expect(fetchVideo).toHaveBeenCalledTimes(1);
    for (const queryKey of [["videos"], ["library"], ["library-status"]]) {
      expect(invalidate).toHaveBeenCalledWith({ queryKey });
    }
    expect(container.textContent).toContain("中召回已完成识别");
  } finally {
    act(() => root.unmount());
    client.clear();
    container.remove();
  }
});
