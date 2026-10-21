import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { expect, it, vi } from "vitest";
import * as api from "../api/client";
import { SettingsPage } from "./SettingsPage";
import type { AiSettings, AsrModel } from "../types";

vi.mock("../api/client");

const catalogue: AsrModel[] = [
  { id: "whisper-turbo", name: "Whisper turbo", engine: "whisper", selected: true,
    downloaded: null, cache_size_bytes: null, inspection_status: "unknown" },
  { id: "dolphin-small-cn", name: "Dolphin", engine: "dolphin", selected: false, device: "cpu",
    downloaded: null, cache_size_bytes: null, inspection_status: "unknown" },
];

async function waitFor(check: () => void) {
  let failure: unknown;
  for (let attempt = 0; attempt < 100; attempt++) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
    try { check(); return; } catch (error) { failure = error; }
  }
  throw failure;
}

function mountSettings(path = "/settings", models = catalogue) {
  mockDirectories();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(api.getAsrModels).mockResolvedValue(models);
  vi.mocked(api.getLibraryStatus).mockResolvedValue({
    data_root: "D:\\course", database_path: "D:\\course\\project.db", temp_audio_dir: "D:\\course\\audio", video_count: 0,
  });
  vi.mocked(api.getAiSettings).mockResolvedValue({
    provider: "deepseek", base_url: "https://api.deepseek.com", model: "model",
    output_tokens: 2048, token_parameter: "max_tokens", json_mode: true,
    timeout_seconds: 60, max_retries: 2, has_api_key: false,
  });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const render = (active = true) => act(() => root.render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <SettingsPage active={active} />
      </MemoryRouter>
    </QueryClientProvider>,
  ));
  render();
  return { container, client, render, async dispose() {
    await act(async () => { root.unmount(); client.clear(); });
    container.remove();
  } };
}

it.each(["gpu", "cpu"] as const)("clears only the chosen Dolphin %s environment and displays real cleanup counts", async (device) => {
  vi.resetAllMocks();
  const models: AsrModel[] = catalogue.map((model) => model.engine === "dolphin"
    ? { ...model, device: "gpu", gpu_available: true, gpu_runtime_ready: true, downloaded: true } : model);
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => models.find((model) => model.id === id)!);
  vi.mocked(api.clearAsrRuntime).mockResolvedValue({ job_id: "runtime-delete" });
  vi.mocked(api.getJobStatus).mockResolvedValue({ id: "runtime-delete", status: "running", cleanup: {
    phase: "deleting", scope: "Dolphin GPU 环境", message: "删除文件", phase_progress: .7,
    files_scanned: 10, directories_scanned: 1, files_deleted: 7, directories_deleted: 0,
    files_total: 10, directories_total: 1, bytes_deleted: 70, bytes_total: 100,
    eta_seconds: null, elapsed_seconds: 2, updated_at: Date.now() / 1000,
  } });
  const page = mountSettings("/settings?section=asr", models);
  try {
    await waitFor(() => expect(page.container.querySelector('[aria-label*="Dolphin GPU 运行环境"]')).not.toBeNull());
    if (device === "cpu") act(() => (page.container.querySelector('[data-model-id="dolphin-small-cn"] input[value="cpu"]') as HTMLInputElement).click());
    const button = () => page.container.querySelector(`[aria-label*="Dolphin ${device.toUpperCase()} 运行环境"]`) as HTMLButtonElement;
    expect(button().disabled).toBe(false);
    act(() => button().click());
    await waitFor(() => expect(api.clearAsrRuntime).toHaveBeenCalledWith("dolphin-small-cn", device));
    expect(api.clearAsrModel).not.toHaveBeenCalled();
    await waitFor(() => expect(page.container.textContent).toContain("7 / 10 个文件"));
    expect(button().getAttribute("title")).toContain("保留模型权重");
  } finally { await page.dispose(); }
});

it("keeps AI edits in memory across server refreshes and pauses checks while hidden", async () => {
  vi.resetAllMocks();
  vi.mocked(api.getAsrModelStatus).mockResolvedValue({ ...catalogue[0], downloaded: true });
  const page = mountSettings("/settings?section=ai");
  try {
    await waitFor(() => expect(page.container.querySelectorAll(".settings-field input").length).toBeGreaterThan(0));
    const input = [...page.container.querySelectorAll<HTMLLabelElement>(".settings-field")]
      .find((label) => label.textContent?.includes("模型名称"))!.querySelector("input")!;
    act(() => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "unsaved-model");
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const saved = page.client.getQueryData<AiSettings>(["settings", "ai"])!;
    act(() => page.client.setQueryData(["settings", "ai"], { ...saved, model: "server-model", output_tokens: 4096 }));
    expect(input.value).toBe("unsaved-model");
    page.render(false);
    vi.mocked(api.getAsrModelStatus).mockClear();
    vi.mocked(api.getAiSettings).mockClear();
    await act(async () => {
      await page.client.invalidateQueries({ queryKey: ["asr-status"] });
      await page.client.invalidateQueries({ queryKey: ["settings", "ai"] });
    });
    expect(api.getAsrModelStatus).not.toHaveBeenCalled();
    expect(api.getAiSettings).not.toHaveBeenCalled();
    expect(input.value).toBe("unsaved-model");
    page.render();
    await waitFor(() => expect(api.getAiSettings).toHaveBeenCalledTimes(1));
    expect(input.value).toBe("unsaved-model");
  } finally { await page.dispose(); }
});

