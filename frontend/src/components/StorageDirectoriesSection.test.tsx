import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { StorageDirectoriesSection } from "./StorageDirectoriesSection";
import { createPicker, getJobStatus, getStorageDirectories, pollPicker, previewStorageMigration, saveStorageDirectories, startStorageMigration } from "../api/client";
import type { StorageDirectories } from "../types";

vi.mock("../api/client", () => ({
  createPicker: vi.fn(), pollPicker: vi.fn(), saveStorageDirectories: vi.fn(),
  getJobStatus: vi.fn(), getStorageDirectories: vi.fn(), previewStorageMigration: vi.fn(), startStorageMigration: vi.fn(),
}));

const data: StorageDirectories = {
  media_storage_root: "D:\\course", media_storage_bound: false, media_storage_default_root: "D:\\course",
  library_id: "test-library", data_root: "D:\\course", asr_storage_root: "D:\\assets", audio_cache_dir: "D:\\course\\temporary_audio",
  audio_cache_default_dir: "D:\\course\\temporary_audio", log_dir: "D:\\logs",
  video_library_dir: "D:\\course\\videos", video_library_default_dir: "D:\\course\\videos",
  model_dir: "D:\\assets\\models\\asr", runtime_dir: "D:\\assets\\.runtime", package_cache_dir: "D:\\assets\\.runtime\\cache",
  config_dir: "C:\\app\\data", active_asr_storage_root: "D:\\assets", active_log_dir: "D:\\logs",
  restart_required: false, busy: false, has_paused_jobs: false,
};
let container: HTMLDivElement, root: Root, client: QueryClient;
const onSuccess = vi.fn(), onError = vi.fn(), onRetry = vi.fn();

function render(value = data, flags = {}) {
  act(() => root.render(<QueryClientProvider client={client}><StorageDirectoriesSection
    data={value} isLoading={false} isError={false} onSuccess={onSuccess} onError={onError} onRetry={onRetry} {...flags}
  /></QueryClientProvider>));
}
const input = (label: string) => container.querySelector<HTMLInputElement>(`input[aria-label="${label}"]`)!;
const save = () => [...container.querySelectorAll("button")].find((button) => /保存设置|检查并迁移/.test(button.textContent ?? ""))!;
const plan = { plan_id: "plan", files_total: 2, bytes_total: 1024,
  sections: [{ label: "临时音频与转写缓存", source: data.audio_cache_dir, target: "E:\\cache", files: 2, bytes: 1024 }],
  disks: [{ path: "E:\\", required_bytes: 1024, free_bytes: 4096 }], warnings: ["旧运行环境保留"] };
function change(label: string, value: string) {
  act(() => {
    const field = input(label);
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(field, value);
    field.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function settle(check: () => void) {
  await vi.waitFor(async () => {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 5)); });
    check();
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(previewStorageMigration).mockResolvedValue(plan);
  vi.mocked(startStorageMigration).mockResolvedValue({ job_id: "move-1" });
  vi.mocked(getJobStatus).mockResolvedValue({ id: "move-1", status: "completed", progress: 1, message: "目录迁移完成" });
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  render();
});
afterEach(async () => {
  await act(async () => { root.unmount(); client.clear(); });
  container.remove();
});

it("shows saved paths and derived directories without changing anything", () => {
  expect(input("ASR 资源目录").value).toBe(data.asr_storage_root);
  expect(input("视频与音频存储目录").value).toBe(data.media_storage_root);
  expect(container.querySelector('input[aria-label="视频库目录"]')).toBeNull();
  expect(container.querySelector('input[aria-label="临时音频与转写缓存"]')).toBeNull();
  expect(container.textContent).toContain(data.video_library_dir);
  expect(container.textContent).toContain(data.audio_cache_dir);
  expect(container.textContent).toContain(data.package_cache_dir);
  expect(container.textContent).toContain("下方“应用数据”中迁移");
  expect(save().disabled).toBe(true);
  expect(saveStorageDirectories).not.toHaveBeenCalled();
});

