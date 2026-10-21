import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Scene, PerspectiveCamera } from 'three';
const shared = vi.hoisted(() => ({ instances: [], pending: null }));
vi.mock('../../../experiments/subtitle-cyberspace/src/scene.js', () => ({
  SubtitleScene: class {
    constructor(_canvas, _select, _hover, options) {
      this.options = options;
      this.scene = new Scene(); this.camera = new PerspectiveCamera();
      this.present = new Set(); this.uniqueShown = 0;
      this.render = vi.fn(); this.dispose = vi.fn(); this.setMappings = vi.fn(); this.degrade = vi.fn();
      this.load = vi.fn(async (data) => {
        if (shared.pending) await shared.pending;
        this.present = new Set(data.subtitles.flatMap(s => Array.from(s.text).map((_, i) => `${s.id}:${i}`)));
        this.uniqueShown = this.present.size;
      });
      shared.instances.push(this);
    }
  },
}));
import { createTaskSpace } from './runtime.js';
let controller, canvas, options, sequence = 0;
const text = [{ id: 1, start_ms: 0, end_ms: 10000, text: '学习递归算法' }];
const markers = [{ id: 5, start_ms: 0, end_ms: 10000, title: '递归', source_quote: '递归', note: '' }];
beforeEach(() => {
  vi.useFakeTimers(); shared.instances = []; shared.pending = null;
  vi.stubGlobal('requestAnimationFrame', (fn) => setTimeout(() => fn(Date.now()), 16));
  vi.stubGlobal('cancelAnimationFrame', clearTimeout);
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  canvas = document.createElement('canvas');
  options = { key: `runtime-test-${sequence++}`, onSelect: vi.fn(), onError: vi.fn(), onQuality: vi.fn(), onReady: vi.fn() };
  controller = createTaskSpace(canvas, options);
});
afterEach(() => { controller.dispose(); vi.useRealTimers(); vi.unstubAllGlobals(); });
const advance = (ms) => vi.advanceTimersByTimeAsync(ms);
it('keeps the preparation scene until real text, freezes on pause and skips it after remount', async () => {
  controller.dispose();
  const high = { ...options, analysisQuality: 'high', analysisVariant: 'nailong' };
  controller = createTaskSpace(canvas, high);
  const snapshot = { subtitles: [], markers: [], phase: 'preparing', reduced: false };
  controller.update(snapshot); await advance(300);
  expect(canvas.dataset.preparation).toBe('waiting');
  controller.update({ ...snapshot, phase: 'paused' }); await advance(300);
  expect(canvas.dataset.preparation).toBe('waiting');
  controller.update({ ...snapshot, phase: 'transcribing' }); await advance(300);
  expect(canvas.dataset.preparationOpacity).toBe('1.000');
  controller.update({ ...snapshot, phase: 'transcribing', subtitles: text }); await advance(300);
  expect(canvas.dataset.preparation).toBe('leaving');
  expect(shared.instances.at(-1).present.size).toBeGreaterThan(0);
  controller.update({ ...snapshot, phase: 'transcribing', subtitles: text, paused: true }); await advance(100);
  const weight = canvas.dataset.preparationOpacity;
  await advance(1500); expect(canvas.dataset.preparationOpacity).toBe(weight);
  controller.update({ ...snapshot, phase: 'transcribing', subtitles: text, reduced: true }); await advance(100);
  expect(canvas.dataset.preparation).toBe('done');
  controller.dispose(); controller = createTaskSpace(canvas, high);
  controller.update(snapshot); await advance(100);
  expect(canvas.dataset.preparation).toBe('done');
  expect(options.onError).not.toHaveBeenCalled();
});
it('routes the high-quality option to gravity while keeping the previous particle renderer addressable', () => {
  controller.dispose();
  controller = createTaskSpace(canvas, { ...options, analysisQuality: 'high' });
  expect(shared.instances.at(-1).options.analysisVariant).toBe('gravity');
  controller.dispose();
  controller = createTaskSpace(canvas, { ...options, analysisQuality: 'high', analysisVariant: 'nailong' });
  expect(shared.instances.at(-1).options.analysisVariant).toBe('nailong');
  controller.dispose();
  controller = createTaskSpace(canvas, { ...options, analysisQuality: 'high', analysisVariant: 'particle' });
  expect(shared.instances.at(-1).options.analysisVariant).toBe('particle');
});