it.each(["", ".asr-model-description", ".asr-model-meta dd"])("selects an engine from the card surface (%s) without immediately saving", async (target) => {
  vi.resetAllMocks();
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => catalogue.find(model => model.id === id)!);
  const page = mountSettings("/settings?section=asr");
  try {
    await waitFor(() => expect(page.container.querySelectorAll(".asr-model-option")).toHaveLength(2));
    const card = page.container.querySelector<HTMLElement>('[data-model-id="dolphin-small-cn"]')!;
    act(() => (target ? card.querySelector<HTMLElement>(target)! : card).click());
    expect(card.querySelector<HTMLInputElement>('input[name="asr-model"]')!.checked).toBe(true);
    expect(document.activeElement).toBe(card.querySelector('input[name="asr-model"]'));
    expect(page.container.querySelector(".asr-current-choice")?.textContent).toContain("Dolphin");
    expect(api.selectAsrModel).not.toHaveBeenCalled();
    expect(api.prepareAsrModel).not.toHaveBeenCalled();
  } finally { await page.dispose(); }
});

it.each(["prepare", "cache", "runtime"])("keeps the %s tool independent of engine card selection", async (tool) => {
  vi.resetAllMocks();
  const models = catalogue.map(model => model.engine === "dolphin"
    ? { ...model, downloaded: tool !== "prepare", cache_size_bytes: tool === "prepare" ? 0 : 1000 } : model);
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => models.find(model => model.id === id)!);
  vi.mocked(api.prepareAsrModel).mockResolvedValue({ job_id: "tool-job" });
  vi.mocked(api.clearAsrModel).mockResolvedValue({ job_id: "tool-job" });
  vi.mocked(api.clearAsrRuntime).mockResolvedValue({ job_id: "tool-job" });
  vi.mocked(api.getJobStatus).mockResolvedValue({ id: "tool-job", status: "running" });
  const page = mountSettings("/settings?section=asr", models);
  try {
    await waitFor(() => expect(page.container.querySelectorAll(".asr-model-option")).toHaveLength(2));
    const card = page.container.querySelector<HTMLElement>('[data-model-id="dolphin-small-cn"]')!;
    const button = tool === "prepare"
      ? [...card.querySelectorAll<HTMLButtonElement>("button")].find(node => node.textContent?.trim() === "准备模型")!
      : card.querySelector<HTMLButtonElement>(tool === "cache" ? '[aria-label="清理Dolphin缓存"]' : '[aria-label*="CPU 运行环境"]')!;
    expect(button.disabled).toBe(false);
    // Clicking an icon must be ignored by the card handler just like clicking the button itself.
    act(() => (button.querySelector("svg") ?? button).dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await waitFor(() => expect(tool === "prepare" ? api.prepareAsrModel : tool === "cache" ? api.clearAsrModel : api.clearAsrRuntime).toHaveBeenCalledTimes(1));
    expect(card.querySelector<HTMLInputElement>('input[name="asr-model"]')!.checked).toBe(false);
    expect(page.container.querySelector(".asr-current-choice")?.textContent).toContain("Whisper");
    expect(api.selectAsrModel).not.toHaveBeenCalled();
  } finally { await page.dispose(); }
});

it("does not change the engine while the user selects description text", async () => {
  vi.resetAllMocks();
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => catalogue.find(model => model.id === id)!);
  const page = mountSettings("/settings?section=asr");
  const selection = vi.spyOn(window, "getSelection").mockReturnValue({ isCollapsed: false } as Selection);
  try {
    await waitFor(() => expect(page.container.querySelectorAll(".asr-model-option")).toHaveLength(2));
    const card = page.container.querySelector<HTMLElement>('[data-model-id="dolphin-small-cn"]')!;
    act(() => card.querySelector<HTMLElement>(".asr-model-description")!.click());
    expect(card.querySelector<HTMLInputElement>('input[name="asr-model"]')!.checked).toBe(false);
  } finally { selection.mockRestore(); await page.dispose(); }
});

it("keeps the warm background across categories and pauses it on inactive pages or hidden windows", async () => {
  vi.resetAllMocks();
  const hidden = vi.spyOn(document, "hidden", "get").mockReturnValue(false);
  const page = mountSettings();
  try {
    const background = page.container.querySelector(".settings-page > .warm-atmosphere")!;
    expect(background).not.toBeNull();
    expect(background.getAttribute("aria-hidden")).toBe("true");
    expect(background.getAttribute("data-moving")).toBe("true");
    expect(background.children).toHaveLength(2);
    const herons = page.container.querySelector(".settings-herons")!;
    expect(herons.getAttribute("aria-hidden")).toBe("true");
    expect([...herons.querySelectorAll(".settings-heron-left img")].map(image => image.getAttribute("src")))
      .toEqual(["head", "neck", "body"].map(part => `/art/settings-herons/left-${part}.webp`));
    expect([...herons.querySelectorAll(".settings-heron-right img")].map(image => image.getAttribute("src")))
      .toEqual(["head", "neck", "body"].map(part => `/art/settings-herons/right-${part}.webp`));
    act(() => [...page.container.querySelectorAll<HTMLButtonElement>(".settings-navigation button")]
      .find(button => button.textContent?.includes("快捷键"))!.click());
    expect(page.container.querySelector(".warm-atmosphere")).toBe(background);
    expect(page.container.querySelector(".settings-herons")).toBe(herons);
    page.render(false);
    expect(background.getAttribute("data-moving")).toBe("false");
    page.render();
    expect(background.getAttribute("data-moving")).toBe("true");
    hidden.mockReturnValue(true);
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(background.getAttribute("data-moving")).toBe("false");
    hidden.mockReturnValue(false);
    act(() => document.dispatchEvent(new Event("visibilitychange")));
    expect(background.getAttribute("data-moving")).toBe("true");
  } finally {
    await page.dispose();
    hidden.mockRestore();
  }
});

