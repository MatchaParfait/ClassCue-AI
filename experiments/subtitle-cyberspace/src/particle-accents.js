import * as THREE from 'three';
import { GLYPH_DEPTH, particleVertex, particleFragment } from './particle-shaders.js';

const RELIEF_LIMIT = 24000;
const TRAIL_LIMIT = 10000;
const ATTRIBUTES = { aOrigin: 3, aParticle: 3, aStroke: 4, aFlow: 2 };

function pass(scene, uniforms, kind, capacity) {
  const geometry = new THREE.InstancedBufferGeometry();
  const shape = new THREE.PlaneGeometry(1, 1, kind === 'TRAIL' ? 3 : 1, 1);
  geometry.setIndex(shape.index);
  geometry.setAttribute('position', shape.attributes.position);
  geometry.setAttribute('uv', shape.attributes.uv);
  shape.dispose();
  for (const [name, size] of Object.entries({ ...ATTRIBUTES, ...(kind === 'RELIEF' ? { aSurface: 4 } : {}) })) {
    geometry.setAttribute(name, new THREE.InstancedBufferAttribute(new Float32Array(capacity * size), size).setUsage(THREE.DynamicDrawUsage));
  }
  geometry.instanceCount = 0;
  const material = new THREE.ShaderMaterial({ uniforms, defines: { [kind]: 1 }, vertexShader: particleVertex,
    fragmentShader: particleFragment, transparent: true, depthWrite: false, depthTest: false });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.frustumCulled = false; mesh.visible = false;
  mesh.renderOrder = kind === 'RELIEF' ? -2 : -1; // Both sit behind the clear face.
  scene.add(mesh);
  return mesh;
}

function copySample(target, source, index, dest) {
  for (const [name, size] of Object.entries(ATTRIBUTES)) {
    const attribute = source.getAttribute(name);
    target.getAttribute(name).array.set(attribute.array.subarray(index * size, (index + 1) * size), dest * size);
  }
}

function uploaded(geometry, count) {
  geometry.instanceCount = count;
  for (const [name, attribute] of Object.entries(geometry.attributes)) {
    if (!name.startsWith('a')) continue;
    attribute.clearUpdateRanges();
    if (count) { attribute.addUpdateRange(0, count * attribute.itemSize); attribute.needsUpdate = true; }
  }
}

// Small extra passes consume ONLY the front face's remaining particle budget.
// Static buffers are rebuilt with the face; no per-particle history is stored.
export class ParticleAccents {
  constructor(scene, uniforms) {
    this.relief = pass(scene, uniforms, 'RELIEF', RELIEF_LIMIT);
    this.trails = pass(scene, uniforms, 'TRAIL', TRAIL_LIMIT);
    this.reliefCount = this.trailCount = this.visibleTrails = 0;
  }

  sync(field, budget) {
    const available = Math.max(0, budget - field.count), candidates = [];
    for (let n = 0; n < field.count; n++) {
      const slot = field.particles.array[n * 3];
      // Spend accents on foreground stroke boundaries, not solid interiors.
      if (field.strokes.array[n * 4 + 3] > .35 && field.entries[slot].record.wallFrame[2] < 3) candidates.push(n);
    }
    this.trailCount = Math.min(TRAIL_LIMIT, Math.floor(available * .36), Math.floor(candidates.length * .25));
    const pairs = Math.min(Math.floor(RELIEF_LIMIT / 2), Math.floor((available - this.trailCount) / 2), candidates.length);
    this.reliefCount = pairs * 2;
    for (let n = 0; n < pairs; n++) {
      const index = candidates[Math.floor(n * candidates.length / pairs)], slot = field.particles.array[index * 3];
      const yaw = field.entries[slot].record.wallFrame[0];
      for (let layer = 0; layer < 2; layer++) {
        const dest = n * 2 + layer;
        copySample(this.relief.geometry, field.geometry, index, dest);
        this.relief.geometry.getAttribute('aSurface').array.set([Math.sin(yaw), 0, Math.cos(yaw),
          layer ? GLYPH_DEPTH : GLYPH_DEPTH * .46], dest * 4);
      }
    }
    for (let n = 0; n < this.trailCount; n++) {
      copySample(this.trails.geometry, field.geometry, candidates[Math.floor((n + .5) * candidates.length / this.trailCount)], n);
    }
    uploaded(this.relief.geometry, this.reliefCount);
    uploaded(this.trails.geometry, this.trailCount);
  }

  update(field, visible, scanning, revealing) {
    this.relief.visible = visible && this.reliefCount > 0;
    // Only submit the ribbon pass during an active sweep or a new real hit;
    // the shader makes other ribbons transparent without rewriting buffers.
    // Completed letters retain depth but never keep a trail alive on polling.
    const state = field.state, slots = this.trails.geometry.getAttribute('aParticle').array;
    const order = this.trails.geometry.getAttribute('aFlow').array;
    this.visibleTrails = 0;
    // A cheap CPU eligibility count also lets an idle scene skip the whole draw.
    for (let n = 0; n < this.trailCount; n++) {
      const slot = slots[n * 3] * 4, hit = state[slot], front = state[slot + 3] - order[n * 2 + 1];
      if ((scanning && hit < .16 && state[slot + 3] > -1 && front > -.30 && front < .24)
        || (revealing && hit > 0 && hit < .95)) this.visibleTrails++;
    }
    this.trails.visible = visible && this.visibleTrails > 0;
  }

  dispose() {
    for (const mesh of [this.relief, this.trails]) { mesh.removeFromParent(); mesh.geometry.dispose(); mesh.material.dispose(); }
    this.reliefCount = this.trailCount = this.visibleTrails = 0;
  }
}
