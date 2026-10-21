import { expect, it, vi } from 'vitest';
import * as THREE from 'three';
import { ParticleText, PARTICLE_BUDGET, sampleGlyphPixels } from '../../../experiments/subtitle-cyberspace/src/particle-text.js';
import { SubtitleScene } from '../../../experiments/subtitle-cyberspace/src/scene.js';
import { TIMES, WALL_TIME } from '../../../experiments/subtitle-cyberspace/src/core.js';
import { GLYPH_DEPTH } from '../../../experiments/subtitle-cyberspace/src/particle-shaders.js';

const pixels = new Uint8ClampedArray(48 * 48 * 4);
for (let y = 8; y < 40; y++) for (let x = 10; x < 38; x++) pixels[(y * 48 + x) * 4 + 3] = 255;
function record(key = '1:0', char = '课') {
  return { key, char, subtitleId: 1, departure: 1e6, wallFace: 'layer-0', wall: [0, 1.5, -5],
    wallFrame: [0, 0, 0], markers: [42], atlas: { index: 0, page: { ctx: { getImageData: vi.fn(() => ({ data: pixels })) } } } };
}

it('samples only ink, keeps glyph coordinates centered and leaves empty glyphs empty', () => {
  expect(sampleGlyphPixels(new Uint8ClampedArray(48 * 48 * 4))).toEqual([]);
  const points = sampleGlyphPixels(pixels);
  expect(points.length).toBeGreaterThan(100);
  for (const [x, y, a] of points) {
    expect(x).toBeGreaterThan(-.3); expect(x).toBeLessThan(.3);
    expect(y).toBeGreaterThan(-.35); expect(y).toBeLessThan(.35); expect(a).toBe(1);
  }
});

it('derives flow direction from actual stroke ink and distinguishes solid interiors from edges', () => {
  const horizontal = new Uint8ClampedArray(48 * 48 * 4), vertical = new Uint8ClampedArray(horizontal.length);
  for (let y = 0; y < 48; y++) for (let x = 0; x < 48; x++) {
    if (y >= 22 && y < 25 && x >= 8 && x < 40) horizontal[(y * 48 + x) * 4 + 3] = 255;
    if (x >= 22 && x < 25 && y >= 8 && y < 40) vertical[(y * 48 + x) * 4 + 3] = 255;
  }
  const h = sampleGlyphPixels(horizontal).filter(p => Math.abs(p[0]) < .2);
  const v = sampleGlyphPixels(vertical).filter(p => Math.abs(p[1]) < .2);
  expect(h.every(p => Math.abs(p[3]) > .95 && Math.abs(p[4]) < .1)).toBe(true);
  expect(v.every(p => Math.abs(p[4]) > .95 && Math.abs(p[3]) < .1)).toBe(true);
  const solid = sampleGlyphPixels(pixels);
  expect(solid.some(p => p[5] === 0)).toBe(true);
  expect(solid.some(p => p[5] > .5)).toBe(true);
  expect(solid.every(p => p.every(Number.isFinite))).toBe(true);
});

it('reuses sampled glyphs and particle buffers across polling and marker-ID changes', () => {
  const field = new ParticleText(new THREE.Scene()), a = record(), b = record('1:1');
  field.update([a, b], new Map(), null, 1, true, 1);
  const revision = field.revision, geometry = field.geometry, origin = field.origin.array.slice(0, field.count * 3);
  const accentRevision = field.accents.trails.geometry.getAttribute('aOrigin').version;
  expect(a.atlas.page.ctx.getImageData).toHaveBeenCalledTimes(1);
  expect(b.atlas.page.ctx.getImageData).not.toHaveBeenCalled();
  a.markers = [101];
  field.update([a, b], new Map([['1:0', .4]]), 101, 2, true, 1);
  expect(field.revision).toBe(revision); expect(field.geometry).toBe(geometry);
  expect(field.origin.array.slice(0, field.count * 3)).toEqual(origin);
  expect(field.state[0]).toBeCloseTo(.4); expect(field.state[1]).toBe(1);
  expect(field.strokes.count).toBe(PARTICLE_BUDGET);
  expect(field.accents.trails.geometry.getAttribute('aOrigin').version).toBe(accentRevision);
  field.dispose();
});

it('combs only one sentence, preserves the sweep on polling and skips already confirmed text', () => {
  const field = new ParticleText(new THREE.Scene());
  const a = record('1:0'), b = record('1:1'), c = { ...record('2:0'), subtitleId: 2 };
  const records = [a, b, c];
  field.update(records, new Map(), null, 10, true, 1);
  const selected = field.scanGroup, started = field.scanStartedAt, revision = field.revision;
  field.update(records, new Map(), null, 11, true, 1);
  expect(field.scanGroup).toBe(selected); expect(field.scanStartedAt).toBe(started);
  expect(field.revision).toBe(revision);
  const scanning = field.entries.filter((_, i) => field.state[i * 4 + 3] > -1);
  expect(new Set(scanning.map(e => String(e.record.subtitleId)))).toEqual(new Set([selected]));
  const hits = new Map(records.filter(r => String(r.subtitleId) === selected).map(r => [r.key, 1]));
  field.update(records, hits, null, 12, true, 1);
  expect(field.scanGroup).not.toBe(selected);
  field.update(records, new Map(records.map(r => [r.key, 1])), null, 13, true, 1);
  expect(field.scanGroup).toBeNull();
  field.dispose();
});

