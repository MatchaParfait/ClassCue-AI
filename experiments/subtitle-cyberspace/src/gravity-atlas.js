import * as THREE from 'three';

// Keep the reference atlas dimensions/aspect: both the lens and ribbon shaders
// derive physical glyph size and mip footprints from this layout.
export const GRAVITY_TEXT = Object.freeze({ width: 4096, height: 3072, columns: 4, rows: 12, capacity: 48, letters: 12, cellWidth: 1024, cellHeight: 256, inset: 42, advance: 78 });

// Code-point offsets match mapMarkers, including spaces, emoji and CJK extensions.
export function subtitlePhrases(subtitles) {
  const phrases = [];
  for (const subtitle of subtitles) {
    let chars = [], keys = [], first = 0;
    const flush = () => {
      if (chars.some(char => !/\s/u.test(char))) phrases.push({ id: `${subtitle.id}:${first}`, subtitleId: subtitle.id, start_ms: subtitle.start_ms, text: chars.join(''), keys: [...keys] });
      chars = []; keys = [];
    };
    Array.from(subtitle.text ?? '').forEach((char, index) => {
      if (!chars.length) first = index;
      chars.push(char); keys.push(`${subtitle.id}:${index}`);
      if (chars.length >= GRAVITY_TEXT.letters || (chars.length >= 8 && /[，。！？；,!?;]/u.test(char))) flush();
    });
    flush();
  }
  return phrases;
}

export function chooseGravityPhrases(phrases, mappings, focus, capacity = GRAVITY_TEXT.capacity, previous = []) {
  if (phrases.length <= capacity) return phrases;
  const chosen = new Set();
  // Focus and real hits take priority over decorative coverage of a long lesson.
  const ordered = [...mappings.filter(m => String(m.id) === String(focus)), ...mappings];
  for (const mapping of ordered) {
    const keys = new Set(mapping.keys);
    for (let i = 0; i < phrases.length && chosen.size < capacity; i++) if (phrases[i].keys.some(key => keys.has(key))) chosen.add(i);
  }
  // New evidence may replace background excerpts, but a new packet must not
  // resample the whole lesson and replace dozens of unrelated visible lines.
  const indexById = new Map(phrases.map((phrase, index) => [phrase.id, index]));
  for (const entry of previous) {
    const index = indexById.get(entry.id);
    if (index !== undefined && chosen.size < capacity) chosen.add(index);
  }
  const remaining = phrases.map((_, i) => i).filter(i => !chosen.has(i)), room = capacity - chosen.size;
  for (let i = 0; i < room; i++) chosen.add(remaining[Math.floor(i * remaining.length / room)]);
  return [...chosen].sort((a, b) => a - b).map(i => phrases[i]);
}

export class GravityAtlas {
  constructor() {
    const canvas = document.createElement('canvas');
    canvas.width = GRAVITY_TEXT.width; canvas.height = GRAVITY_TEXT.height;
    this.context = canvas.getContext('2d');
    if (!this.context) throw new Error('字幕贴图无法创建');
    this.texture = new THREE.CanvasTexture(canvas);
    this.texture.wrapS = this.texture.wrapT = THREE.RepeatWrapping;
    this.texture.minFilter = THREE.LinearMipmapLinearFilter;
    this.texture.magFilter = THREE.LinearFilter;
    this.state = new Uint8Array(32 * GRAVITY_TEXT.capacity * 4);
    this.states = new THREE.DataTexture(this.state, 32, GRAVITY_TEXT.capacity, THREE.RGBAFormat);
    this.states.magFilter = this.states.minFilter = THREE.NearestFilter;
    this.states.needsUpdate = true;
    this.phrases = []; this.entries = []; this.signature = null; this.revision = 0;
    this.keyToSlot = new Map(); this.hitCount = 0;
    this.tiles = new Array(GRAVITY_TEXT.capacity);
    this.opacity = new Float32Array(GRAVITY_TEXT.capacity).fill(1);
    this.changes = new Map(); this.initialized = false;
  }

  get pending() { return this.changes.size > 0; }

  ready(keys) {
    const wanted = new Set(keys);
    return this.tiles.some((entry, slot) => this.opacity[slot] >= .9 && entry?.keys.some(key => wanted.has(key)));
  }

