import { QueryClient, QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { act, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { createRoot, type Root } from "react-dom/client";
import { createMemoryRouter, Link, RouterProvider, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/client";
import { OnboardingGuide, ONBOARDING_RESET_EVENT } from "./OnboardingGuide";
import { OnboardingMascot } from "./OnboardingMascot";
import { spotlightLayout } from "./OnboardingSpotlight";
import { UnsavedChangesProvider, useUnsavedDraft } from "./UnsavedChanges";
import { useModalFocus } from "../hooks/useModalFocus";
import { defaultShortcuts, SHORTCUTS_KEY } from "../shortcuts";
import type { AsrModel, StorageDirectories, Video } from "../types";

vi.mock("../api/client", () => ({
  getVideos: vi.fn(), getStorageDirectories: vi.fn(), saveStorageDirectories: vi.fn(),
  getAsrSelection: vi.fn(), getAsrModelStatus: vi.fn(), getAiSettings: vi.fn(),
}));
const key = "classroom-onboarding:v1:test-library";
const model: AsrModel = { id: "whisper-turbo", name: "Whisper", engine: "whisper", downloaded: true, selected: true, cache_size_bytes: 100 };
const directories = {
  library_id: "test-library", video_library_dir: "D:\\videos", audio_cache_dir: "D:\\audio", model_dir: "D:\\models",
  asr_storage_root: "D:\\models", log_dir: "D:\\logs", delete_source_after_import: true,
} as StorageDirectories;
const connected = {
  provider: "openai_compatible", base_url: "https://test.invalid", model: "test-model",
  output_tokens: 1000, token_parameter: "max_tokens", json_mode: false, timeout_seconds: 60, max_retries: 1, has_api_key: true,
};
const shortcutsState = { started: true, step: "shortcuts", storageConfirmed: true, modelConfirmed: true, aiConfirmed: true };
let host: HTMLDivElement, root: Root, client: QueryClient, router: ReturnType<typeof createMemoryRouter>;
const unrelated = vi.fn(), activeChanged = vi.fn();

function NestedDialog({ close }: { close: () => void }) {
  const dialog = useRef<HTMLDivElement>(null), initial = useRef<HTMLButtonElement>(null);
  useModalFocus(true, dialog, initial, close);
  return createPortal(<div role="dialog" aria-modal="true" ref={dialog}>
    <button ref={initial} onClick={close}>关闭模型列表</button>
  </div>, document.body);
}
function RealPages() {
  const location = useLocation(), navigate = useNavigate(), cache = useQueryClient();
  const section = new URLSearchParams(location.search).get("section") ?? "storage";
  const [dirty, setDirty] = useState(false), [error, setError] = useState(""), [nested, setNested] = useState(false);
  const [keepSource, setKeepSource] = useState(false);
  useUnsavedDraft({ label: "真实页面设置", dirty, save: async () => { setDirty(false); return true; }, discard: () => setDirty(false) });
  return <>
    <Link data-onboarding="settings" to="/settings">设置</Link>
    <Link to="/batch-process">无关页面</Link><button onClick={unrelated}>背景按钮</button>
    <div hidden={location.pathname !== "/"}>
      <Link to="/process">导入视频</Link>
    </div>
    <div hidden={location.pathname !== "/settings"}>
      <button data-onboarding="back" onClick={() => navigate("/")}>返回视频库</button>
      <nav>{["storage", "asr", "ai", "shortcuts", "updates"].map(value =>
        <button key={value} data-onboarding-section={value} onClick={() => navigate(`/settings?section=${value}`, { replace: true })}>{value}</button>)}</nav>
      <section className="storage-directories-section" hidden={section !== "storage"}>
        <button onClick={() => { setKeepSource(true); setDirty(true); }}>取消删除原文件</button>
        <button onClick={async () => {
          try {
            const value = await api.saveStorageDirectories({ ...directories, delete_source_after_import: !keepSource });
            cache.setQueryData(["settings", "directories"], value); setDirty(false); setError("");
          } catch (failure) { setError(String(failure)); }
        }}>保存设置</button>
        {error && <p role="alert">{error}</p>}
      </section>
      <section data-onboarding="asr" hidden={section !== "asr"}>
        <button>保存模型选择</button><button>准备模型</button>
      </section>
      <section data-onboarding="ai" hidden={section !== "ai"}>
        <button onClick={() => setNested(true)}>获取模型</button>
        <button onClick={() => setDirty(true)}>修改连接</button>
      </section>
      <section className="shortcut-settings" hidden={section !== "shortcuts"}>
        <input data-shortcut-recorder aria-label="录入快捷键" />
        <button onClick={() => setDirty(true)}>修改快捷键</button>
      </section>
    </div>
    {nested && <NestedDialog close={() => setNested(false)} />}
  </>;
}
function Harness() {
  return <UnsavedChangesProvider>
    <RealPages />
    <OnboardingGuide active scope="test-library" onActiveChange={activeChanged} />
  </UnsavedChangesProvider>;
}
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks(); window.localStorage.clear();
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    if (this.classList.contains("onboarding-spotlight")) return {
      x: 0, y: 0, left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight,
      width: window.innerWidth, height: window.innerHeight, toJSON: () => ({}),
    };
    return { x: 420, y: 120, left: 420, top: 120, right: 800, bottom: 420, width: 380, height: 300, toJSON: () => ({}) };
  });
  vi.mocked(api.getVideos).mockResolvedValue([]);
  vi.mocked(api.getAsrSelection).mockResolvedValue(model);
  vi.mocked(api.getAsrModelStatus).mockResolvedValue(model);
  vi.mocked(api.getStorageDirectories).mockResolvedValue(directories);
  vi.mocked(api.saveStorageDirectories).mockResolvedValue({ ...directories, delete_source_after_import: false });
  vi.mocked(api.getAiSettings).mockResolvedValue(connected);
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(async () => {
  await act(async () => { root.unmount(); client.clear(); router?.dispose(); });
  host.remove(); vi.restoreAllMocks();
});
async function settle(check: () => void) {
  let failure: unknown;
  for (let attempt = 0; attempt < 200; attempt++) {
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)); });
    try { check(); return; } catch (error) { failure = error; }
  }
  throw failure;
}
async function mount(state?: object, path = "/") {
  if (state) window.localStorage.setItem(key, JSON.stringify(state));
  router = createMemoryRouter([{ path: "*", element: <Harness /> }], { initialEntries: ["/previous", path] });
  await act(async () => root.render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>));
  await settle(() => expect(api.getVideos).toHaveBeenCalled());
}
function tip() { return document.querySelector<HTMLElement>(".onboarding-tip"); }
function button(text: string, area: ParentNode = document) {
  return [...area.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent?.trim() === text)!;
}
async function click(text: string, area?: ParentNode) { await act(async () => button(text, area).click()); }
async function link(text: string) { await act(async () => [...host.querySelectorAll("a")].find(item => item.textContent === text)!.click()); }
const title = () => tip()?.querySelector("h2")?.textContent;
async function readyStorage() {
  await mount({ started: true, step: "storage" }, "/settings");
  await settle(() => expect(button("继续", tip()!).disabled).toBe(false));
}

