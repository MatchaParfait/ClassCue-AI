import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import * as THREE from 'three';
import { LiveSubtitleField } from '../../../experiments/subtitle-cyberspace/src/live-field.js';
import { TIMES } from '../../../experiments/subtitle-cyberspace/src/core.js';

let field, owner, draw, fontDescriptor;
beforeEach(async () => {
  draw = vi.fn();
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ fillText: draw, clearRect: vi.fn() });
  fontDescriptor = Object.getOwnPropertyDescriptor(document, 'fonts');
  Object.defineProperty(document, 'fonts', { configurable: true, value: { load: vi.fn().mockResolvedValue([]) } });
  owner = { scene: new THREE.Scene(), options: { maxCharacters: 96 }, meshes: [], records: [], charToMarkers: new Map(),
    setMappings(mappings) { this.charToMarkers = new Map(mappings.flatMap(m => m.keys.map(key => [key, [m.id]]))); } };
  field = new LiveSubtitleField(owner, '', '', new THREE.PlaneGeometry(1, 1));
  await vi.waitFor(() => expect(field.fontReady).toBe(true));
  draw.mockClear();
});
afterEach(() => {
  field.dispose(); owner.rowVolume.dispose(); owner.intro.dispose(); vi.restoreAllMocks();
  if (fontDescriptor) Object.defineProperty(document, 'fonts', fontDescriptor); else delete document.fonts;
});
const data = (text, id = 1) => ({ subtitles: [{ id, text, start_ms: 0, end_ms: 1000 }], markers: [] });
function settle(at) { for (let i = 0; i < 70; i++) field.render(20, at + i / 30, false); }
it('reuses the atlas, mesh and geometry when a batch only adds known glyphs', () => {
  field.update(data('甲乙甲'), [], null); settle(0);
  const page = field.pages[0], mesh = page.mesh, geometry = mesh.geometry, texture = page.texture;
  const first = field.layout.records.get('1:0');
  expect(draw).toHaveBeenCalledTimes(2);
  field.update(data('甲乙甲乙甲'), [], null); settle(3);
  expect(field.pages[0]).toBe(page); expect(page.mesh).toBe(mesh); expect(page.mesh.geometry).toBe(geometry); expect(page.texture).toBe(texture);
  expect(draw).toHaveBeenCalledTimes(2); expect(field.layout.records.get('1:0')).toBe(first);
  expect(owner.total).toBe(5);
});
it('adds only missing glyphs, without removing or duplicating existing records', () => {
  field.update(data('甲乙'), [], null); settle(0);
  field.update(data('甲乙丙😀'), [], null); settle(3);
  expect(draw).toHaveBeenCalledTimes(4); expect(field.glyphs.size).toBe(4);
  expect(owner.present).toEqual(new Set(['1:0', '1:1', '1:2', '1:3']));
  expect(new Set(owner.records.map(r => r.cell)).size).toBe(4);
});
it('compacts GPU indices without changing surviving world positions', () => {
  field.update(data('甲乙丙'), [], null); settle(0);
  const first = field.layout.records.get('1:0'), position = [...first.stair];
  field.update(data('甲丁'), [], null); settle(3);
  expect(field.layout.records.get('1:0')).toBe(first); expect(first.stair).toEqual(position);
  expect(owner.records.map(r => r.char).sort()).toEqual(['丁', '甲']);
  for (const page of field.pages) page.records.forEach((r, index) => {
    expect(r.gpuIndex).toBe(index);
    expect([...page.mesh.geometry.attributes.aStair.array.slice(index * 3, index * 3 + 3)]).toEqual(r.stair.map(Math.fround));
  });
});
it('does not allocate a glyph atlas for a job without text', () => {
  field.update({ subtitles: [], markers: [] }, [], null); settle(0);
  expect(field.pages).toHaveLength(0); expect(owner.total).toBe(0); expect(owner.rowVolume.mesh).toBeDefined();
});
it('uses graphite only when explicitly requested, for both glyphs and depth particles', () => {
  field.update(data('真实字幕'), [], null); settle(0);
  expect(field.pages[0].mesh.material.uniforms.uGraphite.value).toBe(0);
  expect(owner.rowVolume.mesh.material.uniforms.uGraphite.value).toBe(0);
  field.dispose(); owner.rowVolume.dispose(); owner.intro.dispose();
  owner.options.graphite = true;
  field = new LiveSubtitleField(owner, '', '', new THREE.PlaneGeometry(1, 1));
  const page = field.createPage();
  expect(page.mesh.material.uniforms.uGraphite.value).toBe(1);
  expect(owner.rowVolume.mesh.material.uniforms.uGraphite.value).toBe(1);
});
it('disposes cached textures and buffers on departure', () => {
  field.update(data('真实字幕'), [], null); settle(0);
  const page = field.pages[0], texture = vi.spyOn(page.texture, 'dispose'), geometry = vi.spyOn(page.mesh.geometry, 'dispose');
  field.dispose();
  expect(texture).toHaveBeenCalledTimes(1); expect(geometry).toHaveBeenCalledTimes(1); expect(owner.meshes).toHaveLength(0);
});

it('assigns analysis targets only at the stage boundary and preserves the ASR source geometry', async () => {
  field.dispose(); owner.rowVolume.dispose(); owner.intro.dispose();
  owner.options.analysisLayers = true;
  field = new LiveSubtitleField(owner, '', '', new THREE.PlaneGeometry(1, 1));
  await vi.waitFor(() => expect(field.fontReady).toBe(true));
  field.update(data('保持字形稳定，按句整理字幕'), [], null); settle(0);
  const first = owner.records[0], stair = [...first.stair], page = field.pages[0];
  expect(field.analysis.cells.size).toBe(0);
  field.render(TIMES.build, 3, false);
  expect(first.wallFace).toMatch(/^layer-/);
  const wall = [...first.wall];
  expect(first.stair).toEqual(stair);
  field.update(data('保持字形稳定，按句整理字幕'), [], null);
  field.render(TIMES.build + 3, 6, false);
  expect(first.wall).toEqual(wall);
  expect(field.pages[0]).toBe(page);
  const target = page.mesh.geometry.attributes.aWall;
  expect([...target.array.slice(first.gpuIndex * 3, first.gpuIndex * 3 + 3)]).toEqual(wall.map(Math.fround));
});

it('keeps the full demo transcript on ASR geometry until analysis actually begins', async () => {
  field.dispose(); owner.rowVolume.dispose(); owner.intro.dispose();
  owner.options.analysisLayers = true; owner.options.maxCharacters = 4000;
  field = new LiveSubtitleField(owner, '', '', new THREE.PlaneGeometry(1, 1));
  await vi.waitFor(() => expect(field.fontReady).toBe(true));
  field.update(data('课堂字幕'.repeat(1000)), [], null); settle(0);
  expect(owner.records).toHaveLength(field.layout.limit);
  expect(owner.records.length).toBeGreaterThan(3900);
  expect(field.analysisStarted).not.toBe(true);
  for (const record of owner.records) {
    expect(record.wall).toEqual(record.stair);
    expect(record.wallFrame).toEqual([0, 0, 0]);
    expect(record.wallFace).toBe('asr');
  }
});
