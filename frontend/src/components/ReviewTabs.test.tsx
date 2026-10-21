import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { createMemoryRouter, RouterProvider, useLocation } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ReviewTabs, ReviewTabsProvider, useReviewTabs } from "./ReviewTabs";
import type { Video } from "../types";

let container: HTMLDivElement, root: Root;
let router: ReturnType<typeof createMemoryRouter>;
const video = (id: number, library_id = "library-a", display_name = `Lesson ${id}`) =>
  ({ id, library_id, display_name } as Video);
function Fixture() {
  const tabs = useReviewTabs()!;
  const location = useLocation();
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
  </>;
}
beforeEach(async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  router = createMemoryRouter([{ path: "*", element: <ReviewTabsProvider><Fixture /></ReviewTabsProvider> }]);
  await act(async () => root.render(<RouterProvider router={router} />));
});
afterEach(() => { act(() => root.unmount()); container.remove(); router.dispose(); vi.restoreAllMocks(); });
const links = () => [...container.querySelectorAll<HTMLAnchorElement>(".review-tab-link")];
const button = (label: string) => [...container.querySelectorAll<HTMLButtonElement>("button")]
  .find(item => item.textContent === label || item.getAttribute("aria-label") === label)!;
async function click(element: HTMLElement) { await act(async () => element.click()); }
async function go(path: string | number) { await act(async () => { await router.navigate(path as string); }); }

it("does not add any extra row before a video has been opened", () => {
  expect(container.querySelector("nav")).toBeNull();
});

it("preserves opening order, deduplicates a video, and updates its title in place", async () => {
  await click(button("Open 1")); await click(button("Open 2")); await click(button("Open 1"));
  expect(links().map(link => link.textContent)).toEqual(["Lesson 1", "Lesson 2"]);
  await click(button("Rename"));
  expect(links().map(link => link.textContent)).toEqual(["Renamed", "Lesson 2"]);
  expect(links()[0].title).toBe("Renamed");
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
