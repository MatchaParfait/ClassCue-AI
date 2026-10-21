import * as THREE from 'three';
import { asrPosition } from '../../../experiments/subtitle-cyberspace/src/asr-layout.js';
import { liveSlots } from '../../../experiments/subtitle-cyberspace/src/live-layout.js';

// A monotonic source watermark survives polling, rolling previews and remounts.
export class TranscriptArrivals {
  constructor(previous) { this.primed = previous?.primed ?? false; this.until = previous?.until ?? -1; }
  observe(subtitles, enabled) {
    const fresh = subtitles.filter(s => s.text.trim() && s.end_ms > this.until);
    const first = !this.primed;
    this.primed = true;
    for (const s of fresh) this.until = Math.max(this.until, s.end_ms);
    return !first && enabled ? fresh.map(s => s.id) : [];
  }
  snapshot() { return { primed: this.primed, until: this.until }; }
}

const pointVertex = `
  attribute float seed;
  uniform float clock;
  uniform float size;
  varying float light;
  void main() {
    vec4 p = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * p;
    gl_PointSize = clamp(size * 38.0 / max(10.0, -p.z), 1.0, size * 2.0);
    light = .65 + .35 * sin(seed * 18.0 + clock * .22);
  }`;
const pointFragment = `
  uniform vec3 tint;
  uniform float opacity;
  varying float light;
  void main() {
    float r = length(gl_PointCoord - .5) * 2.0;
    float a = (1.0 - smoothstep(.10, 1.0, r)) * opacity * light;
    if (a < .003) discard;
    gl_FragColor = vec4(tint, a);
  }`;

function points(positions, tint, size, opacity) {
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  geometry.setAttribute('seed', new THREE.Float32BufferAttribute(positions.filter((_, i) => i % 3 === 0).map((_, i) => (i * .618034) % 1), 1));
  const material = new THREE.ShaderMaterial({ vertexShader: pointVertex, fragmentShader: pointFragment,
    uniforms: { clock: { value: 0 }, tint: { value: tint }, size: { value: size }, opacity: { value: opacity } },
    transparent: true, depthTest: true, depthWrite: false, blending: THREE.AdditiveBlending });
  const mesh = new THREE.Points(geometry, material);
  mesh.frustumCulled = false;
  return mesh;
}

export class AsrStage {
  constructor(scene, variant = 'nailong') {
    const rgb = variant === 'gpt' ? [.67, .56, .88] : variant.startsWith('dafeiyu') ? [.44, .74, .91] : [.87, .71, .43];
    this.tint = new THREE.Vector3(...rgb);
    this.group = new THREE.Group(); scene.add(this.group);
    this.group.visible = false;
    this.bursts = 0; this.pending = null; this.started = null; this.disposed = false;
    this.arcs = [];
    for (let lane = 0; lane < 3; lane++) {
      const positions = [];
      for (let i = 0; i < 240; i++) {
        const angle = i / 239 * Math.PI * 1.60 + lane * .48;
        if (i % 47 > 39) continue;
        const x = Math.cos(angle) * (22 + lane * 1.4);
        const y = Math.sin(angle) * (8.5 + lane * .85);
        positions.push(x, y + 3.2, -20 - lane * 2 + Math.sin(angle) * 1.4);
      }
      const arc = points(positions, this.tint, 3.1 - lane * .25, .65 - lane * .12);
      this.arcs.push(arc); this.group.add(arc);
    }
    const reflection = [];
    for (const cell of liveSlots(4000, true).cells) {
      const p = asrPosition({}, cell, 0, 0, true);
      reflection.push(p[0], -4.0 - (p[1] + 3.5) * .075, p[2] - .2);
    }
    this.reflection = points(reflection, this.tint, 2.3, .24);
    this.group.add(this.reflection);
    this.floor = new THREE.Mesh(new THREE.PlaneGeometry(36, 6), new THREE.ShaderMaterial({
      uniforms: { tint: { value: this.tint }, breath: { value: 0 } },
      vertexShader: 'varying vec2 vUv; void main(){vUv=uv;gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0);}',
      fragmentShader: `varying vec2 vUv; uniform vec3 tint; uniform float breath;
        void main(){vec2 q=(vUv-.5)*2.0; float r=length(q/vec2(.85, .46+breath*.025));
          float pool=exp(-r*r*3.2)*.34;
          float rim=exp(-pow((r-.78)/.035,2.0))*.20;
          float a=(pool+rim)*(1.0-smoothstep(.78,1.0,length(q)));
          gl_FragColor=vec4(tint,a);}`,
      transparent: true, depthTest: true, depthWrite: false, blending: THREE.AdditiveBlending,
    }));
    this.floor.position.set(0, -4.0, -10); this.group.add(this.floor);
    this.flow = points(new Array(48 * 3).fill(0), this.tint, 3.4, .8);
    this.flow.visible = false; this.group.add(this.flow);
    this.arrival = points([0, 0, 0], this.tint, 46, 0);
    this.arrival.visible = false; this.group.add(this.arrival);
    this.target = new THREE.Vector3();
    this.curve = new THREE.QuadraticBezierCurve3();
  }

  enqueue(ids) { if (ids.length) this.pending = new Set(ids); }
  update(clock, { active, reduced, records = [], positionOf, presence = 1 }) {
    if (this.disposed) return;
    this.group.visible = active;
    if (!active || reduced) {
      this.pending = null; this.started = null; this.flow.visible = this.arrival.visible = false;
    }
    if (!active) return;
    const time = reduced ? 0 : clock;
    this.reflection.material.uniforms.opacity.value = .24 * presence;
    this.floor.material.uniforms.breath.value = Math.sin(time * .48);
    this.reflection.position.y = Math.sin(time * .48) * -.025;
    this.arcs.forEach((arc, i) => {
      arc.rotation.z = Math.sin(time * .065 + i) * .025;
      arc.material.uniforms.clock.value = time;
    });
    if (this.started === null && this.pending) {
      const record = [...records].reverse().find(r => this.pending.has(r.subtitleId) && r.departure >= 1e6);
      if (record && positionOf) {
        this.target.copy(positionOf(record));
        this.curve.v0.set(-18, -4.8, -9);
        this.curve.v1.set(-13, this.target.y - 3, -7);
        this.curve.v2.copy(this.target);
        this.started = clock; this.pending = null; this.bursts++;
      }
    }
    if (this.started === null) return;
    const p = (clock - this.started) / 1.8;
    this.flow.visible = p < 1;
    if (this.flow.visible) {
      const attr = this.flow.geometry.attributes.position;
      for (let i = 0; i < attr.count; i++) {
        const t = Math.max(0, Math.min(1, p - i * .0035));
        const point = this.curve.getPoint(t);
        point.y += Math.sin(i * 2.4) * .035;
        attr.setXYZ(i, point.x, point.y, point.z);
      }
      attr.needsUpdate = true;
      this.flow.material.uniforms.opacity.value = Math.min(1, p * 8) * .8;
    }
    const glow = Math.max(0, 1 - Math.abs(p - 1) / .26);
    this.arrival.visible = glow > 0;
    this.arrival.position.copy(this.target);
    this.arrival.material.uniforms.opacity.value = glow * .18;
    if (p >= 1.26) this.started = null;
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true; this.group.removeFromParent();
    this.group.traverse(object => { object.geometry?.dispose(); object.material?.dispose(); });
    this.pending = null;
  }
}
