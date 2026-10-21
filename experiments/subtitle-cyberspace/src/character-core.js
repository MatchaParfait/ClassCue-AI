import * as THREE from 'three';

// A camera-facing scene object, not a screen overlay. Alpha-tested depth keeps
// foreground subtitles visible and hides only ink behind the character itself.
export class CharacterCore {
  constructor({ name, src, size, brightness = 1, onReady = () => {}, onError = () => {} }) {
    this.disposed = false;
    this.brightness = brightness;
    this.mesh = new THREE.Mesh(new THREE.PlaneGeometry(size, size), new THREE.MeshBasicMaterial({
      alphaTest: .08, depthWrite: true, depthTest: true, toneMapped: false,
    }));
    // Neutral linear-light gain affects only the artwork, not the scene effects.
    this.mesh.material.color.setScalar(brightness);
    this.mesh.name = name;
    this.mesh.visible = false;
    this.texture = new THREE.TextureLoader().load(src,
      texture => {
        if (this.disposed) return;
        try {
          const mask = document.createElement('canvas'); mask.width = mask.height = 256;
          const context = mask.getContext('2d', { willReadFrequently: true });
          context.drawImage(texture.image, 0, 0, 256, 256);
          this.alpha = context.getImageData(0, 0, 256, 256).data;
          texture.colorSpace = THREE.SRGBColorSpace;
          this.mesh.material.map = texture; this.mesh.material.needsUpdate = true;
          this.mesh.visible = true; onReady();
        } catch (error) { onError(error); }
      }, undefined, error => { if (!this.disposed) onError(error); });
  }

  update(camera) { this.mesh.quaternion.copy(camera.quaternion); }

  obstruction(raycaster) {
    const hit = raycaster.intersectObject(this.mesh)[0];
    if (!hit?.uv || !this.alpha) return Infinity;
    const x = Math.min(255, Math.max(0, Math.floor(hit.uv.x * 256)));
    const y = Math.min(255, Math.max(0, Math.floor((1 - hit.uv.y) * 256)));
    return this.alpha[(y * 256 + x) * 4 + 3] >= 21 ? hit.distance : Infinity;
  }

  dispose() {
    this.disposed = true;
    this.texture.dispose(); this.mesh.geometry.dispose(); this.mesh.material.dispose();
    this.mesh.removeFromParent(); this.alpha = null;
  }
}
