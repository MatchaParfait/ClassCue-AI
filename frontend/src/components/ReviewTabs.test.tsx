import { act, useEffect, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ReviewTabs, ReviewTabsProvider, useReviewTabs } from "./ReviewTabs";
import { UnsavedChangesProvider, useUnsavedDraft } from "./UnsavedChanges";
import type { Video } from "../types";

let container: HTMLDivElement, root: Root;
let router: ReturnType<typeof createMemoryRouter>;
const saveDraft = vi.fn<() => Promise<void>>();
const video = (id: number, library_id = "library-a", display_name = `Lesson ${id}`) =>
  ({ id, library_id, display_name } as Video);
function Fixture() {
  const tabs = useReviewTabs()!;
  const location = useLocation();
  const [dirty, setDirty] = useState(false);
  useUnsavedDraft({ label: "测试笔记", dirty,
    save: async () => { await saveDraft(); setDirty(false); }, discard: () => setDirty(false) });
  useEffect(() => {
    const id = Number(location.pathname.split("/").pop());
    if (location.pathname.startsWith("/review/") && id !== 404) tabs.register(video(id));
  }, [location.pathname, tabs.register]);
  return <>
    <ReviewTabs /><output>{location.pathname}</output>
    <button onClick={() => tabs.register(video(1))}>Open 1</button>
    <button onClick={() => tabs.register(video(2))}>Open 2</button>
    <button onClick={() => tabs.register(video(1, "library-a", "Renamed"))}>Rename</button>
    <button onClick={() => tabs.setLibrary("library-b")}>Library B</button>
    <button onClick={() => tabs.setLibrary("library-a")}>Library A</button>
    <button onClick={() => tabs.register(video(1, "library-b", "Other library"))}>Open B</button>
    <button onClick={() => tabs.registerStudyMaterials("library-a", "/study-materials?course=course&document=1")}>Open materials</button>
    <button onClick={() => tabs.registerStudyMaterials("library-a", "/study-materials?course=other&document=2")}>Other document</button>
    <input aria-label="测试笔记" onChange={() => setDirty(true)} />
    <input aria-label="录入快捷键" data-shortcut-recorder readOnly />
    <button onClick={() => setDirty(true)}>Edit note</button>
  </>;
}
beforeEach(async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  saveDraft.mockReset().mockResolvedValue(undefined);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  router = createMemoryRouter([{ path: "*", element:
    <UnsavedChangesProvider><ReviewTabsProvider><Fixture /></ReviewTabsProvider></UnsavedChangesProvider> }]);
  await act(async () => root.render(<RouterProvider router={router} />));
});
afterEach(() => { act(() => root.unmount()); container.remove(); router.dispose(); vi.restoreAllMocks(); });
const links = () => [...container.querySelectorAll<HTMLAnchorElement>(".review-tab-link")];
const button = (label: string) => [...document.querySelectorAll<HTMLButtonElement>("button")]
  .find(item => item.textContent === label || item.getAttribute("aria-label") === label)!;
async function click(element: HTMLElement) { await act(async () => element.click()); }
async function go(path: string | number) { await act(async () => { await router.navigate(path as string); }); }
async function key(options: KeyboardEventInit, target: EventTarget = document) {
  const event = new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ctrlKey: true, ...options });
  await act(async () => { target.dispatchEvent(event); });
  return event;
}

it("does not add any extra row before a video has been opened", () => {
  expect(container.querySelector("nav")).toBeNull();
});

it("preserves opening order, deduplicates a video, and updates its title in place", async () => {
  await click(button("Open 1")); await click(button("Open 2")); await click(button("Open 1"));
  expect(links().map(link => link.textContent)).toEqual(["Lesson 1", "Lesson 2"]);
  await click(button("Rename"));
  expect(links().map(link => link.textContent)).toEqual(["Renamed", "Lesson 2"]);
  expect(links()[0].title).toBe("Renamed\n切换标签：Ctrl+Tab 向右，Ctrl+Shift+Tab 向左；也可用 Ctrl+PageUp / PageDown");
});

it("switches by URL without reusing stale searches or autoplay state and supports browser history", async () => {
  await go("/review/1?start_ms=1000&library_id=library-a");
  await go("/review/2");
  await click(links()[0]);
  expect(router.state.location.pathname).toBe("/review/1");
  expect(router.state.location.search).toBe("");
  expect(router.state.location.state).toBeNull();
  expect(links()[0].getAttribute("aria-current")).toBe("page");
  await go(-1);
  expect(router.state.location.pathname).toBe("/review/2");
  await go(1);
  expect(router.state.location.pathname).toBe("/review/1");
  expect(links()).toHaveLength(2);
});

it("keeps tabs when returning to the library and closing an inactive tab never navigates", async () => {
  await go("/review/1"); await go("/review/2"); await go("/");
  expect(links()).toHaveLength(2);
  expect(container.querySelector(".review-tabs-library")?.getAttribute("aria-current")).toBe("page");
  await click(button("关闭视频标签：Lesson 1"));
  expect(router.state.location.pathname).toBe("/");
  expect(links().map(link => link.textContent)).toEqual(["Lesson 2"]);
});

