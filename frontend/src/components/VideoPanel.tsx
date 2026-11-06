import { useShortcuts, matchesShortcut, shortcutTitle, shortcutAria } from "../shortcuts";
import { AnimatePresence, AnimatedPopup, useEntryMotion } from "./Motion";
import { useEffect, useId, useImperativeHandle, useRef, useState } from "react";
import { Captions, Check, ChevronDown, Maximize, Minimize, Pause, Play, RotateCcw, Type, Volume2, VolumeX } from "lucide-react";
import type { CSSProperties, PointerEvent as ReactPointerEvent, RefObject } from "react";
import type { SecondaryView, Subtitle, Video } from "../types";
import { getStreamUrl } from "../api/client";
import { formatTime } from "../utils";
import { DraggableSubtitle } from "./DraggableSubtitle";
import { AudioChannelControl } from "./AudioChannelControl";
import type { SearchSeek } from "../searchNavigation";
import { useReviewTabs } from "./ReviewTabs";
import { playbackProgressKey, savePlaybackProgress } from "../playbackProgress";
import { SecondaryViewControl } from "./SecondaryViewControl";

export interface PlayerTimeline {
  getCurrentTime: () => number;
  seekTo: (seconds: number, autoPlay?: boolean) => void;
  isReady: () => boolean;
}

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
  timelineRef?: RefObject<PlayerTimeline>;
  navigationSeek?: SearchSeek;
  autoPlay?: boolean;
  studyProgressReady?: boolean;
  studyPositionMs?: number;
  studyObservedAt?: number;
  onStudyProgress?: (seconds: number, duration: number, keepalive?: boolean) => void;
}

const PREFERENCES_KEY = "classroom-player-subtitles";
const PLAYER_INPUTS = 'input, select, textarea, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="combobox"], [data-shortcut-recorder], .video-caption, .review-tabs, [role="tablist"], [role="menu"], [role="listbox"], [role="dialog"]';
const PLAYER_POPOVERS = '.audio-channel-popover, .subtitle-settings-popover, .secondary-view-popover, .player-speed-menu, [role="dialog"], [role="menu"], [role="listbox"]';

function isPlayerHidden(element: Element) {
  return Boolean(element.closest('[hidden], [inert], [aria-hidden="true"]'));
}

function holdKey(event: { code: string; key: string }): string {
  return event.code || event.key.toLowerCase();
}

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

