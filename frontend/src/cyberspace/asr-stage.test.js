import { expect, it, vi } from 'vitest';
import { Scene, Vector3 } from 'three';
import { AsrStage, TranscriptArrivals } from './asr-stage.js';

const subtitle = (id, end_ms = id * 1000) => ({ id, start_ms: end_ms - 1000, end_ms, text: '课堂字幕' });
it('only signals new source batches, not hydration, polling, corrections or restored tasks', () => {
  const arrivals = new TranscriptArrivals();
  expect(arrivals.observe([subtitle(1)], true)).toEqual([]);
  expect(arrivals.observe([subtitle(1), subtitle(2), subtitle(3)], true)).toEqual([2, 3]);
  expect(arrivals.observe([subtitle(1), subtitle(2), subtitle(3)], true)).toEqual([]);
  expect(arrivals.observe([{ ...subtitle(3), id: 77, text: '修正的字幕' }], true)).toEqual([]);
  const restored = new TranscriptArrivals(arrivals.snapshot());
  expect(restored.observe([subtitle(3)], true)).toEqual([]);
  expect(restored.observe([subtitle(4)], true)).toEqual([4]);
});
it('accepts the first live batch after an empty preview and consumes suppressed updates', () => {
  const arrivals = new TranscriptArrivals();
  arrivals.observe([], true);
  expect(arrivals.observe([subtitle(1)], true)).toEqual([1]);
  expect(arrivals.observe([subtitle(2)], false)).toEqual([]);
  expect(arrivals.observe([subtitle(2)], true)).toEqual([]);
  expect(arrivals.observe([subtitle(1)], true)).toEqual([]);
});
it('waits for actual rendered characters, runs once per batch and uses the shared clock', () => {
  const stage = new AsrStage(new Scene());
  const context = { active: true, reduced: false, records: [], positionOf: () => new Vector3(4, 2, -9) };
  stage.enqueue([1, 2]); stage.update(0, context);
  expect(stage.bursts).toBe(0);
  context.records = [{ subtitleId: 2, departure: 1e6 }];
  stage.update(.1, context); stage.update(.8, context);
  expect(stage.bursts).toBe(1); expect(stage.flow.visible).toBe(true);
  const positions = [...stage.flow.geometry.attributes.position.array];
  stage.update(.8, context);
  expect([...stage.flow.geometry.attributes.position.array]).toEqual(positions);
  stage.update(3, context);
  expect(stage.started).toBeNull(); expect(stage.flow.visible).toBe(false);
  expect(stage.target.toArray()).toEqual([4, 2, -9]);
  stage.dispose();
});
it('keeps at most the latest queued burst and cancels motion on reduction or stage departure', () => {
  const stage = new AsrStage(new Scene());
  stage.enqueue([1]); stage.enqueue([2]);
  expect([...stage.pending]).toEqual([2]);
  stage.update(0, { active: true, reduced: true });
  expect(stage.pending).toBeNull(); expect(stage.group.visible).toBe(true);
  stage.enqueue([3]); stage.update(1, { active: false, reduced: false });
  expect(stage.group.visible).toBe(false); expect(stage.pending).toBeNull();
  stage.dispose();
});
it('releases every stage buffer and material exactly once', () => {
  const scene = new Scene(), stage = new AsrStage(scene, 'gpt'), disposals = [];
  stage.group.traverse(object => {
    if (object.geometry) disposals.push(vi.spyOn(object.geometry, 'dispose'));
    if (object.material) disposals.push(vi.spyOn(object.material, 'dispose'));
  });
  stage.dispose(); stage.dispose();
  expect(scene.children).toHaveLength(0);
  for (const dispose of disposals) expect(dispose).toHaveBeenCalledOnce();
});
