import * as THREE from 'three';
import { ANALYSIS, ANALYSIS_PLANES } from './analysis-layout.js';

// Decorative activity shares the scene clock and camera. No extra animation loop,
// task progress, text displacement or hit-test geometry is introduced.
export class AnalysisActivity {
  constructor(scene) {
    this.seen = new Set();
    this.geometry = new THREE.PlaneGeometry(ANALYSIS.columns * ANALYSIS.pitch + 1.2, ANALYSIS.rows * ANALYSIS.lineHeight + 1.2);
    this.layers = ANALYSIS_PLANES.map((plane, index) => {
      const material = new THREE.ShaderMaterial({
        uniforms: {
          uTime: { value: 0 }, uActive: { value: 0 }, uOpacity: { value: 0 },
          uLayer: { value: index }, uQuiet: { value: 0 }, uPulseAge: { value: 10 }, uPulseCenter: { value: new THREE.Vector2() },
        },
        vertexShader: `
          varying vec2 vPoint;
          void main() {
            vPoint = position.xy;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          }
        `,
        fragmentShader: `
          varying vec2 vPoint;
          uniform float uTime, uActive, uOpacity, uLayer, uPulseAge, uQuiet;
          uniform vec2 uPulseCenter;
          void main() {
            vec2 halfSize = vec2(${((ANALYSIS.columns * ANALYSIS.pitch + 1.2) / 2 - .15).toFixed(3)}, ${((ANALYSIS.rows * ANALYSIS.lineHeight + 1.2) / 2 - .15).toFixed(3)});
            vec2 edge = halfSize - abs(vPoint);
            float inside = smoothstep(0.0, 0.08, min(edge.x, edge.y));
            float rail = 1.0 - smoothstep(0.02, 0.055, min(edge.x, edge.y));
            float corner = 1.0 - smoothstep(0.55, 0.95, max(edge.x, edge.y));
            // A line, a soft trailing sheet and two bright tips travel together.
            // Layer offsets make the movement legible without flashing in unison.
            float cycle = fract(uTime / 5.6 + uLayer * 0.23);
            float scanY = mix(halfSize.y + 1.4, -halfSize.y - 1.4, cycle);
            float d = vPoint.y - scanY;
            float line = exp(-pow(d / 0.045, 2.0));
            float sheet = exp(-pow((d - 0.55) / 0.75, 2.0));
            float tips = exp(-pow(d / 0.13, 2.0)) * (1.0 - smoothstep(0.08, 0.28, edge.x));
            // Particle ink carries the main scan; keep its supporting gold
            // light but soften the rectangular rails and secondary scan.
            float gold = rail * (0.11 + corner * 0.30) * mix(1.0, 0.35, uQuiet)
              + uActive * (line * 0.60 + sheet * 0.105 + tips * 0.70) * mix(1.0, 0.12, uQuiet);
            float age = clamp(uPulseAge / 1.15, 0.0, 1.0);
            float radius = 0.18 + age * 4.4;
            float distance = length(vPoint - uPulseCenter);
            float ring = exp(-pow((distance - radius) / 0.095, 2.0));
            float wash = exp(-pow(distance / max(0.2, radius), 2.0));
            float green = (ring * 0.55 + wash * 0.07) * (1.0 - age) * uActive;
            float alpha = (gold + green) * inside * uOpacity;
            if (alpha < 0.004) discard;
            vec3 color = (vec3(1.0, 0.80, 0.36) * gold + vec3(0.48, 0.91, 0.53) * green) / max(0.001, gold + green);
            gl_FragColor = vec4(color, min(alpha, 0.8));
          }
        `,
        transparent: true, depthWrite: false, depthTest: false, side: THREE.DoubleSide,
      });
      const mesh = new THREE.Mesh(this.geometry, material);
      mesh.position.set(plane.center[0], plane.center[1], plane.center[2] - .12);
      mesh.rotation.y = plane.yaw;
      mesh.renderOrder = -20;
      mesh.visible = false;
      scene.add(mesh);
      return { mesh, pulseAt: -Infinity };
    });
  }

  update(records, hits, time, active, opacity, pulseEnabled = true) {
    const occupied = new Set(), fresh = new Map();
    for (const record of records) {
      if (record.departure < 1e6) continue;
      const layer = record.wallFrame?.[2];
      if (!this.layers[layer]) continue;
      occupied.add(layer);
      if (!(hits.get(record.key) > 0) || this.seen.has(record.key)) continue;
      this.seen.add(record.key);
      if (!active) continue;
      const group = fresh.get(layer) ?? [];
      group.push(record.wall); fresh.set(layer, group);
    }
    for (let i = 0; i < this.layers.length; i++) {
      const layer = this.layers[i], uniforms = layer.mesh.material.uniforms;
      layer.mesh.visible = opacity > 0 && occupied.has(i);
      uniforms.uOpacity.value = opacity * (i < 3 ? 1 : .55);
      uniforms.uActive.value = active ? 1 : 0;
      uniforms.uQuiet.value = pulseEnabled ? 0 : 1;
      if (active) uniforms.uTime.value = time;
      const points = fresh.get(i);
      // Coalesce a burst; overlapping evidence cannot restart the same pulse.
      if (points && time - layer.pulseAt > .3) {
        const plane = ANALYSIS_PLANES[i];
        uniforms.uPulseCenter.value.set(
          points.reduce((sum, p) => sum + (p[0] - plane.center[0]) / Math.cos(plane.yaw), 0) / points.length,
          points.reduce((sum, p) => sum + p[1] - plane.center[1], 0) / points.length,
        );
        layer.pulseAt = time;
      }
      // Consuming events while stopped prevents stale results flashing on resume.
      if (!active) layer.pulseAt = -Infinity;
      uniforms.uPulseAge.value = pulseEnabled && Number.isFinite(layer.pulseAt) ? time - layer.pulseAt : 10;
    }
  }

  dispose() {
    for (const { mesh } of this.layers) { mesh.removeFromParent(); mesh.material.dispose(); }
    this.geometry.dispose(); this.seen.clear();
  }
}
