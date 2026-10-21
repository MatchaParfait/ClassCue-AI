import { createDenoiseStream } from "./denoiseStream";

export type AudioChannelMode = "original" | "left" | "right";

export interface AudioChannelRouter {
  setMode: (mode: AudioChannelMode) => void;
  setDenoise: (enabled: boolean) => Promise<void>;
  resume: () => Promise<void>;
  dispose: () => void;
}

export async function createAudioChannelRouter(
  element: HTMLMediaElement, onDenoiseError: (message: string) => void = () => undefined,
): Promise<AudioChannelRouter> {
  const context = new AudioContext({ sampleRate: 16000, latencyHint: "interactive" });
  try {
    // Resume before capturing the element so a denied activation leaves native audio intact.
    await context.resume();
    if (context.sampleRate !== 16000) throw new Error("播放器音频需要支持 16 kHz 的桌面浏览器");
    const stereo = context.createGain();
    stereo.channelCount = 2;
    stereo.channelCountMode = "explicit";
    stereo.channelInterpretation = "speakers";
    const splitter = context.createChannelSplitter(2);
    const merger = context.createChannelMerger(2);
    const source = context.createMediaElementSource(element);
    source.connect(context.destination);
    let denoise: Awaited<ReturnType<typeof createDenoiseStream>> | null = null;
    let currentMode: AudioChannelMode = "original";
    let disposed = false;

    const route = (mode: AudioChannelMode) => {
      const destination = denoise?.node ?? context.destination;
      source.disconnect();
      stereo.disconnect();
      splitter.disconnect();
      merger.disconnect();
      if (mode === "original") {
        source.connect(destination);
      } else {
        // Explicit stereo upmix also makes a true mono track audible in either mode.
        source.connect(stereo);
        stereo.connect(splitter);
        const channel = mode === "left" ? 0 : 1;
        splitter.connect(merger, channel, 0);
        splitter.connect(merger, channel, 1);
        merger.connect(destination);
      }
    };
    return {
      setMode(mode) {
        currentMode = mode;
        route(mode);
        denoise?.reset();
      },
      async setDenoise(enabled) {
        if (disposed) throw new Error("播放器已关闭");
        if (!denoise && enabled) {
          const stream = await createDenoiseStream(context, element, onDenoiseError);
          if (disposed) { stream.dispose(); throw new Error("播放器已关闭"); }
          denoise = stream;
          denoise.node.connect(context.destination);
          route(currentMode);
        }
        await denoise?.setEnabled(enabled);
      },
      async resume() {
        if (context.state !== "running") await context.resume();
      },
      dispose() {
        disposed = true;
        denoise?.dispose();
        source.disconnect();
        stereo.disconnect();
        splitter.disconnect();
        merger.disconnect();
        void context.close().catch(() => undefined);
      },
    };
  } catch (error) {
    void context.close().catch(() => undefined);
    throw error;
  }
}
