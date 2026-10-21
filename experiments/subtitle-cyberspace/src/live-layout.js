import { allCharacters, smooth } from './core.js';
import { infinityPoint } from './asr-layout.js';

export const LIVE = Object.freeze({ columns: 12, appear: .65, disappear: .45, spread: .8, maxGlyphs: 4096 });

export const ASR_RIBBON = Object.freeze({ rows: 72, columns: 10, fontSize: .60, nearDensity: .4 });
export const ROW_HANDOFF = Object.freeze({ appear: .35, disappear: .25, stagger: .06, letterDelay: .012, concurrent: 6 });

// Gradually tighten the near lobe instead of inserting alternating half-gap rows.
// Smaller glyphs leave breathing room while longer rows retain more source text.
function ribbonPhases() {
  const samples = 1024, distances = [0];
  for (let i = 0; i < samples; i++) {
    const x = infinityPoint((i + .5) / samples)[0];
    const density = 1 + ASR_RIBBON.nearDensity * smooth((-x - 2) / 7);
    distances.push(distances[i] + density);
  }
  const total = distances[samples], rows = Math.round(ASR_RIBBON.rows * total / samples);
  let cursor = 1;
  return Array.from({ length: rows }, (_, row) => {
    const target = row / rows * total;
    while (distances[cursor] < target) cursor++;
    return (cursor - 1 + (target - distances[cursor - 1]) / (distances[cursor] - distances[cursor - 1])) / samples;
  });
}

export function liveSlots(limit, ribbon = false) {
  const rowCount = ribbon ? ASR_RIBBON.rows : 240;
  const columns = ribbon ? ASR_RIBBON.columns : LIVE.columns;
  const phases = ribbon ? ribbonPhases() : [];
  for (let row = 0; !ribbon && row < rowCount; row++) {
    const phase = row / rowCount;
    phases.push(phase);
    if (!ribbon && infinityPoint(phase)[0] < -3) phases.push(phase + .5 / 240);
  }
  const entries = phases.map((phase, slot) => ({ slot, slots: phases.length, phase, length: columns,
    group: 0, ribbon, scale: 1, width: 2.3, arrival: -1, departure: 1e6, batch: -1, lane: -1 }));
  const available = new Set(entries.map((_, i) => i)), ordered = [];
  const anchors = [1, 7, 9, 15, 3, 5, 11, 13].map(n => n / 16);
  for (let batch = 0; available.size; batch++) for (let lane = 0; lane < 8 && available.size; lane++) {
    const target = (anchors[lane] + batch / entries.length) % 1;
    let slot, nearest = Infinity;
    for (const candidate of available) {
      const d = Math.abs(entries[candidate].phase - target), distance = Math.min(d, 1 - d);
      if (distance < nearest) { slot = candidate; nearest = distance; }
    }
    available.delete(slot); ordered.push({ slot, lane, batch });
  }
  const cells = ordered.flatMap(({ slot, lane, batch }) =>
    Array.from({ length: columns }, (_, index) => ({ ...entries[slot], index, lane, batch })));
  return { entries, cells: cells.slice(0, limit) };
}

// Preserve recent source characters, giving real hits (and the selected hit) priority.
// Unlike evenly resampling the whole transcript, appending text never moves survivors.
export function liveCharacters(data, mappings, focus, limit) {
  const all = allCharacters(data), available = new Map(all.map(c => [c.key, c]));
  const wanted = new Set();
  for (const mapping of [...mappings.filter(m => String(m.id) === String(focus)), ...mappings]) {
    for (const key of mapping.keys) if (available.has(key) && wanted.size < limit) wanted.add(key);
  }
  for (let i = all.length - 1; i >= 0 && wanted.size < limit; i--) wanted.add(all[i].key);
  return all.filter(c => wanted.has(c.key));
}

export function liveOpacity(record, clock) {
  return smooth((clock - record.arrival) / (record.appear ?? LIVE.appear))
    * (1 - smooth((clock - record.departure) / (record.disappear ?? LIVE.disappear)));
}

export class LiveLayout {
  constructor(limit = 4000, ribbon = false) {
    const { cells, entries } = liveSlots(limit, ribbon);
    this.cells = cells; this.entries = entries; this.limit = cells.length;
    this.free = new Set(cells.map((_, i) => i));
    this.records = new Map(); this.pending = new Map(); this.desired = new Map();
    this.clock = 0; this.batchStart = 0; this.endAt = 0; this.revision = 0;
    this.rowTransitions = ribbon;
    this.appear = ribbon ? ROW_HANDOFF.appear : LIVE.appear;
    this.disappear = ribbon ? ROW_HANDOFF.disappear : LIVE.disappear;
    this.activeRows = new Map(); this.nextRowAt = 0;
    this.recycled = new Map(); this.preferred = new Map();
  }