it("highlights real settings without embedding forms, blocks unrelated clicks/history and contains keyboard focus", async () => {
  localStorage.setItem(SHORTCUTS_KEY, JSON.stringify({ ...defaultShortcuts(), search: ["Ctrl+K"] }));
  await mount();
  await settle(() => expect(tip()).not.toBeNull());
  expect(tip()?.querySelector(".storage-directories-section")).toBeNull();
  expect(document.querySelectorAll(".storage-directories-section")).toHaveLength(1);
  expect(document.body.style.overflow).toBe("hidden");
  expect(document.querySelector(".onboarding-mask")?.getAttribute("style")).toContain("clip-path");
  await click("背景按钮"); expect(unrelated).not.toHaveBeenCalled();
  await link("无关页面"); expect(router.state.location.pathname).toBe("/");
  await act(async () => { await router.navigate(-1); });
  expect(router.state.location.pathname).toBe("/");
  await act(async () => button("背景按钮").focus());
  expect(document.activeElement).toBe(tip());
  const search = new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true });
  await act(async () => document.dispatchEvent(search)); expect(search.defaultPrevented).toBe(true);
  await act(async () => document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(tip()).not.toBeNull();
  await link("设置");
  expect(router.state.location.pathname).toBe("/settings");
  expect(title()).toBe("确认存储");
  expect(document.body.getAttribute("data-onboarding-active")).toBe("true");
});

