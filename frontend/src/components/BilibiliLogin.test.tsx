import { act } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import * as api from "../api/bilibili";
import type { BiliLoginSession } from "../api/bilibili";
import { BilibiliLogin } from "./BilibiliLogin";

vi.mock("../api/bilibili", () => ({
  startBiliLogin: vi.fn(), pollBiliLogin: vi.fn(), cancelBiliLogin: vi.fn(), deleteBiliAccount: vi.fn(),
}));

let host: HTMLDivElement, root: ReturnType<typeof createRoot>, client: QueryClient;
const activeChanged = vi.fn();
let unmounted: boolean;
const waiting = (): BiliLoginSession => ({
  session_id: "a".repeat(32), status: "waiting_scan",
  image: "data:image/svg+xml;base64,PHN2Zy8+", expires_at: Date.now() / 1000 + 180, poll_after_ms: 3000,
});

function button(label: string) {
  return [...host.querySelectorAll<HTMLButtonElement>("button")].find(item => item.textContent?.trim() === label)!;
}
async function render(configured = false, disabled = false) {
  await act(async () => root.render(<QueryClientProvider client={client}>
    <BilibiliLogin configured={configured} disabled={disabled} onActiveChange={activeChanged} />
  </QueryClientProvider>));
}
async function advance(ms: number) {
  await act(async () => { await vi.advanceTimersByTimeAsync(ms); });
}
beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  unmounted = false;
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  vi.mocked(api.startBiliLogin).mockImplementation(async () => waiting());
  vi.mocked(api.pollBiliLogin).mockImplementation(async () => waiting());
  vi.mocked(api.cancelBiliLogin).mockResolvedValue({ status: "cancelled" });
  vi.mocked(api.deleteBiliAccount).mockResolvedValue({ configured: false, pause: null });
});
afterEach(async () => {
  if (!unmounted) await act(async () => root.unmount());
  client.clear(); host.remove();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it("does not request a QR until clicked and never offers a manual Cookie field", async () => {
  await render();
  expect(api.startBiliLogin).not.toHaveBeenCalled();
  expect(api.pollBiliLogin).not.toHaveBeenCalled();
  expect(host.textContent).toContain("当前匿名读取");
  expect(host.querySelector("input")).toBeNull();
  await act(async () => button("扫码登录").click());
  expect(host.querySelector("img")?.alt).toBe("B 站登录二维码");
  expect(host.textContent).toContain("等待 B 站 App 扫码");
  expect(activeChanged).toHaveBeenLastCalledWith(true);
});

it("polls at a bounded interval through phone confirmation and success", async () => {
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const store = vi.fn();
  vi.stubGlobal("localStorage", { setItem: store });
  vi.stubGlobal("sessionStorage", { setItem: store });
  vi.mocked(api.pollBiliLogin)
    .mockResolvedValueOnce({ ...waiting(), status: "waiting_confirm" })
    .mockResolvedValueOnce({ ...waiting(), status: "success", image: "" });
  await render();
  await act(async () => button("扫码登录").click());
  await advance(2999);
  expect(api.pollBiliLogin).not.toHaveBeenCalled();
  await advance(1);
  expect(host.textContent).toContain("已扫码，请在手机上确认登录");
  await advance(3000);
  expect(host.textContent).toContain("登录成功");
  expect(host.querySelector("img")).toBeNull();
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["bilibili-account"] });
  expect(activeChanged).toHaveBeenLastCalledWith(false);
  await advance(10000);
  expect(api.pollBiliLogin).toHaveBeenCalledTimes(2);
  expect(store).not.toHaveBeenCalled();
});

it("lets an expired QR be refreshed only by the user", async () => {
  vi.mocked(api.pollBiliLogin).mockResolvedValue({ ...waiting(), status: "expired", image: "" });
  await render();
  await act(async () => button("扫码登录").click());
  await advance(3000);
  expect(host.textContent).toContain("二维码已过期");
  expect(host.querySelector("img")).toBeNull();
  await advance(10000);
  expect(api.startBiliLogin).toHaveBeenCalledTimes(1);
  await act(async () => button("刷新二维码").click());
  expect(api.startBiliLogin).toHaveBeenCalledTimes(2);
});

it("expires a QR locally even if no terminal platform result arrives", async () => {
  vi.mocked(api.startBiliLogin).mockResolvedValue({ ...waiting(), expires_at: Date.now() / 1000 + 2 });
  await render();
  await act(async () => button("扫码登录").click());
  await advance(2000);
  expect(host.textContent).toContain("二维码已过期");
  expect(api.pollBiliLogin).not.toHaveBeenCalled();
  expect(api.cancelBiliLogin).toHaveBeenCalledWith("a".repeat(32));
});

it("stops polling after cancellation and when its parent panel closes", async () => {
  await render();
  await act(async () => button("扫码登录").click());
  await act(async () => button("取消登录").click());
  await advance(6000);
  expect(api.pollBiliLogin).not.toHaveBeenCalled();
  expect(api.cancelBiliLogin).toHaveBeenCalledWith("a".repeat(32));
  await act(async () => button("扫码登录").click());
  await act(async () => root.unmount());
  unmounted = true;
  await advance(6000);
  expect(api.pollBiliLogin).not.toHaveBeenCalled();
  expect(activeChanged).toHaveBeenLastCalledWith(false);
});

it("stops on errors without automatic retries and allows manual retry", async () => {
  vi.mocked(api.pollBiliLogin).mockRejectedValue(new Error("B 站要求验证"));
  await render();
  await act(async () => button("扫码登录").click());
  await advance(3000);
  expect(host.querySelector('[role="alert"]')?.textContent).toContain("B 站要求验证");
  expect(host.querySelector("img")).toBeNull();
  await advance(9000);
  expect(api.pollBiliLogin).toHaveBeenCalledTimes(1);
  await act(async () => button("重试登录").click());
  expect(api.startBiliLogin).toHaveBeenCalledTimes(2);
});

it("disables account changes during a study task and removes saved state on logout", async () => {
  const invalidate = vi.spyOn(client, "invalidateQueries");
  await render(true, true);
  expect(button("重新扫码登录").disabled).toBe(true);
  expect(button("退出登录").disabled).toBe(true);
  await render(true, false);
  await act(async () => button("退出登录").click());
  expect(api.deleteBiliAccount).toHaveBeenCalledTimes(1);
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["bilibili-account"] });
});

it("cancels a generated session returned after the user cancels the request", async () => {
  let resolve!: (value: BiliLoginSession) => void;
  vi.mocked(api.startBiliLogin).mockReturnValue(new Promise(done => { resolve = done; }));
  await render();
  await act(async () => button("扫码登录").click());
  await act(async () => button("取消登录").click());
  await act(async () => resolve(waiting()));
  expect(host.querySelector("img")).toBeNull();
  expect(api.cancelBiliLogin).toHaveBeenCalledWith("a".repeat(32));
  await advance(6000);
  expect(api.pollBiliLogin).not.toHaveBeenCalled();
});