it('keeps particle reading order stable, with local pointer focus disabled while stopped', () => {
  const field = new ParticleText(new THREE.Scene()), a = record('1:0'), b = record('1:1');
  field.update([b, a], new Map(), null, 3, true, 1, { x: .2, y: -.4 }, 1.5);
  const slotOrders = new Map();
  for (let n = 0; n < field.count; n++) {
    const slot = field.particles.array[n * 3], orders = slotOrders.get(slot) ?? [];
    orders.push(field.flow.array[n * 2 + 1]); slotOrders.set(slot, orders);
  }
  expect(Math.max(...slotOrders.get(1))).toBeLessThan(Math.min(...slotOrders.get(0)));
  const u = field.material.uniforms;
  expect(u.uPointer.value.toArray()).toEqual([.2, -.4, 1]);
  expect(u.uAspect.value).toBe(1.5);
  field.update([b, a], new Map(), null, 4, false, 1, { x: .2, y: -.4 });
  expect(u.uPointer.value.z).toBe(0); expect(u.uTime.value).toBe(3);
  expect(field.entries.every((_, i) => field.state[i * 4 + 3] === -2)).toBe(true);
  field.dispose();
});

it('bounds the full scene budget, excludes departing and ASR glyphs, and stops its clock', () => {
  const field = new ParticleText(new THREE.Scene());
  const records = Array.from({ length: 1800 }, (_, i) => record(`${i}:0`));
  field.update(records, new Map(), null, 2, true, 1);
  expect(field.count).toBeGreaterThan(100000); expect(field.count).toBeLessThanOrEqual(PARTICLE_BUDGET);
  expect(field.totalCount).toBeLessThanOrEqual(PARTICLE_BUDGET);
  expect(field.accents.reliefCount).toBeGreaterThan(0); expect(field.accents.trailCount).toBeGreaterThan(0);
  records[0].departure = 3; records[1].wallFace = 'asr';
  field.update(records, new Map(), null, 10, false, 1);
  expect(field.entries).toHaveLength(1798);
  expect(field.material.uniforms.uActive.value).toBe(0);
  expect(field.material.uniforms.uTime.value).toBe(2);
  const geometry = vi.spyOn(field.geometry, 'dispose'), texture = vi.spyOn(field.stateTexture, 'dispose');
  field.dispose(); expect(geometry).toHaveBeenCalledTimes(1); expect(texture).toHaveBeenCalledTimes(1);
  expect(field.mesh.parent).toBeNull();
});

it('adds shallow depth along the actual plane normal without moving the front or source records', () => {
  const scene = new THREE.Scene(), field = new ParticleText(scene), a = record();
  a.wallFrame[0] = .25;
  const wall = [...a.wall];
  field.update([a], new Map(), null, 1, true, 1);
  const { relief, trails, reliefCount } = field.accents;
  const surface = relief.geometry.getAttribute('aSurface').array;
  expect(reliefCount).toBeGreaterThan(0);
  expect(surface[0]).toBeCloseTo(Math.sin(.25)); expect(surface[2]).toBeCloseTo(Math.cos(.25));
  expect(surface[3]).toBeCloseTo(GLYPH_DEPTH * .46); expect(surface[7]).toBeCloseTo(GLYPH_DEPTH);
  expect(GLYPH_DEPTH / .36).toBeCloseTo(.13);
  expect(a.wall).toEqual(wall);
  expect(relief.renderOrder).toBeLessThan(field.mesh.renderOrder);
  expect(trails.renderOrder).toBeLessThan(field.mesh.renderOrder);
  expect(trails.material.uniforms).toBe(field.material.uniforms);
  expect(field.totalCount).toBeLessThanOrEqual(PARTICLE_BUDGET);
  const disposals = [relief.geometry, relief.material, trails.geometry, trails.material].map(item => vi.spyOn(item, 'dispose'));
  field.dispose();
  expect(scene.children).toHaveLength(0);
  for (const dispose of disposals) expect(dispose).toHaveBeenCalledTimes(1);
});

