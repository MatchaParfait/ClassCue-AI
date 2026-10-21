import { CharacterCore } from './character-core.js';

// Opaque radius 0.50355 × 3.8 = 1.9135, inside the original 1.94 core.
export const NAILONG_CORE_SIZE = 3.8;
export class NailongCore extends CharacterCore {
  constructor(options = {}) {
    super({ name: '奶龙', size: NAILONG_CORE_SIZE,
      src: new URL('../../../frontend/src/assets/llm/nailong.png', import.meta.url).href, ...options });
  }
}
