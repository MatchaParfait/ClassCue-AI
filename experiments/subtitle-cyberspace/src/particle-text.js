import * as THREE from 'three';
import { ANALYSIS } from './analysis-layout.js';
import { GLYPH_STATE_WIDTH, particleVertex, particleFragment } from './particle-shaders.js';
import { ParticleAccents } from './particle-accents.js';

export const PARTICLE_BUDGET = 280000;
const STATE_WIDTH = GLYPH_STATE_WIDTH;
const SCAN_SECONDS = 3.6;

// Sample only ink. Repeated glyphs share this result, not their animated state.
export function sampleGlyphPixels(rgba, width = 48, height = 48) {
  const points = [];
  for (let y = 0; y < height; y += 2) for (let x = 0; x < width; x += 2) {
    let alpha = 0, px = x, py = y;
    for (let dy = 0; dy < 2 && y + dy < height; dy++) for (let dx = 0; dx < 2 && x + dx < width; dx++) {
      const a = rgba[((y + dy) * width + x + dx) * 4 + 3];
      if (a > alpha) { alpha = a; px = x + dx; py = y + dy; }
    }
    if (alpha < 48) continue;
    // Local ink covariance estimates the stroke tangent. This is not a font's
    // handwriting order: motion follows nearby ink, not a random radial vector.
    let weight = 0, sx = 0, sy = 0, xx = 0, yy = 0, xy = 0;
    for (let dy = -3; dy <= 3; dy++) for (let dx = -3; dx <= 3; dx++) {
      const nx = px + dx, ny = py + dy;
      const a = nx >= 0 && ny >= 0 && nx < width && ny < height ? rgba[(ny * width + nx) * 4 + 3] / 255 : 0;
      weight += a; sx += dx * a; sy -= dy * a;
      xx += dx * dx * a; yy += dy * dy * a; xy -= dx * dy * a;
    }
    const angle = .5 * Math.atan2(2 * (xy - sx * sy / weight), xx - yy - (sx * sx - sy * sy) / weight);
    const edge = Math.min(1, Math.max(0, (1 - weight / 49) * 1.8));
    points.push([(px + .5) / width - .5, .5 - (py + .5) / height, alpha / 255,
      Math.cos(angle), Math.sin(angle), edge]);
  }
  return points;
}

function hash(value) {
  let n = 2166136261;
  for (const char of String(value)) n = Math.imul(n ^ char.charCodeAt(0), 16777619);
  // Avalanche nearby numeric subtitle IDs so whole pages do not loosen together.
  n = Math.imul(n ^ (n >>> 16), 0x85ebca6b);
  n = Math.imul(n ^ (n >>> 13), 0xc2b2ae35);
  n ^= n >>> 16;
  return (n >>> 0) / 4294967296;
}

// High-quality analysis only: ink samples are the text itself, with no intact
// glyph beneath them. The face and budgeted accents share one small state texture.
export class ParticleText {
  constructor(scene) {
    this.samples = new Map(); this.entries = []; this.groups = []; this.count = 0; this.revision = 0;
    this.scanGroup = null; this.scanStartedAt = 0; this.scanSerial = 0;
    this.state = new Float32Array(STATE_WIDTH * 4);
    this.stateTexture = new THREE.DataTexture(this.state, STATE_WIDTH, 1, THREE.RGBAFormat, THREE.FloatType);
    this.stateTexture.minFilter = this.stateTexture.magFilter = THREE.NearestFilter;
    this.stateTexture.generateMipmaps = false;
    this.geometry = new THREE.InstancedBufferGeometry();
    const quad = new THREE.PlaneGeometry(1, 1);
    this.geometry.setIndex(quad.index);
    this.geometry.setAttribute('position', quad.attributes.position);
    this.geometry.setAttribute('uv', quad.attributes.uv);
    quad.dispose();
    this.origin = new THREE.InstancedBufferAttribute(new Float32Array(PARTICLE_BUDGET * 3), 3).setUsage(THREE.DynamicDrawUsage);
    this.particles = new THREE.InstancedBufferAttribute(new Float32Array(PARTICLE_BUDGET * 3), 3).setUsage(THREE.DynamicDrawUsage);
    this.strokes = new THREE.InstancedBufferAttribute(new Float32Array(PARTICLE_BUDGET * 4), 4).setUsage(THREE.DynamicDrawUsage);
    this.flow = new THREE.InstancedBufferAttribute(new Float32Array(PARTICLE_BUDGET * 2), 2).setUsage(THREE.DynamicDrawUsage);
    this.geometry.setAttribute('aOrigin', this.origin);
    this.geometry.setAttribute('aParticle', this.particles);
    this.geometry.setAttribute('aStroke', this.strokes);
    this.geometry.setAttribute('aFlow', this.flow);
    this.geometry.instanceCount = 0;
    this.material = new THREE.ShaderMaterial({
      uniforms: { uGlyphState: { value: this.stateTexture }, uTime: { value: 0 }, uActive: { value: 0 },
        uOpacity: { value: 0 }, uHasHits: { value: 0 }, uReveal: { value: 0 },
        uPointer: { value: new THREE.Vector3(0, 0, 0) }, uAspect: { value: 1 } },
      vertexShader: particleVertex, fragmentShader: particleFragment,
      transparent: true, depthWrite: false, depthTest: false,
    });
    this.mesh = new THREE.Mesh(this.geometry, this.material);
    this.mesh.frustumCulled = false; this.mesh.visible = false;
    scene.add(this.mesh);
    this.accents = new ParticleAccents(scene, this.material.uniforms);
    this.accentsReduced = false;
  }

