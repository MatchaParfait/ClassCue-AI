import * as THREE from 'three';
import meditationUrl from '../assets/llm/nailong-meditation.png';

// Task data starts the handoff; the shared scene clock freezes it on pause/hide.
export class PreparationState {
  constructor(done = false) { this.state = done ? 'done' : null; this.started = null; }
  update(clock, { active, hasText, drawable, reduced }) {
    if (!active) { this.state = 'done'; return 0; }
    if (this.state === null) this.state = hasText ? 'done' : 'waiting';
    if (this.state === 'waiting' && hasText && drawable) {
      this.state = 'leaving'; this.started = clock;
    }
    if (this.state === 'leaving') {
      const p = reduced ? 1 : Math.min(1, Math.max(0, (clock - this.started) / .8));
      if (p === 1) this.state = 'done';
      return 1 - p * p * (3 - 2 * p);
    }
    return this.state === 'waiting' ? 1 : 0;
  }
}

export class AsrPreparation {
  constructor(scene, { done = false, invalidate = () => {}, onError = () => {} } = {}) {
    this.lifecycle = new PreparationState(done);
    this.disposed = false; this.ready = false; this.weight = 0;
    this.mesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({
      transparent: true, depthWrite: false, depthTest: false, toneMapped: false, color: 0xdedede,
    }));
    this.mesh.visible = false; this.mesh.renderOrder = 2; scene.add(this.mesh);
    this.texture = new THREE.TextureLoader().load(meditationUrl, texture => {
      if (this.disposed) { texture.dispose(); return; }
      texture.colorSpace = THREE.SRGBColorSpace;
      this.mesh.scale.set(13.8 * texture.image.width / texture.image.height, 13.8, 1);
      this.ready = true; invalidate();
    }, undefined, () => {
      if (!this.disposed) { onError('等待角色素材未能加载，后台任务不受影响'); invalidate(); }
    });
    this.mesh.material.map = this.texture;
  }
  update(clock, snapshot) {
    this.weight = this.lifecycle.update(clock, snapshot);
    this.mesh.visible = this.ready && this.weight > 0 && snapshot.active;
    this.mesh.material.opacity = this.weight;
    const float = snapshot.reduced ? 0 : Math.sin(clock * Math.PI * 2 / 5) * .12;
    this.mesh.position.set(0, 3.1 + float - (1 - this.weight) * .55, -8);
    return this.weight;
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true; this.mesh.removeFromParent();
    this.mesh.geometry.dispose(); this.mesh.material.dispose(); this.texture.dispose();
  }
}