  update(subtitles, mappings, focus, hits, { delta = 0, immediate = true } = {}) {
    const sourceChanged = this.source !== subtitles;
    if (sourceChanged) { this.phrases = subtitlePhrases(subtitles); this.source = subtitles; }
    const selectionChanged = sourceChanged || this.mappings !== mappings || this.focus !== focus;
    if (selectionChanged) {
      const chosen = chooseGravityPhrases(this.phrases, mappings, focus, GRAVITY_TEXT.capacity, this.entries);
      const pending = new Map(chosen.map(entry => [entry.id, entry]));
      this.selection = new Array(chosen.length);
      // Keep existing inscriptions in their physical atlas slots when new
      // results arrive; moving the camera must not shuffle text underneath it.
      this.entries.forEach((entry, slot) => {
        if (slot < chosen.length && pending.has(entry.id)) {
          this.selection[slot] = pending.get(entry.id); pending.delete(entry.id);
        }
      });
      const remaining = pending.values();
      for (let i = 0; i < chosen.length; i++) this.selection[i] ??= remaining.next().value;
      this.mappings = mappings; this.focus = focus;
      this.markerByKey = new Map();
      for (const mapping of mappings) for (const key of mapping.keys) {
        if (!this.markerByKey.has(key) || String(mapping.id) === String(focus)) this.markerByKey.set(key, mapping.id);
      }
    }
    const entries = this.selection;
    const signature = selectionChanged ? JSON.stringify(entries.map(e => [e.id, e.text])) : this.signature;
    if (signature !== this.signature) {
      this.signature = signature;
      for (let slot = 0; slot < GRAVITY_TEXT.capacity; slot++) {
        const entry = entries[slot % entries.length], current = this.tiles[slot];
        if (current?.id === entry?.id && current?.text === entry?.text) {
          if (this.changes.has(slot)) this.changes.set(slot, { entry: current, phase: 'in' });
          continue;
        }
        const pending = this.changes.get(slot);
        if (pending?.entry?.id !== entry?.id || pending?.entry?.text !== entry?.text || !pending) {
          this.changes.set(slot, { entry, phase: 'out' });
        }
      }
    }
    this.advanceTiles(delta, immediate || !this.initialized);
    this.initialized = true;
    const markerByKey = this.markerByKey;
    this.entries = entries;
    if (selectionChanged) for (const entry of entries) {
      entry.markerIds = [...new Set(entry.keys.map(key => markerByKey.get(key)).filter(id => id !== undefined))];
    }
    let changed = false;
    this.hitCount = 0;
    for (let slot = 0; slot < GRAVITY_TEXT.capacity; slot++) {
      const entry = this.tiles[slot];
      if (entry) entry.markerIds = [...new Set(entry.keys.map(key => markerByKey.get(key)).filter(id => id !== undefined))];
      for (let index = 0; index < 32; index++) {
        const key = entry?.keys[index], offset = (slot * 32 + index) * 4;
        const hit = key ? Math.round((hits.get(key) ?? 0) * 255) : 0;
        const selected = key && focus !== null && String(markerByKey.get(key)) === String(focus) ? 255 : 0;
        const opacity = entry ? Math.round(this.opacity[slot] * 255) : 0;
        if (hit && slot < entries.length) this.hitCount++;
        if (this.state[offset] !== hit || this.state[offset + 1] !== selected || this.state[offset + 2] !== opacity) changed = true;
        this.state[offset] = hit; this.state[offset + 1] = selected;
        this.state[offset + 2] = opacity;
      }
    }
    if (changed) this.states.needsUpdate = true;
  }

  advanceTiles(delta, immediate) {
    let painted = false;
    const ctx = this.context;
    for (const [slot, change] of this.changes) {
      if (change.phase === 'out') {
        this.opacity[slot] = immediate ? 0 : Math.max(0, this.opacity[slot] - Math.max(0, delta) / .16);
        if (this.opacity[slot] > 0) continue;
        const { entry } = change, x = slot % 4 * 1024, y = Math.floor(slot / 4) * 256;
        ctx.fillStyle = '#000'; ctx.fillRect(x, y, 1024, 256);
        ctx.font = '400 70px "Noto Experiment", "Microsoft YaHei", sans-serif';
        ctx.textBaseline = 'middle'; ctx.fillStyle = '#fff';
        if (entry) Array.from(entry.text).forEach((char, index) => ctx.fillText(char, x + 42 + index * 78, y + 128));
        this.tiles[slot] = entry; change.phase = 'in'; painted = true;
        // Publish the new ink at zero opacity for a full frame before fading in.
        if (!immediate) continue;
      }
      this.opacity[slot] = immediate ? 1 : Math.min(1, this.opacity[slot] + Math.max(0, delta) / .22);
      if (this.opacity[slot] === 1) this.changes.delete(slot);
    }
    if (painted) {
      this.revision++; this.texture.needsUpdate = true;
      this.keyToSlot.clear();
      this.tiles.forEach((entry, slot) => entry?.keys.forEach((key, index) => this.keyToSlot.set(key, { slot, index })));
    }
  }

  characterAt(u, v) {
    const fract = value => value - Math.floor(value);
    const x = fract(u) * 4, y = (1 - fract(v)) * 12;
    const slot = Math.min(11, Math.floor(y)) * 4 + Math.floor(x);
    const index = Math.floor(((x % 1) * 1024 - 42) / 78);
    const entry = this.tiles?.[slot];
    const key = entry?.keys[index];
    return key ? { entry, key, index, slot, markerId: this.markerByKey.get(key) } : null;
  }

  dispose() { this.texture.dispose(); this.states.dispose(); }
}
