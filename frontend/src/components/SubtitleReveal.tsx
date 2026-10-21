import { memo, useMemo, useState, type CSSProperties } from "react";

const segmenter = new Intl.Segmenter("zh", { granularity: "grapheme" });
const MAX_VISIBLE_CHARACTERS = 160;

const RevealCharacter = memo(function RevealCharacter({ character, index, count, duration }: { character: string; index: number; count: number; duration: number }) {
  // Keep an existing glyph's animation clock when a streaming sentence grows.
  const [style] = useState(() => ({
    "--glyph-delay": `${index * Math.min(34, Math.max(0, duration - Math.min(440, duration * .4)) / Math.max(1, count - 1))}ms`,
    "--glyph-duration": `${Math.min(440, duration * .4)}ms`,
    "--spark-x": `${index % 2 ? 5 : -4}px`,
    "--spark-y": `${-5 - index % 4}px`,
  }) as CSSProperties);
  return <span className="task-space-reveal-glyph" style={style} data-ink={character.trim() ? "true" : "false"}>
    <span>{character}</span>
  </span>;
});

export const SubtitleReveal = memo(function SubtitleReveal({ text, durationMs = 1540, state = "active" }: { text: string; durationMs?: number; state?: "pending" | "active" | "settled" }) {
  const characters = useMemo(() => Array.from(segmenter.segment(text), part => part.segment), [text]);
  const shown = characters.slice(0, MAX_VISIBLE_CHARACTERS);
  const count = shown.length + Number(characters.length > shown.length);
  return <span className="task-space-subtitle-copy" title={text} data-reveal={state}>
    <span className="task-space-accessible-text">{text}</span>
    <span className="task-space-subtitle-ink" aria-hidden="true">{shown.map((character, index) =>
      <RevealCharacter key={`${index}:${character}`} character={character} index={index} count={count} duration={durationMs} />
    )}{characters.length > shown.length && <RevealCharacter key="ellipsis" character="…" index={shown.length} count={count} duration={durationMs} />}</span>
  </span>;
});
