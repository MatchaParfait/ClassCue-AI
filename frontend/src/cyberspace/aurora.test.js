import { expect, it, vi } from 'vitest';
import { Scene } from 'three';
import { TaskAurora } from './aurora.js';

it('keeps the analysis background visible and freezes its clock for reduced motion or degraded rendering', () => {
  const aurora = new TaskAurora(new Scene()), u = aurora.material.uniforms;
  aurora.update(2, 1.5, false, 0, false);
  expect(u.uAnalysis.value).toBe(0);
  aurora.update(4, 1.5, false, 1, false);
  expect(u.uAnalysis.value).toBe(1);
  expect(u.uStrength.value).toBe(1);
  expect(u.uTime.value).toBe(4);
  aurora.update(8, .5, true, 1, false);
  expect(u.uTime.value).toBe(4);
  expect(u.uAspect.value).toBe(.5);
  aurora.update(9, .5, false, 1, true);
  expect(u.uTime.value).toBe(4);
  aurora.update(10, 1.5, false, 1, false);
  expect(u.uTime.value).toBe(10);
  aurora.dispose();
});

it('removes the background mesh and frees its GPU resources on departure', () => {
  const scene = new Scene(), aurora = new TaskAurora(scene);
  const geometry = vi.spyOn(aurora.mesh.geometry, 'dispose'), material = vi.spyOn(aurora.material, 'dispose');
  aurora.dispose();
  expect(scene.children).toHaveLength(0);
  expect(geometry).toHaveBeenCalledTimes(1);
  expect(material).toHaveBeenCalledTimes(1);
});

it('opts into warm particle atmosphere without changing the lightweight default or fallback', () => {
  const aurora = new TaskAurora(new Scene()), u = aurora.material.uniforms;
  aurora.update(3, 1, false, 1, false);
  expect(u.uParticleStyle.value).toBe(0);
  aurora.update(4, 1, false, 1, false, true);
  expect(u.uParticleStyle.value).toBe(1);
  aurora.update(8, 1, true, 1, false, true);
  expect(u.uTime.value).toBe(4);
  aurora.update(8, 1, false, 1, true, true);
  expect(u.uParticleStyle.value).toBe(0);
  aurora.dispose();
});


it('selects matching ASR palettes without changing the animation lifecycle', () => {
  const scenes = ['nailong', 'gravity', 'dafeiyu', 'dafeiyu31', 'dafeiyu-rice', 'gpt'].map(id => new TaskAurora(new Scene(), id));
  const colors = scenes.map(a => a.material.uniforms.uGlow.value.toArray());
  expect(colors[0]).toEqual(colors[1]);
  expect(colors[2]).toEqual(colors[3]);
  expect(colors[3]).toEqual(colors[4]);
  expect(colors[0]).not.toEqual(colors[2]);
  expect(colors[5]).not.toEqual(colors[0]);
  scenes.forEach(a => a.dispose());
});