it('hands off once while markers update immediately, skips replay on remount and cancels on hiding', async () => {
  const host = document.createElement('div'); host.append(canvas); document.body.append(host);
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ fillRect() {}, drawImage() {} });
  vi.spyOn(HTMLCanvasElement.prototype, 'toDataURL').mockReturnValue('data:image/png;base64,fixture');
  const scene = shared.instances[0]; scene.captureFrame = vi.fn(() => true);
  const snapshot = { subtitles: text, markers: [], phase: 'transcribing', reduced: false };
  controller.update(snapshot); await advance(100);
  controller.update({ ...snapshot, phase: 'analyzing', markers }); await advance(50);
  expect(canvas.dataset.stageTransitionCount).toBe('1');
  expect(scene.setMappings.mock.lastCall[0][0].id).toBe(5);
  expect(canvas.dataset.stageTransition).toBe('running');
  Object.defineProperty(document, 'hidden', { value: true }); document.dispatchEvent(new Event('visibilitychange'));
  expect(host.querySelector('.subtitle-stage-handoff')).toBeNull();
  Object.defineProperty(document, 'hidden', { value: false }); document.dispatchEvent(new Event('visibilitychange'));
  controller.update({ ...snapshot, phase: 'analyzing', markers }); await advance(250);
  expect(scene.captureFrame).toHaveBeenCalledOnce();
  controller.dispose();
  controller = createTaskSpace(canvas, options);
  shared.instances.at(-1).captureFrame = vi.fn(() => true);
  controller.update({ ...snapshot, phase: 'analyzing', markers }); await advance(100);
  expect(shared.instances.at(-1).captureFrame).not.toHaveBeenCalled();
  host.remove(); vi.restoreAllMocks();
});

it('reduces gravity decoration on slow frames without affecting the processing snapshot', async () => {
  vi.stubGlobal('requestAnimationFrame', fn => setTimeout(() => fn(Date.now()), 80));
  const scene = shared.instances[0];
  const gravity = { simplified: false, atlas: { entries: [], revision: 1, hitCount: 0 },
    reduceQuality: vi.fn(() => { gravity.simplified = true; }) };
  scene.gravity = gravity; scene.gravityActive = true;
  controller.update({ subtitles: text, markers: [], phase: 'analyzing', reduced: false });
  await advance(3500);
  expect(gravity.reduceQuality).toHaveBeenCalledTimes(1);
  expect(canvas.dataset.phase).toBe('analyzing');
  expect(canvas.dataset.analysisStyle).toBe('gravity-subtitles');
  expect(options.onError).not.toHaveBeenCalled();
});