it("waits for the real list and preserves existing dismissal until explicitly reopened", async () => {
  let resolve!: (videos: Video[]) => void;
  vi.mocked(api.getVideos).mockImplementation(() => new Promise(done => { resolve = done; }));
  await mount({ dismissed: true, storageConfirmed: true });
  expect(tip()).toBeNull();
  await act(async () => resolve([])); expect(tip()).toBeNull();
  expect(api.getStorageDirectories).not.toHaveBeenCalled();
  await act(async () => window.dispatchEvent(new Event(ONBOARDING_RESET_EVENT)));
  await settle(() => expect(title()).toBe("确认存储"));
});

it("does not start until a previously unknown library is confirmed empty", async () => {
  let resolve!: (videos: Video[]) => void;
  vi.mocked(api.getVideos).mockImplementation(() => new Promise(done => { resolve = done; }));
  await mount(); expect(tip()).toBeNull();
  await act(async () => resolve([{ id: 1, status: "uploaded" } as Video]));
  await settle(() => expect(client.getQueryData(["videos"])).toHaveLength(1));
  expect(tip()).toBeNull();
});

it.each([true, false])("confirms existing storage with delete_source_after_import=%s without requiring or changing that choice", async deleteSource => {
  vi.mocked(api.getStorageDirectories).mockResolvedValue({ ...directories, delete_source_after_import: deleteSource });
  await mount(undefined, "/settings");
  await settle(() => expect(button("继续", tip()!).disabled).toBe(false));
  expect(tip()?.textContent).not.toContain("删除原文件");
  expect(tip()?.textContent).not.toContain("保留原文件");
  expect(api.saveStorageDirectories).not.toHaveBeenCalled();
  await click("继续", tip()!);
  await settle(() => expect(title()).toBe("准备字幕模型"));
  expect(api.saveStorageDirectories).not.toHaveBeenCalled();
  expect(client.getQueryData(["settings", "directories"])).toMatchObject({ delete_source_after_import: deleteSource });
  expect(router.state.location.search).toBe("");
  await click("ai"); expect(router.state.location.search).toBe("");
  await click("asr"); expect(router.state.location.search).toBe("?section=asr");
});

it("revalidates the server instead of treating a stale saved value as success", async () => {
  await readyStorage();
  vi.mocked(api.getStorageDirectories).mockRejectedValueOnce(new Error("读取失败"));
  await click("继续", tip()!);
  await settle(() => expect(tip()?.textContent).toContain("读取失败"));
  expect(title()).toBe("确认存储");
  await click("继续", tip()!);
  await settle(() => expect(title()).toBe("准备字幕模型"));
});

it("does not rewrite storage when a paused job prevents changes", async () => {
  vi.mocked(api.getStorageDirectories).mockResolvedValue({ ...directories, has_paused_jobs: true });
  await mount(undefined, "/settings");
  await settle(() => expect(button("继续", tip()!).disabled).toBe(false));
  expect(tip()?.textContent).toContain("暂不能修改存储");
  await click("继续", tip()!);
  await settle(() => expect(title()).toBe("准备字幕模型"));
  expect(api.saveStorageDirectories).not.toHaveBeenCalled();
});

it("waits for model readiness and freshly revalidates before continuing", async () => {
  vi.mocked(api.getAsrModelStatus).mockResolvedValue({ ...model, downloaded: false });
  await mount({ started: true, storageConfirmed: true, step: "model" }, "/settings?section=asr");
  await settle(() => expect(api.getAsrModelStatus).toHaveBeenCalled());
  await settle(() => expect(button("继续", tip()!).disabled).toBe(true));
  await act(async () => client.setQueryData(["asr-status", model.id, undefined, false], model));
  await settle(() => expect(button("继续", tip()!).disabled).toBe(false));
  await click("继续", tip()!);
  await settle(() => expect(tip()?.textContent).toContain("请先保存模型选择"));
  expect(title()).toBe("准备字幕模型");
  vi.mocked(api.getAsrModelStatus).mockResolvedValue(model);
  await act(async () => client.setQueryData(["asr-status", model.id, undefined, false], model));
  await settle(() => expect(button("继续", tip()!).disabled).toBe(false));
  await click("继续", tip()!);
  await settle(() => expect(title()).toBe("连接 AI 服务"));
});

