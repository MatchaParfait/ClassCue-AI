import { describe, expect, it, vi } from "vitest";
import source from "./gtcrn-worklet.js?raw";

interface Processor {
  port: { postMessage: ReturnType<typeof vi.fn>; onmessage: (event: { data: unknown }) => void };
  process: (inputs: Float32Array[][], outputs: Float32Array[][]) => boolean;
}

function processor() {
  let ProcessorClass!: new () => Processor;
  class Base { port = { postMessage: vi.fn(), onmessage: () => undefined }; }
  new Function("AudioWorkletProcessor", "registerProcessor", source)(Base, (_name: string, type: typeof ProcessorClass) => { ProcessorClass = type; });
  return new ProcessorClass();
}

function render(worklet: Processor, left = 0.1, right = 0.2) {
  const output = [new Float32Array(128), new Float32Array(128)];
  worklet.process([[new Float32Array(128).fill(left), new Float32Array(128).fill(right)]], [output]);
  return output;
}

function reset(worklet: Processor, epoch: number, active: boolean) {
  worklet.port.onmessage({ data: { type: "reset", epoch, active } });
}

function output(worklet: Processor, epoch: number, value = 0.03) {
  const buffer = new ArrayBuffer(8 + 256 * 4);
  new Float32Array(buffer, 8).fill(value);
  worklet.port.onmessage({ data: { type: "output", epoch, buffer } });
}

describe("GTCRN playback worklet", () => {
  it("passes stereo through and does not send audio while off", () => {
    const worklet = processor();
    const [left, right] = render(worklet);
    expect(left[0]).toBeCloseTo(0.1);
    expect(right[0]).toBeCloseTo(0.2);
    expect(worklet.port.postMessage).not.toHaveBeenCalled();
  });

  it("emits ordered 256-sample mono packets while preserving dry audio until output arrives", () => {
    const worklet = processor();
    reset(worklet, 7, true);
    expect(render(worklet)[0][0]).toBeCloseTo(0.1);
    render(worklet);
    const input = worklet.port.postMessage.mock.calls[0][0];
    expect(input.type).toBe("input");
    expect(new DataView(input.buffer).getUint32(0, true)).toBe(7);
    expect(new DataView(input.buffer).getUint32(4, true)).toBe(0);
    expect(new Float32Array(input.buffer, 8)[0]).toBeCloseTo(0.15);
  });

  it("crossfades to real output and returns to stereo without silence when disabled", () => {
    const worklet = processor();
    reset(worklet, 1, true);
    for (let i = 0; i < 6; i++) output(worklet, 1);
    for (let i = 0; i < 4; i++) render(worklet);
    expect(render(worklet)[0][0]).toBeCloseTo(0.03);
    reset(worklet, 2, false);
    for (let i = 0; i < 4; i++) render(worklet);
    const [left, right] = render(worklet);
    expect(left[0]).toBeCloseTo(0.1);
    expect(right[0]).toBeCloseTo(0.2);
  });

  it("rejects stale output after seek and resets sequence", () => {
    const worklet = processor();
    reset(worklet, 1, true);
    reset(worklet, 2, true);
    for (let i = 0; i < 6; i++) output(worklet, 1, 0.9);
    expect(render(worklet)[0][0]).toBeCloseTo(0.1);
    render(worklet);
    const input = worklet.port.postMessage.mock.calls[0][0];
    expect(new DataView(input.buffer).getUint32(0, true)).toBe(2);
    expect(new DataView(input.buffer).getUint32(4, true)).toBe(0);
  });

  it("bounds the wet queue and recovers to original audio on overflow", () => {
    const worklet = processor();
    reset(worklet, 3, true);
    for (let i = 0; i < 17; i++) output(worklet, 3);
    expect(worklet.port.postMessage).toHaveBeenCalledWith({ type: "overflow", epoch: 3 });
    expect(render(worklet)[0][0]).toBeCloseTo(0.1);
  });
});
