import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { getLibrary, getVideos } from "../api/client";
import { transferPost, transferStatus, type TransferStatus } from "../api/deviceTransfers";
import DeviceTransferDialog from "./DeviceTransferDialog";

vi.mock("../api/client", () => ({ getLibrary: vi.fn(), getVideos: vi.fn() }));
vi.mock("../api/deviceTransfers", () => ({ transferPost: vi.fn(), transferStatus: vi.fn() }));
let root: Root, container: HTMLDivElement, query: QueryClient;
const onClose = vi.fn();
const itemId = "a".repeat(64);
const fixture: TransferStatus = {
  devices: [], pairings: [], snapshots: [], error: "",
  tasks: [{
    id: "synthetic-task", direction: "receive", title: "Synthetic course", state: "awaiting_confirmation",
    peer_name: "Synthetic tablet", total_bytes: 100, transferred_bytes: 0, video_count: 1, error: "", results: [],
    items: [{ id: itemId, name: "Synthetic lesson", need_video: false, conflict: true, same: false, blocked: "",
      subtitle_count: 2, marker_count: 1 }],
  }],
};
beforeEach(() => {
  vi.resetAllMocks();
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  query = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(transferStatus).mockResolvedValue(fixture);
  vi.mocked(transferPost).mockResolvedValue({});
  vi.mocked(getLibrary).mockResolvedValue({ folder: null, parent_id: null, sort: "manual", order: "asc", items: [] });
  vi.mocked(getVideos).mockResolvedValue([]);
});
afterEach(() => {
  act(() => root.unmount());
  query.clear();
  container.remove();
  vi.restoreAllMocks();
});
async function mount() {
  await act(async () => root.render(<QueryClientProvider client={query}>
    <DeviceTransferDialog libraryId="synthetic-library" returnFocus={null} onClose={onClose} />
  </QueryClientProvider>));
  await vi.waitFor(() => expect(document.body.textContent).toContain("Synthetic lesson"));
}
function button(text: string) {
  return [...document.querySelectorAll<HTMLButtonElement>("button")].find(element => element.textContent === text)!;
}

it("keeps conflicting study data skipped unless the user explicitly selects overwrite", async () => {
  await mount();
  expect(document.querySelector<HTMLSelectElement>('[aria-label="Synthetic lesson的接收操作"]')?.value).toBe("skip");
  await act(async () => button("确认接收").click());
  expect(transferPost).toHaveBeenCalledWith("/tasks/synthetic-task/accept", {
    library_id: "synthetic-library", items: [{ id: itemId, action: "skip" }], target_folder_id: null,
  });
});

it("sends explicit overwrite consent and keeps the preview usable after a failed confirmation", async () => {
  await mount();
  const select = document.querySelector<HTMLSelectElement>('[aria-label="Synthetic lesson的接收操作"]')!;
  await act(async () => { select.value = "overwrite"; select.dispatchEvent(new Event("change", { bubbles: true })); });
  vi.mocked(transferPost).mockRejectedValueOnce(new Error("Study changed after preview"));
  await act(async () => button("确认接收").click());
  expect(transferPost).toHaveBeenCalledWith("/tasks/synthetic-task/accept", {
    library_id: "synthetic-library", items: [{ id: itemId, action: "overwrite" }], target_folder_id: null,
  });
  expect(document.querySelector('[role="alert"]')?.textContent).toContain("Study changed after preview");
  expect(button("确认接收").disabled).toBe(false);
  expect(select.value).toBe("overwrite");
});

it("lets the dialog keyboard handler close on Escape", async () => {
  await mount();
  await act(async () => document.querySelector('[role="dialog"]')!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
  expect(onClose).toHaveBeenCalledTimes(1);
});
