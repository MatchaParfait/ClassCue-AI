import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { SettingsPage } from "./SettingsPage";
import * as api from "../api/dshBridge";
import * as pickers from "../api/client";

const desktop = vi.hoisted(() => ({
  enabled: true,
  api: { describe: vi.fn(async () => ({ hotkey: "", tray: true, close_behavior: "ask" })) },
}));
vi.mock("../hooks/useDesktopAPI", () => ({ useDesktopAPI: () => desktop.enabled ? desktop.api : null }));
vi.mock("../api/dshBridge", () => ({
  getDshStatus: vi.fn(), getDshWorkspaceRoot: vi.fn(), setDshWorkspaceRoot: vi.fn(),
  connectDsh: vi.fn(), connectDshAuto: vi.fn(), prepareDshQuestion: vi.fn(), getDshDiscussions: vi.fn(),
}));
vi.mock("../api/client", async original => ({
  ...await original<typeof import("../api/client")>(), createPicker: vi.fn(), pollPicker: vi.fn(),
}));
const connection = { protocol: 1, desktop_version: "0.2.1-rc.5", instance_id: "desktop",
  client_connected: true, capabilities: ["workspace_tree"] };
const saved = { config_id: "root", instance_id: "desktop", path: "synthetic-root", workspace_id: "workspace" };
let root: Root | undefined, host: HTMLDivElement, cache: QueryClient;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  desktop.enabled = true;
  vi.mocked(api.getDshStatus).mockResolvedValue(connection);
  vi.mocked(api.getDshWorkspaceRoot).mockResolvedValue(null);
  vi.mocked(api.connectDshAuto).mockResolvedValue(connection);
  vi.mocked(api.setDshWorkspaceRoot).mockResolvedValue(saved);
  vi.mocked(pickers.createPicker).mockResolvedValue("picker");
  vi.mocked(pickers.pollPicker).mockResolvedValue(saved.path);
  host = document.createElement("div"); document.body.append(host);
  root = createRoot(host);
  cache = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
});
afterEach(async () => {
  await act(async () => root?.unmount());
  host.remove(); cache.clear();
});
async function mount(path = "/settings?section=dsh", active = true) {
  await act(async () => root!.render(<QueryClientProvider client={cache}>
    <MemoryRouter initialEntries={[path]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <SettingsPage active={active} scope="library" />
    </MemoryRouter>
  </QueryClientProvider>));
}
const button = (text: string) => [...host.querySelectorAll("button")].find(b => b.textContent?.trim() === text)!;
const section = () => host.querySelector(".dsh-settings-section")!;
async function settle() {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 20)); });
}

it("configures the shared root from global settings before any course or question exists", async () => {
  await mount(); await settle();
  expect(host.querySelector('.settings-navigation button[aria-current="page"]')?.textContent).toBe("DSH 讨论");
  expect(section().textContent).toContain("尚未设置");
  await act(async () => button("选择默认工作区").click());
  expect(pickers.createPicker).toHaveBeenCalledWith("directory");
  expect(api.setDshWorkspaceRoot).toHaveBeenCalledWith(saved.path, "desktop");
  expect(section().textContent).toContain(saved.path);
  expect(section().textContent).toContain("默认工作区已保存");
  expect(cache.getQueryData(["dsh-workspace-root", "library"])).toEqual(saved);
  expect(api.prepareDshQuestion).not.toHaveBeenCalled();
  expect(api.getDshDiscussions).not.toHaveBeenCalled();
});

it("keeps the saved root when the native directory picker is cancelled", async () => {
  vi.mocked(api.getDshWorkspaceRoot).mockResolvedValue(saved);
  vi.mocked(pickers.pollPicker).mockResolvedValue(null);
  await mount(); await settle();
  await act(async () => button("更改默认工作区").click());
  expect(section().textContent).toContain(saved.path);
  expect(api.setDshWorkspaceRoot).not.toHaveBeenCalled();
});

it("reads a saved directory while DSH is offline and preserves it on connection failure", async () => {
  vi.mocked(api.getDshWorkspaceRoot).mockResolvedValue(saved);
  vi.mocked(api.getDshStatus).mockRejectedValue(new Error("DSH 未连接"));
  vi.mocked(api.connectDshAuto).mockRejectedValue(new Error("请启动 DSH"));
  await mount(); await settle();
  expect(section().textContent).toContain(saved.path);
  await act(async () => button("更改默认工作区").click());
  expect(section().textContent).toContain("请启动 DSH");
  expect(section().textContent).toContain(saved.path);
  expect(pickers.createPicker).not.toHaveBeenCalled();
});

it("does not query DSH until its settings category is visible, or while the settings page is hidden", async () => {
  await mount("/settings?section=desktop");
  expect(api.getDshStatus).not.toHaveBeenCalled();
  expect(api.getDshWorkspaceRoot).not.toHaveBeenCalled();
  await act(async () => button("DSH 讨论").click()); await settle();
  expect(api.getDshWorkspaceRoot).toHaveBeenCalledTimes(1);
  await mount("/settings?section=dsh", false);
  vi.mocked(api.getDshStatus).mockClear(); vi.mocked(api.getDshWorkspaceRoot).mockClear();
  await act(async () => { await cache.invalidateQueries({ queryKey: ["dsh-workspace-root"] });
    await cache.invalidateQueries({ queryKey: ["dsh-status"] }); });
  expect(api.getDshStatus).not.toHaveBeenCalled();
  expect(api.getDshWorkspaceRoot).not.toHaveBeenCalled();
});

it("hides the DSH category in an ordinary browser", async () => {
  desktop.enabled = false;
  await mount();
  expect(button("DSH 讨论")).toBeUndefined();
  expect(section()).toBeNull();
  expect(api.getDshStatus).not.toHaveBeenCalled();
  expect(api.getDshWorkspaceRoot).not.toHaveBeenCalled();
});
