import { CharacterCore } from './character-core.js';

// Fit the complete silhouette, including hair, tail and shoes, inside radius
// 1.94. The supplied PNG's opaque radius is 0.51773 of its canvas width.
export const DAFEIYU_CORE_SIZE = 3.68;
export class DafeiyuCore extends CharacterCore {
  constructor(options = {}) {
    super({ name: '大肥鱼1', size: DAFEIYU_CORE_SIZE, brightness: .8,
      src: new URL('../../../frontend/src/assets/llm/dafeiyu.png', import.meta.url).href, ...options });
  }
}
