import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApplicationDataSection } from "./ApplicationDataSection";
import { getStorageDirectories, migrateApplicationData, previewApplicationData } from "../api/client";
import type { StorageDirectories } from "../types";

vi.mock("../api/client", () => ({
  createPicker: vi.fn(), pollPicker: vi.fn(), getStorageDirectories: vi.fn(),
  migrateApplicationData: vi.fn(), previewApplicationData: vi.fn(),
}));
const data = {
  library_id: "library", busy: false,
  application_data: { root: null, database_dir: "D:\\old", config_dir: "C:\\app\\data",
    database_bytes: 1024, config_bytes: 100, migration: null },
} as StorageDirectories;
const plan = { destination: "E:\\small", database_dir: "E:\\small\\library", config_dir: "E:\\small\\config",
  files_total: 3, bytes_total: 1124, free_bytes: 99999, plan_id: "reviewed" };
let container: HTMLDivElement, root: ReturnType<typeof createRoot>, client: QueryClient;
const onError = vi.fn();
function change(value: string) {
  act(() => {
    const input = container.querySelector<HTMLInputElement>('input[aria-label="应用数据目录"]')!;
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}
async function openConfirmation() {
  change("E:\\small");
  await act(async () => {
    const button = [...container.querySelectorAll("button")].find(b => b.textContent?.includes("检查迁移范围"))!;
    button.focus(); button.click();
  });
  await vi.waitFor(() => expect(document.querySelector('[role="dialog"]')).toBeTruthy());
}
beforeEach(() => {
  vi.resetAllMocks(); sessionStorage.clear();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(previewApplicationData).mockResolvedValue(plan);
  vi.mocked(migrateApplicationData).mockResolvedValue({ id: "migration", status: "restarting" });
  vi.mocked(getStorageDirectories).mockResolvedValue({ ...data, application_data: {
    ...data.application_data!, migration: { id: "migration", status: "pending", message: "正在重启", destination: "E:\\small" },
  } });
  container = document.createElement("div"); document.body.append(container);
  root = createRoot(container); client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => root.render(<QueryClientProvider client={client}><ApplicationDataSection directories={data} onError={onError} /></QueryClientProvider>));
});
afterEach(async () => {
  await act(async () => { root.unmount(); client.clear(); });
  container.remove(); sessionStorage.clear();
});

it("warns in a modal and does not restart on choosing or previewing a directory", async () => {
  await openConfirmation();
  const dialog = document.querySelector('[role="dialog"]')!;
  expect(dialog.getAttribute("aria-modal")).toBe("true");
  expect(dialog.textContent).toContain("软件会自动立即重启");
  expect(dialog.textContent).toContain("E:\\small\\library");
  expect(dialog.textContent).toContain("E:\\small\\config");
  expect(migrateApplicationData).not.toHaveBeenCalled();
  expect(document.activeElement?.textContent).toBe("取消");
});

it("cancels the restart warning without requesting migration", async () => {
  await openConfirmation();
  await act(async () => { [...document.querySelectorAll("button")].find(b => b.textContent === "取消")!.click(); });
  expect(migrateApplicationData).not.toHaveBeenCalled();
  expect(container.querySelector<HTMLInputElement>('input[aria-label="应用数据目录"]')!.value).toBe("E:\\small");
  expect(document.activeElement?.textContent).toBe("检查迁移范围");
});

it("traps keyboard focus and allows Escape to cancel without migration", async () => {
  await openConfirmation();
  act(() => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true })); });
  expect(document.activeElement?.textContent).toBe("迁移并立即重启");
  act(() => { document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })); });
  expect(document.activeElement?.textContent).toBe("检查迁移范围");
  expect(migrateApplicationData).not.toHaveBeenCalled();
});

it("only explicitly confirming starts migration and remembers automatic reconnection across reloads", async () => {
  await openConfirmation();
  await act(async () => { [...document.querySelectorAll("button")].find(b => b.textContent === "迁移并立即重启")!.click(); });
  await vi.waitFor(() => expect(migrateApplicationData).toHaveBeenCalledWith({
    destination: "E:\\small", library_id: "library", plan_id: "reviewed",
  }));
  await vi.waitFor(() => expect(sessionStorage.getItem("classroom-application-data-restart")).toContain("migration"));
  expect(container.textContent).toContain("等待自动重连");
});

it("restores interactivity after reconnecting to a completed migration", async () => {
  vi.mocked(getStorageDirectories).mockResolvedValue({ ...data, application_data: {
    ...data.application_data!, root: "E:\\small",
    migration: { id: "migration", status: "completed", message: "", destination: "E:\\small" },
  } });
  await openConfirmation();
  await act(async () => { [...document.querySelectorAll("button")].find(b => b.textContent === "迁移并立即重启")!.click(); });
  await vi.waitFor(() => expect(container.textContent).toContain("应用数据已迁移，软件已重新启动"));
  expect(sessionStorage.getItem("classroom-application-data-restart")).toBeNull();
  expect(container.querySelector("fieldset")!.disabled).toBe(false);
});

it("reports a failed migration after restarting with the original data", async () => {
  vi.mocked(getStorageDirectories).mockResolvedValue({ ...data, application_data: {
    ...data.application_data!,
    migration: { id: "migration", status: "failed", message: "目标文件冲突", destination: "E:\\small" },
  } });
  await openConfirmation();
  await act(async () => { [...document.querySelectorAll("button")].find(b => b.textContent === "迁移并立即重启")!.click(); });
  await vi.waitFor(() => expect(onError).toHaveBeenCalledWith(expect.objectContaining({ message: "目标文件冲突" })));
  expect(sessionStorage.getItem("classroom-application-data-restart")).toBeNull();
  expect(container.querySelector<HTMLInputElement>('input[aria-label="应用数据目录"]')!.value).toBe("E:\\small");
});
