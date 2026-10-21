import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation, useNavigationType } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { ProcessPage } from "./ProcessPage";
import { RECALL_SELECTION_KEY } from "../hooks/useRecallSelection";

vi.mock("../api/client", () => ({
  getThumbnailUrl: (video: { id: number }) => `/api/videos/${video.id}/thumbnail`,
  getStreamUrl: (videoId: number) => `/api/videos/${videoId}/stream`,
  getJob: vi.fn(), cancelJob: vi.fn(), getPicker: vi.fn(), getVideo: vi.fn(),
  getVideoProcessingOptions: vi.fn(), getLibraryStatus: vi.fn(),
  getAiSettings: vi.fn(),
  getRecallSchemes: vi.fn(),
  openPicker: vi.fn(), processVideo: vi.fn(), registerVideo: vi.fn(), retryVideo: vi.fn(),
  resumeJob: vi.fn(),
  renameVideoFile: vi.fn(),
  getAsrResourceProfile: vi.fn(),
  getAsrSelection: vi.fn(),
  getAsrModelStatus: vi.fn(),
}));

const video = {
  id: 1, display_name: "Lesson", filename: "lesson.mp4", duration_ms: 10000,
  video_path: "D:\\lesson.mp4", status: "failed",
};
let container: HTMLDivElement;
let root: Root;
let client: QueryClient;

beforeEach(() => {
  window.localStorage.removeItem(`${RECALL_SELECTION_KEY}:default`);
  vi.resetAllMocks();
  vi.mocked(api.getRecallSchemes).mockResolvedValue([]);
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(api.getVideo).mockResolvedValue({ value: video, source: "api" });
  vi.mocked(api.getLibraryStatus).mockResolvedValue({
    data_root: "D:\\course", database_path: "D:\\course\\project.db",
    temp_audio_dir: "D:\\course\\audio", video_count: 1,
  });
  vi.mocked(api.getVideoProcessingOptions).mockResolvedValue({
    asr_model: {
      id: "fireredasr2-aed", name: "FireRedASR2-AED", engine: "firered",
      downloaded: true, cache_size_bytes: 1000, selected: true,
    },
    ai_service: { model: "configured-model", configured: true },
    active_job: null,
  });
  vi.mocked(api.getAiSettings).mockResolvedValue({
    provider: "openai_compatible", base_url: "https://relay.test/v1", model: "configured-model",
    output_tokens: 16384, token_parameter: "max_tokens", json_mode: true,
    timeout_seconds: 120, max_retries: 5, reasoning_effort: "high", has_api_key: true,
  });
  vi.mocked(api.getAsrSelection).mockResolvedValue({
    id: "fireredasr2-aed", name: "FireRedASR2-AED", engine: "firered",
    downloaded: true, cache_size_bytes: 1000, selected: true,
  });
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => ({
    id, name: id, engine: "firered", downloaded: true, cache_size_bytes: 1000, selected: true,
  }));
  vi.mocked(api.retryVideo).mockResolvedValue({ id: "job", status: "queued" });
  vi.mocked(api.getAsrResourceProfile).mockResolvedValue({
    engine: "firered", device: "cuda:0", gpu_total_mib: 8188,
    cuda_available: true, initial_batch_size: 6,
  });
  vi.mocked(api.getJob).mockResolvedValue({
    id: "job", status: "running", stage: "transcribing", progress: 0.125,
  });
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
    client.clear();
    await new Promise((resolve) => setTimeout(resolve, 15));
  });
  container.remove();
  vi.restoreAllMocks();
});

it("checks only the current ASR model and opens its editing section", async () => {
  act(() => root.render(
    <QueryClientProvider client={client}><MemoryRouter><ProcessPage /></MemoryRouter></QueryClientProvider>,
  ));
  await settle(() => expect(api.getAsrModelStatus).toHaveBeenCalledWith("fireredasr2-aed", { full: false }));
  expect(container.querySelector('a[href="/settings?section=asr"]')).not.toBeNull();
  expect(api.getAsrModelStatus).toHaveBeenCalledTimes(1);
});

