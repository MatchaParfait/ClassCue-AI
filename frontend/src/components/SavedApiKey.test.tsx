import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { revealAiApiKey } from "../api/client";
import { SavedApiKey } from "./SavedApiKey";

vi.mock("../api/client", () => ({ revealAiApiKey: vi.fn() }));

let container: HTMLDivElement;
let root: Root;
let onError: ReturnType<typeof vi.fn>;
const endpoint = "https://example.test/v1";

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.resetAllMocks();
  vi.mocked(revealAiApiKey).mockResolvedValue("test-only-secret");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  onError = vi.fn();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function mount(baseUrl = endpoint, hasApiKey = true, revision = 0) {
  act(() => root.render(
    <SavedApiKey key={`${baseUrl}:${hasApiKey}:${revision}`} baseUrl={baseUrl} hasApiKey={hasApiKey} onError={onError} />,
  ));
}

function mountEditable(draftValue = "", hasApiKey = true) {
  const onDraftChange = vi.fn();
  act(() => root.render(
    <SavedApiKey
      baseUrl={endpoint}
      hasApiKey={hasApiKey}
      onError={onError}
      editable
      draftValue={draftValue}
      onDraftChange={onDraftChange}
    />,
  ));
  return onDraftChange;
}

const input = () => container.querySelector("input")!;
const button = () => container.querySelector("button")!;

describe("saved API key visibility", () => {
  it("defaults to stars without requesting the secret", () => {
    mount();
    expect(input().value).toBe("********");
    expect(input().readOnly).toBe(true);
    expect(button().getAttribute("aria-label")).toBe("显示 API Key");
    expect(revealAiApiKey).not.toHaveBeenCalled();
  });

  it("reveals only on click and removes plaintext when hidden", async () => {
    mount();
    await act(async () => button().click());
    expect(revealAiApiKey).toHaveBeenCalledWith(endpoint, expect.any(AbortSignal));
    expect(input().value).toBe("test-only-secret");
    expect(button().getAttribute("aria-label")).toBe("隐藏 API Key");
    act(() => button().click());
    expect(input().value).toBe("********");
    expect(container.innerHTML).not.toContain("test-only-secret");
    await act(async () => button().click());
    expect(revealAiApiKey).toHaveBeenCalledTimes(2);
  });

  it("stays masked after a failed reveal and allows retry", async () => {
    vi.mocked(revealAiApiKey).mockRejectedValueOnce(new Error("Unable to load key"));
    mount();
    await act(async () => button().click());
    expect(input().value).toBe("********");
    expect(onError).toHaveBeenCalledOnce();
    await act(async () => button().click());
    expect(input().value).toBe("test-only-secret");
  });

  it("disables reveal for an unconfigured key", () => {
    mount(endpoint, false);
    expect(input().value).toBe("");
    expect(button().disabled).toBe(true);
    act(() => button().click());
    expect(revealAiApiKey).not.toHaveBeenCalled();
  });

  it("discards the visible secret after endpoint changes or key replacement", async () => {
    mount();
    await act(async () => button().click());
    mount("https://other.test/v1");
    expect(input().value).toBe("********");
    await act(async () => button().click());
    mount("https://other.test/v1", true, 1);
    expect(input().value).toBe("********");
  });

  it("cancels a pending reveal when hidden and ignores a late result", async () => {
    let resolve!: (value: string) => void;
    vi.mocked(revealAiApiKey).mockReturnValue(new Promise((done) => { resolve = done; }));
    mount();
    act(() => button().click());
    const signal = vi.mocked(revealAiApiKey).mock.calls[0][1]!;
    expect(button().getAttribute("aria-busy")).toBe("true");
    act(() => button().click());
    expect(signal.aborted).toBe(true);
    await act(async () => resolve("late-secret"));
    expect(input().value).toBe("********");
    expect(onError).not.toHaveBeenCalled();
  });

  it("cancels pending requests on endpoint change", async () => {
    let resolve!: (value: string) => void;
    vi.mocked(revealAiApiKey).mockReturnValue(new Promise((done) => { resolve = done; }));
    mount();
    act(() => button().click());
    const signal = vi.mocked(revealAiApiKey).mock.calls[0][1]!;
    mount("https://other.test/v1");
    expect(signal.aborted).toBe(true);
    await act(async () => resolve("old-endpoint-secret"));
    expect(input().value).toBe("********");
  });

  it("uses one editable field for replacing an existing key", () => {
    const onDraftChange = mountEditable();
    expect(input().readOnly).toBe(false);
    expect(input().value).toBe("********");
    act(() => input().dispatchEvent(new Event("focus")));
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      setter?.call(input(), "new-secret");
      input().dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(onDraftChange).toHaveBeenCalledWith("new-secret");
    expect(revealAiApiKey).not.toHaveBeenCalled();
  });

  it("toggles draft visibility locally without revealing the saved key", () => {
    mountEditable("new-secret");
    expect(input().type).toBe("password");
    act(() => button().click());
    expect(input().type).toBe("text");
    expect(button().getAttribute("aria-label")).toBe("隐藏 API Key");
    expect(revealAiApiKey).not.toHaveBeenCalled();
  });
});