  glyph(record) {
    let points = this.samples.get(record.char);
    if (points) return points;
    const { page, index } = record.atlas;
    const pixels = page.ctx.getImageData(index % 16 * 48, Math.floor(index / 16) * 48, 48, 48);
    points = sampleGlyphPixels(pixels.data);
    this.samples.set(record.char, points);
    return points;
  }

  sync(records) {
    const active = records.filter(r => r.departure >= 1e6 && r.wallFace?.startsWith('layer-')).slice(0, ANALYSIS.maxCharacters);
    if (active.length === this.entries.length && active.every((r, i) => {
      const old = this.entries[i];
      return old.record === r && old.char === r.char && old.wall.every((v, j) => v === r.wall[j]);
    })) return;
    const grouped = new Map();
    this.entries = active.map(record => {
      const entry = { record, char: record.char, wall: [...record.wall] };
      const id = String(record.subtitleId);
      if (!grouped.has(id)) grouped.set(id, { id, entries: [] });
      grouped.get(id).entries.push(entry);
      return entry;
    });
    this.groups = [...grouped.values()].sort((a, b) => hash(a.id) - hash(b.id));
    for (const group of this.groups) {
      group.entries.sort((a, b) => (a.record.index ?? Number(a.record.key.split(':').at(-1))) - (b.record.index ?? Number(b.record.key.split(':').at(-1))));
      group.entries.forEach((entry, index) => { entry.group = group.id; entry.order = index; entry.length = group.entries.length; });
    }
    this.count = 0;
    const perChar = Math.min(192, Math.floor(PARTICLE_BUDGET / Math.max(1, active.length)));
    for (let slot = 0; slot < active.length; slot++) {
      const record = active[slot], points = this.glyph(record);
      const limit = Math.min(points.length, perChar, record.wallFrame[2] < 3 ? 192 : 96);
      const yaw = record.wallFrame[0], baseSeed = hash(record.key);
      for (let n = 0; n < limit; n++) {
        const [x, y, coverage, tx, ty, edge] = points[Math.floor(n * points.length / limit)];
        const particle = this.count++, i = particle * 3;
        this.origin.array.set([record.wall[0] + x * .36 * Math.cos(yaw), record.wall[1] + y * .36,
          record.wall[2] - x * .36 * Math.sin(yaw)], i);
        this.particles.array.set([slot, (baseSeed + n * .61803398875) % 1, coverage], i);
        this.strokes.array.set([tx * Math.cos(yaw), ty, -tx * Math.sin(yaw), edge], particle * 4);
        const order = (.5 - y) * .65 + (x + .5) * .35, entry = this.entries[slot];
        this.flow.array.set([order, (entry.order + order) / entry.length], particle * 2);
      }
    }
    this.geometry.instanceCount = this.count;
    for (const attribute of [this.origin, this.particles, this.strokes, this.flow]) {
      attribute.clearUpdateRanges();
      if (this.count) { attribute.addUpdateRange(0, this.count * attribute.itemSize); attribute.needsUpdate = true; }
    }
    this.accents?.sync(this, PARTICLE_BUDGET);
    this.revision++;
  }

  update(records, hits, focus, time, active, opacity, pointer = null, aspect = 1, revealActive = active) {
    this.sync(records);
    // Decorative local activity, not a claim about the LLM's token/reading
    // position. The API exposes chunk counts, not exact active subtitle ranges.
    // Pick just one sentence; polling and preview -> saved IDs never restart it.
    const candidates = this.groups.filter(group => group.entries.some(e => !(hits.get(e.record.key) > 0)));
    if (active && (time - this.scanStartedAt >= SCAN_SECONDS || !candidates.some(g => g.id === this.scanGroup))) {
      this.scanGroup = candidates.length ? candidates[this.scanSerial++ % candidates.length].id : null;
      this.scanStartedAt = time;
    }
    const scan = active ? Math.min(1.16, (time - this.scanStartedAt) / SCAN_SECONDS * 1.32 - .16) : -2;
    this.state.fill(0);
    for (let slot = 0; slot < this.entries.length; slot++) {
      const { record, group } = this.entries[slot], hit = hits.get(record.key) ?? 0;
      this.state.set([hit, hit > 0 && record.markers.some(id => String(id) === String(focus)) ? 1 : 0,
        1, group === this.scanGroup ? scan : -2], slot * 4);
    }
    this.stateTexture.needsUpdate = true;
    const u = this.material.uniforms;
    if (active || revealActive) u.uTime.value = time;
    u.uReveal.value = revealActive ? 1 : 0;
    u.uActive.value = active ? 1 : 0; u.uOpacity.value = opacity; u.uHasHits.value = hits.size ? 1 : 0;
    u.uPointer.value.set(pointer?.x ?? 0, pointer?.y ?? 0, active && pointer ? 1 : 0);
    u.uAspect.value = aspect;
    this.mesh.visible = this.count > 0 && opacity > 0;
    this.accents?.update(this, this.mesh.visible, active, revealActive);
  }

  get totalCount() { return this.count + (this.accents?.reliefCount ?? 0) + (this.accents?.trailCount ?? 0); }

  reduceAccents() {
    if (!this.accents) return false;
    this.accents.dispose(); this.accents = null; this.accentsReduced = true;
    return true;
  }

  dispose() {
    this.accents?.dispose(); this.accents = null;
    this.mesh.removeFromParent(); this.geometry.dispose(); this.material.dispose(); this.stateTexture.dispose();
    this.samples.clear(); this.entries = []; this.groups = []; this.scanGroup = null; this.count = 0;
  }
}