it('uses a 600ms layer entrance and scans only during active analysis', async () => {
  const snapshot = { subtitles: text, markers: [], phase: 'transcribing', reduced: false };
  controller.update(snapshot); await advance(100);
  const scene = shared.instances[0];
  expect(scene.analysisScanning).toBe(false);
  controller.update({ ...snapshot, phase: 'analyzing' }); await advance(300);
  expect(scene.render.mock.lastCall[0]).toBeGreaterThan(124);
  expect(scene.render.mock.lastCall[0]).toBeLessThan(126.4);
  expect(scene.analysisScanning).toBe(true);
  await advance(450); expect(scene.render.mock.lastCall[0]).toBeGreaterThanOrEqual(126.4);
  controller.update({ ...snapshot, phase: 'analyzing', reduced: true }); await advance(100);
  expect(scene.analysisScanning).toBe(false);
  controller.update({ ...snapshot, phase: 'analyzing', paused: true }); await advance(100);
  expect(scene.analysisScanning).toBe(false);
  controller.update({ ...snapshot, phase: 'completed' }); await advance(100);
  expect(scene.analysisScanning).toBe(false);
});
it('slows the isolated demo analysis clock and camera together while leaving ASR at normal speed', async () => {
  controller.dispose();
  controller = createTaskSpace(canvas, { ...options, analysisPlaybackRate: .4 });
  const scene = shared.instances.at(-1);
  const snapshot = { subtitles: text, markers: [], phase: 'transcribing', reduced: false };
  controller.update(snapshot); await advance(200);
  let before = scene.render.mock.lastCall[4];
  await advance(1000);
  const asrElapsed = scene.render.mock.lastCall[4] - before;
  controller.update({ ...snapshot, phase: 'analyzing' }); await advance(200);
  before = scene.render.mock.lastCall[4];
  await advance(1000);
  expect((scene.render.mock.lastCall[4] - before) / asrElapsed).toBeCloseTo(.4, 1);
  expect(scene.gravityMotionDelta).toBeLessThan(.02);
  expect(scene.analysisScanning).toBe(true);
});
it('pauses the demonstration without switching its ASR structure into a wall', async () => {
  const snapshot = { subtitles: text, markers: [], phase: 'transcribing', reduced: false };
  controller.update(snapshot); await advance(2000);
  controller.update({ ...snapshot, paused: true }); await advance(100);
  const scene = shared.instances[0], time = scene.render.mock.lastCall[0], frames = scene.render.mock.calls.length;
  await advance(5000);
  expect(scene.render).toHaveBeenCalledTimes(frames); expect(scene.render.mock.lastCall[0]).toBe(time);
  controller.update(snapshot); await advance(200);
  expect(scene.render.mock.calls.length).toBeGreaterThan(frames);
  expect(scene.render.mock.lastCall[0]).toBeLessThan(time + .3);
});
it('redraws idle scenes for view input without advancing processing, then parks again', async () => {
  controller.update({ subtitles: text, markers, phase: 'waiting', reduced: false });
  await advance(2000);
  const scene = shared.instances[0], time = scene.render.mock.lastCall[0];
  canvas.dispatchEvent(new WheelEvent('wheel', { deltaY: -120, cancelable: true }));
  await advance(2000);
  expect(scene.userView.zoom).toBeGreaterThan(1);
  expect(scene.render.mock.lastCall[0]).toBe(time);
  const frames = scene.render.mock.calls.length;
  await advance(1000); expect(scene.render).toHaveBeenCalledTimes(frames);
  controller.resetView(); await advance(2000); expect(scene.userView.zoom).toBe(1);
  controller.dispose();
  const event = new WheelEvent('wheel', { deltaY: -120, cancelable: true }); canvas.dispatchEvent(event);
  expect(event.defaultPrevented).toBe(false);
});
it('incremental chunks turn green during analysis, without replay on final IDs or polling', async () => {
  const snapshot = { subtitles: text, markers: [], phase: 'analyzing', reduced: false, resultScope: 'medium' };
  controller.update(snapshot); await advance(2000);
  const scene = shared.instances[0];
  expect(scene.render.mock.lastCall[1].size).toBe(0);
  controller.update({ ...snapshot, markers }); await advance(200);
  const first = scene.render.mock.lastCall[1];
  expect(first.get('1:2')).toBeGreaterThan(0);
  expect(first.get('1:3')).toBe(first.get('1:2'));
  await advance(1400);
  const second = { ...markers[0], id: 6, source_quote: '算法' };
  controller.update({ ...snapshot, markers: [...markers, second] }); await advance(200);
  expect(scene.render.mock.lastCall[1].get('1:2')).toBe(1);
  expect(scene.render.mock.lastCall[1].get('1:5')).toBeGreaterThan(0);
  await advance(1400);
  controller.update({ ...snapshot, markers: [...markers, second].map(m => ({ ...m, id: m.id + 100 })), phase: 'completed' });
  await advance(100);
  expect([...scene.render.mock.lastCall[1].values()]).toEqual([1, 1, 1, 1]);
  const count = scene.render.mock.calls.length;
  await advance(2000); expect(scene.render).toHaveBeenCalledTimes(count);
});
it('never advances ASR or invents results on a demo timer', async () => {
  controller.update({ subtitles: [], markers: [], phase: 'transcribing', reduced: false });
  await advance(25000);
  const scene = shared.instances[0];
  expect(scene.render.mock.lastCall[0]).toBe(4);
  expect(scene.render.mock.lastCall[1].size).toBe(0);
  expect(scene.uniqueShown).toBe(0);
  expect(scene.render.mock.calls.length).toBeLessThan(760);
});
it('completion bypasses the intro and stops rendering after highlights settle', async () => {
  controller.update({ subtitles: text, markers, phase: 'completed', reduced: false });
  await advance(2500);
  const scene = shared.instances[0];
  expect(scene.render.mock.lastCall[0]).toBeGreaterThanOrEqual(126.4);
  expect(scene.render.mock.lastCall[1].get('1:2')).toBe(1);
  const calls = scene.render.mock.calls.length;
  await advance(10000); expect(scene.render).toHaveBeenCalledTimes(calls);
});