it("previews trimmed paths and starts a reviewed background migration before displaying the persisted result", async () => {
  change("视频与音频存储目录", " E:\\cache ");
  expect(save().disabled).toBe(false);
  const result = { ...data, media_storage_root: "E:\\cache", media_storage_bound: true,
    audio_cache_dir: "E:\\cache\\audio", video_library_dir: "E:\\cache\\videos" };
  vi.mocked(getStorageDirectories).mockResolvedValue(result);
  onSuccess.mockImplementation((value) => render(value));
  await act(async () => { save().click(); await new Promise((resolve) => setTimeout(resolve, 20)); });
  await settle(() => expect(container.textContent).toContain("确认迁移范围"));
  expect(startStorageMigration).not.toHaveBeenCalled();
  expect(saveStorageDirectories).not.toHaveBeenCalled();
  await act(async () => { [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("开始迁移"))!.click(); });
  await settle(() => expect(onSuccess).toHaveBeenCalledWith(result));
  expect(startStorageMigration).toHaveBeenCalledWith({
    asr_storage_root: data.asr_storage_root, media_storage_root: "E:\\cache", log_dir: data.log_dir, library_id: data.library_id,
    delete_source_after_import: true,
    plan_id: "plan",
  });
  expect(input("视频与音频存储目录").value).toBe("E:\\cache");
  expect(save().disabled).toBe(true);
  expect(container.querySelector('[aria-label="目录迁移进度"]')?.textContent).toContain("目录迁移完成");
  await act(async () => { root.unmount(); });
  root = createRoot(container);
  render({ ...result, migration_job: { id: "move-1", status: "completed", progress: 1, message: "目录迁移完成" } });
  expect(container.querySelector('[aria-label="目录迁移进度"]')).toBeNull();
  expect(input("视频与音频存储目录").value).toBe("E:\\cache");
  expect(getJobStatus).toHaveBeenCalledTimes(1);
});

it("keeps the edit and reports a failed preflight instead of claiming success", async () => {
  change("诊断日志目录", "E:\\logs");
  const error = new Error("Access denied");
  vi.mocked(previewStorageMigration).mockRejectedValue(error);
  await act(async () => { save().click(); await new Promise((resolve) => setTimeout(resolve, 20)); });
  await settle(() => expect(onError).toHaveBeenCalledWith(error));
  expect(onSuccess).not.toHaveBeenCalled();
  expect(input("诊断日志目录").value).toBe("E:\\logs");
  expect(save().disabled).toBe(false);
});

it("uses the native directory picker but waits for explicit save", async () => {
  vi.mocked(createPicker).mockResolvedValue("picker-1");
  vi.mocked(pollPicker).mockResolvedValue("E:\\assets");
  await act(async () => { container.querySelector<HTMLButtonElement>('[aria-label="选择ASR 资源目录"]')!.click(); });
  await settle(() => expect(input("ASR 资源目录").value).toBe("E:\\assets"));
  expect(createPicker).toHaveBeenCalledWith("directory");
  expect(pollPicker).toHaveBeenCalledWith("picker-1");
  expect(saveStorageDirectories).not.toHaveBeenCalled();
});

it("one directory picker derives both videos and audio and migrates them together", async () => {
  vi.mocked(createPicker).mockResolvedValue("picker-media");
  vi.mocked(pollPicker).mockResolvedValue("E:\\media");
  await act(async () => { container.querySelector<HTMLButtonElement>('[aria-label="选择视频与音频存储目录"]')!.click(); });
  await settle(() => expect(input("视频与音频存储目录").value).toBe("E:\\media"));
  expect(container.textContent).toContain("E:\\media\\videos");
  expect(container.textContent).toContain("E:\\media\\audio");
  const result = { ...data, media_storage_root: "E:\\media", media_storage_bound: true,
    video_library_dir: "E:\\media\\videos", audio_cache_dir: "E:\\media\\audio" };
  vi.mocked(getStorageDirectories).mockResolvedValue(result);
  onSuccess.mockImplementation((value) => render(value));
  await act(async () => { save().click(); });
  await settle(() => expect(container.textContent).toContain("确认迁移范围"));
  await act(async () => { [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("开始迁移"))!.click(); });
  await settle(() => expect(onSuccess).toHaveBeenCalledWith(result));
  expect(startStorageMigration).toHaveBeenCalledWith({
    asr_storage_root: data.asr_storage_root, media_storage_root: "E:\\media",
    log_dir: data.log_dir, library_id: data.library_id,
    delete_source_after_import: true,
    plan_id: "plan",
  });
  expect(input("视频与音频存储目录").value).toBe("E:\\media");
});

