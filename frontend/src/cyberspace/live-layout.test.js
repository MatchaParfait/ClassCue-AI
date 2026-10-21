import { describe, expect, it, vi } from 'vitest';
import { LIVE, ROW_HANDOFF, LiveLayout, liveCharacters, liveOpacity, liveSlots } from '../../../experiments/subtitle-cyberspace/src/live-layout.js';
import { asrPosition } from '../../../experiments/subtitle-cyberspace/src/asr-layout.js';
import { mapMarkers } from '../../../experiments/subtitle-cyberspace/src/core.js';

const data = (texts, offset = 0) => ({ subtitles: texts.map((text, i) => ({ id: i + offset, text, start_ms: (i + offset) * 1000, end_ms: (i + offset + 1) * 1000 })), markers: [] });
const ops = () => ({ add: vi.fn(), remove: vi.fn(), budget: Infinity, maxAdds: Infinity, now: () => 0 });
const update = (layout, input, clock = 0, mappings = [], focus = null) => layout.update(input, mappings, focus, clock, () => {});
const settle = (layout, clock = 3, callbacks = ops()) => {
  if (layout.rowTransitions) for (let i = 0; i <= 60; i++) layout.advance(clock + i / 30, false, callbacks);
  else { layout.advance(clock, false, callbacks); layout.advance(clock + 2, false, callbacks); }
  return callbacks;
};

