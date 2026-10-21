import { expect, it } from 'vitest';
import { CharacterHighlights } from './highlights.js';

const mapping = (id, keys) => ({ id, keys });
it('reveals only returned characters and retains old hits across chunks and final IDs', () => {
  const hits = new CharacterHighlights();
  hits.sync([mapping(-1, ['1:0', '1:1'])], 10, 'medium');
  expect(hits.values(10).size).toBe(0);
  expect(hits.values(10.6).get('1:0')).toBeCloseTo(.5);
  expect(hits.values(10.2).get('1:1')).toBe(hits.values(10.2).get('1:0'));
  expect(hits.values(11.3).get('1:1')).toBe(1);
  hits.sync([mapping(-1, ['1:0', '1:1']), mapping(-2, ['2:0'])], 20, 'medium');
  expect(hits.values(20).get('1:1')).toBe(1);
  expect(hits.values(20).has('2:0')).toBe(false);
  hits.sync([mapping(101, ['1:0', '1:1']), mapping(102, ['2:0'])], 22, 'medium');
  expect(hits.values(22).size).toBe(3);
  expect(hits.pending(22)).toBe(false);
});
it('deduplicates overlap, only animates new characters of an expanded match', () => {
  const hits = new CharacterHighlights();
  hits.sync([mapping(-1, ['1:0', '1:1'])], 0, 'medium');
  hits.sync([mapping(-1, ['1:0', '1:1', '1:2']), mapping(-2, ['1:1'])], 3, 'medium');
  expect(hits.values(3).size).toBe(2);
  expect(hits.values(3.5).size).toBe(3);
  hits.sync([mapping(4, ['1:0'])], 5, 'medium');
  expect([...hits.values(5).keys()]).toEqual(['1:0']);
});
it('bounds burst duration and resets for a different recall mode', () => {
  const hits = new CharacterHighlights();
  hits.sync(Array.from({ length: 1000 }, (_, i) => mapping(i, [`${i}:0`])), 10, 'medium');
  expect(hits.endAt).toBeLessThanOrEqual(11.45);
  expect(hits.values(10, true).size).toBe(1000);
  hits.sync([mapping(1, ['1:0'])], 30, 'high');
  expect(hits.values(30).size).toBe(0);
  expect([...hits.values(31).keys()]).toEqual(['1:0']);
});
it('keeps the color transition visible for 1.2 seconds but applies reduced-motion results immediately', () => {
  const hits = new CharacterHighlights();
  hits.sync([mapping(1, ['1:0'])], 0, 'medium');
  expect(hits.values(.35).get('1:0')).toBeLessThan(.3);
  expect(hits.pending(1)).toBe(true);
  expect(hits.values(1.2).get('1:0')).toBe(1);
  expect(hits.pending(1.2)).toBe(false);
  expect(hits.values(0, true).get('1:0')).toBe(1);
});
it('high quality propagates green along the sentence without restarting on polling or final IDs', () => {
  const hits = new CharacterHighlights({ wave: true });
  hits.sync([mapping(-1, ['1:0', '1:1', '1:2'])], 10, 'medium');
  expect(hits.values(10.2).get('1:0')).toBeGreaterThan(0);
  expect(hits.values(10.2).has('1:2')).toBe(false);
  hits.sync([mapping(42, ['1:0', '1:1', '1:2'])], 10.3, 'medium');
  expect(hits.starts.get('1:0')).toBe(10);
  expect(hits.starts.get('1:2')).toBe(10.3);
  expect([...hits.values(10.81).values()]).toEqual([1, 1, 1]);
  expect([...hits.values(10, true).values()]).toEqual([1, 1, 1]);
  expect([...hits.values(10.1).values()]).toEqual([1, 1, 1]);
});

it('bounds high-quality confirmation to 800ms plus a capped burst stagger', () => {
  const hits = new CharacterHighlights({ wave: true });
  hits.sync(Array.from({ length: 100 }, (_, i) => mapping(i, [`${i}:0`, `${i}:1`, `${i}:2`])), 10, 'medium');
  expect(hits.endAt).toBeLessThanOrEqual(11.05);
  expect(hits.values(11.05).size).toBe(300);
  expect([...hits.values(11.05).values()].every(v => v === 1)).toBe(true);
  hits.sync([mapping(500, ['1:0', '1:1'])], 12, 'high');
  expect(hits.values(12).size).toBe(0);
});