it('finishes new confirmation trails at completion, then disables all reveal motion', async () => {
  controller.update({ subtitles: text, markers, phase: 'completed', reduced: false });
  await advance(150);
  const scene = shared.instances[0];
  expect(scene.analysisScanning).toBe(false); expect(scene.analysisRevealing).toBe(true);
  await advance(1800);
  expect(scene.analysisRevealing).toBe(false);
  controller.update({ subtitles: text, markers, phase: 'analyzing', paused: true, reduced: false });
  await advance(100); expect(scene.analysisRevealing).toBe(false);
});

it('drops particle accents before falling back from the readable face on sustained slow frames', async () => {
  vi.stubGlobal('requestAnimationFrame', (fn) => setTimeout(() => fn(Date.now()), 80));
  const scene = shared.instances[0];
  const particleText = { mesh: { visible: true }, accents: { trails: { visible: false } }, material: { uniforms: { uPointer: { value: { z: 0 } } } },
    reduceAccents: vi.fn(() => { particleText.accents = null; }) };
  scene.particleText = particleText;
  scene.degrade.mockImplementation(() => { scene.degraded = true; });
  controller.update({ subtitles: text, markers: [], phase: 'analyzing', reduced: false });
  await advance(3400);
  expect(options.onError).not.toHaveBeenCalled();
  expect(particleText.reduceAccents).toHaveBeenCalledTimes(1);
  expect(scene.degrade).not.toHaveBeenCalled();
  expect(options.onQuality).toHaveBeenCalledWith('已简化拖尾与立体细节');
  await advance(5500);
  expect(scene.degrade).toHaveBeenCalledTimes(1);
});
it('static mode renders once and still accepts real result selection', async () => {
  controller.update({ subtitles: text, markers, phase: 'completed', reduced: true });
  await advance(200);
  const scene = shared.instances[0], count = scene.render.mock.calls.length;
  await advance(10000); expect(scene.render).toHaveBeenCalledTimes(count);
  controller.select(5); await advance(100);
  expect(scene.focusId).toBe(5);
  expect(scene.render).toHaveBeenCalledTimes(count + 1);
});
it('hidden windows stop rendering and do not catch up on return', async () => {
  controller.update({ subtitles: text, markers: [], phase: 'analyzing', reduced: false });
  await advance(1000);
  const scene = shared.instances[0], time = scene.render.mock.lastCall[0], count = scene.render.mock.calls.length;
  Object.defineProperty(document, 'hidden', { value: true }); document.dispatchEvent(new Event('visibilitychange'));
  await advance(30000); expect(scene.render).toHaveBeenCalledTimes(count);
  Object.defineProperty(document, 'hidden', { value: false }); document.dispatchEvent(new Event('visibilitychange'));
  await advance(50); expect(scene.render.mock.lastCall[0] - time).toBeLessThan(.1);
});
it.each(['waiting', 'paused', 'failed', 'stopped'])('%s freezes without losing persisted hits', async (phase) => {
  controller.update({ subtitles: text, markers, phase, reduced: false });
  await advance(100);
  const scene = shared.instances[0], count = scene.render.mock.calls.length;
  expect(scene.render.mock.lastCall[1].size).toBe(2);
  await advance(1000); expect(scene.render).toHaveBeenCalledTimes(count);
});
it('late font initialization cannot render or retain a disposed scene', async () => {
  let resolve;
  shared.pending = new Promise(r => { resolve = r; });
  controller.update({ subtitles: text, markers: [], phase: 'analyzing', reduced: false });
  controller.dispose(); resolve(); await advance(100);
  expect(shared.instances[0].dispose).toHaveBeenCalledTimes(1);
  expect(shared.instances[0].render).not.toHaveBeenCalled();
});
it('WebGL loss releases resources and reports an independent display failure', async () => {
  controller.update({ subtitles: text, markers: [], phase: 'analyzing', reduced: false });
  await advance(100); canvas.dispatchEvent(new Event('webglcontextlost', { cancelable: true }));
  expect(options.onError).toHaveBeenCalledWith(expect.stringContaining('后台任务不受影响'));
  expect(shared.instances[0].dispose).toHaveBeenCalledTimes(1);
});
it('shares the background clock and removes its GPU resources on departure', async () => {
  const scene = shared.instances[0];
  const background = scene.scene.children[0];
  const geometryDisposed = vi.spyOn(background.geometry, 'dispose');
  const materialDisposed = vi.spyOn(background.material, 'dispose');
  controller.update({ subtitles: text, markers: [], phase: 'transcribing', reduced: false });
  await advance(1000);
  const uniforms = background.material.uniforms;
  expect(uniforms.uTime.value).toBeGreaterThan(0);
  const clock = uniforms.uTime.value;
  Object.defineProperty(document, 'hidden', { value: true }); document.dispatchEvent(new Event('visibilitychange'));
  await advance(30000);
  expect(uniforms.uTime.value).toBe(clock);
  Object.defineProperty(document, 'hidden', { value: false }); document.dispatchEvent(new Event('visibilitychange'));
  controller.update({ subtitles: text, markers: [], phase: 'transcribing', reduced: true });
  await advance(100);
  expect(uniforms.uTime.value).toBe(clock);
  controller.dispose();
  expect(scene.scene.children).toHaveLength(0);
  expect(geometryDisposed).toHaveBeenCalledTimes(1);
  expect(materialDisposed).toHaveBeenCalledTimes(1);
});
it('keeps the old scene drawing during an update and honors completion before it finishes', async () => {
  controller.update({ subtitles: text, markers: [], phase: 'transcribing', reduced: false });
  await advance(1000);
  const scene = shared.instances[0], before = scene.render.mock.calls.length;
  let resolve;
  shared.pending = new Promise(r => { resolve = r; });
  controller.update({ subtitles: [...text, { id: 2, text: '新增文字', start_ms: 10000, end_ms: 11000 }], markers: [], phase: 'analyzing', reduced: false });
  await advance(500);
  expect(scene.render.mock.calls.length).toBeGreaterThan(before + 5);
  expect(scene.render.mock.lastCall[0]).toBeGreaterThan(124);
  resolve(); await advance(300);
  expect(scene.render.mock.lastCall[0]).toBeGreaterThanOrEqual(126.4);
  expect(options.onError).not.toHaveBeenCalled();
});
it('grows the live ring once and reports readiness only after its first rendered frame', async () => {
  controller.update({ subtitles: [], markers: [], phase: 'preparing', reduced: false });
  expect(options.onReady).not.toHaveBeenCalled();
  await advance(50);
  const scene = shared.instances[0], first = scene.entranceScale;
  expect(first).toBeGreaterThanOrEqual(.48); expect(first).toBeLessThan(.6);
  expect(scene.render.mock.lastCall[0]).toBeGreaterThanOrEqual(4);
  expect(options.onReady).toHaveBeenCalledTimes(1); expect(options.onReady).toHaveBeenCalledWith(true);
  await advance(500); expect(scene.entranceScale).toBeGreaterThan(first);
  const middle = scene.entranceScale;
  controller.update({ subtitles: text, markers: [], phase: 'transcribing', reduced: false });
  await advance(50); expect(scene.entranceScale).toBeGreaterThanOrEqual(middle);
  await advance(1000); expect(scene.entranceScale).toBe(1);
  expect(options.onReady).toHaveBeenCalledTimes(1);
});
it('does not jump the entry scale when returning from a hidden window', async () => {
  controller.update({ subtitles: [], markers: [], phase: 'preparing', reduced: false });
  await advance(250); const scene = shared.instances[0], scale = scene.entranceScale;
  Object.defineProperty(document, 'hidden', { value: true }); document.dispatchEvent(new Event('visibilitychange'));
  await advance(30000); expect(scene.entranceScale).toBe(scale);
  Object.defineProperty(document, 'hidden', { value: false }); document.dispatchEvent(new Event('visibilitychange'));
  await advance(20); expect(scene.entranceScale).toBe(scale);
});
it.each(['analyzing', 'completed', 'stopped', 'failed'])('never delays %s behind the ring entrance', async phase => {
  controller.update({ subtitles: [], markers: [], phase: 'preparing', reduced: false }); await advance(100);
  controller.update({ subtitles: text, markers, phase, reduced: false }); await advance(60);
  expect(shared.instances[0].entranceScale).toBe(1);
});
it('skips the entrance on reopening a remembered task and in reduced motion', async () => {
  controller.update({ subtitles: [], markers: [], phase: 'preparing', reduced: false }); await advance(200);
  controller.dispose(); options.onReady.mockClear();
  controller = createTaskSpace(canvas, options);
  controller.update({ subtitles: text, markers: [], phase: 'transcribing', reduced: false }); await advance(50);
  expect(shared.instances[1].entranceScale).toBe(1); expect(options.onReady).toHaveBeenCalledTimes(1); expect(options.onReady).toHaveBeenCalledWith(false);
  controller.dispose(); options.onReady.mockClear();
  controller = createTaskSpace(canvas, { ...options, key: 'reduced-entry-only' });
  controller.update({ subtitles: [], markers: [], phase: 'preparing', reduced: true }); await advance(50);
  expect(shared.instances[2].entranceScale).toBe(1); expect(options.onReady).toHaveBeenCalledTimes(1); expect(options.onReady).toHaveBeenCalledWith(false);
});

