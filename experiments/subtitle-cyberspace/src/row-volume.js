import * as THREE from 'three';
import { TIMES } from './core.js';
import { infinityFrame, rowPhase, PATH_LENGTH, RIPPLE, RIPPLE_GLSL, REPLACEMENT_TINT_GLSL, REPLACE_SWEEP } from './asr-layout.js';

const vertexShader = /* glsl */`
  attribute vec3 aCenter;
  attribute vec2 aAcross;
  attribute vec4 aShape;
  attribute vec4 aFiber;
  attribute vec2 aStyle;
  attribute float aSwap;
  uniform float uTime;
  uniform float uClock;
  uniform float uReduced;
  uniform float uOpacity;
  varying vec2 vUv;
  varying float vAlpha;
  varying float vBrightness;
  varying vec2 vTint;
  ${RIPPLE_GLSL}
  ${REPLACEMENT_TINT_GLSL}
  vec3 yaw(vec3 p) { return vec3(p.x * 0.866025404 + p.z * 0.5, p.y, -p.x * 0.5 + p.z * 0.866025404); }
  float hash(float n) { return fract(sin(n * 127.1) * 43758.5453); }
  void main() {
    float seed = hash(aStyle.x + aShape.x * 193.0);
    float width = aShape.y;
    float wave = ringRipple(aShape.x, uClock) * width * ${RIPPLE.amplitude} * (1.0 - uReduced);
    vec2 along = vec2(aAcross.y, -aAcross.x);
    float across = aFiber.x + (seed - 0.5) * 0.018;
    float depth = (aFiber.z + position.y * aFiber.w) * (0.55 + seed * 0.45);
    float thickness = min(1.15, width * 0.5);
    vec3 p = aCenter;
    p.xy += aAcross * (across * width + wave) + along * aFiber.y * aShape.z;
    p.z += cos(across * 1.1) * width * 0.64 - 0.12 - depth * thickness;
    p = yaw(p) + vec3(0.0, 3.6 - depth * thickness * 0.85, -10.0);
    // Narrow disconnected strokes, not a masked solid surface.
    float strokeWidth = aStyle.y > 0.5 ? 0.019 : 0.008;
    p.x += position.x * strokeWidth * min(1.0, width / 1.3);
    vUv = vec2(position.x, position.y);
    float localClock = uClock - (aFiber.x * 0.5 + 0.5) * ${REPLACE_SWEEP.toFixed(1)};
    vTint = (replacementTint(localClock, -1.0, aSwap, 0.0) + replacementTint(localClock, aSwap, 1000000.0, 1.0)) * (1.0 - uReduced);
    float born = uReduced > 0.5 ? step(${TIMES.recognize.toFixed(1)}, uTime) : smoothstep(${TIMES.recognize.toFixed(1)}, ${(TIMES.recognize + 0.8).toFixed(1)}, uTime);
    float fade = uReduced > 0.5 ? 1.0 - step(${TIMES.build.toFixed(1)}, uTime) : 1.0 - smoothstep(${TIMES.build.toFixed(1)}, ${(TIMES.build + 1.2).toFixed(1)}, uTime);
    float rear = aCenter.z < 0.0 ? 0.22 + 0.78 * smoothstep(0.4, 2.6, abs(aCenter.x)) : 1.0;
    float gap = step(0.16, seed);
    vAlpha = born * fade * rear * gap * (0.28 + seed * 0.46) * (1.0 - depth * 0.62) * uOpacity;
    vBrightness = (0.65 + seed * 0.35) * (aStyle.y > 0.5 ? 1.1 : 1.0);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
  }
`;

