import { useShortcuts, matchesShortcut, shortcutTitle, shortcutAria } from "../shortcuts";
import { AnimatePresence, AnimatedPopup, useEntryMotion } from "./Motion";
import { useEffect, useRef, useState } from "react";
import { Captions, Maximize, Minimize, Pause, Play, RotateCcw, Type, Volume2, VolumeX } from "lucide-react";
import type { CSSProperties, KeyboardEvent, RefObject } from "react";
import type { Subtitle, Video } from "../types";
import { getStreamUrl } from "../api/client";
import { formatTime } from "../utils";
import { DraggableSubtitle } from "./DraggableSubtitle";
import { AudioChannelControl } from "./AudioChannelControl";
import type { SearchSeek } from "../searchNavigation";
import { useReviewTabs } from "./ReviewTabs";

interface VideoPanelProps {
  video: Video;
  subtitles: Subtitle[];
  videoId: number;
  libraryId: string;
  demo?: boolean;
  currentTime: number;
  onTimeUpdate: (time: number) => void;
  onDurationChange: (durationSeconds: number) => void;
  videoRef: RefObject<HTMLVideoElement>;
  navigationSeek?: SearchSeek;
  autoPlay?: boolean;
}

const PREFERENCES_KEY = "classroom-player-subtitles";

function readSubtitlePreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREFERENCES_KEY) ?? "{}") as { enabled?: boolean; fontSize?: number };
    return {
      enabled: saved.enabled !== false,
      fontSize: typeof saved.fontSize === "number" && Number.isFinite(saved.fontSize)
        ? Math.max(16, Math.min(40, saved.fontSize)) : 24,
    };
  } catch {
    return { enabled: true, fontSize: 24 };
  }
}