async function settle(check: () => void) {
  let failure: unknown;
  for (let attempt = 0; attempt < 100; attempt++) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
    try {
      check();
      return;
    } catch (error) {
      failure = error;
    }
  }
  throw failure;
}

async function mount(path = "/process?videoId=1") {
  act(() => root.render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ProcessPage /><RouteProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  ));
  await settle(() => expect(path.includes("videoId")
    ? container.querySelector(".video-info-selected")
    : container.querySelector(".video-file-picker button")).toBeTruthy());
  if (path.includes("videoId")) {
    await settle(() => expect(container.textContent).toContain("FireRedASR2-AED"));
  }
}

function RouteProbe() {
  const location = useLocation();
  const type = useNavigationType();
  return <output data-testid="route" data-path={location.pathname + location.search} data-navigation={type}>{JSON.stringify(location.state)}</output>;
}

it("opens the completed video immediately without awaiting metadata, preserving library and requesting playback", async () => {
  vi.mocked(api.getVideo).mockResolvedValue({ value: { ...video, library_id: "library A" }, source: "api" });
  await mount(); start();
  await settle(() => expect(api.getJob).toHaveBeenCalled());
  vi.mocked(api.getVideo).mockImplementation(() => new Promise(() => {}));
  act(() => client.setQueryData(["jobs", "job"], { id: "job", video_id: 1, status: "completed" }));
  await settle(() => expect(container.querySelector('[data-testid="route"]')?.getAttribute('data-path')).toBe('/review/1?library_id=library%20A'));
  expect(container.querySelector('[data-testid="route"]')?.getAttribute('data-navigation')).toBe('REPLACE');
  expect(container.querySelector('[data-testid="route"]')?.textContent).toContain('"completedVideoId":1');
  expect(container.querySelector('[data-testid="route"]')?.textContent).toContain('"completedJobId":"job"');
});

it.each(["failed", "interrupted", "cancelled", "resource_paused", "awaiting_api"])("never navigates automatically for %s", async (status) => {
  await mount(); start();
  await settle(() => expect(api.getJob).toHaveBeenCalled());
  act(() => client.setQueryData(["jobs", "job"], { id: "job", video_id: 1, status }));
  await settle(() => expect(container.querySelector('[data-testid="route"]')?.getAttribute('data-path')).toBe('/process?videoId=1'));
});

it("does not open the wrong video from a mismatched completed task", async () => {
  await mount(); start();
  await settle(() => expect(api.getJob).toHaveBeenCalled());
  act(() => client.setQueryData(["jobs", "job"], { id: "job", video_id: 99, status: "completed" }));
  await settle(() => expect(container.querySelector('.job-completed')).not.toBeNull());
  expect(container.querySelector('[data-testid="route"]')?.getAttribute('data-path')).toBe('/process?videoId=1');
});

it("does not steal navigation from another page and handles completion once on return", async () => {
  const options = await api.getVideoProcessingOptions(1);
  vi.mocked(api.getVideoProcessingOptions).mockResolvedValue({ ...options, active_job: { id: "job", status: "running" } });
  const renderActive = (active: boolean) => act(() => root.render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={["/process?videoId=1"]}><ProcessPage active={active} /><RouteProbe /></MemoryRouter>
  </QueryClientProvider>));
  renderActive(true);
  await settle(() => expect(api.getJob).toHaveBeenCalled());
  renderActive(false);
  act(() => client.setQueryData(["jobs", "job"], { id: "job", video_id: 1, status: "completed" }));
  await settle(() => expect(container.querySelector('.job-completed')).not.toBeNull());
  expect(container.querySelector('[data-testid="route"]')?.getAttribute('data-path')).toBe('/process?videoId=1');
  renderActive(true);
  await settle(() => expect(container.querySelector('[data-testid="route"]')?.getAttribute('data-path')).toBe('/review/1'));
});

