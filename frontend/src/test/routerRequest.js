import { transferableAbortController } from "node:util";

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
