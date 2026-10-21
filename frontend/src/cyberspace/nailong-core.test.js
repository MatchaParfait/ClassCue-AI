import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { PerspectiveCamera, Raycaster, Texture, TextureLoader, Vector2 } from 'three';
import { NailongCore } from '../../../experiments/subtitle-cyberspace/src/nailong-core.js';
import { DafeiyuCore } from '../../../experiments/subtitle-cyberspace/src/dafeiyu-core.js';
import { Dafeiyu31Core } from '../../../experiments/subtitle-cyberspace/src/dafeiyu31-core.js';
import { DafeiyuRiceCore } from '../../../experiments/subtitle-cyberspace/src/dafeiyu-rice-core.js';
import { GptCore } from '../../../experiments/subtitle-cyberspace/src/gpt-core.js';
import { GravitySubtitleScene } from '../../../experiments/subtitle-cyberspace/src/gravity-scene.js';

let loaded, failed, texture, alpha;
beforeEach(() => {
  texture = new Texture(); texture.image = {};
  alpha = new Uint8ClampedArray(256 * 256 * 4);
  vi.spyOn(TextureLoader.prototype, 'load').mockImplementation((_url, ok, _progress, error) => {
    loaded = ok; failed = error; return texture;
  });
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    fillRect() {}, fillText() {}, drawImage() {}, getImageData: () => ({ data: alpha }),
  });
});
afterEach(() => vi.restoreAllMocks());

it.each(['nailong', 'dafeiyu', 'dafeiyu31', 'dafeiyu-rice', 'gpt'])('%s keeps the black hole until the character loads and preserves the original variant', variant => {
  const invalidate = vi.fn(), onAssetError = vi.fn();
  const original = new GravitySubtitleScene();
  expect(TextureLoader.prototype.load).not.toHaveBeenCalled();
  expect(original.hole.visible).toBe(true);
  const originalShader = original.hole.material.fragmentShader;
  const scene = new GravitySubtitleScene({ variant, onInvalidate: invalidate, onAssetError });
  expect(scene.hole.visible).toBe(true);
  failed(new Error('missing asset'));
  expect(onAssetError).toHaveBeenCalledOnce();
  expect(scene.occluder.visible).toBe(true);
  loaded(texture);
  expect(scene.character.mesh.visible).toBe(true);
  expect(scene.hole.visible).toBe(true);
  expect(scene.hole.material.uniforms.uCharacterReady.value).toBe(1);
  expect(scene.hole.material.uniforms.uCharacterMap.value).toBe(texture);
  expect(scene.occluder.visible).toBe(false);
  expect(scene.character.mesh.material.map).toBe(texture);
  const gain = variant === 'nailong' ? 1 : variant === 'gpt' ? .76 : .8;
  expect(scene.character.mesh.material.color.toArray()).toEqual([gain, gain, gain]);
  expect(original.hole.material.fragmentShader).toBe(originalShader);
  expect(original.scene.background).toBeNull();
  expect(invalidate).toHaveBeenCalledOnce();
  original.dispose(); scene.dispose();
});

it.each([NailongCore, DafeiyuCore, Dafeiyu31Core, DafeiyuRiceCore, GptCore])('%s faces the camera and obstructs picking only inside opaque pixels', Core => {
  const core = new Core(); loaded(texture);
  const camera = new PerspectiveCamera(46, 1, .1, 100);
  camera.position.set(7, 5, 12); camera.lookAt(0, 0, 0); camera.updateMatrixWorld();
  core.update(camera); core.mesh.updateMatrixWorld();
  expect(core.mesh.quaternion.angleTo(camera.quaternion)).toBeLessThan(.00001);
  const ray = new Raycaster(); ray.setFromCamera(new Vector2(), camera);
  expect(core.obstruction(ray)).toBe(Infinity);
  alpha.fill(255);
  expect(core.obstruction(ray)).toBeCloseTo(camera.position.length());
  core.dispose();
});

it.each([NailongCore, DafeiyuCore, Dafeiyu31Core, DafeiyuRiceCore, GptCore])('%s releases textures and ignores late loads after leaving the analysis scene', Core => {
  const onReady = vi.fn(), onError = vi.fn();
  const core = new Core({ onReady, onError });
  const dispose = vi.spyOn(texture, 'dispose');
  core.dispose(); loaded(texture); failed(new Error('late failure'));
  expect(dispose).toHaveBeenCalledOnce();
  expect(core.mesh.visible).toBe(false);
  expect(onReady).not.toHaveBeenCalled(); expect(onError).not.toHaveBeenCalled();
});