function currentAiLabel() {
  return [...container.querySelectorAll(".readonly-config")]
    .find((item) => item.querySelector("span")?.textContent === "当前 AI 服务")!
    .querySelector("strong")!.textContent;
}

it("reads the global AI configuration before a video is selected or imported", async () => {
  await mount("/process");
  await settle(() => expect(currentAiLabel()).toBe("configured-model"));
  expect(api.getAiSettings).toHaveBeenCalledTimes(1);
  expect(api.getVideoProcessingOptions).not.toHaveBeenCalled();
  expect(api.registerVideo).not.toHaveBeenCalled();
});

it("updates the current AI model from the shared settings cache even if video options are stale", async () => {
  await mount();
  await settle(() => expect(currentAiLabel()).toBe("configured-model"));
  const configured = await api.getAiSettings();
  act(() => client.setQueryData(["settings", "ai"], { ...configured, model: "switched-model" }));
  await settle(() => expect(currentAiLabel()).toBe("switched-model"));
  act(() => client.setQueryData(["settings", "ai"], {
    ...configured, model: "", base_url: "", connections: [], connection_id: "", has_api_key: false,
  }));
  await settle(() => expect(currentAiLabel()).toBe("未配置"));
});

it("distinguishes loading and request errors from an unconfigured AI service", async () => {
  let reject!: (reason: Error) => void;
  vi.mocked(api.getAiSettings).mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }));
  await mount("/process");
  expect(currentAiLabel()).toBe("正在读取配置");
  await act(async () => { reject(new Error("service unavailable")); });
  await settle(() => expect(currentAiLabel()).toBe("配置读取失败"));
  expect(currentAiLabel()).not.toBe("未配置");
});

function start() {
  const button = [...container.querySelectorAll("button")].find((item) => item.textContent?.includes("重试处理"));
  expect(button).toBeTruthy();
  act(() => button!.click());
}

function selectionButton() {
  return container.querySelector<HTMLButtonElement>(".video-file-picker button, .video-selected-tools button")!;
}

function editFilename() {
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="修改文件名"]')!.click());
}