it("does not inspect ASR models or AI services in the default storage category", async () => {
  vi.resetAllMocks();
  vi.mocked(api.getAsrModelStatus).mockResolvedValue({
    ...catalogue[0], downloaded: true, inspection_status: "ready",
  });
  const page = mountSettings();
  try {
    await waitFor(() => expect(api.getStorageDirectories).toHaveBeenCalled());
    expect(page.container.querySelectorAll(".settings-navigation button")).toHaveLength(8);
    expect(page.container.querySelector(".settings-navigation")?.textContent).toContain("快捷键");
    expect(page.container.querySelector(".settings-navigation")?.textContent).toContain("桌面设置");
    expect(page.container.querySelector(".settings-navigation")?.textContent).not.toContain("工作区");
    expect(page.container.querySelector(".storage-directories-section")).not.toBeNull();
    expect(api.getLibraryStatus).not.toHaveBeenCalled();
    expect(page.container.querySelectorAll('input[name="asr-model"]')).toHaveLength(0);
    expect(api.getAsrModels).not.toHaveBeenCalled();
    expect(api.getAsrModelStatus).not.toHaveBeenCalled();
    expect(api.getAiSettings).not.toHaveBeenCalled();
    expect(page.container.textContent).not.toContain("Dolphin");
  } finally { await page.dispose(); }
});

it("includes drag text preferences in desktop settings even outside the native application", async () => {
  vi.resetAllMocks();
  const page = mountSettings("/settings?section=desktop");
  try {
    expect(page.container.querySelector('div:not([hidden]) > section[aria-label="交互提示"]')).not.toBeNull();
    expect(page.container.querySelector('[aria-label="拖拽文字提示"]')?.getAttribute("role")).toBe("switch");
    expect(api.getAsrModels).not.toHaveBeenCalled();
    expect(api.getAiSettings).not.toHaveBeenCalled();
    expect(api.getStorageDirectories).not.toHaveBeenCalled();
  } finally { await page.dispose(); }
});

it("renders model status independently while sharing in-flight checks across categories", async () => {
  vi.resetAllMocks();
  let resolveSlow!: (model: AsrModel) => void;
  const slow = new Promise<AsrModel>((resolve) => { resolveSlow = resolve; });
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => id === "dolphin-small-cn"
    ? slow : { ...catalogue[0], downloaded: true, inspection_status: "ready" });
  const page = mountSettings();
  const button = (text: string) => [...page.container.querySelectorAll("button")]
    .find((item) => item.textContent?.includes(text))!;
  try {
    act(() => button("ASR 模型").click());
    await waitFor(() => expect(page.container.querySelectorAll('input[name="asr-model"]')).toHaveLength(2));
    const whisper = page.container.querySelector('[data-model-id="whisper-turbo"]')!;
    const dolphin = page.container.querySelector('[data-model-id="dolphin-small-cn"]')!;
    await waitFor(() => expect(whisper.textContent).toContain("已下载"));
    expect(dolphin.textContent).toContain("检查中");
    expect(dolphin.textContent).toContain("待统计");
    expect(dolphin.textContent).not.toContain("未准备");
    expect(button("刷新状态")).toBeUndefined();
    act(() => button("存储与导入").click());
    act(() => button("ASR 模型").click());
    expect(vi.mocked(api.getAsrModelStatus).mock.calls.filter(([id]) => id === "dolphin-small-cn")).toHaveLength(1);
    await act(async () => resolveSlow({
      ...catalogue[1], downloaded: true, cache_size_bytes: 1024, inspection_status: "ready",
    }));
    await waitFor(() => expect(page.container.querySelector('[data-model-id="dolphin-small-cn"]')?.textContent).toContain("已下载"));
    expect(button("刷新状态")).toBeUndefined();
    expect(button("保存模型选择")).toBeTruthy();
    expect(vi.mocked(api.getAsrModelStatus).mock.calls.filter(([, options]) => options?.refresh)).toHaveLength(0);
  } finally {
    resolveSlow({ ...catalogue[1], downloaded: true });
    await page.dispose();
  }
});

