import { expect, it } from 'vitest';
import { Vector3 } from 'three';
import { ANALYSIS, AnalysisLayout, analysisSurface } from '../../../experiments/subtitle-cyberspace/src/analysis-layout.js';
import { SubtitleScene } from '../../../experiments/subtitle-cyberspace/src/scene.js';
import { TIMES, WALL_TIME } from '../../../experiments/subtitle-cyberspace/src/core.js';

const sentence = (subtitleId, count) => Array.from({ length: count }, (_, index) => ({ key: `${subtitleId}:${index}`, subtitleId, index }));

it('lays out whole sentence rows left to right and keeps every glyph on its layer', () => {
  const layout = new AnalysisLayout();
  layout.sync([...sentence(1, 40), ...sentence(2, 12), ...sentence(3, 25)]);
  for (let i = 1; i < ANALYSIS.columns; i++) {
    const a = analysisSurface(layout.cells.get(`1:${i - 1}`)), b = analysisSurface(layout.cells.get(`1:${i}`));
    expect(b.face).toBe(a.face); expect(b.position[1]).toBe(a.position[1]);
    expect(new Vector3(...a.position).distanceTo(new Vector3(...b.position))).toBeCloseTo(ANALYSIS.pitch);
    expect(b.position[0]).toBeGreaterThan(a.position[0]);
  }
  expect(new Set([...layout.cells.values()].map(cell => analysisSurface(cell).face)).size).toBe(3);
});

it('polling, additions and removals do not move surviving sentence positions', () => {
  const layout = new AnalysisLayout(), first = [...sentence(1, 60), ...sentence(2, 25)];
  layout.sync(first);
  const positions = new Map(layout.cells);
  layout.sync(first.map(c => ({ ...c })));
  expect(layout.cells).toEqual(positions);
  layout.sync([...sentence(1, 60), ...sentence(3, 60)]);
  for (const c of sentence(1, 60)) expect(layout.cells.get(c.key)).toBe(positions.get(c.key));
  expect(layout.cells.has('2:0')).toBe(false);
  expect(new Set(layout.cells.values()).size).toBe(layout.cells.size);
});

it('fits the full 4000-character budget including many short sentences without duplicates', () => {
  const layout = new AnalysisLayout(), chars = Array.from({ length: 1000 }, (_, i) => sentence(i, 4)).flat();
  layout.sync(chars);
  expect(layout.cells.size).toBe(4000);
  expect(new Set(layout.cells.values()).size).toBe(4000);
  expect(Math.max(...layout.cells.values())).toBeLessThan(5 * ANALYSIS.columns * ANALYSIS.rows);
});

it('keeps hit testing aligned with lifted evidence and does not drift while analysis waits', () => {
  const record = { key: '1:0', stair: [1, 2, 3], wall: [4, 5, -10], wallFrame: [.1, 0, 1], departure: 1e6, copy: 0 };
  const scene = { options: { analysisLayers: true }, time: WALL_TIME + 2, reduced: false, hits: new Map([['1:0', 1]]) };
  const before = SubtitleScene.prototype.positionOf.call(scene, record);
  expect(before.toArray()).toEqual([4, 5, -10 + ANALYSIS.lift]);
  scene.time += 80;
  expect(SubtitleScene.prototype.positionOf.call(scene, record).toArray()).toEqual(before.toArray());
  scene.reduced = true; scene.time = TIMES.build;
  expect(SubtitleScene.prototype.positionOf.call(scene, record).toArray()).toEqual(before.toArray());
});

it('allows clicking returned evidence as soon as its color transition starts', () => {
  const record = { key: '1:0', markers: [42] };
  const scene = { hits: new Map([['1:0', .01]]),
    canvas: { getBoundingClientRect: () => ({ left: 0, top: 0, width: 200, height: 200 }) },
    meshes: [{ records: [record], mesh: { geometry: { instanceCount: 1 } } }],
    positionOf: () => ({ project: () => new Vector3(0, 0, 0) }),
  };
  expect(SubtitleScene.prototype.pick.call(scene, 100, 100)).toBe(42);
  scene.hits.set('1:0', 0);
  expect(SubtitleScene.prototype.pick.call(scene, 100, 100)).toBeNull();
});

it.each([0, 39, 40, 41, 80, 104])('never morphs ASR text early even with legacy column %s', column => {
  const record = { key: '1:0', stair: [1, 2, 3], wall: [4, 5, -10], wallFrame: [0, 0, column], departure: 1e6, copy: 0 };
  const scene = { options: { analysisLayers: true }, reduced: false, hits: new Map() };
  for (const time of [TIMES.space, 20, TIMES.build - .01, TIMES.build]) {
    scene.time = time;
    expect(SubtitleScene.prototype.positionOf.call(scene, record).toArray()).toEqual(record.stair);
  }
  scene.time = WALL_TIME;
  expect(SubtitleScene.prototype.positionOf.call(scene, record).toArray()).toEqual(record.wall);
});
