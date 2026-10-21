import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import type { AiSettings, AsrModel, StorageDirectories } from "../types";
import { UnsavedChangesProvider } from "../components/UnsavedChanges";
import { SettingsPage } from "./SettingsPage";

vi.mock("../api/client");
let host: HTMLDivElement, root: Root, client: QueryClient, router: ReturnType<typeof createMemoryRouter>;
let ai: AiSettings, models: AsrModel[], storage: StorageDirectories;
const onSaved = vi.fn();
beforeEach(() => {
  vi.resetAllMocks();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  HTMLElement.prototype.scrollIntoView = vi.fn();
  ai = { provider: "openai_compatible", connection_id: "first", connection_name: "测试连接",
    base_url: "https://fixture.test/v1", model: "saved-model", output_tokens: 16384,
    token_parameter: "max_tokens", json_mode: false, timeout_seconds: 120, max_retries: 5, has_api_key: true,
    connections: [{ id: "first", name: "测试连接", model: "saved-model", base_url: "https://fixture.test/v1", has_api_key: true }] };
  models = [
    { id: "paraformer-large", name: "Paraformer", engine: "paraformer", selected: true, device: "gpu", gpu_available: true, downloaded: true, cache_size_bytes: 100 },
    { id: "dolphin-small-cn", name: "Dolphin", engine: "dolphin", selected: false, device: "gpu", gpu_available: true, downloaded: true, cache_size_bytes: 100 },
  ];
  storage = { library_id: "fixture", data_root: "D:/course", asr_storage_root: "D:/assets",
    media_storage_root: "D:/course", media_storage_bound: true, video_library_dir: "D:/course/videos",
    audio_cache_dir: "D:/course/audio", audio_cache_default_dir: "D:/course/audio", log_dir: "D:/logs",
    video_library_default_dir: "D:/course/videos", model_dir: "D:/assets/models", runtime_dir: "D:/assets/runtime",
    package_cache_dir: "D:/assets/cache", config_dir: "D:/config", active_asr_storage_root: "D:/assets",
    active_log_dir: "D:/logs", restart_required: false, busy: false, has_paused_jobs: false,
    delete_source_after_import: true, application_data: { root: null, database_dir: "D:/course", config_dir: "D:/config",
      database_bytes: 100, config_bytes: 100, migration: null } };
  vi.mocked(api.getAiSettings).mockImplementation(async () => ai);
  vi.mocked(api.saveAiSettings).mockImplementation(async settings => { ai = { ...settings, has_api_key: true }; return ai; });
  vi.mocked(api.getAsrModels).mockImplementation(async () => models);
  vi.mocked(api.getAsrModelStatus).mockImplementation(async id => models.find(model => model.id === id)!);
  vi.mocked(api.selectAsrModel).mockImplementation(async (id, _fireRed, paraformer, _qwen, dolphin) => {
    models = models.map(model => ({ ...model, selected: model.id === id,
      device: model.id === id ? paraformer ?? dolphin ?? model.device : model.device }));
  });
  vi.mocked(api.getStorageDirectories).mockImplementation(async () => storage);
  vi.mocked(api.saveStorageDirectories).mockImplementation(async values => { storage = { ...storage, ...values }; return storage; });
  vi.mocked(api.previewStorageMigration).mockResolvedValue({ plan_id: "storage-plan", files_total: 1, bytes_total: 100,
    sections: [], disks: [], warnings: [] });
  vi.mocked(api.previewApplicationData).mockResolvedValue({ plan_id: "app-plan", destination: "E:/new",
    database_dir: "E:/new/library", config_dir: "E:/new/config", files_total: 2, bytes_total: 200, free_bytes: 10000 });
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
});
afterEach(async () => {
  await act(async () => { root.unmount(); client.clear(); router.dispose(); });
  host.remove(); vi.restoreAllMocks();
});
async function mount(section: string) {
  router = createMemoryRouter([{ path: "*", element: <QueryClientProvider client={client}><UnsavedChangesProvider>
    <SettingsPage onConfigurationSaved={onSaved} />
  </UnsavedChangesProvider></QueryClientProvider> }], { initialEntries: [`/settings?section=${section}`] });
  await act(async () => root.render(<RouterProvider router={router} />));
  await settle(() => expect(host.querySelector(".settings-loading")).toBeNull());
}
async function settle(check: () => void) {
  await vi.waitFor(async () => { await act(async () => { await new Promise(resolve => setTimeout(resolve, 5)); }); check(); });
}
async function click(text: string) {
  const button = [...document.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent?.trim() === text)!;
  expect(button, text).toBeDefined(); await act(async () => button.click());
}
function input(label: string) {
  return host.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)
    ?? [...host.querySelectorAll("label")].find(item => item.textContent === label)!.querySelector<HTMLInputElement>("input")!;
}
async function change(label: string, value: string) {
  await act(async () => {
    const field = input(label);
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
it("keeps an AI draft and key when save fails, retries once, and resumes only the requested category", async () => {
  await mount("ai"); await change("模型名称", "draft-model"); await change("API Key", "fixture-secret");
  await click("快捷键");
  expect(router.state.location.search).toBe("?section=ai");
  vi.mocked(api.saveAiSettings).mockRejectedValueOnce(new Error("fixture save failure"));
  await click("保存并离开");
  expect(document.querySelector('.unsaved-dialog [role="alert"]')?.textContent).toContain("fixture save failure");
  expect(input("模型名称").value).toBe("draft-model"); expect(input("API Key").value).toBe("fixture-secret");
  await click("保存并离开");
  expect(router.state.location.search).toBe("?section=shortcuts");
  expect(api.saveAiSettings).toHaveBeenLastCalledWith(expect.objectContaining({ model: "draft-model" }), { api_key: "fixture-secret" });
  expect(api.saveAiSettings).toHaveBeenCalledTimes(2); expect(onSaved).not.toHaveBeenCalled();
  await click("AI 服务"); expect(document.querySelector(".unsaved-dialog")).toBeNull();
});
it("discards AI edits without saving, including a newly created connection", async () => {
  await mount("ai"); await change("模型名称", "unsaved"); await click("存储与导入"); await click("放弃修改");
  await click("AI 服务"); expect(input("模型名称").value).toBe("saved-model");
  await click("新增"); await click("快捷键"); await click("放弃修改"); await click("AI 服务");
  expect(input("模型名称").value).toBe("saved-model");
  expect(api.saveAiSettings).not.toHaveBeenCalled(); expect(api.createAiConnection).not.toHaveBeenCalled();
});
it("guards ASR device changes and does not prompt again after saving", async () => {
  await mount("asr");
  const cpu = () => host.querySelector<HTMLInputElement>('[data-model-id="paraformer-large"] input[value="cpu"]')!;
  await act(async () => cpu().click()); await click("快捷键"); await click("放弃修改"); await click("ASR 模型");
  expect(cpu().checked).toBe(false); expect(api.selectAsrModel).not.toHaveBeenCalled();
  await act(async () => cpu().click()); await click("快捷键"); await click("保存并离开");
  expect(api.selectAsrModel).toHaveBeenCalledWith("paraformer-large", undefined, "cpu");
  expect(router.state.location.search).toBe("?section=shortcuts"); expect(onSaved).not.toHaveBeenCalled();
  await click("ASR 模型"); await click("快捷键"); expect(document.querySelector(".unsaved-dialog")).toBeNull();
});
it("saves ordinary storage preferences but requires a separate confirmation for moving files", async () => {
  await mount("storage"); await act(async () => input("导入后删除原文件").click());
  await click("快捷键"); await click("保存并离开");
  expect(api.saveStorageDirectories).toHaveBeenCalledWith(expect.objectContaining({ delete_source_after_import: false }));
  await click("存储与导入"); await change("视频与音频存储目录", "E:/new");
  await click("快捷键"); await click("保存并离开");
  expect(router.state.location.search).toBe("?section=storage");
  expect(host.querySelector('[aria-label="迁移确认"]')).not.toBeNull();
  expect(api.previewStorageMigration).toHaveBeenCalledTimes(1); expect(api.startStorageMigration).not.toHaveBeenCalled();
  await click("快捷键"); await click("放弃修改"); await click("存储与导入");
  expect(input("视频与音频存储目录").value).toBe("D:/course");
});
it("keeps the application data restart confirmation before any migration can begin", async () => {
  await mount("storage"); await change("应用数据目录", "E:/new");
  await click("快捷键"); await click("保存并离开");
  expect(router.state.location.search).toBe("?section=storage");
  expect(document.querySelector('[aria-labelledby="application-data-confirm-title"]')).not.toBeNull();
  expect(api.migrateApplicationData).not.toHaveBeenCalled();
  await click("取消"); await click("快捷键"); await click("放弃修改");
  expect(input("应用数据目录").value).toBe("");
});