it("keeps cached observations on errors and offers a per-model forced retry", async () => {
  vi.resetAllMocks();
  vi.mocked(api.getAsrModelStatus).mockRejectedValue(new Error("offline"));
  const page = mountSettings("/settings?section=asr");
  try {
    await waitFor(() => expect(page.container.querySelectorAll(".asr-model-option")).toHaveLength(2));
    act(() => page.client.setQueryData(["settings", "asr"], catalogue.map((model) => ({
      ...model, downloaded: true, cache_size_bytes: 2048, inspection_status: "cached",
    }))));
    await waitFor(() => expect(page.container.textContent).toContain("保留上次结果"));
    expect(page.container.querySelector('[data-model-id="dolphin-small-cn"]')?.textContent).toContain("已下载");
    vi.mocked(api.getAsrModelStatus).mockResolvedValue({
      ...catalogue[1], downloaded: true, cache_size_bytes: 2048, inspection_status: "ready",
    });
    act(() => page.container.querySelector<HTMLButtonElement>('button[aria-label="重新检查Dolphin"]')!.click());
    await waitFor(() => expect(api.getAsrModelStatus).toHaveBeenCalledWith(
      "dolphin-small-cn", { full: true, refresh: true },
    ));
    await waitFor(() => expect(page.container.querySelector('[data-model-id="dolphin-small-cn"]')?.textContent).not.toContain("状态检查失败"));
  } finally { await page.dispose(); }
});

const fullCatalogue: AsrModel[] = [
  ...catalogue,
  { id: "whisper-large-v3", name: "Whisper large-v3", engine: "whisper", selected: false,
    downloaded: false, cache_size_bytes: 0 },
  { id: "paraformer-large-cpu", name: "Paraformer-large（BiCIF）", engine: "paraformer", selected: false, device: "cpu",
    downloaded: true, cache_size_bytes: 1024, runtime_size_bytes: 2048, status_checked_at: 123, gpu_available: true },
  { id: "fireredasr2-aed", name: "FireRedASR2S（VAD + AED）", engine: "firered", selected: false, device: "gpu",
    downloaded: true, cache_size_bytes: 1024, aed_ready: true, supports_aed: true },
  { id: "qwen3-asr-1.7b", name: "Qwen3-ASR 1.7B", engine: "qwen", selected: false,
    downloaded: true, cache_size_bytes: 1024, qwen_ready: true, recommendation: "旧说明" },
];

it("recovers a running model download after reload without resubmitting it", async () => {
  vi.resetAllMocks();
  const models = fullCatalogue.map((model) => model.id === "whisper-large-v3"
    ? { ...model, active_job_id: "existing-download", active_job_kind: "preparing" as const } : model);
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => models.find((model) => model.id === id)!);
  vi.mocked(api.getJobStatus).mockResolvedValue({
    id: "existing-download", status: "running", progress: .5, preparation: {
      phase: "downloading", label: "下载模型权重", message: "下载 model.bin",
      current_file: "model.bin", phase_progress: .5, bytes_completed: 100, bytes_total: 200,
      updated_at: Date.now() / 1000,
    },
  });
  const page = mountSettings("/settings?section=asr", models);
  try {
    await waitFor(() => expect(page.container.querySelector('[data-model-id="whisper-large-v3"] [role="progressbar"]')).not.toBeNull());
    expect(page.container.querySelector<HTMLDetailsElement>(".asr-more-models")?.open).toBe(true);
    expect(api.getJobStatus).toHaveBeenCalledWith("existing-download");
    expect(api.prepareAsrModel).not.toHaveBeenCalled();
    expect([...page.container.querySelectorAll<HTMLButtonElement>(".asr-model-actions button")]
      .filter((button) => button.textContent?.includes("准备模型")).every((button) => button.disabled)).toBe(true);
  } finally { await page.dispose(); }
});

it("disables model preparation after the model is already ready", async () => {
  vi.resetAllMocks();
  const models = catalogue.map((model) => model.id === "dolphin-small-cn"
    ? { ...model, downloaded: true, cache_size_bytes: 1024, inspection_status: "ready" as const }
    : model);
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => models.find((model) => model.id === id)!);
  const page = mountSettings("/settings?section=asr", models);
  try {
    await waitFor(() => expect(page.container.querySelector('[data-model-id="dolphin-small-cn"]')).not.toBeNull());
    const card = page.container.querySelector('[data-model-id="dolphin-small-cn"]')!;
    await waitFor(() => expect(card.textContent).toContain("已下载"));
    const prepare = [...card.querySelectorAll<HTMLButtonElement>("button")]
      .find((button) => button.textContent?.includes("模型已准备"));
    expect(prepare).toBeTruthy();
    expect(prepare?.disabled).toBe(true);
    expect(prepare?.title).toContain("无需重复操作");
    expect(api.prepareAsrModel).not.toHaveBeenCalled();
  } finally { await page.dispose(); }
});

it("allows clearing an incomplete model cache and blocks overlapping operations", async () => {
  vi.resetAllMocks();
  const models = fullCatalogue.map((model) => model.id === "dolphin-small-cn"
    ? { ...model, downloaded: false, cache_size_bytes: 8192 } : model);
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => models.find((model) => model.id === id)!);
  vi.mocked(api.clearAsrModel).mockResolvedValue({ job_id: "partial-clear" });
  vi.mocked(api.getJobStatus).mockResolvedValue({
    id: "partial-clear", status: "running", progress: .1, message: "清理部分下载缓存",
  });
  const page = mountSettings("/settings?section=asr", models);
  try {
    const clear = () => page.container.querySelector<HTMLButtonElement>(
      '[data-model-id="dolphin-small-cn"] button[title="清理模型缓存"]',
    )!;
    await waitFor(() => expect(clear()?.disabled).toBe(false));
    act(() => clear().click());
    await waitFor(() => expect(api.clearAsrModel).toHaveBeenCalledWith("dolphin-small-cn", false));
    await waitFor(() => expect(page.container.textContent).toContain("清理部分下载缓存"));
    const prepares = [...page.container.querySelectorAll<HTMLButtonElement>(".asr-model-actions button")]
      .filter((button) => button.textContent?.includes("准备模型") || button.title === "模型已准备，无需重复操作");
    expect(prepares).toHaveLength(6);
    expect(prepares.every((button) => button.disabled)).toBe(true);
    expect(clear().disabled).toBe(true);
    act(() => prepares[0].click());
    expect(api.prepareAsrModel).not.toHaveBeenCalled();
  } finally { await page.dispose(); }
});

