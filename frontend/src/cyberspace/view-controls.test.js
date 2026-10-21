import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { PerspectiveCamera } from 'three';
import { TaskViewControls, VIEW_LIMITS } from './view-controls.js';
import { SubtitleScene } from '../../../experiments/subtitle-cyberspace/src/scene.js';
import { TIMES, WALL_TIME } from '../../../experiments/subtitle-cyberspace/src/core.js';

let canvas, controls, invalidate, enabled;
beforeEach(() => {
  canvas = document.createElement('canvas'); enabled = true; invalidate = vi.fn();
  canvas.getBoundingClientRect = () => ({ left: 100, top: 50, width: 1000, height: 600 });
  controls = new TaskViewControls(canvas, invalidate, () => enabled);
});
afterEach(() => controls.dispose());
function wheel(options) {
  const event = new WheelEvent('wheel', { cancelable: true, deltaY: -120, ...options });
  canvas.dispatchEvent(event); return event;
}
const move = (clientX = 1100, clientY = 50) => canvas.dispatchEvent(new MouseEvent('pointermove', { clientX, clientY }));
const settle = () => { for (let i = 0; i < 90; i++) controls.update(1 / 30); };

it('eases wheel zoom and clamps repeated input without moving the page', () => {
  expect(wheel().defaultPrevented).toBe(true);
  const target = controls.target.zoom;
  controls.update(1 / 30);
  expect(controls.current.zoom).toBeGreaterThan(1); expect(controls.current.zoom).toBeLessThan(target);
  for (let i = 0; i < 30; i++) wheel(); settle();
  expect(controls.current.zoom).toBe(VIEW_LIMITS.max); expect(controls.pending).toBe(false);
  for (let i = 0; i < 30; i++) wheel({ deltaY: 120 }); settle();
  expect(controls.current.zoom).toBe(VIEW_LIMITS.min);
});
it('normalizes line and page scrolling but leaves browser zoom and horizontal scrolling alone', () => {
  wheel({ deltaY: -3, deltaMode: 1 }); const line = controls.target.zoom;
  controls.reset(); wheel({ deltaY: -48 }); expect(controls.target.zoom).toBe(line);
  for (const options of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { deltaY: 0 }, { deltaX: 400 }]) {
    expect(wheel(options).defaultPrevented).toBe(false);
    expect(controls.target.zoom).toBe(line);
  }
  controls.reset(); wheel({ deltaY: -1, deltaMode: 2 });
  expect(controls.target.zoom).toBeCloseTo(Math.exp(.18));
});
it('tilts within small limits and eases back on leave without losing zoom', () => {
  move(3000, -1000); wheel(); settle();
  expect(controls.current.yaw).toBe(VIEW_LIMITS.yaw); expect(controls.current.pitch).toBe(VIEW_LIMITS.pitch);
  const zoom = controls.current.zoom;
  canvas.dispatchEvent(new Event('pointerleave')); controls.update(1 / 30);
  expect(controls.current.yaw).toBeGreaterThan(0); expect(controls.current.yaw).toBeLessThan(VIEW_LIMITS.yaw);
  settle(); expect(controls.current).toEqual({ zoom, yaw: 0, pitch: 0 });
  controls.reset(); settle(); expect(controls.current.zoom).toBe(1);
});
it('respects reduced motion, entry gating and listener disposal', () => {
  enabled = false; move(); expect(wheel().defaultPrevented).toBe(false); expect(invalidate).not.toHaveBeenCalled();
  enabled = true; move(); settle(); controls.setReduced(true);
  move(); wheel(); controls.update(0);
  expect(controls.current).toEqual({ zoom: controls.target.zoom, yaw: 0, pitch: 0 });
  expect(controls.pending).toBe(false);
  controls.dispose(); const target = { ...controls.target };
  move(); expect(wheel().defaultPrevented).toBe(false); expect(controls.target).toEqual(target);
});
it('composes user zoom with entrance and wall framing and keeps the real camera on its orbit', () => {
  const scene = { options: { wallScale: .74 }, baseZoom: 1.5, entranceScale: .48, cameraDistance: 27,
    camera: new PerspectiveCamera(45, 1.5, .1, 130), userView: { zoom: 1.2, yaw: .04, pitch: .02 } };
  SubtitleScene.prototype.fitComposition.call(scene, TIMES.space, false);
  expect(scene.camera.zoom).toBeCloseTo(1.5 * .48 * 1.2);
  expect(scene.camera.position.x).toBeGreaterThan(0); expect(scene.camera.position.y).toBeGreaterThan(2);
  expect(Math.hypot(scene.camera.position.x, scene.camera.position.y - 2, scene.camera.position.z + 15)).toBeCloseTo(42);
  scene.entranceScale = 1;
  SubtitleScene.prototype.fitComposition.call(scene, WALL_TIME, false);
  expect(scene.camera.zoom).toBeCloseTo(1.5 * .74 * 1.2);
  scene.userView = { zoom: 1, yaw: 0, pitch: 0 };
  SubtitleScene.prototype.fitComposition.call(scene, WALL_TIME, false);
  expect(scene.camera.position.toArray()).toEqual([0, 2, 27]);
});
