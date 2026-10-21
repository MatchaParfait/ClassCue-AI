// Stable sentence rows on shallow, staggered planes. Polling and new marker IDs
// do not rearrange surviving text; each glyph keeps its original aspect ratio.
export const ANALYSIS = Object.freeze({ columns: 28, rows: 30, pitch: .37, lineHeight: .47, lift: .55, maxCharacters: 1800 });
export const ANALYSIS_PLANES = [
  { center: [0, 1.5, -5], yaw: 0 },
  { center: [-12.2, 3.0, -13], yaw: .13 },
  { center: [13.2, 2.2, -17], yaw: -.13 },
  { center: [-23.4, 4.3, -27], yaw: .07 },
  { center: [25.1, -.8, -31], yaw: -.07 },
];
const PLANES = ANALYSIS_PLANES;

export function analysisSurface(cell) {
  const perPlane = ANALYSIS.columns * ANALYSIS.rows;
  const layer = Math.floor(cell / perPlane), plane = PLANES[layer];
  const slot = cell % perPlane, row = Math.floor(slot / ANALYSIS.columns), column = slot % ANALYSIS.columns;
  const x = (column - (ANALYSIS.columns - 1) / 2) * ANALYSIS.pitch;
  return { position: [plane.center[0] + x * Math.cos(plane.yaw),
    plane.center[1] + (14.5 - row) * ANALYSIS.lineHeight, plane.center[2] - x * Math.sin(plane.yaw)],
    yaw: plane.yaw, pitch: 0, column: layer, face: `layer-${layer}` };
}

export class AnalysisLayout {
  constructor() { this.cells = new Map(); this.nextLayer = 0; }

  sync(characters) {
    if (this.firstRow === undefined && characters.length) {
      const lengths = new Map();
      for (const c of characters) lengths.set(c.subtitleId, (lengths.get(c.subtitleId) ?? 0) + 1);
      const rows = [...lengths.values()].reduce((total, n) => total + Math.ceil(n / ANALYSIS.columns), 0);
      this.firstRow = Math.max(0, Math.floor((ANALYSIS.rows - Math.ceil(rows / 3)) / 2));
    }
    const wanted = new Set(characters.map(c => c.key));
    for (const key of this.cells.keys()) if (!wanted.has(key)) this.cells.delete(key);
    const occupied = new Set(this.cells.values());
    const pending = characters.filter(c => !this.cells.has(c.key));
    for (let first = 0; first < pending.length;) {
      let end = first + 1;
      while (end < pending.length && pending[end].subtitleId === pending[first].subtitleId
        && pending[end].index - pending[end - 1].index <= 3 && end - first < ANALYSIS.columns) end++;
      const count = end - first;
      let start = -1, chosenLayer = 0;
      for (let offset = 0; offset < PLANES.length && start < 0; offset++) {
        const layer = (this.nextLayer + offset) % PLANES.length;
        for (let row = 0; row < ANALYSIS.rows && start < 0; row++) {
          const base = (layer * ANALYSIS.rows + (row + (this.firstRow ?? 0)) % ANALYSIS.rows) * ANALYSIS.columns;
          // Keep sentences together with a little breathing room between rows.
          if (Array.from({ length: ANALYSIS.columns }, (_, i) => base + i).every(cell => !occupied.has(cell))) {
            start = base; chosenLayer = layer;
          }
        }
      }
      if (start < 0) {
        // A full scene can have partially released rows after source correction.
        // Fill only those holes, never move existing characters to make room.
        for (let cell = 0; cell < PLANES.length * ANALYSIS.rows * ANALYSIS.columns && first < end; cell++) {
          if (occupied.has(cell)) continue;
          this.cells.set(pending[first++].key, cell); occupied.add(cell);
        }
        if (first < end) throw new Error('字幕分层容量不足');
      } else {
        for (let i = 0; i < count; i++) {
          this.cells.set(pending[first + i].key, start + i); occupied.add(start + i);
        }
        first = end;
        // Fill three foreground planes first; bring in the rear pair when needed.
        this.nextLayer = first < pending.length && pending[first].subtitleId === pending[first - 1].subtitleId
          ? chosenLayer : (chosenLayer + 1) % 3;
      }
    }
    return this.cells;
  }
}