it("requires configured AI and permits real nested dialogs without permitting background controls", async () => {
  vi.mocked(api.getAiSettings).mockResolvedValue({ ...connected, configured: false, has_api_key: false });
  await mount({ started: true, step: "ai", storageConfirmed: true, modelConfirmed: true }, "/settings?section=ai");
  await settle(() => expect(button("继续", tip()!).disabled).toBe(true));
  await click("获取模型");
  expect(document.querySelector('[role="dialog"]')).not.toBeNull();
  const dialogWheel = new WheelEvent("wheel", { deltaY: 120, bubbles: true, cancelable: true });
  await act(async () => document.querySelector('[role="dialog"]')!.dispatchEvent(dialogWheel));
  expect(dialogWheel.defaultPrevented).toBe(false);
  const backgroundWheel = new WheelEvent("wheel", { deltaY: 120, bubbles: true, cancelable: true });
  await act(async () => document.body.dispatchEvent(backgroundWheel));
  expect(backgroundWheel.defaultPrevented).toBe(true);
  await click("背景按钮"); expect(unrelated).not.toHaveBeenCalled();
  await click("关闭模型列表"); expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(document.body.style.overflow).toBe("hidden");
  vi.mocked(api.getAiSettings).mockResolvedValue({ ...connected, configured: true });
  await act(async () => client.setQueryData(["settings", "ai"], { ...connected, configured: true }));
  await settle(() => expect(button("继续", tip()!).disabled).toBe(false));
  await click("继续", tip()!);
  await settle(() => expect(title()).toBe("认识常用快捷键"));
  expect(router.state.location.search).toBe("?section=ai");
  await click("shortcuts"); expect(router.state.location.search).toBe("?section=shortcuts");
});

it("protects unsaved shortcut edits and only finishes after the leave decision", async () => {
  await mount(shortcutsState, "/settings?section=shortcuts");
  await settle(() => expect(title()).toBe("认识常用快捷键"));
  const event = new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true });
  await act(async () => document.querySelector("input[data-shortcut-recorder]")!.dispatchEvent(event));
  expect(event.defaultPrevented).toBe(false);
  await click("修改快捷键"); await click("完成引导", tip()!);
  expect(document.querySelector(".unsaved-dialog")).not.toBeNull();
  expect(JSON.parse(localStorage.getItem(key)!).shortcutsSeen).not.toBe(true);
  await click("继续编辑");
  expect(title()).toBe("认识常用快捷键");
  await click("完成引导", tip()!); await click("放弃修改");
  await settle(() => expect(tip()).toBeNull());
  expect(JSON.parse(localStorage.getItem(key)!)).toMatchObject({ shortcutsSeen: true, dismissed: true });
  expect(router.state.location.pathname + router.state.location.search).toBe("/settings?section=shortcuts");
});

it("finishes on the fourth step without opening an existing video and restores scrolling and navigation", async () => {
  vi.mocked(api.getVideos).mockResolvedValue([{ id: 7, status: "review_ready" }] as Video[]);
  await mount(shortcutsState, "/settings?section=shortcuts");
  await settle(() => expect(title()).toBe("认识常用快捷键"));
  expect(tip()?.textContent).toContain("4 / 4");
  expect(tip()?.textContent).not.toContain("首次处理");
  expect(document.body.style.overflow).toBe("hidden");
  await click("完成引导", tip()!);
  await settle(() => expect(tip()).toBeNull());
  expect(router.state.location.pathname + router.state.location.search).toBe("/settings?section=shortcuts");
  expect(document.body.hasAttribute("data-onboarding-active")).toBe(false);
  expect(document.body.style.overflow).not.toBe("hidden");
  const releasedWheel = new WheelEvent("wheel", { deltaY: 120, bubbles: true, cancelable: true });
  await act(async () => document.body.dispatchEvent(releasedWheel));
  expect(releasedWheel.defaultPrevented).toBe(false);
  expect(activeChanged).toHaveBeenLastCalledWith(false);
  await click("背景按钮"); expect(unrelated).toHaveBeenCalledTimes(1);
  await link("无关页面");
  expect(router.state.location.pathname).toBe("/batch-process");
  expect(JSON.parse(localStorage.getItem(key)!)).toMatchObject({ dismissed: true, shortcutsSeen: true });
});