describe('live subtitle layout', () => {
  it('has a fixed full-size ring and eight separated reveal regions', () => {
    const { cells } = liveSlots(4000);
    expect(cells.length).toBeLessThanOrEqual(4000);
    const phases = Array.from({ length: 8 }, (_, i) => cells[i * LIVE.columns].phase);
    expect(new Set(phases.map(p => Math.floor(p * 8))).size).toBe(8);
    expect(cells.every(c => c.scale === 1 && c.length === LIVE.columns)).toBe(true);
  });
  it('keeps every surviving character position and arrival time when text grows', () => {
    const layout = new LiveLayout();
    update(layout, data(['第一批字幕：递归算法。'])); settle(layout);
    const before = [...layout.records.values()].map(r => ({ key: r.key, record: r, arrival: r.arrival, position: asrPosition(r, r.asr) }));
    update(layout, data(['第一批字幕：递归算法。', '第二批字幕：先分解，再合并。']), 6); settle(layout, 7);
    for (const old of before) {
      const next = layout.records.get(old.key);
      expect(next).toBe(old.record); expect(next.arrival).toBe(old.arrival);
      expect(asrPosition(next, next.asr)).toEqual(old.position);
    }
  });
  it('gathers by character rather than making a batch instantly opaque', () => {
    const layout = new LiveLayout();
    update(layout, data(['甲'.repeat(1800)])); layout.advance(0, false, ops());
    const records = [...layout.records.values()];
    expect(new Set(records.map(r => r.arrival)).size).toBeGreaterThan(20);
    expect(records.every(r => liveOpacity(r, 0) === 0)).toBe(true);
    expect(records.some(r => liveOpacity(r, .4) > 0 && liveOpacity(r, .4) < 1)).toBe(true);
    expect(records.every(r => liveOpacity(r, 1.5) === 1)).toBe(true);
  });
  it('fades old characters before recycling their cells and handles same-ID corrections', () => {
    const layout = new LiveLayout(24), callbacks = ops();
    update(layout, data(['甲乙丙'])); settle(layout, 0, callbacks);
    const survivor = layout.records.get('0:0'), old = layout.records.get('0:1');
    update(layout, data(['甲改丙']), 3);
    expect(layout.records.get('0:1')).toBe(old);
    expect(liveOpacity(old, 3.3)).toBeGreaterThan(0); expect(liveOpacity(old, 3.3)).toBeLessThan(1);
    layout.advance(3.3, false, callbacks); expect(layout.records.get('0:1').char).toBe('乙');
    settle(layout, 4, callbacks);
    expect(layout.records.get('0:1').char).toBe('改'); expect(layout.records.get('0:0')).toBe(survivor);
    expect(callbacks.remove).toHaveBeenCalledTimes(1);
  });
  it('coalesces rapid snapshots and does not animate obsolete queued subtitles', () => {
    const layout = new LiveLayout(120), callbacks = ops();
    update(layout, data(['甲'.repeat(100)], 1));
    layout.advance(0, false, { ...callbacks, maxAdds: 8 });
    update(layout, data(['乙'.repeat(100)], 2), .05);
    update(layout, data(['丙'.repeat(100)], 3), .1);
    expect(layout.pending.size).toBe(100);
    settle(layout, 2, callbacks);
    expect([...layout.records.values()].every(r => r.subtitleId === 3)).toBe(true);
    expect(layout.records.size).toBeLessThanOrEqual(120);
    expect(layout.pending.size).toBe(0);
  });
  it('handles a rolling preview without duplicate cells or clearing the ring', () => {
    const layout = new LiveLayout(96), callbacks = ops();
    const texts = Array.from({ length: 30 }, (_, i) => `${i}段文字`);
    update(layout, data(texts.slice(0, 20))); settle(layout, 0, callbacks);
    update(layout, data(texts.slice(10), 10), 4);
    expect(layout.records.size).toBeGreaterThan(0);
    settle(layout, 5, callbacks);
    expect([...layout.records.values()].every(r => r.subtitleId >= 10)).toBe(true);
    expect(new Set([...layout.records.values()].map(r => r.cell)).size).toBe(layout.records.size);
  });
  it('prioritizes actual hits and uses code-point keys for Chinese, emoji and repeated quotes', () => {
    const input = data(['甲😀递归', '递归乙', '丙'.repeat(300)]);
    input.markers = [{ id: 7, start_ms: 0, end_ms: 2000, source_quote: '😀递归递归', title: '递归', note: '' }];
    const mappings = mapMarkers(input), selected = liveCharacters(input, mappings, 7, 12);
    expect(mappings[0].keys).toEqual(['0:1', '0:2', '0:3', '1:0', '1:1']);
    expect(mappings[0].keys.every(key => selected.some(c => c.key === key))).toBe(true);
    expect(selected.length).toBe(12);
  });
  it('caps work per frame, even for a complete replacement', () => {
    const layout = new LiveLayout(120), callbacks = ops();
    update(layout, data(['甲'.repeat(120)])); settle(layout, 0, callbacks);
    callbacks.remove.mockClear(); callbacks.add.mockClear();
    update(layout, data(['乙'.repeat(120)], 1), 3);
    layout.advance(4, true, { ...callbacks, maxAdds: 16 });
    expect(callbacks.remove).toHaveBeenCalledTimes(16);
    expect(callbacks.add.mock.calls.length).toBeLessThanOrEqual(16);
    expect(layout.hasPending).toBe(true);
    settle(layout, 5, callbacks); expect(layout.hasPending).toBe(false);
  });
  it('does nothing for repeated identical data and never fabricates empty input', () => {
    const layout = new LiveLayout(), callbacks = ops();
    update(layout, data([])); settle(layout, 0, callbacks);
    expect(callbacks.add).not.toHaveBeenCalled();
    const input = data(['真实文字']); update(layout, input, 3); settle(layout, 3, callbacks);
    const revision = layout.revision;
    update(layout, structuredClone(input), 6); settle(layout, 6, callbacks);
    expect(layout.revision).toBe(revision); expect(callbacks.add).toHaveBeenCalledTimes(4);
  });
});


it('bounds the ribbon display while preserving source data and surviving placements', () => {
  const layout = new LiveLayout(4000, true), input = data(['甲'.repeat(3000)]);
  update(layout, input); settle(layout);
  expect(layout.entries.length).toBe(81);
  expect(layout.limit).toBe(810);
  expect(layout.records.size).toBe(810);
  expect(input.subtitles[0].text.length).toBe(3000);
  const survivor = layout.records.get('0:2999'), position = asrPosition(survivor, survivor.asr);
  update(layout, data(['甲'.repeat(3010)]), 6); settle(layout, 7);
  expect(layout.records.get('0:2999')).toBe(survivor);
  expect(asrPosition(survivor, survivor.asr)).toEqual(position);
  expect(layout.records.size).toBe(810);
  expect(layout.pending.size).toBe(0);
});

