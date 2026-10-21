import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createAudioChannelRouter } from "./channelRouter";

function node() {
  return {
    connect: vi.fn(), disconnect: vi.fn(),
    channelCount: 0, channelCountMode: "", channelInterpretation: "",
  };
}

let context: {
  state: string;
  sampleRate: number;
  destination: ReturnType<typeof node>;
  resume: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  createGain: ReturnType<typeof vi.fn>;
  createChannelSplitter: ReturnType<typeof vi.fn>;
  createChannelMerger: ReturnType<typeof vi.fn>;
  createMediaElementSource: ReturnType<typeof vi.fn>;
};
let source: ReturnType<typeof node>;
let stereo: ReturnType<typeof node>;
let splitter: ReturnType<typeof node>;
let merger: ReturnType<typeof node>;

beforeEach(() => {
  source = node();
  stereo = node();
  splitter = node();
  merger = node();
  context = {
    state: "running", sampleRate: 16000, destination: node(),
    resume: vi.fn().mockResolvedValue(undefined),
    close: vi.fn().mockResolvedValue(undefined),
    createGain: vi.fn(() => stereo),
    createChannelSplitter: vi.fn(() => splitter),
    createChannelMerger: vi.fn(() => merger),
    createMediaElementSource: vi.fn(() => source),
  };
  vi.stubGlobal("AudioContext", vi.fn(function () { return context; }));
});

afterEach(() => vi.unstubAllGlobals());

describe("audio channel routing", () => {
  it.each([["left", 0], ["right", 1]] as const)("copies %s to both outputs without mixing in the other channel", async (mode, channel) => {
    const element = document.createElement("video");
    element.volume = 0.4;
    element.muted = true;
    element.playbackRate = 1.5;
    element.currentTime = 12;
    const router = await createAudioChannelRouter(element);
    router.setMode(mode);
    expect(context.createMediaElementSource).toHaveBeenCalledWith(element);
    expect(source.connect).toHaveBeenLastCalledWith(stereo);
    expect(stereo.connect).toHaveBeenCalledWith(splitter);
    expect(splitter.connect.mock.calls).toEqual([[merger, channel, 0], [merger, channel, 1]]);
    expect(merger.connect).toHaveBeenCalledWith(context.destination);
    expect([element.volume, element.muted, element.playbackRate, element.currentTime]).toEqual([0.4, true, 1.5, 12]);
  });

  it("upmixes a mono input to stereo before selecting either channel", async () => {
    await createAudioChannelRouter(document.createElement("video"));
    expect(stereo).toMatchObject({ channelCount: 2, channelCountMode: "explicit", channelInterpretation: "speakers" });
    expect(context.createChannelSplitter).toHaveBeenCalledWith(2);
    expect(context.createChannelMerger).toHaveBeenCalledWith(2);
  });

  it("reuses one source and clears old connections when switching or restoring native channels", async () => {
    const router = await createAudioChannelRouter(document.createElement("video"));
    router.setMode("left");
    router.setMode("right");
    router.setMode("original");
    expect(context.createMediaElementSource).toHaveBeenCalledTimes(1);
    for (const audioNode of [source, stereo, splitter, merger]) expect(audioNode.disconnect).toHaveBeenCalledTimes(3);
    expect(source.connect).toHaveBeenLastCalledWith(context.destination);
  });

  it("does not capture native audio if the browser refuses activation", async () => {
    context.resume.mockRejectedValueOnce(new Error("Activation denied"));
    await expect(createAudioChannelRouter(document.createElement("video"))).rejects.toThrow("Activation denied");
    expect(context.createMediaElementSource).not.toHaveBeenCalled();
    expect(context.close).toHaveBeenCalledTimes(1);
  });

  it("closes the unused context on graph creation failure", async () => {
    context.createMediaElementSource.mockImplementationOnce(() => { throw new Error("Unavailable"); });
    await expect(createAudioChannelRouter(document.createElement("video"))).rejects.toThrow("Unavailable");
    expect(context.close).toHaveBeenCalledTimes(1);
  });

  it("resumes suspended audio when playback restarts", async () => {
    const router = await createAudioChannelRouter(document.createElement("video"));
    await router.resume();
    expect(context.resume).toHaveBeenCalledTimes(1);
    context.state = "suspended";
    await router.resume();
    expect(context.resume).toHaveBeenCalledTimes(2);
  });

  it("disconnects every node and releases the context when disposed", async () => {
    const router = await createAudioChannelRouter(document.createElement("video"));
    router.dispose();
    for (const audioNode of [source, stereo, splitter, merger]) expect(audioNode.disconnect).toHaveBeenCalledTimes(1);
    expect(context.close).toHaveBeenCalledTimes(1);
  });
});
