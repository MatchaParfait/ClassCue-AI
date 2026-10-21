import * as THREE from 'three';

// Broad, overlapping light curtains. This shares the subtitle renderer and clock;
// no extra canvas, animation loop, texture download or simulated task progress.
export class TaskAurora {
  constructor(scene, variant = 'nailong') {
    const palette = variant === 'gpt'
      ? [[.040, .043, .055], [.18, .14, .25], [.12, .13, .19]]
      : variant.startsWith('dafeiyu')
        ? [[.035, .045, .052], [.10, .19, .24], [.10, .15, .17]]
        : [[.045, .046, .044], [.24, .20, .13], [.14, .15, .15]];
    this.material = new THREE.ShaderMaterial({
      uniforms: { uTime: { value: 0 }, uAspect: { value: 1 }, uStrength: { value: 1 }, uAnalysis: { value: 0 }, uParticleStyle: { value: 0 }, uBase: { value: new THREE.Vector3(...palette[0]) }, uGlow: { value: new THREE.Vector3(...palette[1]) }, uFill: { value: new THREE.Vector3(...palette[2]) } },
      vertexShader: `
        varying vec2 vUv;
        void main() { vUv = uv; gl_Position = vec4(position.xy, 0.999, 1.0); }
      `,
      fragmentShader: `
        precision mediump float;
        varying vec2 vUv;
        uniform float uTime;
        uniform float uAspect;
        uniform float uStrength;
        uniform float uAnalysis;
        uniform float uParticleStyle;
        uniform vec3 uBase;
        uniform vec3 uGlow;
        uniform vec3 uFill;
        float ribbon(float y, float center, float width) {
          float d = (y - center) / width;
          return exp(-d * d);
        }
        void main() {
          vec2 p = vUv;
          float x = p.x * min(uAspect, 2.6);
          // Broad 20/24-second clouds sit behind the live infinity ribbon.
          float t = uTime * 0.314159;
          vec2 q = (p - .5) * vec2(min(uAspect, 2.0), 1.0);
          vec2 centerA = vec2(-.24 + sin(t) * .16, .08 + cos(t) * .10);
          vec2 centerB = vec2(.30 + cos(t * .8333) * .15, -.06 + sin(t * .8333) * .13);
          float a = exp(-dot((q - centerA) / vec2(.52, .27), (q - centerA) / vec2(.52, .27)));
          float b = exp(-dot((q - centerB) / vec2(.44, .31), (q - centerB) / vec2(.44, .31)));
          float folds = .84 + .16 * sin(q.x * 5.0 + q.y * 3.0 + t);
          float floorLight = exp(-pow(q.x / .56, 2.0) - pow((p.y - .18) / .075, 2.0));
          float vignette = 1.0 - smoothstep(.25, .95, length(q * vec2(.65, 1.0)));
          vec3 color = uBase * (.65 + .35 * vignette);
          color += uGlow * a * folds * .80 + uFill * b * .68;
          color += uGlow * floorLight * .22;
          // Sparse, soft distant motes; bounded screen-space work, no extra draw loop.
          vec2 dustUv = p * vec2(9.0 * min(uAspect, 2.0), 7.0) + vec2(uTime * .012, -uTime * .018);
          vec2 cell = floor(dustUv);
          float seed = fract(sin(dot(cell, vec2(127.1, 311.7))) * 43758.5453);
          vec2 offset = vec2(.2 + seed * .6, .2 + fract(seed * 13.7) * .6);
          float dust = exp(-dot(fract(dustUv) - offset, fract(dustUv) - offset) * (450.0 + seed * 1000.0));
          float periphery = smoothstep(.20, .48, length((p - .5) * vec2(1.0, .8)));
          color += (uFill + vec3(.22)) * dust * step(.94, seed) * periphery * .20;
          float edge = smoothstep(0.0, 0.16, p.y) * smoothstep(0.0, 0.20, 1.0 - p.y);
          // Preserve the archived analysis renderer; the active gravity scene owns LLM.
          float flow = uTime * 0.22;
          float warm = ribbon(p.y, 0.43 + sin(x * 2.0 + flow) * 0.23, 0.20);
          float cool = ribbon(p.y, 0.58 + sin(x * 1.65 - flow * 0.8 + 2.0) * 0.22, 0.26);
          float fold = ribbon(p.y, 0.35 + sin(x * 3.0 - flow * 0.7) * 0.13, 0.07);
          vec3 analysisColor = vec3(0.045, 0.055, 0.070);
          analysisColor += vec3(0.34, 0.22, 0.075) * warm * (0.65 + 0.35 * sin(x + flow * 0.6));
          analysisColor += vec3(0.075, 0.15, 0.23) * cool;
          analysisColor += vec3(0.17, 0.12, 0.06) * fold;
          // High quality uses broad warm fields on a 26-second rhythm.
          // No hit-driven flashes: evidence color belongs to the glyphs.
          float slow = uTime * 0.241661;
          float amber = ribbon(p.y, 0.36 + sin(x * 1.8 + slow) * 0.23, 0.28);
          float cream = ribbon(p.y, 0.61 + sin(x * 1.2 - slow + 1.7) * 0.20, 0.32);
          float silk = ribbon(p.y, 0.42 + sin(x * 2.5 + slow + 0.8) * 0.15, 0.10);
          vec3 warmField = vec3(0.04, 0.045, 0.045) + vec3(0.23, 0.145, 0.045) * amber
            + vec3(0.10, 0.095, 0.065) * cream + vec3(0.075, 0.058, 0.025) * silk;
          analysisColor = mix(analysisColor, warmField, uParticleStyle);
          color = mix(color, analysisColor, uAnalysis);
          float opacity = mix(.96, edge * (0.65 + vignette * 0.24), uAnalysis);
          gl_FragColor = vec4(color, opacity * uStrength);
        }
      `,
      transparent: true, depthTest: false, depthWrite: false,
    });
    this.mesh = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), this.material);
    this.mesh.frustumCulled = false;
    this.mesh.renderOrder = -1000;
    scene.add(this.mesh);
  }

  update(time, aspect, reduced, analysis, degraded, particleStyle = false) {
    const uniforms = this.material.uniforms;
    if (!reduced && !degraded) uniforms.uTime.value = time;
    uniforms.uAspect.value = aspect;
    uniforms.uAnalysis.value = analysis;
    uniforms.uParticleStyle.value = particleStyle && !degraded ? 1 : 0;
  }

  dispose() {
    this.mesh.removeFromParent();
    this.mesh.geometry.dispose();
    this.material.dispose();
  }
}
