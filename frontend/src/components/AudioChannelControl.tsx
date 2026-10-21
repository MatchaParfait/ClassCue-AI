import { AnimatePresence, AnimatedPopup } from "./Motion";
import { AudioLines, Headphones, LoaderCircle } from "lucide-react";
import { useEffect, useId, useRef, useState, type RefObject } from "react";
import { createAudioChannelRouter, type AudioChannelMode, type AudioChannelRouter } from "../audio/channelRouter";

const CHANNEL_OPTIONS = [
  { value: "original", label: "原始声道" },
  { value: "left", label: "左声道 → 双声道" },
  { value: "right", label: "右声道 → 双声道" },
] as const;

export function AudioChannelControl({ videoRef }: { videoRef: RefObject<HTMLVideoElement> }) {
  const id = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const routerRef = useRef<AudioChannelRouter | null>(null);
  const aliveRef = useRef(false);
  const busyRef = useRef(false);
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<AudioChannelMode>("original");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [denoising, setDenoising] = useState(false);
  const [denoiseError, setDenoiseError] = useState<string | null>(null);
  const [denoiseBusy, setDenoiseBusy] = useState(false);
  const supported = typeof AudioContext !== "undefined";
  const denoiseSupported = supported && typeof AudioWorkletNode !== "undefined";
  const label = CHANNEL_OPTIONS.find((option) => option.value === mode)!.label;

  useEffect(() => {
    aliveRef.current = true;
    const element = videoRef.current;
    const resumeAudio = () => {
      void routerRef.current?.resume().catch(() => {
        if (aliveRef.current) {
          setError("声道映射暂时无法启用，请重试");
          setOpen(true);
        }
      });
    };
    element?.addEventListener("play", resumeAudio);
    return () => {
      aliveRef.current = false;
      element?.removeEventListener("play", resumeAudio);
      // Strict Mode and Fast Refresh immediately reattach the same captured media element.
      queueMicrotask(() => {
        if (aliveRef.current) return;
        routerRef.current?.dispose();
        routerRef.current = null;
      });
    };
  }, [videoRef]);

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  const selectMode = async (next: AudioChannelMode) => {
    const element = videoRef.current;
    if (!element || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      if (!routerRef.current && next !== "original") {
        routerRef.current = await createAudioChannelRouter(element, handleDenoiseError);
      } else {
        await routerRef.current?.resume();
      }
      if (!aliveRef.current) {
        routerRef.current?.dispose();
        routerRef.current = null;
        return;
      }
      routerRef.current?.setMode(next);
      setMode(next);
      setOpen(false);
      buttonRef.current?.focus();
    } catch {
      if (aliveRef.current) setError("声道映射暂时无法启用，请重试");
    } finally {
      busyRef.current = false;
      if (aliveRef.current) setBusy(false);
    }
  };

  const handleDenoiseError = (message: string) => {
    if (!aliveRef.current) return;
    setDenoising(false);
    setDenoiseError(message);
  };

  const toggleDenoise = async () => {
    const element = videoRef.current;
    if (!element || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setDenoiseBusy(true);
    setDenoiseError(null);
    try {
      if (!routerRef.current) {
        routerRef.current = await createAudioChannelRouter(element, handleDenoiseError);
      } else {
        await routerRef.current.resume();
      }
      if (!aliveRef.current) {
        routerRef.current.dispose();
        routerRef.current = null;
        return;
      }
      await routerRef.current.setDenoise(!denoising);
      if (aliveRef.current) setDenoising(!denoising);
    } catch {
      handleDenoiseError("降噪暂时无法启用，已保留原声，请重试");
    } finally {
      busyRef.current = false;
      if (aliveRef.current) {
        setBusy(false);
        setDenoiseBusy(false);
      }
    }
  };

  return (
    <>
    <div className="audio-channel-control" ref={containerRef}>
      <button ref={buttonRef} className={`player-control audio-channel-button${mode !== "original" ? " is-active" : ""}`}
        type="button" aria-label="声道映射" title={supported ? `声道映射：${label}` : "此浏览器不支持声道映射"}
        aria-expanded={open} aria-controls={`${id}-panel`} aria-busy={busy} disabled={!supported || busy}
        onClick={() => setOpen((current) => !current)}>
        {busy ? <LoaderCircle className="spin" size={19} /> : <Headphones size={19} />}
        {mode !== "original" && <span className="audio-channel-badge" aria-hidden="true">{mode === "left" ? "L" : "R"}</span>}
      </button>
      <AnimatePresence>{open && (
        <AnimatedPopup className="audio-channel-popover" id={`${id}-panel`}><fieldset className="audio-channel-fieldset" aria-label="声道映射" disabled={busy}>
          {CHANNEL_OPTIONS.map((option) => (
            <label className="audio-channel-option" key={option.value}>
              <input type="radio" name={`${id}-mode`} value={option.value} checked={mode === option.value}
                onChange={() => void selectMode(option.value)} />
              <span>{option.label}</span>
            </label>
          ))}
          {error && <p className="audio-channel-error" role="alert">{error}</p>}
        </fieldset></AnimatedPopup>
      )}</AnimatePresence>
    </div>
    <div className="player-denoise-control">
      <button className={`player-control${denoising ? " is-active" : ""}`}
        type="button" aria-label="实时降噪" aria-pressed={denoising}
        aria-busy={denoiseBusy} disabled={!denoiseSupported || busy}
        title={!denoiseSupported ? "此浏览器不支持实时降噪" : denoiseBusy ? "正在加载 GTCRN" : denoising ? "关闭降噪（GTCRN）" : "开启降噪（GTCRN）"}
        onClick={() => void toggleDenoise()}>
        {denoiseBusy ? <LoaderCircle className="spin" size={19} /> : <AudioLines size={19} />}
      </button>
      {denoiseError && <p className="player-denoise-error" role="alert">{denoiseError}</p>}
    </div>
    </>
  );
}