it.each(['gravity', 'nailong', 'dafeiyu', 'dafeiyu31', 'dafeiyu-rice', 'gpt'])('%s sends one journey per result packet, ignores polling and handles repeated explicit clicks', async analysisVariant => {
  controller.dispose(); controller = createTaskSpace(canvas, { ...options, analysisQuality: 'high', analysisVariant });
  const scene = shared.instances.at(-1);
  const snapshot = { subtitles: text, markers: [], phase: 'analyzing', reduced: false, resultScope: 'medium' };
  controller.update(snapshot); await advance(1500);
  expect(scene.gravityJourneyRequest).toBeUndefined();
  const packet = [...markers, { ...markers[0], id: 6, source_quote: '算法' }];
  controller.update({ ...snapshot, markers: packet }); await advance(1400);
  expect(scene.gravityJourneyRequest).toMatchObject({ serial: 1, reason: 'automatic', markerId: 6 });
  controller.update({ ...snapshot, markers: packet.map(m => ({ ...m })) }); await advance(1200);
  expect(scene.gravityJourneyRequest.serial).toBe(1);
  controller.select(5, { travel: true }); await advance(100);
  expect(scene.gravityJourneyRequest).toMatchObject({ serial: 2, reason: 'manual', markerId: 5 });
  controller.select(5); await advance(100); expect(scene.gravityJourneyRequest.serial).toBe(2);
  controller.select(5, { travel: true }); await advance(100);
  expect(scene.gravityJourneyRequest.serial).toBe(3);
  controller.update({ ...snapshot, phase: 'completed', markers: packet.map(m => ({ ...m, id: m.id + 100 })) });
  await advance(1200); expect(scene.gravityJourneyRequest.serial).toBe(3);
});