export function VideoPanel({ video, subtitles, videoId, libraryId, demo = false, currentTime, onTimeUpdate, onDurationChange, videoRef, timelineRef, navigationSeek, autoPlay = false,
  studyProgressReady = true, studyPositionMs, studyObservedAt, onStudyProgress }: VideoPanelProps) {
  const shortcuts = useShortcuts();
  const headingRef = useRef<HTMLDivElement>(null);
  useEntryMotion(headingRef, { duration: .14 });
  const progressKey = playbackProgressKey(libraryId, videoId);
  const positions = useReviewTabs()?.positions;
  const studySave = useRef(onStudyProgress);
  studySave.current = onStudyProgress;
  const studyReady = useRef(studyProgressReady);
  studyReady.current = studyProgressReady;
  const lastStudySave = useRef(0);
  const [secondary, setSecondary] = useState<SecondaryView | null>(null);
  const [secondaryActive, setSecondaryActive] = useState(false);
  const primaryAudioRef = useRef<HTMLAudioElement>(null);
  const usePrimaryAudio = secondaryActive && Boolean(secondary?.use_primary_audio);
  const [switching, setSwitching] = useState(false);
  const mainDuration = useRef(0);
  const secondaryDuration = useRef(0);
  const mapping = useRef({ active: false, offset: 0 });
  const pendingSource = useRef<{
    time: number; resume: boolean; rate: number; volume: number; muted: boolean;
  } | null>(null);
  const queuedPrimarySeek = useRef<{ seconds: number; play: boolean } | null>(null);
  const lastPrimaryTime = useRef(0);
  const boundTime = (time: number) => Math.max(0, Math.min(mainDuration.current || Infinity, time));
  const getPrimaryTime = () => {
    if (pendingSource.current) return pendingSource.current.time;
    const element = videoRef.current;
    return element && element.readyState >= 1 && Number.isFinite(element.currentTime)
      ? boundTime(element.currentTime - mapping.current.offset) : lastPrimaryTime.current;
  };
  useEffect(() => {
    const element = videoRef.current;
    if (!element) return;
    const save = () => {
      if (!studyReady.current && !restoredRef.current) return;
      if (!pendingSource.current && (!Number.isFinite(element.currentTime) || element.readyState < 1)) return;
      const time = pendingSource.current?.time ?? boundTime(element.currentTime - mapping.current.offset);
      savePlaybackProgress(progressKey, time, positions);
      if (restoredRef.current) studySave.current?.(time, mainDuration.current, true);
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
  const audioSettings = useRef({ volume, muted });
  audioSettings.current = { volume, muted };
  const [playbackRate, setPlaybackRate] = useState(1);
  const [longPressing, setLongPressing] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  const [playbackError, setPlaybackError] = useState<string | null>(null);
  const [captionReset, setCaptionReset] = useState(0);
  const longPressTimer = useRef<number | null>(null);
  const longPressPointerId = useRef<number | null>(null);
  const longPressRestore = useRef<{ rate: number; wasPaused: boolean } | null>(null);
  const suppressLongPressClick = useRef(false);
  const keyboardLongPressTimer = useRef<number | null>(null);
  const keyboardLongPressKey = useRef<string | null>(null);
  const keyboardLongPressDelta = useRef<number | null>(null);
  const keyboardLongPressRestore = useRef<{ rate: number; wasPaused: boolean } | null>(null);
  const keyboardNavigation = useRef(true);
  const activeSubtitle = subtitles.find((subtitle) => currentTime * 1000 >= subtitle.start_ms && currentTime * 1000 < subtitle.end_ms);
  const progress = duration > 0 ? Math.min(100, currentTime / duration * 100) : 0;

  const publishTime = (time: number) => {
    lastPrimaryTime.current = time;
    onTimeUpdate(time);
  };

  const switchSource = (active: boolean, time = getPrimaryTime(), resume = !videoRef.current?.paused) => {
    const element = videoRef.current;
    if (!element || (active && !secondary)) return;
    const offset = active ? secondary!.offset_ms / 1000 : 0;
    const target = time + offset;
    if (active && (target < 0 || (secondaryDuration.current > 0 && target >= secondaryDuration.current))) {
      setPlaybackError("当前主视频时间超出副视角范围，已保留主视角。");
      return;
    }
    pendingSource.current = pendingSource.current
      ? { ...pendingSource.current, time, resume }
      : { time, resume, rate: element.playbackRate, volume: element.volume, muted: usePrimaryAudio ? muted : element.muted };
    // Source changes can emit pause/timeupdate with an empty timeline.
    element.pause();
    mapping.current = { active, offset };
    setSwitching(true);
    setSecondaryActive(active);
    setPlaybackError(null);
    publishTime(time);
  };

  const seek = (seconds: number, play = false) => {
    const element = videoRef.current;
    if (!element || !Number.isFinite(seconds)) return;
    if (mainDuration.current <= 0) {
      queuedPrimarySeek.current = { seconds, play };
      publishTime(Math.max(0, seconds));
      return;
    }
    const next = boundTime(seconds);
    if (pendingSource.current) {
      pendingSource.current.time = next;
      if (play) pendingSource.current.resume = true;
      publishTime(next);
      return;
    }
    const target = next + mapping.current.offset;
    if (mapping.current.active && (target < 0 || target >= element.duration)) {
      switchSource(false, next, play || !element.paused);
      setPlaybackError("该时间点超出副视角范围，已切回主视角。");
      return;
    }
    element.currentTime = target;
    publishTime(next);
    if (play) void element.play().catch(() => undefined);
  };

  const focusPlayer = () => frameRef.current?.focus({ preventScroll: true });

  useImperativeHandle(timelineRef, () => ({
    getCurrentTime: getPrimaryTime,
    seekTo: (seconds, play = false) => {
      if (!Number.isFinite(seconds)) return;
      seek(seconds, play);
      focusPlayer();
    },
    isReady: () => !pendingSource.current && (videoRef.current?.readyState ?? 0) >= 2,
  }));

  const changeSecondary = (view: SecondaryView | null) => {
    const time = getPrimaryTime();
    if (!view && mapping.current.active) switchSource(false, time);
    if (view && mapping.current.active && view.offset_ms / 1000 !== mapping.current.offset) {
      const offset = view.offset_ms / 1000;
      const target = time + offset;
      if (target < 0 || (secondaryDuration.current > 0 && target >= secondaryDuration.current)) {
        switchSource(false, time);
        setPlaybackError("新的偏移超出副视角范围，已切回主视角。");
      } else {
        mapping.current.offset = offset;
        if (pendingSource.current) pendingSource.current.time = time;
        else if (videoRef.current) videoRef.current.currentTime = target;
        publishTime(time);
      }
    }
    secondaryDuration.current = view?.duration_ms ? view.duration_ms / 1000 : 0;
    setSecondary(view);
  };

  useEffect(() => {
    const audio = primaryAudioRef.current;
    if (audio) { audio.volume = volume; audio.muted = muted; }
  }, [volume, muted]);

  useEffect(() => {
    const element = videoRef.current;
    const audio = primaryAudioRef.current;
    if (!element || !audio) return;
    element.muted = usePrimaryAudio || audioSettings.current.muted;
    if (!usePrimaryAudio) return;
    let disposed = false;
    let frame = 0;
    let starting = false;
    let failed = false;
    let buffering = false;
    const sync = () => {
      if (disposed) return;
      const settings = audioSettings.current;
      if (audio.volume !== settings.volume) audio.volume = settings.volume;
      if (audio.muted !== settings.muted) audio.muted = settings.muted;
      if (audio.playbackRate !== element.playbackRate) audio.playbackRate = element.playbackRate;
      const time = getPrimaryTime();
      if (audio.readyState >= 1 && Math.abs(audio.currentTime - time) > .12) audio.currentTime = time;
      if (buffering || element.paused || element.ended || element.seeking || element.readyState < 3 || pendingSource.current) {
        if (!audio.paused) audio.pause();
      } else if (audio.paused && !starting && !failed) {
        starting = true;
        void audio.play().catch(() => {
          failed = true;
          if (!disposed) setPlaybackError("主视频音频暂时无法播放，请点击播放重试。");
        }).finally(() => { starting = false; });
      }
    };
    const stop = () => { buffering = true; audio.pause(); };
    const resume = () => { buffering = false; failed = false; sync(); };
    const events = ["play", "playing", "pause", "seeking", "seeked", "timeupdate", "ratechange", "ended"];
    events.forEach(name => element.addEventListener(name, sync));
    element.addEventListener("waiting", stop);
    element.addEventListener("playing", resume);
    element.addEventListener("play", resume);
    audio.addEventListener("loadedmetadata", sync);
    const tick = () => { sync(); frame = requestAnimationFrame(tick); };
    sync();
    frame = requestAnimationFrame(tick);
    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      events.forEach(name => element.removeEventListener(name, sync));
      element.removeEventListener("waiting", stop);
      element.removeEventListener("playing", resume);
      element.removeEventListener("play", resume);
      audio.removeEventListener("loadedmetadata", sync);
      audio.pause();
    };
  }, [usePrimaryAudio, switching, videoRef]);

  useEffect(() => {
    restoredRef.current = false;
    const element = videoRef.current;
    const seconds = element && Number.isFinite(element.duration) ? element.duration : 0;
    mainDuration.current = seconds;
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
    const next = boundTime(navigationSeek.seconds);
    seek(next, true);
    consumedSeek.current = navigationSeek.token;
    restoredRef.current = true;
    return true;
  };

  const restoreProgress = (element: HTMLVideoElement) => {
    if (navigationSeek) { applyNavigationSeek(element); return; }
    if (restoredRef.current || !studyProgressReady) return;
    restoredRef.current = true;
    let localUpdatedAt = 0;
    try { localUpdatedAt = Number(localStorage.getItem(`${progressKey}:updated-at`)) || 0; } catch { /* Optional metadata. */ }
    if (studyPositionMs !== undefined && localUpdatedAt <= (studyObservedAt ?? 0)) {
      seek(Math.max(0, Math.min(studyPositionMs / 1000, element.duration)));
      return;
    }
    const sessionTime = positions?.get(progressKey);
    if (sessionTime !== undefined && Number.isFinite(sessionTime)) {
      const next = Math.max(0, Math.min(sessionTime, element.duration));
      seek(next);
      return;
    }
    let saved = 0;
    try { saved = Number(localStorage.getItem(progressKey)); } catch { return; }
    if (localUpdatedAt > 0 && Number.isFinite(saved) && saved >= 0) {
      seek(Math.min(saved, element.duration));
      return;
    }
    if (Number.isFinite(saved) && saved > 1 && saved < element.duration - 3) {
      seek(saved);
    }
  };

  const saveProgress = (element: HTMLVideoElement) => {
    if (pendingSource.current || element.readyState < 1 || !restoredRef.current) return;
    const time = boundTime(element.currentTime - mapping.current.offset);
    savePlaybackProgress(progressKey, time, positions);
    studySave.current?.(time, mainDuration.current);
    lastStudySave.current = Date.now();
  };

  useEffect(() => {
    const element = videoRef.current;
    if (element && element.readyState >= 1 && Number.isFinite(element.duration) && element.duration > 0) restoreProgress(element);
  }, [studyProgressReady, studyPositionMs, studyObservedAt, progressKey]);

  const togglePlay = async () => {
    const element = videoRef.current;
    if (!element) return;
    if (pendingSource.current) {
      pendingSource.current.resume = !pendingSource.current.resume;
      setPlaying(pendingSource.current.resume);
      return;
    }
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

  const toggleMute = () => {
    const element = videoRef.current;
    if (!element) return;
    const silent = usePrimaryAudio ? muted || volume === 0 : element.muted || element.volume === 0;
    if (element.volume === 0) element.volume = 0.5;
    element.muted = usePrimaryAudio || !silent;
    setMuted(!silent);
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
  const clearLongPressTimer = () => {
    if (longPressTimer.current !== null) {
      window.clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
  };
  const restoreLongPressPlayback = (restore: { rate: number; wasPaused: boolean }) => {
    if (pendingSource.current) {
      pendingSource.current.rate = restore.rate;
      if (restore.wasPaused) pendingSource.current.resume = false;
    }
    const element = videoRef.current;
    if (element) {
      element.playbackRate = restore.rate;
      if (restore.wasPaused) element.pause();
    }
    setPlaybackRate(restore.rate);
    setLongPressing(false);
  };
  const finishLongPress = () => {
    clearLongPressTimer();
    longPressPointerId.current = null;
    const restore = longPressRestore.current;
    longPressRestore.current = null;
    if (!restore) return;
    restoreLongPressPlayback(restore);
  };
  const beginLongPress = (event: ReactPointerEvent<HTMLVideoElement>) => {
    if (event.button !== 0 || event.isPrimary === false || longPressPointerId.current !== null || keyboardLongPressKey.current !== null) return;
    clearLongPressTimer();
    longPressPointerId.current = event.pointerId;
    suppressLongPressClick.current = false;
    longPressTimer.current = window.setTimeout(() => {
      longPressTimer.current = null;
      const element = videoRef.current;
      if (!element || pendingSource.current) return;
      longPressRestore.current = { rate: element.playbackRate, wasPaused: element.paused };
      element.playbackRate = 3;
      setPlaybackRate(3);
      setLongPressing(true);
      suppressLongPressClick.current = true;
      if (element.paused) void element.play().catch(() => undefined);
    }, 320);
  };
  const clearKeyboardLongPressTimer = () => {
    if (keyboardLongPressTimer.current !== null) {
      window.clearTimeout(keyboardLongPressTimer.current);
      keyboardLongPressTimer.current = null;
    }
  };
  const finishKeyboardLongPress = (seekShortPress = false) => {
    clearKeyboardLongPressTimer();
    const delta = keyboardLongPressDelta.current;
    keyboardLongPressKey.current = null;
    keyboardLongPressDelta.current = null;
    const restore = keyboardLongPressRestore.current;
    keyboardLongPressRestore.current = null;
    if (!restore) {
      if (seekShortPress && delta !== null) seek(getPrimaryTime() + delta);
      return;
    }
    restoreLongPressPlayback(restore);
  };
  const beginKeyboardLongPress = (event: globalThis.KeyboardEvent) => {
    if (event.repeat || keyboardLongPressKey.current !== null || longPressPointerId.current !== null) return;
    keyboardLongPressKey.current = holdKey(event);
    keyboardLongPressDelta.current = 5;
    clearKeyboardLongPressTimer();
    keyboardLongPressTimer.current = window.setTimeout(() => {
      keyboardLongPressTimer.current = null;
      const element = videoRef.current;
      if (!element || pendingSource.current) return;
      keyboardLongPressRestore.current = { rate: element.playbackRate, wasPaused: element.paused };
      element.playbackRate = 3;
      setPlaybackRate(3);
      setLongPressing(true);
      if (element.paused) void element.play().catch(() => undefined);
    }, 320);
  };
  useEffect(() => {
    const release = () => finishLongPress();
    window.addEventListener("pointerup", release);
    window.addEventListener("pointercancel", release);
    return () => {
      window.removeEventListener("pointerup", release);
      window.removeEventListener("pointercancel", release);
      finishLongPress();
    };
  }, [videoRef]);
  useEffect(() => {
    const release = (event: globalThis.KeyboardEvent) => {
      if (keyboardLongPressKey.current === holdKey(event)) {
        event.preventDefault();
        finishKeyboardLongPress(true);
      }
    };
    const cancel = () => finishKeyboardLongPress(false);
    const visibilityChanged = () => {
      if (document.visibilityState === "hidden") cancel();
    };
    window.addEventListener("keyup", release);
    window.addEventListener("blur", cancel);
    document.addEventListener("visibilitychange", visibilityChanged);
    return () => {
      window.removeEventListener("keyup", release);
      window.removeEventListener("blur", cancel);
      document.removeEventListener("visibilitychange", visibilityChanged);
      cancel();
    };
  }, [progressKey, videoRef]);
  const playerKeyboard = (event: globalThis.KeyboardEvent) => {
    const frame = frameRef.current;
    const target = event.target instanceof Element ? event.target : null;
    if (!frame || isPlayerHidden(frame) || document.visibilityState === "hidden"
      || event.defaultPrevented || event.isComposing || target?.closest(PLAYER_INPUTS)
      || (keyboardNavigation.current && target?.closest('button, a, [role="button"], summary'))
      || [...document.querySelectorAll(PLAYER_POPOVERS)].some(element => !isPlayerHidden(element))) return;
    if (event.repeat) {
      if ((["playPause", "seekBack", "seekForward", "mute", "subtitles", "fullscreen"] as const)
        .some(action => matchesShortcut(event, shortcuts, action))) event.preventDefault();
      return;
    }
    if (matchesShortcut(event, shortcuts, "playPause")) { event.preventDefault(); void togglePlay(); }
    else if (matchesShortcut(event, shortcuts, "seekBack")) { event.preventDefault(); seek(getPrimaryTime() - 5); }
    else if (matchesShortcut(event, shortcuts, "seekForward")) {
      event.preventDefault();
      beginKeyboardLongPress(event);
    }
    else if (matchesShortcut(event, shortcuts, "mute")) { event.preventDefault(); toggleMute(); }
    else if (matchesShortcut(event, shortcuts, "subtitles") && subtitles.length) { event.preventDefault(); toggleSubtitles(); }
    else if (matchesShortcut(event, shortcuts, "fullscreen")) { event.preventDefault(); void toggleFullscreen(); }
  };
  const keyboardHandler = useRef(playerKeyboard);
  keyboardHandler.current = playerKeyboard;
  useEffect(() => {
    const key = (event: globalThis.KeyboardEvent) => keyboardHandler.current(event);
    const modality = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Tab" || (event.target instanceof Element && event.target.closest('[role="menu"]'))) {
        keyboardNavigation.current = true;
        finishKeyboardLongPress(false);
      }
    };
    const pointer = (event: PointerEvent) => {
      keyboardNavigation.current = false;
      if (event.target instanceof Element && event.target.closest('button, input, select, textarea, [role="dialog"], [role="menu"]')) {
        finishKeyboardLongPress(false);
      }
    };
    const focus = (event: FocusEvent) => {
      if (event.target instanceof Element && event.target.closest(PLAYER_INPUTS)) finishKeyboardLongPress(false);
    };
    document.addEventListener("keydown", modality, true);
    document.addEventListener("pointerdown", pointer, true);
    document.addEventListener("focusin", focus);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("keydown", modality, true);
      document.removeEventListener("pointerdown", pointer, true);
      document.removeEventListener("focusin", focus);
      document.removeEventListener("keydown", key);
    };
  }, [progressKey]);

  return (
    <section className="video-section">
      <div ref={headingRef} className="section-heading">
        <div>
          <h1>{video.display_name}</h1>
        </div>
        <span className="source-badge">{video.status === "review_ready" ? "可复习" : "字幕可查看"}</span>
      </div>
      <div ref={frameRef} className="video-frame" tabIndex={0} aria-label="视频播放器"
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) finishKeyboardLongPress(false);
        }}
        onKeyUp={(event) => {
          if (keyboardLongPressKey.current === holdKey(event)) {
            event.preventDefault();
            finishKeyboardLongPress(true);
          }
        }}>
        <video
          ref={videoRef}
          className={`video-player${longPressing ? " is-long-pressing" : ""}`}
          playsInline
          muted={usePrimaryAudio || muted}
          autoPlay={autoPlay && !restoredRef.current && !switching}
          crossOrigin="anonymous"
          preload="metadata"
          src={getStreamUrl(secondaryActive && secondary ? secondary.video_id : videoId, demo)}
          onLoadedMetadata={(event) => {
            const element = event.currentTarget;
            const seconds = Number.isFinite(element.duration) ? element.duration : 0;
            if (mapping.current.active) secondaryDuration.current = seconds;
            else {
              mainDuration.current = seconds;
              setDuration(seconds);
              onDurationChange(seconds);
            }
            const pending = pendingSource.current;
            if (pending) {
              const target = pending.time + mapping.current.offset;
              if (mapping.current.active && (target < 0 || target >= seconds)) {
                switchSource(false, pending.time, pending.resume);
                setPlaybackError("该时间点超出副视角范围，已切回主视角。");
                return;
              }
              element.playbackRate = pending.rate;
              element.volume = pending.volume;
              element.muted = usePrimaryAudio || pending.muted;
              element.currentTime = Math.max(0, Math.min(seconds, target));
              pendingSource.current = null;
              setSwitching(false);
              publishTime(boundTime(element.currentTime - mapping.current.offset));
              if (pending.resume) void element.play().catch(() => {
                setPlaying(false);
                setPlaybackError("视角已切换，请点击播放继续。");
              });
              else setPlaying(false);
              applyNavigationSeek(element);
            } else {
              setPlaybackError(null);
              restoreProgress(element);
              const queuedSeek = queuedPrimarySeek.current;
              queuedPrimarySeek.current = null;
              if (queuedSeek) seek(queuedSeek.seconds, queuedSeek.play);
            }
          }}
          onDurationChange={(event) => {
            const seconds = event.currentTarget.duration;
            if (Number.isFinite(seconds) && !mapping.current.active) {
              mainDuration.current = seconds;
              setDuration(seconds);
              onDurationChange(seconds);
              applyNavigationSeek(event.currentTarget);
            }
          }}
          onTimeUpdate={(event) => {
            if (pendingSource.current) return;
            const element = event.currentTarget;
            const time = element.currentTime - mapping.current.offset;
            if (mapping.current.active && mainDuration.current > 0 && time >= mainDuration.current) {
              element.pause();
              const end = mainDuration.current + mapping.current.offset;
              if (Math.abs(element.currentTime - end) > .01) element.currentTime = end;
              saveProgress(element);
            }
            publishTime(boundTime(time));
            if (!element.paused && Date.now() - lastStudySave.current >= 10_000) saveProgress(element);
          }}
          onPlay={() => setPlaying(true)}
          onPause={(event) => {
            if (pendingSource.current) return;
            setPlaying(false);
            saveProgress(event.currentTarget);
          }}
          onEnded={(event) => {
            if (pendingSource.current) return;
            const time = boundTime(event.currentTarget.currentTime - mapping.current.offset);
            if (mapping.current.active && time < mainDuration.current) {
              switchSource(false, time, true);
              setPlaybackError("副视角已结束，已切回主视角继续播放。");
            } else { setPlaying(false); saveProgress(event.currentTarget); }
          }}
          onVolumeChange={(event) => {
            setVolume(event.currentTarget.volume);
            if (!usePrimaryAudio) setMuted(event.currentTarget.muted);
          }}
          onRateChange={(event) => setPlaybackRate(event.currentTarget.playbackRate)}
          onError={() => {
            if (mapping.current.active) {
              switchSource(false, getPrimaryTime(), pendingSource.current?.resume ?? playing);
              setPlaybackError("副视角加载失败，已切回主视角。");
            } else {
              pendingSource.current = null;
              setSwitching(false);
              setPlaying(false);
              setPlaybackError("视频加载失败，请检查视频文件或连接。");
            }
          }}
          onPointerDown={beginLongPress}
          onPointerLeave={finishLongPress}
          onContextMenu={(event) => event.preventDefault()}
          onClick={() => {
            if (suppressLongPressClick.current) {
              suppressLongPressClick.current = false;
              return;
            }
            void togglePlay();
          }}
          onDoubleClick={() => void toggleFullscreen()}
        >
          你的浏览器不支持视频播放。
        </video>
        <audio ref={primaryAudioRef} hidden crossOrigin="anonymous" preload="metadata"
          src={usePrimaryAudio ? getStreamUrl(videoId, demo) : undefined}
          onError={() => {
            if (usePrimaryAudio) setPlaybackError("主视频音频加载失败，请检查视频文件或连接。");
          }} />
        {longPressing && <div className="video-long-press-indicator" role="status" aria-live="polite">3x</div>}
        {subtitles.length > 0 && (
          <DraggableSubtitle key={`${progressKey}:${captionReset}`} text={preferences.enabled ? activeSubtitle?.text ?? "" : ""} fontSize={preferences.fontSize} />
        )}
        {playbackError && <div className="video-playback-error" role="status">{playbackError}</div>}
        <div className="player-controls" onClick={event => {
          const button = (event.target as Element).closest(".player-control");
          if (event.detail > 0 && button && !button.hasAttribute("aria-expanded")
            && !button.closest(PLAYER_POPOVERS)) focusPlayer();
        }}>
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
            onPointerUp={(event) => { if (event.button === 0) focusPlayer(); }}
            onClick={focusPlayer}
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
                videoRef.current.muted = usePrimaryAudio || next === 0;
                setVolume(next);
                setMuted(next === 0);
              }}
              onPointerUp={event => { if (event.button === 0) focusPlayer(); }}
            />
            <span className="player-time">
              <span>{formatTime(currentTime * 1000)}</span><span className="player-time-separator"> / </span><span>{formatTime(duration * 1000)}</span>
            </span>
            <div className="player-toolbar-spacer" />
            <AudioChannelControl videoRef={usePrimaryAudio ? primaryAudioRef : videoRef} />
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
            <PlaybackSpeedControl rate={playbackRate} onClose={focusPlayer}
              onChange={rate => {
                if (!videoRef.current) return;
                videoRef.current.playbackRate = rate;
                setPlaybackRate(rate);
              }} />
            <button className="player-control" type="button" aria-label={fullscreen ? "退出全屏" : "全屏"} title={shortcutTitle(fullscreen ? "退出全屏" : "全屏", shortcuts, "fullscreen")} aria-keyshortcuts={shortcutAria(shortcuts, "fullscreen")} onClick={() => void toggleFullscreen()}>
              {fullscreen ? <Minimize size={19} /> : <Maximize size={19} />}
            </button>
          </div>
        </div>
      </div>
      <div className="video-secondary-row">
        <details className="video-meta-details">
          <summary>文件详情</summary>
          <div className="video-meta">
            <span>{video.filename}</span>
            <span>{video.video_path || "本地视频库"}</span>
          </div>
        </details>
        <SecondaryViewControl videoId={videoId} demo={demo} secondary={secondary} active={secondaryActive}
          switching={switching || duration <= 0} onChange={changeSecondary} onSwitch={() => switchSource(!secondaryActive)} />
      </div>
    </section>
  );
}