it("preserves paths when the directory picker is canceled", async () => {
  vi.mocked(createPicker).mockResolvedValue("picker-1");
  vi.mocked(pollPicker).mockResolvedValue(null);
  await act(async () => { container.querySelector<HTMLButtonElement>('[aria-label="选择ASR 资源目录"]')!.click(); });
  await settle(() => expect(container.querySelector("fieldset")!.disabled).toBe(false));
  expect(input("ASR 资源目录").value).toBe(data.asr_storage_root);
  expect(save().disabled).toBe(true);
});

it("restores the default parent directory for both videos and audio and can revert", () => {
  render({ ...data, media_storage_root: "E:\\custom", media_storage_bound: true,
    video_library_dir: "E:\\custom\\videos", audio_cache_dir: "E:\\custom\\audio" });
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="恢复默认存储目录"]')!.click());
  expect(input("视频与音频存储目录").value).toBe(data.media_storage_default_root);
  expect(container.textContent).toContain("D:\\course\\videos");
  expect(container.textContent).toContain("D:\\course\\audio");
  expect(save().disabled).toBe(false);
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="撤销未保存的目录修改"]')!.click());
  expect(input("视频与音频存储目录").value).toBe("E:\\custom");
  expect(save().disabled).toBe(true);
});

it.each([false, true])("locks changes while jobs are active (paused=%s)", (paused) => {
  render({ ...data, busy: true, has_paused_jobs: paused });
  expect(container.querySelector("fieldset")!.disabled).toBe(true);
  expect(save().disabled).toBe(true);
  expect(container.textContent).toContain("任务未结束");
  if (paused) expect(container.textContent).toContain("暂停的任务仍依赖原缓存");
});

it("shows pending restart paths and keeps drafts during background status refresh", () => {
  change("诊断日志目录", "E:\\draft");
  render({ ...data, restart_required: true, active_asr_storage_root: "C:\\old-assets", busy: true });
  expect(input("诊断日志目录").value).toBe("E:\\draft");
  expect(container.textContent).toContain("C:\\old-assets");
  render({ ...data, restart_required: true });
  expect(container.textContent).toContain("已保存，待重启生效");
});

it("does not save blank paths and supports retry after loading failure", () => {
  change("ASR 资源目录", "   ");
  expect(save().disabled).toBe(true);
  render(data, { isError: true });
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="重新读取存储目录"]')!.click());
  expect(onRetry).toHaveBeenCalledOnce();
});

it("defaults to moving videos and saves an unchecked copy-only preference", async () => {
  const checkbox = input("导入后删除原文件");
  expect(checkbox.checked).toBe(true);
  act(() => checkbox.click());
  expect(checkbox.checked).toBe(false);
  expect(save().disabled).toBe(false);
  const result = { ...data, delete_source_after_import: false };
  vi.mocked(saveStorageDirectories).mockResolvedValue(result);
  onSuccess.mockImplementation((value) => render(value));
  await act(async () => { save().click(); });
  await settle(() => expect(onSuccess).toHaveBeenCalledWith(result));
  expect(saveStorageDirectories).toHaveBeenCalledWith(expect.objectContaining({ delete_source_after_import: false }));
  expect(input("导入后删除原文件").checked).toBe(false);
  expect(save().disabled).toBe(true);
});