it("refreshes partial cache sizes after a failed download so it can be cleared", async () => {
  vi.resetAllMocks();
  let failed = false;
  const models = fullCatalogue.map((model) => model.id === "dolphin-small-cn"
    ? { ...model, downloaded: false, cache_size_bytes: 0 } : model);
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => {
    const model = models.find((item) => item.id === id)!;
    return id === "dolphin-small-cn" && failed ? { ...model, cache_size_bytes: 4096 } : model;
  });
  vi.mocked(api.prepareAsrModel).mockResolvedValue({ job_id: "failed-download" });
  vi.mocked(api.getJobStatus).mockImplementation(async () => {
    failed = true;
    return { id: "failed-download", status: "failed", error_message: "下载连接中断" };
  });
  const page = mountSettings("/settings?section=asr", models);
  try {
    await waitFor(() => expect(page.container.querySelectorAll(".asr-model-option")).toHaveLength(6));
    const card = page.container.querySelector('[data-model-id="dolphin-small-cn"]')!;
    await waitFor(() => expect(card.textContent).toContain("未缓存"));
    const prepare = [...card.querySelectorAll("button")].find((button) => button.textContent?.includes("准备模型"))!;
    act(() => prepare.click());
    await waitFor(() => expect(page.container.textContent).toContain("下载连接中断"));
    await waitFor(() => expect(card.querySelector<HTMLButtonElement>('button[title="清理模型缓存"]')?.disabled).toBe(false));
    expect(card.textContent).toContain("4 KB");
  } finally { await page.dispose(); }
});

it("groups four primary models with Paraformer beside Dolphin and keeps both Whispers collapsed last", async () => {
  vi.resetAllMocks();
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => fullCatalogue.find((model) => model.id === id)!);
  const page = mountSettings("/settings?section=asr", fullCatalogue);
  try {
    await waitFor(() => expect(page.container.querySelectorAll(".asr-model-option")).toHaveLength(6));
    const primary = page.container.querySelectorAll(".asr-model-grid:not(.asr-more-grid) > article");
    expect([...primary].map((node) => node.getAttribute("data-model-id"))).toEqual([
      "qwen3-asr-1.7b", "fireredasr2-aed", "paraformer-large-cpu", "dolphin-small-cn",
    ]);
    const more = page.container.querySelector<HTMLDetailsElement>(".asr-more-models")!;
    expect(more.open).toBe(false);
    expect([...more.querySelectorAll("article")].map((node) => node.getAttribute("data-model-id"))).toEqual([
      "whisper-turbo", "whisper-large-v3",
    ]);
    expect(page.container.querySelector(".asr-current-choice")?.textContent).toContain("Whisper turbo");
    expect(more.querySelector<HTMLInputElement>('input[name="asr-model"]')?.checked).toBe(true);
    act(() => more.querySelector("summary")!.click());
    expect(more.open).toBe(true);
    const large = more.querySelector<HTMLInputElement>('[data-model-id="whisper-large-v3"] input')!;
    act(() => large.click());
    expect(api.selectAsrModel).not.toHaveBeenCalled();
    act(() => more.querySelector("summary")!.click());
    expect(more.open).toBe(false);
    expect(large.checked).toBe(true);
    expect(page.container.querySelector(".asr-current-choice")?.textContent).toContain("Whisper large-v3");
    vi.mocked(api.selectAsrModel).mockResolvedValue(undefined);
    act(() => [...page.container.querySelectorAll("button")].find((node) => node.textContent?.includes("保存模型选择"))!.click());
    await waitFor(() => expect(api.selectAsrModel).toHaveBeenCalledWith("whisper-large-v3"));
    expect(api.prepareAsrModel).not.toHaveBeenCalled();
    expect(api.clearAsrModel).not.toHaveBeenCalled();
  } finally { await page.dispose(); }
});

