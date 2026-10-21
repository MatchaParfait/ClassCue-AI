import { CharacterCore } from './character-core.js';

// The circular portrait occupies radius 0.47928 of its PNG canvas. At 4.03
// scene units it fills radius 1.931, just inside the original 1.94 core.
export const DAFEIYU31_CORE_SIZE = 4.03;
export class Dafeiyu31Core extends CharacterCore {
  constructor(options = {}) {
    super({ name: '大肥鱼2', size: DAFEIYU31_CORE_SIZE, brightness: .8,
      src: new URL('../../../frontend/src/assets/llm/dafeiyu31.png', import.meta.url).href, ...options });
  }
}
