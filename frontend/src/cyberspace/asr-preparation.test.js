import { expect, it } from 'vitest';
import { PreparationState } from './asr-preparation.js';

const waiting = { active: true, hasText: false, drawable: false, reduced: false };
it('waits for real, drawable subtitles and hands off once without delaying data', () => {
  const state = new PreparationState();
  expect(state.update(0, waiting)).toBe(1);
  expect(state.update(60, waiting)).toBe(1);
  expect(state.update(61, { ...waiting, hasText: true })).toBe(1);
  const ready = { ...waiting, hasText: true, drawable: true };
  expect(state.update(62, ready)).toBe(1);
  expect(state.update(62.4, ready)).toBeCloseTo(.5);
  // Frozen shared clock freezes the transition, even during repeated snapshots.
  expect(state.update(62.4, ready)).toBeCloseTo(.5);
  expect(state.update(63, ready)).toBe(0);
  expect(state.update(64, waiting)).toBe(0);
});
it('skips already populated tasks, restored tasks and later pipeline stages', () => {
  expect(new PreparationState().update(0, { ...waiting, hasText: true })).toBe(0);
  expect(new PreparationState(true).update(0, waiting)).toBe(0);
  const state = new PreparationState(); state.update(0, waiting);
  expect(state.update(1, { ...waiting, active: false })).toBe(0);
});
it('keeps a static waiting character with reduced motion and switches immediately on text', () => {
  const state = new PreparationState();
  expect(state.update(0, { ...waiting, reduced: true })).toBe(1);
  expect(state.update(1, { ...waiting, hasText: true, drawable: true, reduced: true })).toBe(0);
});