it("shows only state and cache metadata and uses the supplied model comparisons instead of backend recommendations", async () => {
  vi.resetAllMocks();
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => fullCatalogue.find((model) => model.id === id)!);
  const page = mountSettings("/settings?section=asr", fullCatalogue);
  try {
    await waitFor(() => expect(page.container.querySelectorAll(".asr-model-option")).toHaveLength(6));
    for (const card of page.container.querySelectorAll(".asr-model-option")) {
      expect([...card.querySelectorAll(".asr-model-meta dt")].map((node) => node.textContent)).toEqual(["状态", "缓存"]);
      expect(card.textContent).not.toMatch(/检查时间|AED|Qwen 环境|旧说明|BiCIF|CTC/);
    }
    const text = (id: string) => page.container.querySelector(`[data-model-id="${id}"] .asr-model-features`)!.textContent!;
    expect(text("qwen3-asr-1.7b")).toContain("精度最高");
    expect(text("qwen3-asr-1.7b")).toContain("约 400 秒");
    expect(text("fireredasr2-aed")).toContain("约 200 秒");
    expect(text("paraformer-large-cpu")).toContain("GPU 约 40 秒 · CPU 约 60 秒");
    expect(text("paraformer-large-cpu")).toContain("收音嘈杂时不推荐");
    expect(text("dolphin-small-cn")).toContain("GPU 约 50 秒 · CPU 约 75 秒");
    expect(text("whisper-turbo")).toContain("不推荐用于中文课堂");
    expect(text("whisper-large-v3")).toContain("识别精度更高，但推理速度更慢");
    expect(page.container.querySelectorAll(".firered-device-controls")).toHaveLength(4);
    expect(page.container.querySelector('[data-model-id="qwen3-asr-1.7b"] .asr-fixed-device')?.textContent).toBe("GPU");
    expect(page.container.querySelector('[data-model-id="fireredasr2-aed"] input[value="cpu"]')).toBeNull();
  } finally { await page.dispose(); }
});

it("shows each engine's reported GPU and updates Qwen/FireRed observations without changing selection", async () => {
  vi.resetAllMocks();
  const models = fullCatalogue.map((model) => ({ ...model, gpu_available: true, gpu_name: `${model.engine} GPU` }));
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => models.find((model) => model.id === id)!);
  const page = mountSettings("/settings?section=asr", models);
  try {
    await waitFor(() => expect(page.container.querySelectorAll(".gpu-device-status")).toHaveLength(4));
    for (const engine of ["qwen", "firered", "paraformer", "dolphin"]) {
      const model = models.find((item) => item.engine === engine)!;
      await waitFor(() => expect(page.container.querySelector(`[data-model-id="${model.id}"] .gpu-device-status`)?.textContent).toBe(`${engine} GPU`));
    }
    const qwen = models.find((model) => model.engine === "qwen")!;
    act(() => page.client.setQueryData(["asr-status", qwen.id, qwen.device, true], { ...qwen, gpu_name: "New GPU" }));
    await waitFor(() => expect(page.container.querySelector(`[data-model-id="${qwen.id}"] .gpu-device-status`)?.textContent).toBe("New GPU"));
    const fireRed = models.find((model) => model.engine === "firered")!;
    act(() => page.client.setQueryData(["asr-status", fireRed.id, fireRed.device, true], {
      ...fireRed, gpu_available: false, gpu_unavailable_reason: "FireRed CUDA 不可用",
    }));
    await waitFor(() => expect(page.container.querySelector(`[data-model-id="${fireRed.id}"] .gpu-device-status`)?.textContent).toBe("FireRed CUDA 不可用"));
    expect(page.container.querySelector(".asr-current-choice")?.textContent).toContain("Whisper turbo");
    expect(api.selectAsrModel).not.toHaveBeenCalled();
  } finally { await page.dispose(); }
});

it.each([
  ["paraformer", "paraformer-large-cpu", true],
  ["paraformer", "paraformer-large-cpu", false],
  ["dolphin", "dolphin-small-cn", true],
  ["dolphin", "dolphin-small-cn", false],
] as const)("offers %s device controls before selection and keeps explicit CPU drafts (CUDA: %s, %s)", async (engine, modelId, available) => {
  vi.resetAllMocks();
  const models = fullCatalogue.map((model) => model.id === modelId ? {
    ...model, gpu_available: available, gpu_runtime_ready: available, gpu_prepare_available: available,
  } : model);
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => models.find((model) => model.id === id)!);
  vi.mocked(api.selectAsrModel).mockResolvedValue(undefined);
  const page = mountSettings("/settings?section=asr", models);
  const card = () => page.container.querySelector(`[data-model-id="${modelId}"]`)!;
  const save = () => [...page.container.querySelectorAll("button")].find((node) => node.textContent?.includes("保存模型选择"))!;
  try {
    await waitFor(() => expect(card()?.querySelector(`input[name="${engine}-device"]`)).not.toBeNull());
    const gpu = card().querySelector<HTMLInputElement>('input[value="gpu"]')!;
    const cpu = card().querySelector<HTMLInputElement>('input[value="cpu"]')!;
    await waitFor(() => expect(gpu.checked).toBe(available));
    expect(cpu.checked).toBe(!available);
    expect(gpu.disabled).toBe(!available);
    expect(save().disabled).toBe(true);
    act(() => gpu.click());
    expect(card().querySelector<HTMLInputElement>('input[name="asr-model"]')!.checked).toBe(available);
    expect(api.selectAsrModel).not.toHaveBeenCalled();
    act(() => cpu.click());
    expect(card().querySelector<HTMLInputElement>('input[name="asr-model"]')!.checked).toBe(true);
    expect(cpu.checked).toBe(true);
    // A model refresh may update observations, but must not discard the user's draft.
    act(() => page.client.setQueryData(["settings", "asr"], models.map((model) => ({ ...model, cache_size_bytes: 4096 }))));
    expect(cpu.checked).toBe(true);
    expect(card().querySelector<HTMLInputElement>('input[name="asr-model"]')!.checked).toBe(true);
    expect(save().disabled).toBe(false);
    act(() => save().click());
    await waitFor(() => expect(api.selectAsrModel).toHaveBeenCalledWith(...(
      engine === "dolphin" ? [modelId, undefined, undefined, undefined, "cpu"] : [modelId, undefined, "cpu"]
    )));
    expect(api.prepareAsrModel).not.toHaveBeenCalled();
  } finally { await page.dispose(); }
});

