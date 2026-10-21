// A result packet produces one camera request, independent of how many markers
// it contains. Character identities survive polling and preview -> saved IDs.
export class EvidenceJourney {
  constructor(seen = new Map()) {
    this.seen = seen; this.scope = null; this.pending = null; this.serial = 0;
  }

  sync(mappings, scope, automatic) {
    const switched = this.scope !== null && this.scope !== scope;
    this.scope = scope;
    if (switched) this.pending = null;
    if (!this.seen.has(scope)) this.seen.set(scope, new Set());
    const seen = this.seen.get(scope), fresh = [];
    for (const mapping of mappings) {
      if (mapping.keys.some(key => !seen.has(key))) fresh.push(mapping);
      for (const key of mapping.keys) seen.add(key);
    }
    if (!switched && automatic && fresh.length && this.pending?.reason !== 'manual') {
      const marker = fresh.at(-1);
      this.pending = { keys: [...marker.keys], markerId: marker.id, reason: 'automatic' };
    }
    return switched;
  }

  select(mapping) {
    if (!mapping?.keys.length) return;
    // Explicit clicks are events, including repeated clicks on the same item.
    this.pending = { keys: [...mapping.keys], markerId: mapping.id, reason: 'manual' };
  }

  take(hits, mappings, busy = false) {
    if (!this.pending || (busy && this.pending.reason === 'automatic')) return null;
    const keys = new Set(this.pending.keys);
    const candidates = mappings.filter(m => m.keys.some(key => keys.has(key)));
    const mapping = candidates.find(m => String(m.id) === String(this.pending.markerId))
      ?? candidates.sort((a, b) => b.keys.filter(key => keys.has(key)).length - a.keys.filter(key => keys.has(key)).length)[0];
    if (!mapping) { this.pending = null; return null; }
    if (!this.pending.keys.some(key => (hits.get(key) ?? 0) >= .9)) return null;
    const request = { ...this.pending, markerId: mapping.id, serial: ++this.serial };
    this.pending = null;
    return request;
  }

  reset() { this.pending = null; return { serial: ++this.serial, reset: true }; }
}
