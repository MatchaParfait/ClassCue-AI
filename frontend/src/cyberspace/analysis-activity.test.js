import { expect, it, vi } from 'vitest';
import { Scene } from 'three';
import { AnalysisActivity } from '../../../experiments/subtitle-cyberspace/src/analysis-activity.js';

const record = (key, layer = 0) => ({ key, departure: 1e6, wallFrame: [0, 0, layer], wall: [0, 1.5, -5] });

it('only shows occupied layers and never changes subtitle coordinates', () => {
  const activity = new AnalysisActivity(new Scene()), records = [record('a'), record('b', 2)];
  const before = structuredClone(records);
  activity.update(records, new Map(), 3, true, 0);
  expect(activity.layers.every(l => !l.mesh.visible)).toBe(true);
  activity.update(records, new Map(), 5, true, 1);
  expect(activity.layers.map(l => l.mesh.visible)).toEqual([true, false, true, false, false]);
  expect(records).toEqual(before);
  activity.dispose();
});

it('pulses on real new evidence and ignores polling and marker ID changes', () => {
  const activity = new AnalysisActivity(new Scene()), records = [record('a'), record('b')];
  activity.update(records, new Map(), 1, true, 1);
  expect(activity.layers[0].pulseAt).toBe(-Infinity);
  activity.update(records, new Map([['a', .2]]), 2, true, 1);
  expect(activity.layers[0].pulseAt).toBe(2);
  activity.update(records.map(r => ({ ...r, markers: [123] })), new Map([['a', 1]]), 5, true, 1);
  expect(activity.layers[0].pulseAt).toBe(2);
  activity.update(records, new Map([['a', 1], ['b', .1]]), 6, true, 1);
  expect(activity.layers[0].pulseAt).toBe(6);
  activity.dispose();
});

it('consumes evidence while stopped or reduced without replaying it on resume', () => {
  const activity = new AnalysisActivity(new Scene()), records = [record('a')], hits = new Map([['a', 1]]);
  activity.update(records, new Map(), 2, true, 1);
  activity.update(records, hits, 9, false, 1);
  const uniforms = activity.layers[0].mesh.material.uniforms;
  expect(uniforms.uActive.value).toBe(0);
  expect(uniforms.uTime.value).toBe(2);
  expect(uniforms.uPulseAge.value).toBe(10);
  activity.update(records, hits, 10, true, 1);
  expect(activity.layers[0].pulseAt).toBe(-Infinity);
  expect(uniforms.uActive.value).toBe(1);
  activity.dispose();
});

it('removes effects and releases shared geometry and every material on departure', () => {
  const scene = new Scene(), activity = new AnalysisActivity(scene);
  const geometry = vi.spyOn(activity.geometry, 'dispose');
  const materials = activity.layers.map(l => vi.spyOn(l.mesh.material, 'dispose'));
  activity.dispose();
  expect(scene.children).toHaveLength(0);
  expect(geometry).toHaveBeenCalledTimes(1);
  for (const dispose of materials) expect(dispose).toHaveBeenCalledTimes(1);
});
