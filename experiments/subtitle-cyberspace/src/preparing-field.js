import * as THREE from 'three';
import { infinityFrame, RIPPLE_GLSL } from './asr-layout.js';

// A non-text silhouette for live jobs that have not published subtitles yet.
export class PreparingField {
  constructor(scene) {
    const positions = [], axes = [], phases = [];
    for (let row = 0; row < 240; row++) {
      const phase = row / 240;
      const { center, normal } = infinityFrame(phase);
      for (let column = 0; column < 12; column++) {
        const cross = (column / 11 - 0.5) * 1.7;
        const x = center[0] + normal[0] * cross;
        positions.push(x * .866025404 + center[2] * .5, center[1] + normal[1] * cross, -x * .5 + center[2] * .866025404 - 15);
        axes.push(normal[0] * .866025404, normal[1], -normal[0] * .5);
        phases.push(phase);
      }
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geometry.setAttribute('axis', new THREE.Float32BufferAttribute(axes, 3));
    geometry.setAttribute('phase', new THREE.Float32BufferAttribute(phases, 1));
    const material = new THREE.ShaderMaterial({
      uniforms: { clock: { value: 0 }, motion: { value: 1 } },
      transparent: true, depthWrite: false,
      vertexShader: `attribute vec3 axis; attribute float phase; uniform float clock; uniform float motion; ${RIPPLE_GLSL}
        void main() { vec3 p = position + axis * ringRipple(phase, clock) * .35 * motion;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0); gl_PointSize = 1.7; }`,
      fragmentShader: `void main() { if (distance(gl_PointCoord, vec2(.5)) > .5) discard; gl_FragColor = vec4(.22,.75,.85,.58); }`,
    });
    this.mesh = new THREE.Points(geometry, material);
    this.mesh.frustumCulled = false;
    scene.add(this.mesh);
  }
  render(time, clock, reduced) {
    this.mesh.visible = time >= 4;
    this.mesh.material.uniforms.clock.value = clock;
    this.mesh.material.uniforms.motion.value = reduced ? 0 : 1;
  }
  dispose() {
    this.mesh.removeFromParent(); this.mesh.geometry.dispose(); this.mesh.material.dispose();
  }
}