it.each(["/", "/process?videoId=7", "/settings?section=shortcuts"])("migrates old fifth-step progress at %s without navigation", async path => {
  vi.mocked(api.getVideos).mockResolvedValue([{ id: 7, status: "review_ready" }] as Video[]);
  await mount({ ...shortcutsState, step: "process", shortcutsSeen: true, processVideoId: 7 }, path);
  expect(tip()).toBeNull();
  expect(router.state.location.pathname + router.state.location.search).toBe(path);
  expect(JSON.parse(localStorage.getItem(key)!)).toMatchObject({ dismissed: true, shortcutsSeen: true });
  expect(JSON.parse(localStorage.getItem(key)!)).not.toHaveProperty("processVideoId");
  expect(document.body.style.overflow).not.toBe("hidden");
});

it("resumes the final settings step if legacy fifth-step progress is incomplete", async () => {
  await mount({ ...shortcutsState, step: "process", shortcutsSeen: false }, "/settings?section=shortcuts");
  await settle(() => expect(title()).toBe("认识常用快捷键"));
  expect(tip()?.textContent).toContain("4 / 4");
  expect(JSON.parse(localStorage.getItem(key)!).dismissed).not.toBe(true);
});

it("can explicitly reopen the four-step guide after finishing it", async () => {
  await mount(shortcutsState, "/settings?section=shortcuts");
  await settle(() => expect(title()).toBe("认识常用快捷键"));
  await click("完成引导", tip()!);
  expect(tip()).toBeNull();
  await act(async () => window.dispatchEvent(new Event(ONBOARDING_RESET_EVENT)));
  await settle(() => expect(title()).toBe("确认存储"));
  expect(tip()?.textContent).toContain("1 / 4");
  expect(JSON.parse(localStorage.getItem(key)!).dismissed).not.toBe(true);
});

it("does not reopen the completed guide after its component remounts", async () => {
  await mount(shortcutsState, "/settings?section=shortcuts");
  await settle(() => expect(title()).toBe("认识常用快捷键"));
  await click("完成引导", tip()!);
  await act(async () => root.unmount());
  router.dispose();
  root = createRoot(host);
  await mount(undefined, "/settings?section=shortcuts");
  expect(tip()).toBeNull();
});

it("places compact tips beside targets and clips tall mobile forms above the docked tip", () => {
  const desktop = spotlightLayout({ left: 500, top: 100, width: 700, height: 700 }, 1440, 900, 240);
  expect(desktop.tip.left + desktop.tip.width).toBeLessThan(desktop.hole!.left);
  expect(desktop.tip.top + desktop.tip.height).toBeLessThanOrEqual(900);
  const mobile = spotlightLayout({ left: 12, top: 80, width: 351, height: 1200 }, 375, 667, 240);
  expect(mobile.docked).toBe(true);
  expect(mobile.hole!.top + mobile.hole!.height).toBeLessThan(mobile.tip.top);
  expect(mobile.tip.left + mobile.tip.width).toBeLessThanOrEqual(375 - 12);
});

it("reserves a mascot column beside the tip only when the viewport has room", () => {
  const desktop = spotlightLayout({ left: 560, top: 120, width: 720, height: 620 }, 1440, 900, 260, 464);
  expect(desktop.tip.width).toBe(464);
  expect(desktop.tip.left + desktop.tip.width).toBeLessThan(desktop.hole!.left);
  const narrow = spotlightLayout({ left: 20, top: 90, width: 335, height: 900 }, 375, 667, 260, 344);
  expect(narrow.tip.width).toBe(344);
  expect(narrow.tip.left).toBeGreaterThanOrEqual(12);
  expect(narrow.tip.left + narrow.tip.width).toBeLessThanOrEqual(363);
});

