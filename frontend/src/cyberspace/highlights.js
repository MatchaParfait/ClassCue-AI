import { smooth } from '../../../experiments/subtitle-cyberspace/src/core.js';

export const HIT_REVEAL_SECONDS = 1.2;

// Character identity survives chunk overlap and the change from preview IDs to saved IDs.
export class CharacterHighlights {
  constructor({ wave = false } = {}) {
    this.wave = wave; this.duration = wave ? .5 : HIT_REVEAL_SECONDS;
    this.starts = new Map(); this.scope = null; this.lastGroup = -Infinity; this.endAt = -Infinity;
  }

  sync(mappings, time, scope) {
    if (scope !== this.scope) { this.starts.clear(); this.lastGroup = -Infinity; this.scope = scope; }
    const wanted = new Set(mappings.flatMap(mapping => mapping.keys));
    for (const key of this.starts.keys()) if (!wanted.has(key)) this.starts.delete(key);
    for (const mapping of mappings) {
      const fresh = mapping.keys.filter(key => !this.starts.has(key));
      if (!fresh.length) continue;
      // A burst/cached result gets a bounded reveal, not an unbounded animation backlog.
      const start = Math.min(time + .24, Math.max(time, this.lastGroup + .06));
      fresh.forEach((key, index) => this.starts.set(key, start + (this.wave ? index / Math.max(1, fresh.length - 1) * .3 : 0)));
      this.lastGroup = start;
    }
    this.endAt = -Infinity;
    for (const start of this.starts.values()) this.endAt = Math.max(this.endAt, start + this.duration);
  }

  values(time, immediate = false) {
    const values = new Map();
    for (const [key, start] of this.starts) {
      const amount = immediate ? 1 : smooth((time - start) / this.duration);
      // Reduced motion and paused results are already settled. Resuming high
      // quality must not make those same green characters un-highlight again.
      if (immediate && this.wave && start > time - this.duration) this.starts.set(key, time - this.duration);
      if (amount > 0) values.set(key, amount);
    }
    if (immediate && this.wave) this.endAt = Math.min(this.endAt, time);
    return values;
  }

  pending(time) { return time < this.endAt; }
}