export function VideoPanel({ video, subtitles, videoId, libraryId, demo = false, currentTime, onTimeUpdate, onDurationChange, videoRef, navigationSeek, autoPlay = false }: VideoPanelProps) {
  const shortcuts = useShortcuts();
  const headingRef = useRef<HTMLDivElement>(null);
  useEntryMotion(headingRef, { duration: .14 });
  const progressKey = `classroom-study-progress:${libraryId}:${videoId}`;
  const positions = useReviewTabs()?.positions;
  useEffect(() => {
    const element = videoRef.current;
    if (!element) return;
    const save = () => {
      if (!Number.isFinite(element.currentTime) || element.readyState < 1) return;
      positions?.set(progressKey, element.currentTime);
      try { localStorage.setItem(progressKey, String(element.currentTime)); } catch { /* Playback does not require storage. */ }
    };
    window.addEventListener("pagehide", save);
    return () => {
      save();
      window.removeEventListener("pagehide", save);
      element.pause();
    };
  }, [progressKey, videoRef, positions]);
  const restoredRef = useRef(false);
  const consumedSeek = useRef<string | null>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  const subtitleSettingsRef = useRef<HTMLDivElement>(null);
  const subtitleSettingsButtonRef = useRef<HTMLButtonElement>(null);
  const [preferences, setPreferences] = useState(readSubtitlePreferences);
  const [subtitleSettingsOpen, setSubtitleSettingsOpen] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [duration, setDuration] = useState(0);
  const [volume, setVolume] = useState(1);
  const [muted, setMuted] = useState(false);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [fullscreen, setFullscreen] = useState(false);
  const [playbackError, setPlaybackError] = useState<string | null>(null);
  const [captionReset, setCaptionReset] = useState(0);
  const activeSubtitle = subtitles.find((subtitle) => currentTime * 1000 >= subtitle.start_ms && currentTime * 1000 < subtitle.end_ms);
  const progress = duration > 0 ? Math.min(100, currentTime / duration * 100) : 0;

  useEffect(() => {
    restoredRef.current = false;
    const element = videoRef.current;
    const seconds = element && Number.isFinite(element.duration) ? element.duration : 0;
    setDuration(seconds);
    setPlaying(Boolean(element && !element.paused));
    setPlaybackError(null);
    onTimeUpdate(element?.currentTime ?? 0);
    if (seconds > 0 && element) {
      onDurationChange(seconds);
      restoreProgress(element);
    }
  }, [progressKey]);

  useEffect(() => {
    const element = videoRef.current;
    if (element && Number.isFinite(element.duration) && element.duration > 0) applyNavigationSeek(element);
  }, [navigationSeek?.token, navigationSeek?.seconds]);

  useEffect(() => {
    try {
      localStorage.setItem(PREFERENCES_KEY, JSON.stringify(preferences));
    } catch {
      // Subtitle controls still work when browser storage is unavailable.
    }
  }, [preferences]);

  useEffect(() => {
    const syncFullscreen = () => setFullscreen(document.fullscreenElement === frameRef.current);
    document.addEventListener("fullscreenchange", syncFullscreen);
    return () => document.removeEventListener("fullscreenchange", syncFullscreen);
  }, []);

  useEffect(() => {
    if (!subtitleSettingsOpen) return;
    const closeOutside = (event: globalThis.PointerEvent) => {
      if (!subtitleSettingsRef.current?.contains(event.target as Node)) setSubtitleSettingsOpen(false);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        setSubtitleSettingsOpen(false);
        subtitleSettingsButtonRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [subtitleSettingsOpen]);

  const applyNavigationSeek = (element: HTMLVideoElement) => {
    if (!navigationSeek || consumedSeek.current === navigationSeek.token || !Number.isFinite(element.duration) || element.duration <= 0) return false;
    const next = Math.max(0, Math.min(element.duration, navigationSeek.seconds));
    element.currentTime = next;
    consumedSeek.current = navigationSeek.token;
    restoredRef.current = true;
    onTimeUpdate(next);
    // Seeking is reliable even if the browser denies autoplay after navigation.
    void element.play().catch(() => undefined);
    return true;
  };

  const restoreProgress = (element: HTMLVideoElement) => {
    if (navigationSeek) { applyNavigationSeek(element); return; }
    if (restoredRef.current) return;
    restoredRef.current = true;
    const sessionTime = positions?.get(progressKey);
    if (sessionTime !== undefined && Number.isFinite(sessionTime)) {
      const next = Math.max(0, Math.min(sessionTime, element.duration));
      element.currentTime = next;
      onTimeUpdate(next);
      return;
    }
    let saved = 0;
    try { saved = Number(localStorage.getItem(progressKey)); } catch { return; }
    if (Number.isFinite(saved) && saved > 1 && saved < element.duration - 3) {
      element.currentTime = saved;
      onTimeUpdate(saved);
    }
  };

  const saveProgress = (element: HTMLVideoElement) => {
    positions?.set(progressKey, element.currentTime);
    try { localStorage.setItem(progressKey, String(element.currentTime)); } catch { /* Playback does not require storage. */ }
  };

  const togglePlay = async () => {
    const element = videoRef.current;
    if (!element) return;
    if (!element.paused) {
      element.pause();
      return;
    }
    try {
      await element.play();
      setPlaybackError(null);
    } catch {
      setPlaybackError("视频无法播放，请检查视频文件或连接。");
    }
  };

  const seek = (seconds: number) => {
    if (!videoRef.current || duration <= 0) return;
    const next = Math.max(0, Math.min(duration, seconds));
    videoRef.current.currentTime = next;
    onTimeUpdate(next);
  };

  const toggleMute = () => {
    const element = videoRef.current;
    if (!element) return;
    const silent = element.muted || element.volume === 0;
    if (element.volume === 0) element.volume = 0.5;
    element.muted = !silent;
    setMuted(element.muted);
    setVolume(element.volume);
  };

  const toggleFullscreen = async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await frameRef.current?.requestFullscreen();
    } catch {
      setPlaybackError("浏览器暂时无法进入全屏。");
    }
  };

  const toggleSubtitles = () => setPreferences((current) => ({ ...current, enabled: !current.enabled }));
  const playerKeyboard = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.defaultPrevented || event.nativeEvent.isComposing || (event.target as HTMLElement).closest("button, input, select, textarea, [contenteditable], [role='group']")) return;
    if (event.repeat && !matchesShortcut(event.nativeEvent, shortcuts, "seekBack") && !matchesShortcut(event.nativeEvent, shortcuts, "seekForward")) return;
    if (matchesShortcut(event.nativeEvent, shortcuts, "playPause")) { event.preventDefault(); void togglePlay(); }
    else if (matchesShortcut(event.nativeEvent, shortcuts, "seekBack")) { event.preventDefault(); seek(currentTime - 5); }
    else if (matchesShortcut(event.nativeEvent, shortcuts, "seekForward")) { event.preventDefault(); seek(currentTime + 5); }
    else if (matchesShortcut(event.nativeEvent, shortcuts, "mute")) { event.preventDefault(); toggleMute(); }
    else if (matchesShortcut(event.nativeEvent, shortcuts, "subtitles") && subtitles.length) { event.preventDefault(); toggleSubtitles(); }
    else if (matchesShortcut(event.nativeEvent, shortcuts, "fullscreen")) { event.preventDefault(); void toggleFullscreen(); }
  };

  return (
    <section className="video-section">
      <div ref={headingRef} className="section-heading">
        <div>
          <h1>{video.display_name}</h1>
        </div>
        <span className="source-badge">{video.status === "review_ready" ? "可复习" : "字幕可查看"}</span>
      </div>
      <div ref={frameRef} className="video-frame" tabIndex={0} aria-label="视频播放器" onKeyDown={playerKeyboard}>
        <video
          ref={videoRef}
          className="video-player"
          playsInline
          autoPlay={autoPlay}
          crossOrigin="anonymous"
          preload="metadata"
          src={getStreamUrl(videoId, demo)}
          onLoadedMetadata={(event) => {
            const element = event.currentTarget;
            const seconds = Number.isFinite(element.duration) ? element.duration : 0;
            setDuration(seconds);
            onDurationChange(seconds);
            setPlaybackError(null);
            restoreProgress(element);
          }}
          onDurationChange={(event) => {
            const seconds = event.currentTarget.duration;
            if (Number.isFinite(seconds)) {
              setDuration(seconds);
              onDurationChange(seconds);
              applyNavigationSeek(event.currentTarget);
            }
          }}
          onTimeUpdate={(event) => onTimeUpdate(event.currentTarget.currentTime)}
          onPlay={() => setPlaying(true)}
          onPause={(event) => {
            setPlaying(false);
            saveProgress(event.currentTarget);
          }}
          onEnded={(event) => { setPlaying(false); saveProgress(event.currentTarget); }}
          onVolumeChange={(event) => { setVolume(event.currentTarget.volume); setMuted(event.currentTarget.muted); }}
          onRateChange={(event) => setPlaybackRate(event.currentTarget.playbackRate)}
          onError={() => setPlaybackError("视频加载失败，请检查视频文件或连接。")}
          onClick={() => void togglePlay()}
          onDoubleClick={() => void toggleFullscreen()}
        >
          你的浏览器不支持视频播放。
        </video>
        {subtitles.length > 0 && (
          <DraggableSubtitle key={`${progressKey}:${captionReset}`} text={preferences.enabled ? activeSubtitle?.text ?? "" : ""} fontSize={preferences.fontSize} />
        )}
        {playbackError && <div className="video-playback-error" role="status">{playbackError}</div>}
        <div className="player-controls">
          <input
            className="player-progress"
            type="range"
            aria-label="播放进度"
            aria-valuetext={`${formatTime(currentTime * 1000)} / ${formatTime(duration * 1000)}`}
            min={0}
            max={duration || 1}
            step={0.1}
            value={Math.min(currentTime, duration)}
            disabled={duration <= 0}
            style={{ "--progress": `${progress}%` } as CSSProperties}
            onChange={(event) => seek(Number(event.target.value))}
          />
          <div className="player-toolbar">
            <button className="player-control" type="button" aria-label={playing ? "暂停" : "播放"} title={shortcutTitle(playing ? "暂停" : "播放", shortcuts, "playPause")} aria-keyshortcuts={shortcutAria(shortcuts, "playPause")} onClick={() => void togglePlay()}>
              {playing ? <Pause size={20} /> : <Play size={20} />}
            </button>
            <button className="player-control" type="button" aria-label={muted || volume === 0 ? "取消静音" : "静音"} title={shortcutTitle(muted || volume === 0 ? "取消静音" : "静音", shortcuts, "mute")} aria-keyshortcuts={shortcutAria(shortcuts, "mute")} onClick={toggleMute}>
              {muted || volume === 0 ? <VolumeX size={19} /> : <Volume2 size={19} />}
            </button>
            <input
              className="player-volume"
              type="range"
              aria-label="音量"
              min={0} max={1} step={0.05} value={muted ? 0 : volume}
              onChange={(event) => {
                if (!videoRef.current) return;
                const next = Number(event.target.value);
                videoRef.current.volume = next;
                videoRef.current.muted = next === 0;
                setVolume(next);
                setMuted(next === 0);
              }}
            />
            <span className="player-time">
              <span>{formatTime(currentTime * 1000)}</span><span className="player-time-separator"> / </span><span>{formatTime(duration * 1000)}</span>
            </span>
            <div className="player-toolbar-spacer" />
            <AudioChannelControl videoRef={videoRef} />
            <button
              className={`player-control${preferences.enabled && subtitles.length ? " is-active" : ""}`}
              type="button"
              aria-label="字幕"
              aria-pressed={preferences.enabled}
              disabled={!subtitles.length}
              title={subtitles.length ? shortcutTitle(preferences.enabled ? "关闭字幕" : "开启字幕", shortcuts, "subtitles") : "暂无字幕"}
              aria-keyshortcuts={subtitles.length ? shortcutAria(shortcuts, "subtitles") : undefined}
              onClick={toggleSubtitles}
            ><Captions size={21} /></button>
            <div ref={subtitleSettingsRef} className="subtitle-settings">
              <button
                ref={subtitleSettingsButtonRef}
                className={`player-control${subtitleSettingsOpen ? " is-active" : ""}`}
                type="button"
                aria-label="字幕大小"
                aria-expanded={subtitleSettingsOpen}
                aria-controls="subtitle-settings-panel"
                disabled={!subtitles.length}
                title="字幕大小"
                onClick={() => setSubtitleSettingsOpen((open) => !open)}
              ><Type size={19} /></button>
              <AnimatePresence>{subtitleSettingsOpen && (
                <AnimatedPopup className="subtitle-settings-popover" id="subtitle-settings-panel" role="group" aria-label="字幕设置">
                  <label className="subtitle-size-label" htmlFor="subtitle-font-size"><span>字幕大小</span><output>{preferences.fontSize}px</output></label>
                  <input
                    id="subtitle-font-size" type="range" min={16} max={40} step={2}
                    value={preferences.fontSize}
                    onChange={(event) => setPreferences((current) => ({ ...current, fontSize: Number(event.target.value) }))}
                  />
                  <button type="button" onClick={() => { setCaptionReset((current) => current + 1); setSubtitleSettingsOpen(false); subtitleSettingsButtonRef.current?.focus(); }}>
                    <RotateCcw size={14} /> 恢复字幕位置
                  </button>
                </AnimatedPopup>
              )}</AnimatePresence>
            </div>
            <select
              className="player-speed"
              aria-label="播放速度"
              title="播放速度"
              value={playbackRate}
              onChange={(event) => {
                if (!videoRef.current) return;
                videoRef.current.playbackRate = Number(event.target.value);
                setPlaybackRate(Number(event.target.value));
              }}
            >
              {[0.5, 0.75, 1, 1.25, 1.5, 2].map((rate) => <option key={rate} value={rate}>{rate}x</option>)}
            </select>
            <button className="player-control" type="button" aria-label={fullscreen ? "退出全屏" : "全屏"} title={shortcutTitle(fullscreen ? "退出全屏" : "全屏", shortcuts, "fullscreen")} aria-keyshortcuts={shortcutAria(shortcuts, "fullscreen")} onClick={() => void toggleFullscreen()}>
              {fullscreen ? <Minimize size={19} /> : <Maximize size={19} />}
            </button>
          </div>
        </div>
      </div>
      <details className="video-meta-details">
        <summary>文件详情</summary>
        <div className="video-meta">
          <span>{video.filename}</span>
          <span>{video.video_path || "本地视频库"}</span>
        </div>
      </details>
    </section>
  );
}
