import workletUrl from "./gtcrn-worklet.js?url";

const FRAME_BYTES = 8 + 256 * 4;

export function denoiseSocketUrl() {
  const base = import.meta.env.VITE_API_BASE_URL ?? "";
  const url = new URL(`${base.replace(/\/$/, "")}/api/audio/denoise`, location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}

export async function createDenoiseStream(
  context: AudioContext, element: HTMLMediaElement, onError: (message: string) => void,
) {
  await context.audioWorklet.addModule(workletUrl);
  const node = new AudioWorkletNode(context, "gtcrn-playback", {
    numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [2],
  });
  let socket: WebSocket | null = null;
  let enabled = false;
  let disposed = false;
  let ready = false;
  let epoch = 0;
  let timer: ReturnType<typeof setInterval> | undefined;
  let resetTimeout: ReturnType<typeof setTimeout> | undefined;
  let rejectInitialization: ((error: Error) => void) | undefined;
  const pending = new Map<number, number>();

  const active = () => !element.paused && !element.ended && !element.seeking;
  const stop = () => {
    enabled = ready = false;
    pending.clear();
    clearInterval(timer);
    clearTimeout(resetTimeout);
    node.port.postMessage({ type: "reset", epoch: ++epoch, active: false });
    const old = socket;
    socket = null;
    old?.close();
    rejectInitialization?.(new Error("降噪初始化已取消"));
    rejectInitialization = undefined;
  };
  const fail = (message = "降噪服务不可用，已恢复原声，请重试") => {
    if (disposed || !socket) return;
    stop();
    onError(message);
  };
  const reset = () => {
    if (!enabled || socket?.readyState !== WebSocket.OPEN) return;
    ready = false;
    pending.clear();
    node.port.postMessage({ type: "reset", epoch: ++epoch, active: false });
    socket.send(JSON.stringify({ type: "reset", epoch }));
    clearTimeout(resetTimeout);
    resetTimeout = setTimeout(() => fail(), 10000);
  };
  const events = ["play", "pause", "seeking", "seeked", "ended", "emptied", "ratechange", "volumechange"] as const;
  for (const event of events) element.addEventListener(event, reset);

  node.port.onmessage = ({ data }) => {
    if (data.epoch !== epoch || !enabled || !ready) return;
    if (data.type === "input") {
      if (socket?.readyState !== WebSocket.OPEN) return;
      if (pending.size >= 24 || socket.bufferedAmount > 32768) {
        fail("降噪处理积压，已恢复原声，请重试");
        return;
      }
      const sequence = new DataView(data.buffer).getUint32(4, true);
      pending.set(sequence, performance.now());
      socket.send(data.buffer);
    } else if (data.type === "overflow" || data.type === "stalled") {
      fail("降噪处理未能跟上播放，已恢复原声，请重试");
    }
  };

  return {
    node,
    reset,
    async setEnabled(value: boolean) {
      if (!value) { stop(); return; }
      if (disposed) throw new Error("播放器已关闭");
      if (enabled) return;
      await new Promise<void>((resolve, reject) => {
        const ws = new WebSocket(denoiseSocketUrl());
        socket = ws;
        ws.binaryType = "arraybuffer";
        ready = false;
        const initialEpoch = ++epoch;
        node.port.postMessage({ type: "reset", epoch, active: false });
        const timeout = setTimeout(() => fail("降噪初始化超时，已恢复原声，请重试"), 10000);
        const rejectOnce = (error: Error) => { clearTimeout(timeout); reject(error); };
        rejectInitialization = rejectOnce;
        ws.onopen = () => ws.send(JSON.stringify({ type: "reset", epoch }));
        ws.onerror = () => fail();
        ws.onclose = () => { if (socket === ws) fail(); };
        ws.onmessage = ({ data }) => {
          if (socket !== ws || disposed) return;
          if (typeof data === "string") {
            let message;
            try { message = JSON.parse(data); } catch { fail(); return; }
            if (message.type === "error") { fail(message.message); return; }
            if (message.type !== "ready" || message.epoch !== epoch) return;
            if (message.sample_rate !== 16000) { fail(); return; }
            clearTimeout(resetTimeout);
            ready = enabled = true;
            node.port.postMessage({ type: "reset", epoch, active: active() });
            if (epoch === initialEpoch) {
              clearTimeout(timeout);
              rejectInitialization = undefined;
              resolve();
            }
          } else {
            if (!(data instanceof ArrayBuffer) || data.byteLength !== FRAME_BYTES) { fail(); return; }
            const header = new DataView(data);
            if (!ready || header.getUint32(0, true) !== epoch) return;
            const sequence = header.getUint32(4, true);
            if (!pending.delete(sequence)) { fail(); return; }
            node.port.postMessage({ type: "output", epoch, buffer: data }, [data]);
          }
        };
      });
      timer = setInterval(() => {
        const oldest = pending.values().next().value;
        if (oldest !== undefined && performance.now() - oldest > 1000) fail();
      }, 250);
    },
    dispose() {
      disposed = true;
      stop();
      for (const event of events) element.removeEventListener(event, reset);
      node.port.onmessage = null;
      node.disconnect();
    },
  };
}