it("closes the active tab to its right neighbor, then left neighbor, then the library", async () => {
  await go("/review/1"); await go("/review/2"); await go("/review/3"); await go("/review/2");
  await click(button("关闭视频标签：Lesson 2"));
  expect(router.state.location.pathname).toBe("/review/3");
  expect(links().map(link => link.textContent)).toEqual(["Lesson 1", "Lesson 3"]);
  await click(button("关闭视频标签：Lesson 3"));
  expect(router.state.location.pathname).toBe("/review/1");
  await click(button("关闭视频标签：Lesson 1"));
  expect(router.state.location.pathname).toBe("/");
  expect(container.querySelector("nav")).toBeNull();
});

it("never mixes same-ID videos from different libraries", async () => {
  await click(button("Open 1")); await click(button("Open 2"));
  await click(button("Library B"));
  expect(container.querySelector("nav")).toBeNull();
  await click(button("Open B"));
  expect(links().map(link => link.textContent)).toEqual(["Other library"]);
  await click(button("Library A"));
  expect(links().map(link => link.textContent)).toEqual(["Lesson 1", "Lesson 2"]);
});

it("keeps existing tabs available on a failing video URL without inventing a successful tab", async () => {
  await go("/review/1"); await go("/review/404");
  expect(links()).toHaveLength(1);
  expect(links()[0].getAttribute("aria-current")).toBeNull();
  await click(links()[0]);
  expect(router.state.location.pathname).toBe("/review/1");
});

it("moves keyboard focus without switching until activated, and Delete closes only that tab", async () => {
  await go("/review/1"); await go("/review/2");
  links()[0].focus();
  act(() => links()[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true })));
  expect(document.activeElement).toBe(links()[1]);
  expect(router.state.location.pathname).toBe("/review/2");
  act(() => links()[1].dispatchEvent(new KeyboardEvent("keydown", { key: "Home", bubbles: true, cancelable: true })));
  expect(document.activeElement).toBe(links()[0]);
  await act(async () => links()[0].dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true, cancelable: true })));
  expect(links().map(link => link.textContent)).toEqual(["Lesson 2"]);
  expect(router.state.location.pathname).toBe("/review/2");
});

it("keeps one materials tab among videos and updates its destination without changing its position", async () => {
  await click(button("Open 1")); await click(button("Open materials")); await click(button("Open 2"));
  await click(button("Open materials")); await click(button("Other document"));
  expect(links().map(link => link.textContent)).toEqual(["Lesson 1", "学习资料", "Lesson 2"]);
  expect(links()[1].querySelector(".lucide-file-text")).not.toBeNull();
  await click(links()[1]);
  expect(router.state.location.pathname).toBe("/study-materials");
  expect(new URLSearchParams(router.state.location.search).get("document")).toBe("2");
  expect(new URLSearchParams(router.state.location.search).get("course")).toBe("other");
  expect(links()[1].getAttribute("aria-current")).toBe("page");
  await click(button("关闭资料标签：学习资料"));
  expect(router.state.location.pathname).toBe("/review/2");
  expect(links().map(link => link.textContent)).toEqual(["Lesson 1", "Lesson 2"]);
});

it("isolates the materials tab by library and keeps it active across every materials view", async () => {
  await click(button("Open materials"));
  for (const path of ["/study-materials", "/study-materials?course=course&tab=video", "/study-materials?view=unassigned"]) {
    await go(path);
    expect(links()[0].getAttribute("aria-current")).toBe("page");
  }
  await click(button("Library B"));
  expect(links()).toHaveLength(0);
  await click(button("Library A"));
  expect(links().map(link => link.textContent)).toEqual(["学习资料"]);
});

it("cycles in visible order through videos and the latest materials destination, wrapping both ways", async () => {
  await go("/review/1"); await click(button("Open materials")); await go("/review/2");
  await click(button("Other document"));
  const forward = await key({ key: "Tab" });
  expect(forward.defaultPrevented).toBe(true);
  expect(router.state.location.pathname).toBe("/review/1");
  expect(document.activeElement).toBe(links()[0]);
  await key({ key: "Tab" });
  expect(router.state.location.pathname + router.state.location.search).toBe("/study-materials?course=other&document=2");
  expect(links()[1].getAttribute("aria-current")).toBe("page");
  await key({ key: "Tab" });
  expect(router.state.location.pathname).toBe("/review/2");
  await key({ key: "Tab" });
  await key({ key: "Tab", shiftKey: true });
  expect(router.state.location.pathname).toBe("/review/2");
});

