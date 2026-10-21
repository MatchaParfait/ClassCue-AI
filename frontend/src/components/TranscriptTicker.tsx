import { useEffect, useReducer, useRef, type CSSProperties } from "react";
import type { Subtitle } from "../types";
import { SubtitleReveal } from "./SubtitleReveal";

const ROWS = 3;
const SCROLL_MS = 260;
const MAX_PENDING = 12;
type Part = { key: string; subtitle: Subtitle };
type Entry = Part & { duration: number; parts: Part[] };
type Feed = {
  rows: Entry[]; pending: Entry[]; known: Set<string>; hydrated: boolean; latestStart: number;
  phase: "idle" | "scrolling" | "revealing" | "holding"; turn: number;
};
const initial = (): Feed => ({ rows: [], pending: [], known: new Set(), hydrated: false, latestStart: -Infinity, phase: "idle", turn: 0 });
type Action = { type: "sync"; subtitles: Subtitle[]; live: boolean; reduced: boolean } | { type: "advance" };

function entries(subtitles: Subtitle[], retained: Entry[]): Entry[] {
  const occurrences = new Map<string, number>();
  const incoming: Part[] = [...subtitles].sort((a, b) => a.start_ms - b.start_ms || a.end_ms - b.end_ms).map(subtitle => {
    // The preview and saved database can use different IDs for the same sentence.
    const stamp = `${subtitle.start_ms}:${subtitle.end_ms}`;
    const occurrence = occurrences.get(stamp) ?? 0; occurrences.set(stamp, occurrence + 1);
    return { key: `${stamp}:${occurrence}`, subtitle };
  });
  // A rolling preview may drop the beginning of a row that is still on screen.
  const parts = new Map(incoming.map(part => [part.key, part]));
  for (const row of retained) {
    if (row.parts.some(part => parts.has(part.key))) {
      for (const part of row.parts) if (!parts.has(part.key)) parts.set(part.key, part);
    }
  }
  const grouped: Entry[] = [];
  const short = (text: string) => Array.from(text.replace(/[\s\p{P}]/gu, "")).length <= 5;
  for (const part of [...parts.values()].sort((a, b) => a.subtitle.start_ms - b.subtitle.start_ms || a.subtitle.end_ms - b.subtitle.end_ms)) {
    const text = part.subtitle.text.trim();
    if (!text) continue;
    const previous = grouped.at(-1);
    const combined = previous ? `${previous.subtitle.text} ${text}` : text;
    // Bound decorative rows so a long run of short ASR fragments is not hidden behind the glyph limit.
    if (previous && (short(text) || short(previous.subtitle.text)) && Array.from(combined).length <= 160) {
      previous.parts.push(part);
      previous.subtitle = { ...previous.subtitle, end_ms: Math.max(previous.subtitle.end_ms, part.subtitle.end_ms), text: combined };
    } else {
      grouped.push({ ...part, subtitle: { ...part.subtitle, text }, parts: [part], duration: 0 });
    }
  }
  return grouped;
}
function next(state: Feed): Feed {
  if (!state.pending.length) return { ...state, phase: "idle" };
  const [item, ...pending] = state.pending;
  const duration = pending.length >= 6 ? 480 : pending.length >= 3 ? 800 : Math.min(1540, Math.max(650, Array.from(item.subtitle.text).length * 30 + 440));
  return { ...state, pending, rows: [...state.rows.slice(-ROWS), { ...item, duration }], phase: "scrolling", turn: state.turn + 1 };
}
function reduce(state: Feed, action: Action): Feed {
  if (action.type === "advance") {
    if (state.phase === "scrolling") return { ...state, rows: state.rows.slice(-ROWS), phase: "revealing", turn: state.turn + 1 };
    if (state.phase === "revealing") return { ...state, phase: "holding", turn: state.turn + 1 };
    return next(state);
  }
  const incoming = entries(action.subtitles, [...state.rows, ...state.pending]);
  if (!incoming.length) return state;
  const known = new Set(incoming.map(item => item.key));
  const latestStart = Math.max(state.latestStart, incoming.at(-1)!.subtitle.start_ms);
  if (action.reduced || (!state.hydrated && !action.live)) {
    return { ...state, rows: incoming.slice(-ROWS), pending: [], known, latestStart, hydrated: true, phase: "idle", turn: state.turn + 1 };
  }
  const byKey = new Map(incoming.map(item => [item.key, item]));
  // Loading the full saved transcript must not replay history outside the live preview window.
  const additions = state.hydrated ? incoming.filter(item => item.subtitle.start_ms >= state.latestStart && !state.known.has(item.key)
    && !state.rows.some(row => row.key === item.key) && !state.pending.some(row => row.key === item.key)) : incoming.slice(-ROWS);
  // Only the decorative backlog is bounded. The source transcript is never edited.
  const pending = [...state.pending.map(item => byKey.get(item.key) ?? item), ...additions].slice(-(action.live ? MAX_PENDING : ROWS));
  const rows = state.rows.map(item => ({ ...(byKey.get(item.key) ?? item), duration: item.duration }));
  const updated = { ...state, rows, pending, known, latestStart, hydrated: true };
  return state.phase === "idle" ? next(updated) : updated;
}
function time(ms: number) { const seconds = Math.floor(ms / 1000); return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`; }

export function TranscriptTicker({ subtitles, live, reduced, paused }: { subtitles: Subtitle[]; live: boolean; reduced: boolean; paused: boolean }) {
  const [feed, dispatch] = useReducer(reduce, undefined, initial);
  const clock = useRef({ turn: -1, remaining: 0 });
  useEffect(() => { dispatch({ type: "sync", subtitles, live, reduced }); }, [subtitles, live, reduced]);
  const duration = feed.phase === "scrolling" ? SCROLL_MS + 40 : feed.phase === "revealing" ? (feed.rows.at(-1)?.duration ?? 0) + 60 : 180;
  useEffect(() => {
    if (feed.phase === "idle") return;
    if (clock.current.turn !== feed.turn) clock.current = { turn: feed.turn, remaining: duration };
    if (paused) return;
    const start = performance.now();
    const timer = window.setTimeout(() => dispatch({ type: "advance" }), clock.current.remaining);
    return () => { window.clearTimeout(timer); clock.current.remaining = Math.max(0, clock.current.remaining - (performance.now() - start)); };
  }, [feed.phase, feed.turn, duration, paused]);
  const scrolling = feed.phase === "scrolling";
  const placeholders = Math.max(0, (scrolling ? ROWS + 1 : ROWS) - feed.rows.length);
  return <div className="task-space-transcript-window" data-paused={paused} data-feed-phase={feed.phase} data-pending={feed.pending.length}>
    <ol className="task-space-transcript-track" data-scrolling={scrolling} style={{ "--scroll-duration": `${SCROLL_MS}ms` } as CSSProperties}>
      {Array.from({ length: placeholders }, (_, index) => <li key={`empty-${index}`} className="task-space-transcript-placeholder" aria-hidden="true" />)}
      {feed.rows.map((item, index) => {
        const current = index === feed.rows.length - 1;
        const reveal = !current || reduced || ["idle", "holding"].includes(feed.phase) ? "settled" : scrolling ? "pending" : "active";
        return <li key={item.key} data-subtitle-key={item.key} data-current={current} data-reveal={reveal}>
          <time>{time(item.subtitle.start_ms)}</time><SubtitleReveal text={item.subtitle.text} durationMs={item.duration} state={reveal} />
        </li>;
      })}
    </ol>
  </div>;
}
