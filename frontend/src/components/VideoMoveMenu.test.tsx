import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { getLibrary, type LibraryFolder, type LibraryResponse } from "../api/client";
import { VideoMoveMenu } from "./VideoMoveMenu";
import { ToolbarMenu } from "./ToolbarMenu";

vi.mock("../api/client", () => ({ getLibrary: vi.fn() }));

let container: HTMLDivElement;
let root: Root;
let cache: QueryClient;
let folders: LibraryFolder[];
const onMove = vi.fn();
const response = (parentId: number | null): LibraryResponse => ({
  folder: folders.find(folder => folder.id === parentId) ?? null, parent_id: parentId, sort: "manual", order: "asc",
  items: folders.filter(folder => (folder.parent_id ?? null) === parentId).map(folder => ({ type: "folder", folder })),
});
const flush = async () => {
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
};
function click(selector: string) {
  const button = container.querySelector<HTMLButtonElement>(selector);
  expect(button).not.toBeNull();
  act(() => button!.click());
}
function key(value: string) {
  act(() => document.activeElement?.dispatchEvent(new KeyboardEvent("keydown", { key: value, bubbles: true, cancelable: true })));
}
const open = async () => {
  click('[aria-haspopup="menu"]');
  await vi.waitFor(async () => { await flush(); expect(container.querySelectorAll("[data-move-folder]")).toHaveLength(3); });
};
const expand = async (id: number, child: number) => {
  click(`[data-folder-id="${id}"] > .move-folder-row .move-folder-toggle`);
  await vi.waitFor(async () => { await flush(); expect(container.querySelector(`[data-move-folder="${child}"]`)).not.toBeNull(); });
};
async function render(currentFolder: number | null = null) {
  await act(async () => root.render(<QueryClientProvider client={cache}>
    <ToolbarMenu label="管理视频" icon={<span>管理</span>}>
      <VideoMoveMenu currentFolder={currentFolder} onMove={onMove} />
    </ToolbarMenu>
  </QueryClientProvider>));
}

beforeEach(async () => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.clearAllMocks();
  folders = [
    { id: 1, name: "算法", parent_id: null },
    { id: 2, name: "数据库", parent_id: null },
    { id: 3, name: "第1周", parent_id: 1 },
    { id: 4, name: "第1周", parent_id: 2 },
    { id: 5, name: "课堂笔记", parent_id: 3 },
  ];
  vi.mocked(getLibrary).mockReset().mockImplementation(async id => response(id ?? null));
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await render();
});

afterEach(() => {
  act(() => root.unmount());
  cache.clear();
  container.remove();
  vi.restoreAllMocks();
});

it("only loads root folders on opening and keeps descendants collapsed even when cached", async () => {
  cache.setQueryData(["library", "folder-1"], response(1));
  expect(getLibrary).not.toHaveBeenCalled();
  await open();
  expect([...container.querySelectorAll("[data-move-folder]")].map(node => node.textContent)).toEqual(["一级目录", "算法", "数据库"]);
  expect(container.textContent).not.toContain("第1周");
  expect(getLibrary).toHaveBeenCalledTimes(1);
  expect(getLibrary).toHaveBeenCalledWith();
});

it("expands one branch at a time without moving, closing, or exposing other descendants", async () => {
  await open();
  await expand(1, 3);
  expect(container.querySelector('[role="menu"]')).not.toBeNull();
  expect(container.querySelector('[data-move-folder="4"]')).toBeNull();
  expect(container.querySelector('[data-move-folder="5"]')).toBeNull();
  expect(onMove).not.toHaveBeenCalled();
  expect(container.querySelector('[data-folder-id="1"] .move-folder-toggle')?.getAttribute("aria-expanded")).toBe("true");
  click('[data-folder-id="1"] > .move-folder-row .move-folder-toggle');
  expect(container.querySelector('[data-move-folder="3"]')).toBeNull();
  expect(getLibrary).not.toHaveBeenCalledWith(2);
});

it("shows same names under their own parents with distinct full paths and moves by ID", async () => {
  await open();
  await expand(1, 3);
  await expand(2, 4);
  const first = container.querySelector('[data-move-folder="3"]')!;
  const second = container.querySelector('[data-move-folder="4"]')!;
  expect(first.textContent).toBe("第1周");
  expect(second.textContent).toBe("第1周");
  expect(first.getAttribute("title")).toBe("视频库 / 算法 / 第1周");
  expect(second.getAttribute("title")).toBe("视频库 / 数据库 / 第1周");
  expect(first.closest('[role="group"]')?.getAttribute("aria-label")).toBe("视频库 / 算法的子文件夹");
  click('[data-move-folder="4"]');
  expect(onMove).toHaveBeenCalledTimes(1);
  expect(onMove).toHaveBeenCalledWith(4);
  expect(container.querySelector('[aria-haspopup="menu"]')?.getAttribute("aria-expanded")).toBe("false");
});

