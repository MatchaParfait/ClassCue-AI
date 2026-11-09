import { useEffect, useRef, type KeyboardEvent } from "react";
import { GripVertical } from "lucide-react";
import { formatTime } from "../utils";
import "./ClipRangeSelector.css";

export interface ClipRange {
  startMs: number;
  endMs: number;
}

export function formatClipTime(milliseconds: number) {
  const fraction = Math.round(milliseconds) % 1000;
  return `${formatTime(milliseconds)}${fraction ? `.${String(fraction).padStart(3, "0")}` : ""}`;
}

export function ClipRangeSelector({ range, durationMs, maxMs = durationMs, onChange, onSeek }: {
  range: ClipRange; durationMs: number; maxMs?: number; onChange: (range: ClipRange) => void;
  onSeek: (milliseconds: number) => void;
}) {
  const track = useRef<HTMLDivElement>(null);
  const firstHandle = useRef<HTMLButtonElement>(null);
  const drag = useRef<{ pointerId: number; side: keyof ClipRange; original: ClipRange; originX: number } | null>(null);
  const limitMs = Math.floor(Math.min(durationMs, maxMs));
  useEffect(() => { firstHandle.current?.focus({ preventScroll: true }); }, []);
  const update = (side: keyof ClipRange, value: number) => {
    const bounded = side === "startMs"
      ? Math.max(0, Math.min(range.endMs - 1, Math.round(value)))
      : Math.min(limitMs, Math.max(range.startMs + 1, Math.round(value)));
    if (bounded === range[side]) return;
    onChange({ ...range, [side]: bounded });
    onSeek(bounded);
  };
  const keyDown = (event: KeyboardEvent<HTMLButtonElement>, side: keyof ClipRange) => {
    const step = event.shiftKey ? 10_000 : 1000;
    const value = event.key === "Home" ? 0 : event.key === "End" ? limitMs
      : ["ArrowLeft", "ArrowDown"].includes(event.key) ? range[side] - step
      : ["ArrowRight", "ArrowUp"].includes(event.key) ? range[side] + step
      : event.key === "PageDown" ? range[side] - 10_000
      : event.key === "PageUp" ? range[side] + 10_000 : null;
    if (value === null) return;
    event.preventDefault();
    event.stopPropagation();
    update(side, value);
  };
  return <div ref={track} className="player-clip-range" role="group" aria-label="讲解片段时间范围">
    <div className="player-clip-highlight" style={{
      left: `${range.startMs / durationMs * 100}%`,
      width: `${(range.endMs - range.startMs) / durationMs * 100}%`,
    }} />
    {(["startMs", "endMs"] as const).map(side => <button key={side}
      ref={side === "startMs" ? firstHandle : undefined}
      type="button" role="slider" className={`player-clip-handle ${side === "startMs" ? "is-start" : "is-end"}`}
      aria-label={side === "startMs" ? "片段开始时间" : "片段结束时间"}
      aria-valuemin={side === "startMs" ? 0 : range.startMs + 1}
      aria-valuemax={side === "startMs" ? range.endMs - 1 : limitMs}
      aria-valuenow={range[side]} aria-valuetext={formatClipTime(range[side])}
      title={`${side === "startMs" ? "开始" : "结束"} ${formatClipTime(range[side])}`}
      style={{ left: `${range[side] / durationMs * 100}%` }}
      onKeyDown={event => keyDown(event, side)}
      onPointerDown={event => {
        if (event.button !== 0 || drag.current) return;
        event.preventDefault();
        event.stopPropagation();
        event.currentTarget.focus({ preventScroll: true });
        event.currentTarget.setPointerCapture(event.pointerId);
        drag.current = { pointerId: event.pointerId, side, original: range, originX: event.clientX };
      }}
      onPointerMove={event => {
        if (drag.current?.pointerId !== event.pointerId || drag.current.side !== side) return;
        const rect = track.current?.getBoundingClientRect();
        if (!rect?.width) return;
        const value = drag.current.original[side] + (event.clientX - drag.current.originX) / rect.width * durationMs;
        update(side, Math.round(value / 100) * 100);
      }}
      onPointerUp={event => {
        if (drag.current?.pointerId !== event.pointerId) return;
        drag.current = null;
        event.currentTarget.releasePointerCapture(event.pointerId);
      }}
      onPointerCancel={event => {
        if (drag.current?.pointerId !== event.pointerId) return;
        const original = drag.current.original;
        drag.current = null;
        onChange(original);
        onSeek(original[side]);
      }}
      onLostPointerCapture={() => { drag.current = null; }}>
      <GripVertical size={14} aria-hidden="true" />
    </button>)}
  </div>;
}