it('renders a finite camera journey after completion without restarting the analysis clock', async () => {
  controller.dispose(); controller = createTaskSpace(canvas, { ...options, analysisQuality: 'high' });
  const scene = shared.instances.at(-1);
  controller.update({ subtitles: text, markers, phase: 'completed', reduced: false }); await advance(1600);
  const clock = scene.render.mock.lastCall[4];
  scene.gravity = { journeyPending: true, presentationPending: true, atlas: { entries: [], revision: 0, hitCount: 0 } };
  controller.select(5, { travel: true }); await advance(350);
  const frames = scene.render.mock.calls.length;
  expect(scene.render.mock.lastCall[4]).toBe(clock); expect(scene.analysisScanning).toBe(false);
  expect(scene.gravityMotionDelta).toBeGreaterThan(0);
  scene.gravity.journeyPending = false; await advance(100);
  expect(scene.render.mock.calls.length).toBeGreaterThan(frames);
  const arrival = scene.render.mock.calls.length;
  await advance(500); expect(scene.render.mock.calls.length).toBeGreaterThan(arrival);
  expect(scene.render.mock.lastCall[4]).toBe(clock);
  scene.gravity.presentationPending = false; await advance(100);
  const settled = scene.render.mock.calls.length; await advance(500);
  expect(settled).toBeGreaterThanOrEqual(frames); expect(scene.render.mock.calls.length).toBe(settled);
  expect(options.onError).not.toHaveBeenCalled();
});