it("disambiguates genuinely same-level names without merging or renaming them", async () => {
  folders.push({ id: 6, name: "第1周", parent_id: 1 });
  await open();
  await expand(1, 3);
  expect(container.querySelector('[data-move-folder="3"]')?.textContent).toBe("第1周#3");
  expect(container.querySelector('[data-move-folder="6"]')?.textContent).toBe("第1周#6");
  expect(folders.find(folder => folder.id === 6)?.name).toBe("第1周");
  click('[data-move-folder="6"]');
  expect(onMove).toHaveBeenCalledWith(6);
});

it("can browse the current folder but cannot move into it again", async () => {
  await render(1);
  await open();
  expect(container.querySelector<HTMLButtonElement>('[data-move-folder="1"]')?.disabled).toBe(true);
  expect(container.querySelector('[data-folder-id="1"] .move-folder-row')?.classList.contains("is-current")).toBe(true);
  await expand(1, 3);
  await expand(3, 5);
  click('[data-move-folder="5"]');
  expect(onMove).toHaveBeenCalledWith(5);
});

it("keeps loading inline and provides a retry without closing the menu or making a move", async () => {
  await open();
  vi.mocked(getLibrary).mockRejectedValueOnce(new Error("目录暂时不可用"));
  click('[data-folder-id="1"] > .move-folder-row .move-folder-toggle');
  await vi.waitFor(async () => { await flush(); expect(container.querySelector('[role="alert"]')?.textContent).toBe("目录暂时不可用"); });
  click('[title="重新读取文件夹"]');
  await vi.waitFor(async () => { await flush(); expect(container.querySelector('[data-move-folder="3"]')).not.toBeNull(); });
  expect(container.querySelector('[aria-haspopup="menu"]')?.getAttribute("aria-expanded")).toBe("true");
  expect(onMove).not.toHaveBeenCalled();
});

it("does not invent subfolders while a directory is still loading", async () => {
  await open();
  let finish!: (value: LibraryResponse) => void;
  vi.mocked(getLibrary).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  click('[data-folder-id="1"] > .move-folder-row .move-folder-toggle');
  expect(container.querySelector('[role="status"]')?.textContent).toContain("正在读取文件夹");
  expect(container.textContent).not.toContain("无子文件夹");
  await act(async () => { finish(response(1)); });
  await vi.waitFor(async () => { await flush(); expect(container.querySelector('[data-move-folder="3"]')).not.toBeNull(); });
});

it("deduplicates repeated IDs and ignores incorrect parent entries", async () => {
  await open();
  const branch = response(1);
  branch.items.push(...branch.items, { type: "folder", folder: folders[1] }, { type: "folder", folder: folders[0] });
  vi.mocked(getLibrary).mockResolvedValueOnce(branch);
  await expand(1, 3);
  expect(container.querySelectorAll('[data-folder-id="3"]')).toHaveLength(1);
  expect(container.querySelectorAll('[data-folder-id="1"]')).toHaveLength(1);
  expect(container.querySelectorAll('[data-folder-id="2"]')).toHaveLength(1);
});

it("supports keyboard expansion, collapse, and Escape returning focus", async () => {
  await open();
  const toggle = container.querySelector<HTMLButtonElement>('[data-folder-id="1"] .move-folder-toggle')!;
  act(() => toggle.focus());
  key("ArrowRight");
  await vi.waitFor(async () => { await flush(); expect(container.querySelector('[data-move-folder="3"]')).not.toBeNull(); });
  key("ArrowLeft");
  expect(container.querySelector('[data-move-folder="3"]')).toBeNull();
  key("ArrowDown");
  expect(document.activeElement?.getAttribute("data-move-folder")).toBe("1");
  key("Escape");
  expect(document.activeElement).toBe(container.querySelector('[aria-haspopup="menu"]'));
  expect(container.querySelector('[aria-haspopup="menu"]')?.getAttribute("aria-expanded")).toBe("false");
  expect(onMove).not.toHaveBeenCalled();
});

it("moves back to the root without loading child folders", async () => {
  await render(5);
  await open();
  click('[data-move-folder="root"]');
  expect(onMove).toHaveBeenCalledWith(null);
  expect(getLibrary).toHaveBeenCalledTimes(1);
});
