type Rect = Pick<DOMRect, "left" | "right" | "top" | "bottom">;
export type PreviewPlacement = { left: number; top: number; width: number; side: "left" | "right" | "above" | "below" };

// Keep the hovered row visible and never reserve space inside the result list.
export function placeSearchPreview(anchor: Rect, dialog: Rect, viewport: { width: number; height: number }): PreviewPlacement | null {
  const margin = 16, gap = 12, caption = 92;
  const width = Math.min(560, Math.max(360, viewport.width * .44), viewport.width - margin * 2);
  const height = width * 9 / 16 + caption;
  const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(high, value));
  if (viewport.height >= height + margin * 2) {
    const top = clamp((anchor.top + anchor.bottom - height) / 2, margin, viewport.height - height - margin);
    if (viewport.width - dialog.right - margin - gap >= width) return { left: dialog.right + gap, top, width, side: "right" };
    if (dialog.left - margin - gap >= width) return { left: dialog.left - gap - width, top, width, side: "left" };
  }
  const above = anchor.top - margin - gap;
  const below = viewport.height - anchor.bottom - margin - gap;
  const side = above >= below ? "above" : "below";
  const available = Math.max(above, below);
  if (available < caption + 100) return null;
  const fitted = Math.min(width, (available - caption) * 16 / 9);
  const fittedHeight = fitted * 9 / 16 + caption;
  return {
    left: clamp(anchor.right - fitted, margin, viewport.width - fitted - margin),
    top: side === "above" ? anchor.top - gap - fittedHeight : anchor.bottom + gap,
    width: fitted, side,
  };
}
