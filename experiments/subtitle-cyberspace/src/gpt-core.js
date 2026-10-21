import { CharacterCore } from './character-core.js';

// Complete alpha-tested silhouette radius .53359 * 3.60 = 1.921,
// keeping horns, wings and dress inside the original 1.94 core.
export const GPT_CORE_SIZE = 3.60;
export class GptCore extends CharacterCore {
  constructor(options = {}) {
    super({ name: 'GPT', size: GPT_CORE_SIZE, brightness: .76,
      src: new URL('../../../frontend/src/assets/llm/gpt.png', import.meta.url).href, ...options });
  }
}