function PlaybackSpeedControl({ rate, onChange, onClose }: {
  rate: number; onChange: (rate: number) => void; onClose: () => void;
}) {
  const id = useId();
  const container = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const rates = [0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3];
  const close = (keyboard: boolean) => {
    setOpen(false);
    if (keyboard) trigger.current?.focus({ preventScroll: true });
    else onClose();
  };
  useEffect(() => {
    if (!open) return;
    container.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus({ preventScroll: true });
    const outside = (event: PointerEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  }, [open]);
  return <div ref={container} className="player-speed-control" onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }} onKeyDown={event => {
    if (event.key === "Escape" && open) {
      event.preventDefault(); event.stopPropagation(); close(true);
    }
  }}>
    <button ref={trigger} className="player-speed" type="button" aria-label="播放速度" title="播放速度"
      aria-haspopup="menu" aria-expanded={open} aria-controls={id}
      onClick={event => { if (open) close(event.detail === 0); else setOpen(true); }}
      onKeyDown={event => {
        if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); setOpen(true); }
      }}>
      <span>{rate}x</span><ChevronDown size={13} aria-hidden="true" />
    </button>
    {open && <div id={id} className="player-speed-menu" role="menu" aria-label="播放速度" onKeyDown={event => {
      if (event.key === "Tab") { close(true); return; }
      const items = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]')];
      const index = items.indexOf(document.activeElement as HTMLButtonElement);
      const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
        : event.key === "ArrowDown" ? (index + 2) % items.length
        : event.key === "ArrowUp" ? (index - 2 + items.length) % items.length
        : event.key === "ArrowRight" ? (index + 1) % items.length
        : event.key === "ArrowLeft" ? (index - 1 + items.length) % items.length : null;
      if (next !== null) { event.preventDefault(); event.stopPropagation(); items[next]?.focus(); }
    }}>
      {rates.map(value => <button key={value} type="button" role="menuitemradio"
        aria-checked={rate === value} tabIndex={rate === value ? 0 : -1}
        onClick={event => { onChange(value); close(event.detail === 0); }}>
        <span>{value}x</span>{rate === value && <Check size={13} aria-hidden="true" />}
      </button>)}
    </div>}
  </div>;
}