function mockDirectories() {
  vi.mocked(api.getStorageDirectories).mockResolvedValue({
    library_id: "test-library", data_root: "D:\\course", asr_storage_root: "D:\\assets", audio_cache_dir: "D:\\course\\audio",
    audio_cache_default_dir: "D:\\course\\temporary_audio", log_dir: "D:\\logs",
    video_library_dir: "D:\\course\\videos", video_library_default_dir: "D:\\course\\videos",
    model_dir: "D:\\assets\\models\\asr", runtime_dir: "D:\\assets\\.runtime", package_cache_dir: "D:\\assets\\.runtime\\cache",
    config_dir: "C:\\app\\data", active_asr_storage_root: "D:\\assets", active_log_dir: "D:\\logs",
    restart_required: false, busy: false, has_paused_jobs: false,
  });
}

it("requires explicit GPU migration for saved FireRed CPU without model preparation", async () => {
  vi.resetAllMocks();
  mockDirectories();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const model = {
    id: "fireredasr2-aed", name: "FireRedASR2S", engine: "firered",
    device: "cpu" as "gpu" | "cpu", selected: true, downloaded: true, cache_size_bytes: 1000,
  };
  vi.mocked(api.getAsrModels).mockImplementation(async () => [{ ...model }]);
  vi.mocked(api.getAsrModelStatus).mockImplementation(async () => ({ ...model }));
  vi.mocked(api.selectAsrModel).mockImplementation(async (_id, device) => {
    model.device = device ?? "gpu";
  });
  vi.mocked(api.getLibraryStatus).mockResolvedValue({
    data_root: "D:\\course", database_path: "D:\\course\\project.db", temp_audio_dir: "D:\\course\\audio",
    video_count: 0,
  });
  vi.mocked(api.getAiSettings).mockResolvedValue({
    provider: "deepseek", base_url: "https://api.deepseek.com", model: "model",
    output_tokens: 2048, token_parameter: "max_tokens", json_mode: true,
    timeout_seconds: 60, max_retries: 2, has_api_key: false,
  });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  async function settle(check: () => void) {
    let failure: unknown;
    for (let i = 0; i < 100; i++) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
      try { check(); return; } catch (error) { failure = error; }
    }
    throw failure;
  }
  const saveButton = () => [...container.querySelectorAll("button")]
    .find((button) => button.textContent?.includes("保存模型选择"))!;
  try {
    act(() => root.render(
      <QueryClientProvider client={client}><MemoryRouter initialEntries={["/settings?section=asr"]}><SettingsPage /></MemoryRouter></QueryClientProvider>,
    ));
    await settle(() => expect(container.querySelector<HTMLInputElement>('input[value="gpu"]')).not.toBeNull());
    expect(saveButton().disabled).toBe(true);
    expect(container.querySelector('input[value="cpu"]')).toBeNull();
    expect(container.querySelector<HTMLInputElement>('input[value="gpu"]')!.checked).toBe(false);
    expect(container.textContent).toContain("CPU 处理已禁用");
    expect(api.selectAsrModel).not.toHaveBeenCalled();
    act(() => container.querySelector<HTMLInputElement>('input[value="gpu"]')!.click());
    expect(saveButton().disabled).toBe(false);
    act(() => saveButton().click());
    await settle(() => expect(api.selectAsrModel).toHaveBeenCalledWith("fireredasr2-aed", "gpu"));
    await settle(() => expect(saveButton().disabled).toBe(true));
    expect(api.prepareAsrModel).not.toHaveBeenCalled();
  } finally {
    await act(async () => { root.unmount(); client.clear(); });
    container.remove();
  }
});