it("supports PageUp and PageDown, shows the active tab, and exposes shortcut help", async () => {
  await go("/review/1"); await go("/review/2"); await go("/review/1");
  const list = container.querySelector<HTMLElement>(".review-tabs-list")!;
  Object.defineProperty(list, "clientWidth", { configurable: true, value: 200 });
  Object.defineProperty(links()[1].parentElement!, "offsetLeft", { configurable: true, value: 400 });
  Object.defineProperty(links()[1].parentElement!, "offsetWidth", { configurable: true, value: 180 });
  await key({ key: "PageDown" });
  expect(router.state.location.pathname).toBe("/review/2");
  expect(list.scrollLeft).toBe(380);
  expect(document.activeElement).toBe(links()[1]);
  await key({ key: "PageUp" });
  expect(router.state.location.pathname).toBe("/review/1");
  expect(container.querySelector("nav")?.title).toContain("Ctrl+Shift+Tab");
});

it("enters the first or last open tab from the library without including the library in the cycle", async () => {
  await go("/review/1"); await go("/review/2"); await go("/");
  await key({ key: "Tab" });
  expect(router.state.location.pathname).toBe("/review/1");
  await go("/");
  await key({ key: "Tab", shiftKey: true });
  expect(router.state.location.pathname).toBe("/review/2");
  await key({ key: "Tab" });
  expect(router.state.location.pathname).toBe("/review/1");
});

it("works while a normal input has focus and does not leave focus in the previous page", async () => {
  await go("/review/1"); await go("/review/2");
  const input = container.querySelector<HTMLInputElement>('[aria-label="测试笔记"]')!;
  input.focus();
  await key({ key: "Tab" }, input);
  expect(router.state.location.pathname).toBe("/review/1");
  expect(document.activeElement).toBe(links()[0]);
});

it("keeps unsaved edits on cancel and switches only after save or discard", async () => {
  await go("/review/1"); await go("/review/2"); await click(button("Edit note"));
  await key({ key: "Tab" });
  expect(router.state.location.pathname).toBe("/review/2");
  expect(document.querySelector('[role="dialog"]')?.textContent).toContain("测试笔记");
  await key({ key: "PageUp" });
  await click(button("继续编辑"));
  expect(router.state.location.pathname).toBe("/review/2");
  await key({ key: "PageUp" }); await click(button("保存并离开"));
  expect(router.state.location.pathname).toBe("/review/1");
  await click(button("Edit note")); await key({ key: "PageUp" });
  await click(button("放弃修改"));
  expect(router.state.location.pathname).toBe("/review/2");
});

it("keeps the current tab and draft on save failure and completes the same switch after retry", async () => {
  await go("/review/1"); await go("/review/2"); await click(button("Edit note"));
  saveDraft.mockRejectedValueOnce(new Error("测试保存失败"));
  await key({ key: "Tab" }); await click(button("保存并离开"));
  expect(router.state.location.pathname).toBe("/review/2");
  expect(document.querySelector('[role="alert"]')?.textContent).toContain("测试保存失败");
  await key({ key: "Tab", shiftKey: true });
  expect(router.state.location.pathname).toBe("/review/2");
  await click(button("保存并离开"));
  expect(saveDraft).toHaveBeenCalledTimes(2);
  expect(router.state.location.pathname).toBe("/review/1");
  expect(document.querySelector('[role="dialog"]')).toBeNull();
  expect(document.activeElement).toBe(links()[0]);
});

it.each([
  { key: "Tab", ctrlKey: false }, { key: "Tab", altKey: true }, { key: "Tab", metaKey: true },
  { key: "Tab", isComposing: true }, { key: "PageDown", shiftKey: true },
])("ignores unrelated modifier combinations and composition: %j", async options => {
  await go("/review/1"); await go("/review/2");
  expect((await key(options)).defaultPrevented).toBe(false);
  expect(router.state.location.pathname).toBe("/review/2");
});

it("does not interrupt recording, already handled events, hidden pages or modal operations", async () => {
  await go("/review/1"); await go("/review/2");
  const recorder = container.querySelector<HTMLInputElement>("[data-shortcut-recorder]")!;
  expect((await key({ key: "Tab" }, recorder)).defaultPrevented).toBe(false);
  expect((await key({ key: "PageDown" }, recorder)).defaultPrevented).toBe(false);
  const handled = new KeyboardEvent("keydown", { key: "Tab", ctrlKey: true, bubbles: true, cancelable: true });
  handled.preventDefault();
  await act(async () => { document.dispatchEvent(handled); });
  container.hidden = true;
  expect((await key({ key: "Tab" })).defaultPrevented).toBe(false);
  container.hidden = false;
  document.body.setAttribute("data-modal-locked", "true");
  try {
    expect((await key({ key: "Tab" })).defaultPrevented).toBe(false);
  } finally { document.body.removeAttribute("data-modal-locked"); }
  expect(router.state.location.pathname).toBe("/review/2");
});

it("ignores auto-repeat and keeps a single active tab without adding history", async () => {
  await go("/review/1"); await go("/review/2");
  await key({ key: "Tab", repeat: true });
  expect(router.state.location.pathname).toBe("/review/2");
  await click(button("关闭视频标签：Lesson 1"));
  const locationKey = router.state.location.key;
  await key({ key: "Tab" });
  expect(router.state.location.key).toBe(locationKey);
});