it('synchronizes evidence feedback once per phase, clears it, and accepts repeated manual events', async () => {
  controller.dispose(); const onEvidence = vi.fn();
  controller = createTaskSpace(canvas, { ...options, onEvidence, analysisQuality: 'high' });
  const scene = shared.instances.at(-1);
  scene.gravityActive = true;
  scene.gravity = { atlas: { entries: [], revision: 0, hitCount: 0 }, evidenceCue: { id: 5, serial: 1, phase: 'travel' } };
  controller.update({ subtitles: text, markers, phase: 'analyzing', reduced: false });
  await advance(200); expect(onEvidence).toHaveBeenCalledTimes(1);
  scene.gravity.evidenceCue = { id: 5, serial: 1, phase: 'arrival' };
  await advance(500); expect(onEvidence).toHaveBeenCalledTimes(2);
  scene.gravity.evidenceCue = { id: 5, serial: 2, phase: 'travel' };
  await advance(100); expect(onEvidence).toHaveBeenCalledTimes(3);
  scene.gravity.evidenceCue = null;
  await advance(200); expect(onEvidence).toHaveBeenLastCalledWith(null);
  const calls = onEvidence.mock.calls.length;
  controller.dispose(); await advance(500);
  expect(onEvidence).toHaveBeenCalledTimes(calls); expect(options.onError).not.toHaveBeenCalled();
});

it('renders high-quality analysis at display cadence and keeps its clock moving across polling', async () => {
  controller.dispose(); controller = createTaskSpace(canvas, { ...options, analysisQuality: 'high' });
  const snapshot = { subtitles: text, markers: [], phase: 'analyzing', reduced: false };
  controller.update(snapshot); await advance(1000);
  const scene = shared.instances.at(-1), start = scene.render.mock.calls.length;
  await advance(1000);
  expect(scene.render.mock.calls.length - start).toBeGreaterThanOrEqual(58);
  expect(scene.render.mock.calls.length - start).toBeLessThanOrEqual(64);
  const previousClock = scene.render.mock.lastCall[4];
  controller.update({ ...snapshot, markers: [] }); await advance(16);
  expect(scene.render.mock.lastCall[4]).toBeGreaterThan(previousClock);
  expect(scene.render.mock.lastCall[4] - previousClock).toBeLessThan(.025);
  expect(options.onError).not.toHaveBeenCalled();
});
