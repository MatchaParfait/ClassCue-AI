import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { StageHandoff } from './stage-handoff.js';

let canvas, host, handoff, capture;
const snapshot = { phase: 'analyzing', reduced: false };
beforeEach(() => {
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ fillRect() {}, drawImage() {} });
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/png;base64,fixture');
  host = document.createElement('div'); canvas = document.createElement('canvas');
  host.append(canvas); document.body.append(host);
  capture = vi.fn(() => true); handoff = new StageHandoff(canvas, { high: true, variant: 'gpt' });
});
afterEach(() => { handoff.dispose(); host.remove(); vi.restoreAllMocks(); });
it('waits for the actual LLM frame, then finishes once without polling replay', () => {
  handoff.observe(snapshot, 'transcribing', capture);
  handoff.advance(10, { frameReady: false });
  expect(canvas.dataset.stageTransition).toBe('waiting');
  expect(handoff.elapsed).toBe(0);
  handoff.advance(.3); expect(canvas.dataset.stageTransition).toBe('running');
  handoff.observe(snapshot, 'analyzing', capture);
  handoff.advance(.61);
  expect(host.querySelector('.subtitle-stage-handoff')).toBeNull();
  handoff.observe(snapshot, 'transcribing', capture);
  expect(capture).toHaveBeenCalledOnce();
  expect(handoff.count).toBe(1);
});
it('pauses without advancing and can immediately reveal the scene for an interaction', () => {
  handoff.observe(snapshot, 'transcribing', capture); handoff.advance(.2);
  handoff.advance(5, { paused: true }); expect(handoff.elapsed).toBe(.2);
  handoff.advance(.1); expect(handoff.elapsed).toBeCloseTo(.3);
  handoff.finish(); expect(host.children).toHaveLength(1);
});
it('does not replay on direct entry, restored sessions, reduced motion or background stage changes', () => {
  handoff.observe(snapshot, null, capture); expect(capture).not.toHaveBeenCalled();
  handoff = new StageHandoff(canvas, { high: true, seen: true });
  handoff.observe(snapshot, 'transcribing', capture); expect(capture).not.toHaveBeenCalled();
  handoff = new StageHandoff(canvas, { high: true });
  handoff.observe({ ...snapshot, reduced: true }, 'transcribing', capture);
  expect(capture).not.toHaveBeenCalled();
  handoff = new StageHandoff(canvas, { high: true });
  Object.defineProperty(document, 'hidden', { value: true });
  handoff.observe(snapshot, 'transcribing', capture); expect(capture).not.toHaveBeenCalled();
});
it('uses a short fade for light rendering and removes overlays on reduced motion, failure and disposal', () => {
  handoff = new StageHandoff(canvas);
  handoff.observe(snapshot, 'transcribing', capture);
  expect(host.querySelector('svg')).toBeNull();
  handoff.advance(.19); expect(handoff.active).toBe(false);
  for (const action of ['reduce', 'fail', 'dispose']) {
    handoff = new StageHandoff(canvas, { high: true });
    handoff.observe(snapshot, 'transcribing', capture);
    if (action === 'reduce') handoff.advance(.1, { reduced: true });
    if (action === 'fail') handoff.observe({ phase: 'failed' }, 'analyzing', capture);
    if (action === 'dispose') handoff.dispose();
    expect(host.children).toHaveLength(1);
  }
});
it('does not conceal a valid fallback forever when a character asset cannot load', () => {
  handoff.observe(snapshot, 'transcribing', capture);
  handoff.advance(1, { ready: false }); expect(handoff.elapsed).toBe(0);
  handoff.advance(.5, { ready: false }); handoff.advance(.5, { ready: false });
  expect(handoff.active).toBe(false);
});