it("preserves copy-only mode when editing directories and reverting drafts", () => {
  render({ ...data, delete_source_after_import: false });
  change("视频与音频存储目录", "E:\\videos");
  expect(input("导入后删除原文件").checked).toBe(false);
  act(() => input("导入后删除原文件").click());
  expect(input("导入后删除原文件").checked).toBe(true);
  act(() => container.querySelector<HTMLButtonElement>('[aria-label="撤销未保存的目录修改"]')!.click());
  expect(input("导入后删除原文件").checked).toBe(false);
  expect(input("视频与音频存储目录").value).toBe(data.media_storage_root);
  expect(save().disabled).toBe(true);
});

it("invalidates the reviewed plan if the destination is edited", async () => {
  change("视频与音频存储目录", "E:\\cache");
  await act(async () => { save().click(); });
  await settle(() => expect(container.textContent).toContain("确认迁移范围"));
  change("视频与音频存储目录", "F:\\cache");
  expect(container.textContent).not.toContain("确认迁移范围");
  expect(startStorageMigration).not.toHaveBeenCalled();
});

it("recovers running progress after remount and disables editing until it finishes", async () => {
  const migration = { phase: "copying" as const, files_completed: 1, files_total: 2, bytes_completed: 512,
    bytes_total: 1024, warnings: [], current_file: "lesson.wav" };
  vi.mocked(getJobStatus).mockResolvedValue({ id: "running", status: "running", progress: .45, migration });
  render({ ...data, busy: true, migration_job: { id: "running", status: "running", migration } });
  await settle(() => expect(getJobStatus).toHaveBeenCalledWith("running"));
  expect(container.querySelector("fieldset")!.disabled).toBe(true);
  expect(container.textContent).toContain("lesson.wav");
  expect(container.textContent).toContain("1 / 2");
});

it("retains draft and reports a background migration failure", async () => {
  vi.mocked(getJobStatus).mockResolvedValue({ id: "move-1", status: "failed", error_message: "磁盘空间不足" });
  change("视频与音频存储目录", "E:\\cache");
  await act(async () => { save().click(); });
  await settle(() => expect(container.textContent).toContain("确认迁移范围"));
  await act(async () => { [...container.querySelectorAll("button")].find((button) => button.textContent?.includes("开始迁移"))!.click(); });
  await settle(() => expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: "磁盘空间不足" })));
  expect(input("视频与音频存储目录").value).toBe("E:\\cache");
  expect(onSuccess).not.toHaveBeenCalled();
});

it("hides historical completed migration progress without polling or overwriting fresh edits", () => {
  render({ ...data, migration_job: { id: "old-move", status: "completed" } });
  expect(container.querySelector('[aria-label="目录迁移进度"]')).toBeNull();
  expect(getJobStatus).not.toHaveBeenCalled();
  change("视频与音频存储目录", "E:\\draft");
  expect(getStorageDirectories).not.toHaveBeenCalled();
  expect(input("视频与音频存储目录").value).toBe("E:\\draft");
});

it.each(["failed", "interrupted"])("keeps historical %s migration errors visible after restarting", (status) => {
  render({ ...data, migration_job: { id: "old-move", status, error_message: "迁移未完成" } });
  expect(container.querySelector('[aria-label="目录迁移进度"]')?.textContent).toContain("迁移未完成");
  expect(getJobStatus).not.toHaveBeenCalled();
  expect(container.querySelector("fieldset")!.disabled).toBe(false);
});

it("keeps legacy split directories until a parent is explicitly selected", () => {
  render({ ...data, media_storage_root: null, video_library_dir: "E:\\old-videos", audio_cache_dir: "F:\\old-audio" });
  expect(input("视频与音频存储目录").value).toBe("");
  expect(container.textContent).toContain("E:\\old-videos");
  expect(container.textContent).toContain("F:\\old-audio");
  expect(save().disabled).toBe(true);
  change("视频与音频存储目录", "G:\\media");
  expect(container.textContent).toContain("G:\\media\\videos");
  expect(container.textContent).toContain("G:\\media\\audio");
  expect(save().disabled).toBe(false);
});

it("rejects an empty parent draft without starting a migration", () => {
  change("视频与音频存储目录", "   ");
  expect(save().disabled).toBe(true);
  expect(previewStorageMigration).not.toHaveBeenCalled();
  expect(startStorageMigration).not.toHaveBeenCalled();
});