const fragmentShader = /* glsl */`
  uniform float uGraphite;
  varying vec2 vUv;
  varying float vAlpha;
  varying float vBrightness;
  varying vec2 vTint;
  void main() {
    float edge = 1.0 - smoothstep(0.25, 1.0, abs(vUv.x));
    float tip = smoothstep(0.0, 0.16, vUv.y) * (1.0 - smoothstep(0.72, 1.0, vUv.y));
    float alpha = vAlpha * edge * tip;
    if (alpha < 0.035) discard;
    vec3 base = mix(vec3(0.10, 0.68, 0.75), vec3(0.40, 0.49, 0.47), uGraphite);
    vec3 outgoing = mix(vec3(0.85, 0.22, 0.28), vec3(0.65, 0.36, 0.35), uGraphite);
    vec3 incoming = mix(vec3(0.22, 0.80, 0.43), vec3(0.42, 0.64, 0.48), uGraphite);
    vec3 color = mix(mix(base, outgoing, vTint.x), incoming, vTint.y);
    gl_FragColor = vec4(color * vBrightness, alpha);
  }
`;

// Each row contains only separated line fragments and specks, leaving real empty space.
export function createFiberGeometry() {
  const positions = [], fibers = [], styles = [];
  const stroke = (x, side, start, length, seed, speck = 0) => {
    for (const [u, v] of [[-1,0], [1,0], [1,1], [-1,0], [1,1], [-1,1]]) {
      positions.push(u, v, 0);
      fibers.push(x, side, start, length);
      styles.push(seed, speck);
    }
  };
  for (let layer = 0; layer < 2; layer++) {
    for (let column = 0; column < 48; column++) {
      const x = -0.98 + (column + layer * 0.37) / 48 * 1.96;
      const seed = column * 13.7 + layer * 53.1;
      for (let segment = 0; segment < 3; segment++) {
        stroke(x, layer * 2 - 1, segment * 0.32 + 0.025, 0.13 + (column % 4) * 0.025, seed + segment * 7.3);
      }
      stroke(x, layer * 2 - 1, (column % 5) * 0.08, 0.025, seed + 91.2, 1);
      if (column % 6 === 0) stroke(x + 0.012, layer * 2 - 1, 0.04, 0.58, seed + 134.6);
    }
  }
  const geometry = new THREE.InstancedBufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute('aFiber', new THREE.Float32BufferAttribute(fibers, 4));
  geometry.setAttribute('aStyle', new THREE.Float32BufferAttribute(styles, 2));
  return geometry;
}

export class RowVolume {
  constructor(scene, entries, graphite = false) {
    const rows = entries.slice(0, entries[0]?.slots ?? 0);
    const geometry = createFiberGeometry();
    const attribute = (name, size, data) => geometry.setAttribute(name, new THREE.InstancedBufferAttribute(new Float32Array(data), size));
    const frames = rows.map((info) => infinityFrame(rowPhase(info)));
    attribute('aCenter', 3, rows.flatMap((info, i) => frames[i].center.map((v) => v * info.scale)));
    attribute('aAcross', 2, frames.flatMap((f) => f.normal.slice(0, 2)));
    attribute('aSwap', 1, rows.map((info) => info.departure));
    attribute('aShape', 4, rows.flatMap((info) => [rowPhase(info), info.width, Math.min(0.12, PATH_LENGTH * info.scale / info.slots * 0.22), 0]));
    geometry.instanceCount = rows.length;
    const material = new THREE.ShaderMaterial({ vertexShader, fragmentShader,
      uniforms: { uTime: { value: 0 }, uClock: { value: 0 }, uReduced: { value: 0 }, uOpacity: { value: 1 }, uGraphite: { value: graphite ? 1 : 0 } },
      transparent: true, depthTest: true, depthWrite: true, side: THREE.DoubleSide });
    this.mesh = new THREE.Mesh(geometry, material);
    this.mesh.frustumCulled = false;
    this.mesh.renderOrder = -1;
    scene.add(this.mesh);
  }

  render(time, clock, reduced) {
    this.mesh.visible = time >= TIMES.recognize && time < TIMES.build + 1.2;
    this.mesh.material.uniforms.uTime.value = time;
    this.mesh.material.uniforms.uClock.value = clock;
    this.mesh.material.uniforms.uReduced.value = reduced ? 1 : 0;
  }

  dispose() {
    this.mesh.removeFromParent(); this.mesh.geometry.dispose(); this.mesh.material.dispose();
  }
}