describe("desktop processing workflow", () => {
  it.each(["uploaded", "failed"])("checks empty recall choices on submit for %s videos and allows correction", async (status) => {
    vi.mocked(api.getVideo).mockResolvedValue({ value: { ...video, status }, source: "api" });
    vi.mocked(api.processVideo).mockResolvedValue({ id: "new-job", status: "queued" });
    await mount();
    const medium = container.querySelector<HTMLInputElement>('.recall-mode-control input[value="medium_recall"]')!;
    act(() => medium.click());
    expect(container.querySelectorAll(".recall-mode-control input:checked")).toHaveLength(0);
    expect(container.querySelector(".process-error")).toBeNull();
    const button = container.querySelector<HTMLButtonElement>(".process-start-button")!;
    expect(button.disabled).toBe(false);
    act(() => button.click());
    await settle(() => expect(container.querySelector(".process-error")?.textContent).toContain("请至少选择一种考点召回类型"));
    expect(api.processVideo).not.toHaveBeenCalled();
    expect(api.retryVideo).not.toHaveBeenCalled();
    act(() => medium.click());
    expect(container.querySelector(".process-error")).toBeNull();
    act(() => button.click());
    await settle(() => expect(status === "failed" ? api.retryVideo : api.processVideo).toHaveBeenCalledWith(
      1, expect.objectContaining({ analysis_modes: ["medium_recall"] }),
    ));
  });

  it("does not show Qwen resource controls for a Whisper selection", async () => {
    vi.mocked(api.getAsrSelection).mockResolvedValue({
      id: "whisper-turbo", name: "Whisper turbo", engine: "whisper",
      downloaded: true, cache_size_bytes: 1000, selected: true,
    });
    await mount("/process");
    await settle(() => expect(container.textContent).toContain("Whisper turbo"));
    expect(container.querySelector(".qwen-resource-controls")).toBeNull();
    expect(container.querySelector(".qwen-hardware-warning")).toBeNull();
  });

  it("shows an imported video prominently without another import or selection step", async () => {
    vi.mocked(api.getVideo).mockResolvedValue({ value: { ...video, status: "uploaded" }, source: "api" });
    vi.mocked(api.processVideo).mockResolvedValue({ id: "new-job", status: "queued" });
    await mount();
    expect(container.querySelector("h1")?.textContent).toBe("处理视频");
    expect(container.querySelector('[role="heading"][aria-level="2"]')?.textContent).toBe("lesson.mp4");
    expect(container.textContent).toContain("已导入视频库");
    expect(container.querySelector(".video-info .status-badge")?.textContent).toBe("待处理");
    expect(container.querySelector(".video-file-picker")).toBeNull();
    expect(container.querySelector("#video-path")).toBeNull();
    expect(container.querySelector("#video-filename")).toBeNull();
    expect(container.querySelector(".video-storage-details")?.hasAttribute("open")).toBe(false);
    expect(container.querySelector(".process-status-section")).toBeNull();
    const preview = container.querySelector<HTMLVideoElement>(".process-preview-player")!;
    expect(preview.getAttribute("src")).toBe("/api/videos/1/stream");
    expect(preview.controls).toBe(true);
    expect(preview.autoplay).toBe(false);
    expect(container.querySelector(".process-selected-preview .video-thumbnail")).toBeNull();
    const button = container.querySelector<HTMLButtonElement>(".process-options-section .process-start-button")!;
    expect(button.disabled).toBe(false);
    expect(button.textContent).toContain("开始处理");
    act(() => button.click());
    await settle(() => expect(api.processVideo).toHaveBeenCalledWith(1, expect.any(Object)));
    expect(api.registerVideo).not.toHaveBeenCalled();
    expect(api.openPicker).not.toHaveBeenCalled();
  });

  it("shows the central file selection button with the manual path field collapsed", async () => {
    await mount("/process");
    const picker = container.querySelector(".video-file-picker")!;
    expect(picker.querySelector("button")?.textContent).toContain("选择文件");
    expect(picker.textContent).toContain("MP4");
    expect(container.querySelector(".video-path-details")?.hasAttribute("open")).toBe(false);
    expect(container.querySelector(".video-info")).toBeNull();
    expect(api.openPicker).not.toHaveBeenCalled();
  });

  it("hides temporarily unused screening controls while keeping recall and retranscription", async () => {
    await mount();
    expect(container.textContent).not.toContain("关键词预筛选");
    expect(container.textContent).not.toContain("排除的公式文本");
    expect(container.textContent).not.toContain("过滤低信息字幕");
    expect(container.textContent).toContain("考点召回");
    expect(container.textContent).toContain("使用当前模型重新转写");
  });

  it("disables selection while opening and polling, then accepts the selected file", async () => {
    let resolvePicker!: (value: Awaited<ReturnType<typeof api.openPicker>>) => void;
    vi.mocked(api.openPicker).mockImplementation(() => new Promise((resolve) => { resolvePicker = resolve; }));
    vi.mocked(api.getPicker).mockResolvedValue({ id: "picker", status: "completed", path: "D:\\new-lesson.mp4" });
    await mount();
    const button = selectionButton();
    act(() => button.click());
    expect(button.disabled).toBe(true);
    expect(button.textContent).toContain("等待选择");
    expect(api.openPicker).toHaveBeenCalledWith("file");
    act(() => button.click());
    expect(api.openPicker).toHaveBeenCalledTimes(1);
    await act(async () => resolvePicker({ id: "picker", status: "pending" }));
    await settle(() => expect(container.querySelector(".video-info strong")?.textContent).toBe("new-lesson.mp4"));
    expect(api.getPicker).toHaveBeenCalledWith("picker");
    expect(selectionButton().disabled).toBe(false);
    expect(container.querySelector<HTMLInputElement>("#video-path")?.value).toBe("D:\\new-lesson.mp4");
    expect(container.querySelector(".video-info .status-badge")).toBeNull();
    expect(container.querySelector<HTMLButtonElement>(".process-start-button")?.disabled).toBe(true);
  });

  it("keeps the selected video when file selection is cancelled", async () => {
    vi.mocked(api.openPicker).mockResolvedValue({ id: "cancelled-picker", status: "pending" });
    vi.mocked(api.getPicker).mockResolvedValue({ id: "cancelled-picker", status: "cancelled" });
    await mount();
    act(() => selectionButton().click());
    await settle(() => expect(container.querySelector('[role="status"]')?.textContent).toContain("已取消选择视频"));
    expect(container.querySelector(".video-info strong")?.textContent).toBe("lesson.mp4");
    expect(selectionButton().disabled).toBe(false);
    expect(container.querySelector(".video-file-picker")).toBeNull();
  });

  it("shows a picker startup error and allows another selection", async () => {
    vi.mocked(api.openPicker).mockRejectedValue(new Error("无法打开文件选择器"));
    await mount();
    act(() => selectionButton().click());
    await settle(() => expect(container.querySelector('[role="alert"]')?.textContent).toContain("无法打开文件选择器"));
    expect(selectionButton().disabled).toBe(false);
    expect(container.querySelector(".video-info strong")?.textContent).toBe("lesson.mp4");
  });

  it("shows a terminal picker failure and enables selection again", async () => {
    vi.mocked(api.openPicker).mockResolvedValue({ id: "failed-picker", status: "pending" });
    vi.mocked(api.getPicker).mockResolvedValue({ id: "failed-picker", status: "failed", error: "选择文件失败" });
    await mount();
    act(() => selectionButton().click());
    await settle(() => expect(container.querySelector('[role="alert"]')?.textContent).toContain("选择文件失败"));
    expect(selectionButton().disabled).toBe(false);
  });

  it("registers the file selected from the central button", async () => {
    vi.mocked(api.openPicker).mockResolvedValue({ path: video.video_path, status: "completed" });
    vi.mocked(api.registerVideo).mockResolvedValue(video);
    await mount("/process");
    act(() => container.querySelector<HTMLButtonElement>(".video-file-picker button")!.click());
    await settle(() => expect(container.querySelector(".video-info strong")?.textContent).toBe("lesson.mp4"));
    const register = [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("导入视频库"))!;
    act(() => register.click());
    await settle(() => expect(api.registerVideo).toHaveBeenCalledWith({ video_path: video.video_path, filename: video.filename }));
    await settle(() => expect(container.querySelector(".video-info .status-badge")).toBeTruthy());
    expect(container.querySelector(".video-file-picker")).toBeNull();
    expect(container.querySelector("#video-filename")).toBeNull();
    expect(container.querySelector("h1")?.textContent).toBe("处理视频");
    expect(container.textContent).toContain("已导入视频库");
  });

  it("edits the real filename and updates the playback path without a display-name field", async () => {
    await mount();
    expect(container.querySelector("#display-name")).toBeNull();
    editFilename();
    const field = container.querySelector<HTMLInputElement>("#video-filename")!;
    expect(field.value).toBe("lesson.mp4");
    const renamed = {
      ...video, filename: "chapter.mp4", display_name: "chapter", video_path: "D:\\course\\videos\\hash\\chapter.mp4",
    };
    vi.mocked(api.renameVideoFile).mockResolvedValue(renamed);
    vi.mocked(api.getVideo).mockResolvedValue({ value: renamed, source: "api" });
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, "chapter.mp4");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="保存文件名"]')!.click());
    await settle(() => expect(api.renameVideoFile).toHaveBeenCalledWith(1, "chapter.mp4"));
    await settle(() => expect(container.querySelector(".video-info strong")?.textContent).toBe("chapter.mp4"));
    expect(container.querySelector(".video-storage-details dd")?.textContent).toBe("D:\\course\\videos\\hash\\chapter.mp4");
    expect(container.querySelector("#video-filename")).toBeNull();
    expect(api.registerVideo).not.toHaveBeenCalled();
  });

  it("keeps the filename draft and original path after a failed rename", async () => {
    await mount();
    editFilename();
    vi.mocked(api.renameVideoFile).mockRejectedValue(new Error("文件被占用"));
    act(() => {
      const field = container.querySelector<HTMLInputElement>("#video-filename")!;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, "chapter.mp4");
      field.dispatchEvent(new Event("input", { bubbles: true }));
    });
    act(() => container.querySelector<HTMLButtonElement>('[aria-label="保存文件名"]')!.click());
    await settle(() => expect(container.textContent).toContain("文件被占用"));
    expect(container.querySelector<HTMLInputElement>("#video-filename")!.value).toBe("chapter.mp4");
    expect(container.querySelector(".video-storage-details dd")?.textContent).toBe(video.video_path);
  });

  it("shows the fixed FireRed tier without Qwen or manual resource controls", async () => {
    await mount();
    await settle(() => expect(container.textContent).toContain("初始 BS6"));
    expect(api.getAsrResourceProfile).toHaveBeenCalledWith("fireredasr2-aed");
    expect(container.textContent).not.toContain("Qwen 资源模式");
    expect(container.querySelector(".firered-resource-profile input")).toBeNull();
    expect(api.processVideo).not.toHaveBeenCalled();
  });

  it("restores saved FireRed recovery metadata after entering the page", async () => {
    const options = await api.getVideoProcessingOptions(1);
    vi.mocked(api.getVideoProcessingOptions).mockResolvedValue({
      ...options, active_job: { id: "paused", status: "resource_paused" },
    });
    vi.mocked(api.getJob).mockResolvedValue({
      id: "paused", status: "resource_paused", progress: .5,
      resource: {
        engine: "firered", device: "cuda:0", initial_batch_size: 6, current_batch_size: 3,
        completed_segments: 200, total_segments: 915, recovery_count: 2, recovery_reason: "inference: CUDA OOM",
      },
    });
    await mount();
    await settle(() => expect(container.textContent).toContain("当前 BS3"));
    expect(container.textContent).toContain("已保存 200/915");
    expect(container.textContent).toContain("CUDA OOM");
    expect(container.textContent).toContain("继续处理");
  });

  it("shows a paused resource job and resumes the saved request", async () => {
    vi.mocked(api.getVideoProcessingOptions).mockResolvedValue({
      asr_model: "Qwen", active_job: { id: "paused", status: "resource_paused" },
    });
    vi.mocked(api.getJob).mockResolvedValue({
      id: "paused", status: "resource_paused", progress: 40, error_message: "resources",
    });
    vi.mocked(api.resumeJob).mockResolvedValue({ id: "resumed", status: "queued" });
    act(() => root.render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/process?videoId=1"]}>
          <ProcessPage />
        </MemoryRouter>
      </QueryClientProvider>,
    ));
    await settle(() => expect(container.textContent).toContain("资源暂停"));
    const button = [...container.querySelectorAll("button")].find((item) => item.textContent?.includes("继续处理"));
    expect(button).toBeTruthy();
    act(() => button!.click());
    await settle(() => expect(api.resumeJob).toHaveBeenCalledWith("paused"));
  });
  it("shows the configured AI model and explicitly requests AED retranscription", async () => {
    await mount();
    expect(container.textContent).toContain("configured-model");
    const label = [...container.querySelectorAll("label")].find((item) => item.textContent?.includes("使用当前模型重新转写"));
    act(() => label!.querySelector("input")!.click());
    start();
    await settle(() => expect(api.retryVideo).toHaveBeenCalledWith(1, expect.objectContaining({
      force_retranscribe: true, asr_model_id: "fireredasr2-aed",
    })));
    await settle(() => expect(container.querySelector(".video-info .status-badge")?.textContent).toBe("处理中"));
    await settle(() => expect(container.querySelector(".job-status")?.textContent).toContain("12.5%"));
  });

  it("ordinary retry does not replace existing subtitles", async () => {
    await mount();
    start();
    await settle(() => expect(api.retryVideo).toHaveBeenCalledWith(1, expect.objectContaining({
      force_retranscribe: false,
      analysis_mode: "medium_recall",
      filter_formula_noise: false,
      filter_formula: null,
      use_keyword_prefilter: false,
      keywords: [],
    })));
  });

  it("selects several recall modes without legacy focus flags", async () => {
    await mount();
    const choices = container.querySelectorAll<HTMLInputElement>(".recall-mode-control input");
    expect([...choices].map(item => item.value)).toEqual(["high_recall", "medium_recall", "low_recall"]);
    expect([...choices].filter(item => item.checked).map(item => item.value)).toEqual(["medium_recall"]);
    act(() => choices[2].click());
    start();
    await settle(() => expect(api.retryVideo).toHaveBeenCalledWith(1, expect.objectContaining({
      analysis_modes: ["medium_recall", "low_recall"],
    })));
    const payload = vi.mocked(api.retryVideo).mock.calls[0][1];
    expect(payload).not.toHaveProperty("exam_focus");
    expect(payload).not.toHaveProperty("extension_focus");
  });

  it("uses the remembered custom scheme after remount without adding medium recall", async () => {
    const id = `custom_${"a".repeat(32)}` as const;
    localStorage.setItem(`${RECALL_SELECTION_KEY}:default`, JSON.stringify([id]));
    vi.mocked(api.getRecallSchemes).mockResolvedValue([{
      id, name: "已修改的方案名称", base_mode: "medium_recall", revision: 2, archived: false, rules: [], notes: "",
    }]);
    await mount();
    await settle(() => expect(container.textContent).toContain("已修改的方案名称"));
    expect([...container.querySelectorAll<HTMLInputElement>(".recall-mode-control input:checked")].map(item => item.value)).toEqual([id]);
    start();
    await settle(() => expect(api.retryVideo).toHaveBeenCalledWith(1, expect.objectContaining({ analysis_modes: [id] })));
  });

  it("restores an active task even when cached video metadata says failed", async () => {
    const options = await api.getVideoProcessingOptions(1);
    vi.mocked(api.getVideoProcessingOptions).mockResolvedValue({
      ...options, active_job: { id: "job", status: "running" },
    });
    await mount();
    await settle(() => expect(container.querySelector(".video-info .status-badge")?.textContent).toBe("处理中"));
    expect(container.querySelector(".process-start-button")?.hasAttribute("disabled")).toBe(true);
    expect(selectionButton().disabled).toBe(true);
    expect(container.querySelector<HTMLButtonElement>('[aria-label="修改文件名"]')?.disabled).toBe(true);
  });

  it("shows an existing-video error without suggesting a new file import", async () => {
    vi.mocked(api.getVideo).mockRejectedValue(new Error("视频记录不存在"));
    act(() => root.render(
      <QueryClientProvider client={client}><MemoryRouter initialEntries={["/process?videoId=999"]}>
        <ProcessPage />
      </MemoryRouter></QueryClientProvider>,
    ));
    await settle(() => expect(container.querySelector('[role="alert"]')?.textContent).toContain("视频记录不存在"));
    expect(container.querySelector(".video-file-picker")).toBeNull();
    expect(api.openPicker).not.toHaveBeenCalled();
  });

  it("refreshes the video status after task completion", async () => {
    await mount();
    vi.mocked(api.getVideo).mockResolvedValue({ value: { ...video, status: "review_ready" }, source: "api" });
    vi.mocked(api.getJob).mockResolvedValue({ id: "job", status: "completed", progress: 1 });
    start();
    await settle(() => expect(container.querySelector(".video-info .status-badge")?.textContent).toBe("可复习"));
    expect(container.querySelector(".process-start-button")?.textContent).toContain("开始处理");
  });
});
