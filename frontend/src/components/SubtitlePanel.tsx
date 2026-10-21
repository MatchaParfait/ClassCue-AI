import { useEffect, useRef } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import type { Subtitle } from "../types";
import { activeSubtitleId, formatTime } from "../utils";

interface SubtitlePanelProps {
  subtitles: Subtitle[];
  currentTime: number;
  onSeek: (seconds: number) => void;
}

export function SubtitlePanel({ subtitles, currentTime, onSeek }: SubtitlePanelProps) {
  const activeId = activeSubtitleId(subtitles, currentTime * 1000);
  const scrollRef = useRef<HTMLDivElement>(null);
  const rowHeight = 54;
  const virtualizer = useVirtualizer({
    count: subtitles.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => rowHeight,
    overscan: 8,
  });

  useEffect(() => {
    const activeIndex = subtitles.findIndex((subtitle) => subtitle.id === activeId);
    if (activeIndex >= 0) {
      virtualizer.scrollToIndex(activeIndex, { align: "center", behavior: "smooth" });
    }
  }, [activeId, subtitles, virtualizer]);

  return (
    <section className="transcript-section">
      <div className="section-heading compact">
        <div>
          <p className="eyebrow">课堂字幕</p>
          <h2>跟随播放位置</h2>
        </div>
        <span className="muted-label">{subtitles.length} 条</span>
      </div>
      <div ref={scrollRef} className="subtitle-list" aria-label="课堂字幕列表">
        <div className="subtitle-virtual-content" style={{ height: `${virtualizer.getTotalSize()}px` }}>
          {virtualizer.getVirtualItems().map((virtualRow) => {
            const subtitle = subtitles[virtualRow.index];
            const active = subtitle.id === activeId;
            return (
              <button
                key={subtitle.id}
                className={`subtitle-row${active ? " is-active" : ""}`}
                type="button"
                style={{
                  height: `${rowHeight}px`,
                  transform: `translateY(${virtualRow.start}px)`,
                }}
                onClick={() => onSeek(subtitle.start_ms / 1000)}
              >
                <span className="subtitle-time">{formatTime(subtitle.start_ms)}</span>
                <span>{subtitle.text}</span>
              </button>
            );
          })}
        </div>
      </div>
    </section>
  );
}