it('replaces a full ASR snapshot in staggered rows while keeping the silhouette occupied', () => {
  const layout = new LiveLayout(4000, true), callbacks = ops();
  update(layout, data(['甲'.repeat(810)])); settle(layout, 0, callbacks);
  update(layout, data(['乙'.repeat(810)], 1), 3);
  let minOpacity = 810, maxRows = 0;
  const departures = new Set();
  for (let i = 0; i < 480; i++) {
    const clock = 3 + i / 30;
    layout.advance(clock, false, callbacks);
    const rows = [...layout.records.values()];
    const active = new Set(rows.filter(r => r.departure < 1e6 || clock < r.arrival + r.appear).map(r => r.asr.slot));
    maxRows = Math.max(maxRows, active.size);
    minOpacity = Math.min(minOpacity, rows.reduce((sum, r) => sum + liveOpacity(r, clock), 0));
    rows.filter(r => r.departure < 1e6).forEach(r => departures.add(r.departure));
    expect(new Set(rows.map(r => r.cell)).size).toBe(rows.length);
  }
  expect(maxRows).toBeLessThanOrEqual(ROW_HANDOFF.concurrent);
  expect(maxRows).toBeGreaterThan(1);
  expect(minOpacity).toBeGreaterThan(810 * .9);
  expect(departures.size).toBe(81);
  expect([...layout.records.values()].every(r => r.subtitleId === 1)).toBe(true);
  expect(layout.hasPending).toBe(false);
});

it('keeps corrections in their own cells and never reschedules the same pending snapshot', () => {
  const layout = new LiveLayout(64, true), callbacks = ops();
  update(layout, data(['甲'.repeat(64)])); settle(layout, 0, callbacks);
  const before = new Map([...layout.records].map(([key, r]) => [key, r.cell]));
  const next = data(['乙'.repeat(64)]);
  update(layout, next, 3); layout.advance(3, false, callbacks);
  const revision = layout.revision, first = layout.records.get('0:0'), departure = first.departure;
  update(layout, structuredClone(next), 3.03);
  expect(layout.revision).toBe(revision); expect(first.departure).toBe(departure);
  for (let i = 1; i < 120; i++) layout.advance(3 + i / 30, false, callbacks);
  expect(layout.hasPending).toBe(false);
  for (const [key, record] of layout.records) {
    expect(record.cell).toBe(before.get(key)); expect(record.char).toBe('乙');
  }
});

it('coalesces rapid ASR updates and cancels untouched departures when text returns', () => {
  const layout = new LiveLayout(64, true), callbacks = ops(), original = data(['甲'.repeat(64)]);
  update(layout, original); settle(layout, 0, callbacks);
  update(layout, data(['乙'.repeat(64)], 1), 3); layout.advance(3, false, callbacks);
  const untouched = [...layout.records.values()].filter(r => r.departure === 1e6);
  update(layout, original, 3.02);
  expect(untouched.every(r => !r.queued && r.departure === 1e6)).toBe(true);
  for (let i = 1; i < 90; i++) layout.advance(3 + i / 30, false, callbacks);
  expect([...layout.records.values()].every(r => r.char === '甲')).toBe(true);
  expect(layout.hasPending).toBe(false);
  expect(callbacks.add.mock.calls.every(([r]) => r.char !== '乙')).toBe(true);
});

it('freezes row handoffs with the clock and bypasses the queue for reduced motion', () => {
  const layout = new LiveLayout(64, true), callbacks = ops();
  update(layout, data(['甲'.repeat(64)])); settle(layout, 0, callbacks);
  update(layout, data(['乙'.repeat(64)], 1), 3); layout.advance(3, false, callbacks);
  const before = [...layout.records.values()].map(r => [r.key, r.arrival, r.departure, liveOpacity(r, 3)]);
  for (let i = 0; i < 20; i++) layout.advance(3, false, callbacks);
  expect([...layout.records.values()].map(r => [r.key, r.arrival, r.departure, liveOpacity(r, 3)])).toEqual(before);
  layout.advance(3, true, callbacks);
  expect(layout.hasPending).toBe(false);
  expect([...layout.records.values()].every(r => r.char === '乙' && liveOpacity(r, 3) === 1)).toBe(true);
});