it("uses the generated mascot poses and mirrors the pointing pose for the opposite side", async () => {
  await act(async () => root.render(<OnboardingMascot pose="welcome" direction={null} />));
  const image = host.querySelector("img")!;
  expect(image.getAttribute("src")).toBe("/art/onboarding/welcome.png");
  expect(image.alt).toBe("");
  await act(async () => root.render(<OnboardingMascot pose="point" direction="right" />));
  expect(host.querySelector("img")?.getAttribute("src")).toBe("/art/onboarding/point-right.png");
  await act(async () => root.render(<OnboardingMascot pose="point" direction="left" />));
  expect(host.querySelector("img")?.getAttribute("src")).toBe("/art/onboarding/point-right.png");
  expect(host.querySelector("img")?.style.transform).toBe("scaleX(-1)");
});

it("shows the studying pose while the current onboarding target is busy", async () => {
  await mount({ started: true, step: "ai", storageConfirmed: true, modelConfirmed: true }, "/settings?section=ai");
  const target = document.querySelector('[data-onboarding="ai"]')!;
  target.setAttribute("aria-busy", "true");
  await settle(() => expect(document.querySelector(".onboarding-mascot")?.getAttribute("data-pose")).toBe("reading"));
  target.setAttribute("aria-busy", "false");
  await settle(() => expect(document.querySelector(".onboarding-mascot")?.getAttribute("data-pose")).not.toBe("reading"));
});

it("blocks page wheel, touch and scroll keys without breaking text editing, shortcut recording or button activation", async () => {
  await mount(shortcutsState, "/settings?section=shortcuts");
  await settle(() => expect(tip()).not.toBeNull());
  const recorder = document.querySelector<HTMLInputElement>("[data-shortcut-recorder]")!;
  for (const area of [document.body, document.querySelector(".onboarding-mask")!, recorder]) {
    for (const type of ["wheel", "touchmove"]) {
      const event = new Event(type, { bubbles: true, cancelable: true });
      await act(async () => area.dispatchEvent(event));
      expect(event.defaultPrevented).toBe(true);
    }
  }
  for (const key of ["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "]) {
    const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
    await act(async () => document.body.dispatchEvent(event));
    expect(event.defaultPrevented).toBe(true);
  }
  for (const key of ["ArrowDown", "Home", "End", " "]) {
    const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true });
    await act(async () => recorder.dispatchEvent(event));
    expect(event.defaultPrevented).toBe(false);
  }
  const space = new KeyboardEvent("keydown", { key: " ", bubbles: true, cancelable: true });
  await act(async () => button("修改快捷键").dispatchEvent(space));
  expect(space.defaultPrevented).toBe(false);
});

it("automatically positions late tall targets and offers controlled region navigation while manual scrolling stays locked", async () => {
  let ready = false;
  const bounds = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect");
  bounds.mockImplementation(function (this: HTMLElement) {
    if (this.classList.contains("onboarding-spotlight")) return {
      x: 0, y: 0, left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight,
      width: window.innerWidth, height: window.innerHeight, toJSON: () => ({}),
    };
    if (this.classList.contains("shortcut-settings") && !ready) {
      return { x: 0, y: 0, left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON: () => ({}) };
    }
    const top = this.classList.contains("shortcut-settings") ? 900 : 120;
    const height = this.classList.contains("shortcut-settings") ? 1400 : 40;
    return { x: 12, y: top, left: 12, top, right: 363, bottom: top + height, width: 351, height, toJSON: () => ({}) };
  });
  await mount(shortcutsState, "/settings?section=shortcuts");
  expect(window.scrollTo).not.toHaveBeenCalled();
  ready = true;
  await settle(() => expect(window.scrollTo).toHaveBeenCalledWith({ top: 804, behavior: "instant" }));
  await settle(() => expect(document.querySelector('[aria-label="下一个操作区域"]')).not.toBeNull());
  vi.mocked(window.scrollTo).mockClear();
  await act(async () => document.querySelector<HTMLButtonElement>('[aria-label="下一个操作区域"]')!.click());
  expect(window.scrollTo).toHaveBeenCalledWith(expect.objectContaining({ top: expect.any(Number), behavior: "instant" }));
  expect(document.body.style.overflow).toBe("hidden");
});