  update(data, mappings, focus, clock, onChange, limit = this.limit) {
    this.clock = clock;
    this.onChange = onChange;
    const chars = liveCharacters(data, mappings, focus, Math.min(this.limit, limit));
    if (chars.length === this.desired.size && chars.every(c => this.desired.get(c.key)?.char === c.char)) return;
    this.desired = new Map(chars.map(c => [c.key, c]));
    let changed = false;
    for (const record of this.records.values()) {
      const next = this.desired.get(record.key);
      record.queued = !next || next.char !== record.char;
      if (!record.queued && record.departure < 1e6 && clock <= record.departure) {
        record.departure = 1e6; onChange(record); changed = true;
      }
      if ((!next || next.char !== record.char) && record.departure === 1e6) {
        if (!this.rowTransitions) {
          record.departure = clock + .1;
          this.endAt = Math.max(this.endAt, record.departure + this.disappear);
          onChange(record);
        }
        changed = true;
      }
    }
    // Replace, never append to, the queue when a newer ASR snapshot arrives.
    const pending = chars.filter(c => {
      const current = this.records.get(c.key);
      return !current || current.char !== c.char || current.departure < 1e6;
    });
    this.pending = new Map(pending.map((c, order) => [c.key, { ...c, revealOrder: order, revealCount: pending.length }]));
    if (changed || this.pending.size) { this.batchStart = clock; this.revision++; }
  }

  advance(clock, immediate, { add, remove, budget = 3, maxAdds = 256, now = () => performance.now() }) {
    this.clock = clock;
    const start = now();
    for (const [slot, until] of this.activeRows) if (clock >= until
      && ![...this.records.values()].some(record => record.asr.slot === slot
        && (record.departure < 1e6 || clock < record.arrival + record.appear))) this.activeRows.delete(slot);
    const queued = new Map();
    for (const record of this.records.values()) if (record.queued && record.departure === 1e6) {
      const row = queued.get(record.asr.slot) ?? [];
      row.push(record); queued.set(record.asr.slot, row);
    }
    for (const [slot, row] of queued) {
      if (!immediate && this.rowTransitions) {
        if (this.activeRows.has(slot)) continue;
        if (this.activeRows.size >= ROW_HANDOFF.concurrent || clock < this.nextRowAt) break;
      }
      for (const record of row) {
        record.departure = clock;
        this.onChange?.(record);
      }
      const until = clock + this.disappear + this.appear + ROW_HANDOFF.letterDelay * (row[0].asr.length - 1);
      this.activeRows.set(slot, until); this.endAt = Math.max(this.endAt, until);
      this.nextRowAt = clock + ROW_HANDOFF.stagger;
    }
    let removed = 0;
    for (const [key, record] of this.records) {
      if (record.departure < 1e6 && (immediate || clock >= record.departure + this.disappear)) {
        if (removed >= maxAdds || now() - start >= budget) break;
        remove(record); this.records.delete(key); this.free.add(record.cell);
        this.recycled.set(record.cell, key); this.preferred.set(key, record.cell);
        removed++;
      }
    }
    let added = 0;
    for (const [key, char] of this.pending) {
      if (!this.free.size || added >= maxAdds || now() - start >= budget) break;
      if (this.records.has(key)) continue;
      const preferred = this.preferred.get(key);
      const cell = this.free.has(preferred) ? preferred : [...this.free].find(candidate => {
        const reserved = this.recycled.get(candidate);
        return !reserved || !this.pending.has(reserved);
      });
      if (cell === undefined) continue;
      const info = this.cells[cell];
      const round = Math.floor(char.revealOrder / (info.length * 8));
      const rounds = Math.max(1, Math.ceil(char.revealCount / (info.length * 8)) - 1);
      const delay = Math.min(LIVE.spread, round / rounds * .4 + info.lane * .012 + info.index * .025);
      const replacing = this.recycled.has(cell);
      const arrival = immediate ? clock - this.appear : replacing && this.rowTransitions
        ? clock + info.index * ROW_HANDOFF.letterDelay : Math.max(clock, this.batchStart + delay);
      const record = { ...char, cell, asr: info, arrival, departure: 1e6, appear: this.appear, disappear: this.disappear };
      add(record);
      this.records.set(key, record); this.free.delete(cell); this.pending.delete(key);
      this.preferred.delete(this.recycled.get(cell)); this.recycled.delete(cell);
      if (replacing) this.activeRows.set(info.slot, Math.max(this.activeRows.get(info.slot) ?? 0, arrival + this.appear));
      this.endAt = Math.max(this.endAt, arrival + this.appear);
      added++;
    }
    return added;
  }

  get hasPending() {
    if (this.pending.size) return true;
    for (const record of this.records.values()) if (record.queued || record.departure < 1e6) return true;
    return false;
  }
  get transitioning() { return this.clock < this.endAt; }
}
