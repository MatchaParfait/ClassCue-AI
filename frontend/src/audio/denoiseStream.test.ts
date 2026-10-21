import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createDenoiseStream } from "./denoiseStream";

class FakeSocket {
  static OPEN = 1;
  static instances: FakeSocket[] = [];
  readyState = 0;
  bufferedAmount = 0;
  binaryType = "";
  send = vi.fn();
  close = vi.fn(() => { this.readyState = 3; this.onclose?.(); });
  onopen?: () => void;
  onerror?: () => void;
  onclose?: () => void;
  onmessage?: (event: { data: string | ArrayBuffer }) => void;
  constructor(public url: string) { FakeSocket.instances.push(this); }
  open() { this.readyState = 1; this.onopen?.(); }
  ready() {
    const { epoch } = JSON.parse(this.send.mock.calls.at(-1)![0] as string);
    this.onmessage?.({ data: JSON.stringify({ type: "ready", epoch, sample_rate: 16000 }) });
    return epoch as number;
  }
}

let port: { postMessage: ReturnType<typeof vi.fn>; onmessage: ((event: { data: unknown }) => void) | null };
let disconnect: ReturnType<typeof vi.fn>;
let element: HTMLVideoElement;
let context: AudioContext;

beforeEach(() => {
  FakeSocket.instances = [];
  port = { postMessage: vi.fn(), onmessage: null };
  disconnect = vi.fn();
  element = document.createElement("video");
  Object.defineProperty(element, "paused", { configurable: true, value: false });
  context = { audioWorklet: { addModule: vi.fn().mockResolvedValue(undefined) } } as unknown as AudioContext;
  vi.stubGlobal("AudioWorkletNode", vi.fn(function () { return { port, disconnect }; }));
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function input(epoch: number, sequence: number) {
  const buffer = new ArrayBuffer(8 + 256 * 4);
  const header = new DataView(buffer);
  header.setUint32(0, epoch, true);
  header.setUint32(4, sequence, true);
  port.onmessage?.({ data: { type: "input", epoch, buffer } });
  return buffer;
}

async function start() {
  const onError = vi.fn();
  const stream = await createDenoiseStream(context, element, onError);
  const starting = stream.setEnabled(true);
  const ws = FakeSocket.instances.at(-1)!;
  ws.open();
  const epoch = ws.ready();
  await starting;
  return { stream, ws, epoch, onError };
}

describe("GTCRN local stream lifecycle", () => {
  it("sends PCM only while enabled and restarts using the same worklet", async () => {
    const { stream, ws, epoch } = await start();
    const buffer = input(epoch, 0);
    expect(ws.send).toHaveBeenLastCalledWith(buffer);
    await stream.setEnabled(false);
    expect(ws.close).toHaveBeenCalledTimes(1);
    input(epoch, 1);
    expect(ws.send).toHaveBeenCalledTimes(2);
    const restarting = stream.setEnabled(true);
    const next = FakeSocket.instances.at(-1)!;
    next.open();
    next.ready();
    await restarting;
    expect(context.audioWorklet.addModule).toHaveBeenCalledTimes(1);
    expect(FakeSocket.instances).toHaveLength(2);
    stream.dispose();
  });

  it("resets generations on seek and rejects late output", async () => {
    const { stream, ws, epoch } = await start();
    const old = input(epoch, 0);
    element.dispatchEvent(new Event("seeking"));
    const next = ws.ready();
    expect(next).toBeGreaterThan(epoch);
    port.postMessage.mockClear();
    ws.onmessage?.({ data: old });
    expect(port.postMessage).not.toHaveBeenCalled();
    const packet = input(next, 0);
    ws.onmessage?.({ data: packet });
    expect(port.postMessage).toHaveBeenCalledWith({ type: "output", epoch: next, buffer: packet }, [packet]);
    stream.dispose();
  });

  it("recovers from a disconnected backend without pausing media", async () => {
    const { stream, ws, onError } = await start();
    const pause = vi.spyOn(element, "pause");
    ws.close();
    expect(onError).toHaveBeenCalledWith(expect.stringContaining("原声"));
    expect(pause).not.toHaveBeenCalled();
    stream.dispose();
  });

  it("limits pending frames and falls back before memory can grow indefinitely", async () => {
    const { stream, epoch, onError, ws } = await start();
    for (let i = 0; i < 25; i++) input(epoch, i);
    expect(onError).toHaveBeenCalledWith(expect.stringContaining("积压"));
    expect(ws.close).toHaveBeenCalledTimes(1);
    stream.dispose();
  });

  it("times out an unresponsive initialized backend", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "performance"] });
    const { stream, epoch, onError } = await start();
    input(epoch, 0);
    vi.advanceTimersByTime(1500);
    expect(onError).toHaveBeenCalledWith(expect.stringContaining("原声"));
    stream.dispose();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("times out a reset acknowledgement after seeking instead of remaining falsely active", async () => {
    vi.useFakeTimers();
    const { stream, onError } = await start();
    element.dispatchEvent(new Event("seeking"));
    vi.advanceTimersByTime(10000);
    expect(onError).toHaveBeenCalledWith(expect.stringContaining("原声"));
    stream.dispose();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("times out initialization and leaves the node bypassed", async () => {
    vi.useFakeTimers();
    const onError = vi.fn();
    const stream = await createDenoiseStream(context, element, onError);
    const starting = stream.setEnabled(true);
    const rejected = expect(starting).rejects.toThrow("取消");
    vi.advanceTimersByTime(10000);
    await rejected;
    expect(onError).toHaveBeenCalledWith(expect.stringContaining("初始化超时"));
    expect(port.postMessage).toHaveBeenLastCalledWith(expect.objectContaining({ active: false }));
    stream.dispose();
  });

  it("cancels initialization on unmount and removes all media listeners", async () => {
    const stream = await createDenoiseStream(context, element, vi.fn());
    const starting = stream.setEnabled(true);
    const rejected = expect(starting).rejects.toThrow("取消");
    const ws = FakeSocket.instances.at(-1)!;
    stream.dispose();
    await rejected;
    expect(disconnect).toHaveBeenCalledTimes(1);
    expect(port.onmessage).toBeNull();
    element.dispatchEvent(new Event("play"));
    expect(ws.send).not.toHaveBeenCalled();
  });

  it("falls back on invalid or unsolicited output", async () => {
    const { stream, ws, onError } = await start();
    ws.onmessage?.({ data: new ArrayBuffer(8 + 256 * 4) });
    // A stale epoch is ignored, but a malformed packet is never forwarded.
    ws.onmessage?.({ data: new ArrayBuffer(5) });
    expect(onError).toHaveBeenCalledWith(expect.stringContaining("原声"));
    stream.dispose();
  });
});
