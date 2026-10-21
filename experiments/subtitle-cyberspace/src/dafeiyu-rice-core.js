import { CharacterCore } from './character-core.js';

// The full silhouette has normalized radius .51158; 3.75 scene units keeps
// the hair, basin and shoes within radius 1.919 of the original 1.94 core.
export const DAFEIYU_RICE_CORE_SIZE = 3.75;
export class DafeiyuRiceCore extends CharacterCore {
  constructor(options = {}) {
    super({ name: '大肥鱼吃白饭', size: DAFEIYU_RICE_CORE_SIZE, brightness: .8,
      src: new URL('../../../frontend/src/assets/llm/dafeiyu-rice.png', import.meta.url).href, ...options });
  }
}
