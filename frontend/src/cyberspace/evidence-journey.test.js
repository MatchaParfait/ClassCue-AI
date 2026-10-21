import { expect, it } from 'vitest';
import { EvidenceJourney } from './evidence-journey.js';
import { GravityJourney } from '../../../experiments/subtitle-cyberspace/src/gravity-journey.js';

const a = { id: -1, keys: ['1:0', '1:1'] }, b = { id: -2, keys: ['1:2', '1:3'] };
const green = new Map([...a.keys, ...b.keys].map(key => [key, 1]));

it('coalesces a whole result packet and waits for real green ink', () => {
  const events = new EvidenceJourney();
  events.sync([a, b], 'medium', true);
  expect(events.take(new Map(), [a, b])).toBeNull();
  expect(events.take(green, [a, b])).toMatchObject({ serial: 1, reason: 'automatic', markerId: -2 });
  expect(events.take(green, [a, b])).toBeNull();
  events.sync([{ ...a }, { ...b }], 'medium', true);
  expect(events.take(green, [a, b])).toBeNull();
});

it('does not replay overlapping quotes, saved IDs, temporary removal or scope revisits', () => {
  const events = new EvidenceJourney();
  events.sync([a], 'medium', true); events.take(green, [a]);
  for (const mappings of [[{ ...a, id: 7 }], [], [{ ...a, id: 9 }, { id: 10, keys: ['1:1'] }]]) {
    events.sync(mappings, 'medium', true); expect(events.take(green, mappings)).toBeNull();
  }
  events.sync([a, b], 'high', true); expect(events.take(green, [a, b])).toBeNull();
  events.sync([a], 'medium', true); expect(events.take(green, [a])).toBeNull();
  events.sync([a, b], 'medium', true);
  expect(events.take(green, [a, b])).toMatchObject({ serial: 2, markerId: -2 });
});

it('explicit same-item clicks each travel once and override pending automatic motion', () => {
  const events = new EvidenceJourney(); events.sync([a, b], 'medium', true);
  events.select(a);
  expect(events.take(green, [a, b], true)).toMatchObject({ serial: 1, reason: 'manual', markerId: -1 });
  events.select(a);
  expect(events.take(green, [a, b], true)).toMatchObject({ serial: 2, reason: 'manual', markerId: -1 });
  expect(events.take(green, [a, b])).toBeNull();
});

it('keeps the clicked overlapping marker and preserves identity when preview IDs change', () => {
  const events = new EvidenceJourney(), overlap = { id: 22, keys: a.keys };
  events.select(overlap);
  expect(events.take(green, [a, overlap]).markerId).toBe(22);
  events.select(b);
  expect(events.take(green, [a, { ...b, id: 80 }]).markerId).toBe(80);
});

it('defers automatic motion while a move is in flight and clears pending requests on reset', () => {
  const events = new EvidenceJourney(); events.sync([a], 'medium', true);
  expect(events.take(green, [a], true)).toBeNull();
  events.reset(); expect(events.take(green, [a])).toBeNull();
  events.sync([a, b], 'medium', true);
  expect(events.take(green, [a, b])).toMatchObject({ markerId: -2 });
});

it('retargets from the current pose, freezes on pause and settles immediately for reduced motion', () => {
  const path = new GravityJourney();
  path.start({ serial: 1, keys: a.keys }); path.advance(.6);
  const midway = path.progress;
  expect(midway).toBeGreaterThan(0); expect(path.pending).toBe(true);
  path.advance(0); expect(path.progress).toBe(midway);
  path.start({ serial: 2, keys: b.keys }); path.advance(0);
  expect(path.progress).toBe(midway);
  path.advance(0, true); expect(path.pending).toBe(false); expect(path.progress).toBe(2);
  expect(path.start({ serial: 2, keys: b.keys })).toBe(false); expect(path.count).toBe(2);
  path.start({ serial: 3, reset: true }); path.advance(0, true);
  expect(path.progress).toBe(0); expect(path.keys).toEqual([]);
});