it.each([
  ["paraformer", "paraformer-large-cpu", true],
  ["paraformer", "paraformer-large-cpu", false],
  ["dolphin", "dolphin-small-cn", true],
  ["dolphin", "dolphin-small-cn", false],
] as const)("%s GPU selection is gated by runtime CUDA availability (%s, %s)", async (engine, modelId, available) => {
  vi.resetAllMocks();
  mockDirectories();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const model = {
    id: modelId, name: engine === "dolphin" ? "Dolphin" : "Paraformer-large", engine,
    device: "cpu" as "gpu" | "cpu", selected: true, downloaded: true, cache_size_bytes: 1000,
    gpu_available: available, gpu_runtime_ready: available, gpu_prepare_available: available,
    gpu_name: available ? "Test GPU" : undefined,
  };
  vi.mocked(api.getAsrModels).mockImplementation(async () => [{ ...model }]);
  vi.mocked(api.getAsrModelStatus).mockImplementation(async () => ({ ...model }));
  vi.mocked(api.selectAsrModel).mockImplementation(async (_id, _fireRedDevice, device, nanoDevice, dolphinDevice) => {
    model.device = (engine === "dolphin" ? dolphinDevice : device) ?? "cpu";
  });
  vi.mocked(api.getLibraryStatus).mockResolvedValue({
    data_root: "D:\\course", database_path: "D:\\course\\project.db", temp_audio_dir: "D:\\course\\audio",
    video_count: 0,
  });
  vi.mocked(api.getAiSettings).mockResolvedValue({
    provider: "deepseek", base_url: "https://api.deepseek.com", model: "model",
    output_tokens: 2048, token_parameter: "max_tokens", json_mode: true,
    timeout_seconds: 60, max_retries: 2, has_api_key: false,
  });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  async function settle(check: () => void) {
    let failure: unknown;
    for (let i = 0; i < 100; i++) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
      try { check(); return; } catch (error) { failure = error; }
    }
    throw failure;
  }
  const save = () => [...container.querySelectorAll("button")]
    .find((button) => button.textContent?.includes("保存模型选择"))!;
  try {
    act(() => root.render(
      <QueryClientProvider client={client}><MemoryRouter initialEntries={["/settings?section=asr"]}><SettingsPage /></MemoryRouter></QueryClientProvider>,
    ));
    await settle(() => expect(container.querySelector<HTMLInputElement>(`input[name="${engine}-device"][value="cpu"]`)?.checked).toBe(true));
    const gpu = container.querySelector<HTMLInputElement>(`input[name="${engine}-device"][value="gpu"]`)!;
    expect(gpu.disabled).toBe(!available);
    expect(save().disabled).toBe(true);
    act(() => gpu.click());
    if (available) {
      expect(gpu.checked).toBe(true);
      expect(save().disabled).toBe(false);
      act(() => save().click());
      await settle(() => {
        if (engine === "dolphin") {
          expect(api.selectAsrModel).toHaveBeenCalledWith(modelId, undefined, undefined, undefined, "gpu");
        } else {
          expect(api.selectAsrModel).toHaveBeenCalledWith(modelId, undefined, "gpu");
        }
      });
      await settle(() => expect(save().disabled).toBe(true));
    } else {
      expect(gpu.checked).toBe(false);
      expect(api.selectAsrModel).not.toHaveBeenCalled();
    }
    expect(api.prepareAsrModel).not.toHaveBeenCalled();
  } finally {
    await act(async () => { root.unmount(); client.clear(); });
    container.remove();
  }
});

it.each(["cpu", "gpu"] as const)("hides Nano from an old backend without a retirement notice (%s)", async (device) => {
  vi.resetAllMocks();
  mockDirectories();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  const models = [
    { id: "fun-asr-nano-2512", name: "Fun-ASR-Nano", engine: "nano",
      device, selected: true, downloaded: true, cache_size_bytes: 1000 },
    { id: "paraformer-large-cpu", name: "Paraformer-large", engine: "paraformer",
      device: "cpu" as const, selected: false, downloaded: true, cache_size_bytes: 1000 },
    { id: "dolphin-small-cn", name: "Dolphin", engine: "dolphin",
      device: "cpu" as const, selected: false, downloaded: true, cache_size_bytes: 1000 },
  ];
  vi.mocked(api.getAsrModels).mockResolvedValue(models);
  vi.mocked(api.getAsrModelStatus).mockImplementation(async (id) => models.find((model) => model.id === id)!);
  vi.mocked(api.selectAsrModel).mockResolvedValue(undefined);
  vi.mocked(api.getLibraryStatus).mockResolvedValue({
    data_root: "D:\\course", database_path: "D:\\course\\project.db", temp_audio_dir: "D:\\course\\audio",
    video_count: 0,
  });
  vi.mocked(api.getAiSettings).mockResolvedValue({
    provider: "deepseek", base_url: "https://api.deepseek.com", model: "model",
    output_tokens: 2048, token_parameter: "max_tokens", json_mode: true,
    timeout_seconds: 60, max_retries: 2, has_api_key: false,
  });
  const container = document.createElement("div");
  document.body.append(container);
  const root = createRoot(container);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  try {
    act(() => root.render(
      <QueryClientProvider client={client}><MemoryRouter initialEntries={["/settings?section=asr"]}><SettingsPage /></MemoryRouter></QueryClientProvider>,
    ));
    for (let i = 0; i < 100 && container.querySelectorAll('input[name="asr-model"]').length !== 2; i++) {
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 15)); });
    }
    const radios = container.querySelectorAll<HTMLInputElement>('input[name="asr-model"]');
    expect(radios).toHaveLength(2);
    expect(container.textContent).not.toContain("Nano");
    expect(container.textContent).not.toContain("已下架");
    expect(container.querySelector('input[name="nano-device"]')).toBeNull();
    expect(api.getAsrModelStatus).not.toHaveBeenCalledWith("fun-asr-nano-2512");
    expect(api.selectAsrModel).not.toHaveBeenCalled();
    const save = [...container.querySelectorAll("button")]
      .find((button) => button.textContent?.includes("保存模型选择"))!;
    expect(save.disabled).toBe(true);
    act(() => radios[0].click());
    expect(save.disabled).toBe(false);
    await act(async () => { save.click(); await new Promise((resolve) => setTimeout(resolve, 30)); });
    expect(api.selectAsrModel).toHaveBeenCalledWith("paraformer-large-cpu", undefined, "cpu");
    expect(api.prepareAsrModel).not.toHaveBeenCalled();
    expect(api.clearAsrModel).not.toHaveBeenCalled();
  } finally {
    await act(async () => { root.unmount(); client.clear(); });
    container.remove();
  }
});
