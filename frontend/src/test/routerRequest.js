import { transferableAbortController } from "node:util";

try {
  window.localStorage.getItem("__classcue_probe__");
} catch {
  const values = new Map();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: key => values.get(String(key)) ?? null,
      setItem: (key, value) => values.set(String(key), String(value)),
      removeItem: key => values.delete(String(key)),
      clear: () => values.clear(),
      key: index => [...values.keys()][index] ?? null,
      get length() { return values.size; },
    },
  });
}

// jsdom supplies DOM AbortSignals, while Node's Request requires a Node signal.
// React Router uses Request even for client-only navigation. Bridge cancellation
// in tests without replacing browser controllers or changing production code.
const NativeRequest = globalThis.Request;
globalThis.Request = class extends NativeRequest {
  constructor(input, init) {
    if (init?.signal) {
      const source = init.signal;
      const controller = transferableAbortController();
      if (source.aborted) controller.abort(source.reason);
      else source.addEventListener("abort", () => controller.abort(source.reason), { once: true });
      super(input, { ...init, signal: controller.signal });
    } else super(input, init);
  }
};
