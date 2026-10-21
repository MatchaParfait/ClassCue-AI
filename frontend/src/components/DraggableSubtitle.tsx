import { useLayoutEffect, useRef, useState } from "react";
import type { PointerEvent } from "react";

interface DraggableSubtitleProps {
  text: string;
  fontSize: number;
}

const clamp = (value: number) => Math.max(0, Math.min(1, value));

export function DraggableSubtitle({ text, fontSize }: DraggableSubtitleProps) {
  const areaRef = useRef<HTMLDivElement>(null);
  const captionRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ id: number; x: number; y: number; left: number; top: number } | null>(null);
  const [position, setPosition] = useState({ x: 0.5, y: 1 });
  const [travel, setTravel] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);

  useLayoutEffect(() => {
    const area = areaRef.current;
    const caption = captionRef.current;
    if (!area || !caption) return;
    const measure = () => setTravel({
      x: Math.max(0, area.clientWidth - caption.offsetWidth),
      y: Math.max(0, area.clientHeight - caption.offsetHeight),
    });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(area);
    observer.observe(caption);
    return () => observer.disconnect();
  }, [text, fontSize]);

  const startDrag = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    event.stopPropagation();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      id: event.pointerId, x: event.clientX, y: event.clientY,
      left: position.x * travel.x, top: position.y * travel.y,
    };
    setDragging(true);
  };

  const moveDrag = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.id !== event.pointerId) return;
    setPosition({
      x: travel.x ? clamp((drag.left + event.clientX - drag.x) / travel.x) : 0.5,
      y: travel.y ? clamp((drag.top + event.clientY - drag.y) / travel.y) : 1,
    });
  };

  const stopDrag = () => {
    dragRef.current = null;
    setDragging(false);
  };

  return (
    <div ref={areaRef} className="video-caption-area">
      <div
        ref={captionRef}
        className={`video-caption${dragging ? " is-dragging" : ""}`}
        role="group"
        aria-label="视频字幕"
        tabIndex={text ? 0 : -1}
        title="拖动字幕；双击恢复底部居中。"
        style={{
          fontSize: `${fontSize}px`,
          left: `${position.x * travel.x}px`,
          top: `${position.y * travel.y}px`,
          visibility: text ? "visible" : "hidden",
        }}
        onPointerDown={startDrag}
        onPointerMove={moveDrag}
        onPointerUp={stopDrag}
        onPointerCancel={stopDrag}
        onLostPointerCapture={stopDrag}
        onDoubleClick={(event) => {
          event.stopPropagation();
          setPosition({ x: 0.5, y: 1 });
        }}
      >
        {text}
      </div>
    </div>
  );
}