it('closes trails on confirmed results, pause and reduced motion while retaining static depth', () => {
  const field = new ParticleText(new THREE.Scene()), a = record();
  field.update([a], new Map(), null, 1, true, 1);
  field.update([a], new Map(), null, 2.6, true, 1);
  expect(field.accents.trails.visible).toBe(true);
  field.update([a], new Map([[a.key, 1]]), null, 3, true, 1);
  expect(field.accents.trails.visible).toBe(false);
  field.update([a], new Map([[a.key, .18]]), null, 4, false, 1);
  expect(field.accents.trails.visible).toBe(false);
  expect(field.material.uniforms.uReveal.value).toBe(0);
  expect(field.material.uniforms.uTime.value).toBe(3);
  expect(field.accents.relief.visible).toBe(true);
  // A completed task may still finish the confirmation, without restarting a scan.
  field.update([a], new Map([[a.key, .18]]), null, 4.1, false, 1, null, 1, true);
  expect(field.accents.trails.visible).toBe(true);
  expect(field.material.uniforms.uActive.value).toBe(0);
  expect(field.material.uniforms.uReveal.value).toBe(1);
  expect(field.material.uniforms.uTime.value).toBe(4.1);
  field.update([a], new Map([[a.key, 1]]), null, 5, false, 1);
  expect(field.accents.trails.visible).toBe(false);
  field.dispose();
});

it('removes accents first on slow devices without resampling the face or recreating accents on polling', () => {
  const scene = new THREE.Scene(), field = new ParticleText(scene), a = record();
  field.update([a], new Map(), null, 1, true, 1);
  const face = field.origin.array.slice(0, field.count * 3), revision = field.revision;
  expect(field.reduceAccents()).toBe(true); expect(field.reduceAccents()).toBe(false);
  expect(field.accentsReduced).toBe(true);
  expect(scene.children).toEqual([field.mesh]);
  field.update([a], new Map([[a.key, 1]]), null, 2, true, 1);
  expect(field.revision).toBe(revision);
  expect(field.origin.array.slice(0, field.count * 3)).toEqual(face);
  expect(field.totalCount).toBe(field.count);
  field.update([a, record('1:1')], new Map(), null, 3, true, 1);
  expect(field.accents).toBeNull();
  field.dispose();
});

function sceneHarness(quality = 'high') {
  const uniforms = Object.fromEntries(['uTime', 'uMotionTime', 'uAsrElapsed', 'uReduced', 'uDim', 'uAnalysisLayers', 'uParticleBlend', 'uAnalysisScan', 'uScanTime'].map(name => [name, { value: 0 }]));
  const r = record();
  const mesh = { visible: true, position: new THREE.Vector3(), material: { uniforms }, geometry: { setDrawRange: vi.fn(),
    attributes: { aHit: { array: new Float32Array(1) }, aFocus: { array: new Float32Array(1) } } } };
  const scene = { options: { analysisLayers: true, analysisQuality: quality, onParticleFallback: vi.fn() },
    fitComposition: vi.fn(), live: { fontReady: true, render: vi.fn() }, records: [r], meshes: [{ mesh, records: [r] }],
    scene: new THREE.Scene(), camera: new THREE.PerspectiveCamera(), trails: [], analysisScanning: true,
    renderer: { render: vi.fn(), setPixelRatio: vi.fn() },
  };
  const draw = (time, ambient, reduced = false) => SubtitleScene.prototype.render.call(scene, time, new Map(), reduced, Infinity, ambient);
  const dispose = () => { scene.particleText?.dispose(); scene.analysisActivity?.dispose(); };
  return { scene, draw, dispose, uniforms };
}

it('allocates high-quality particles only after ASR ends and replaces rather than overlays the glyphs', () => {
  const h = sceneHarness();
  h.draw(TIMES.space, 1); expect(h.scene.particleText).toBeUndefined();
  h.draw(TIMES.build, 2); expect(h.scene.particleText).toBeUndefined();
  h.draw(WALL_TIME, 3); h.draw(WALL_TIME + .4, 3.4);
  expect(h.scene.particleText.mesh.visible).toBe(true); expect(h.uniforms.uParticleBlend.value).toBe(1);
  expect(h.scene.analysisActivity.layers[0].mesh.material.uniforms.uPulseAge.value).toBe(10);
  h.scene.analysisScanning = false; h.draw(WALL_TIME + .4, 3.4, true);
  expect(h.scene.particleText.material.uniforms.uActive.value).toBe(0);
  SubtitleScene.prototype.degrade.call(h.scene); h.draw(WALL_TIME + .4, 3.4);
  expect(h.scene.particleText).toBeNull(); expect(h.uniforms.uParticleBlend.value).toBe(0);
  h.dispose();
});

it('keeps light mode intact and falls back if pixel sampling fails', () => {
  const low = sceneHarness('low'); low.draw(WALL_TIME, 1);
  expect(low.scene.particleText).toBeUndefined(); expect(low.uniforms.uParticleBlend.value).toBe(0); low.dispose();
  const high = sceneHarness();
  high.scene.records[0].atlas.page.ctx.getImageData.mockImplementation(() => { throw new Error('unavailable'); });
  high.draw(WALL_TIME, 1);
  expect(high.scene.particleText).toBeNull(); expect(high.uniforms.uParticleBlend.value).toBe(0);
  expect(high.scene.options.onParticleFallback).toHaveBeenCalledTimes(1);
  high.draw(WALL_TIME + 1, 2); expect(high.scene.options.onParticleFallback).toHaveBeenCalledTimes(1);
  high.dispose();
});
