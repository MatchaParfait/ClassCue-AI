import { ANALYSIS } from './analysis-layout.js';

export const GLYPH_STATE_WIDTH = 2048;
export const GLYPH_DEPTH = .0468; // 13% of the .36 world-unit character face.

// All three passes use the same motion function. Trails sample its recent path
// in world space and project with the CURRENT camera, so camera movement never
// leaves a screen-space ghost. Only the TRAIL variant evaluates history.
export const particleVertex = `
  attribute vec3 aOrigin;
  attribute vec3 aParticle;
  attribute vec4 aStroke;
  attribute vec2 aFlow;
  #ifdef RELIEF
    attribute vec4 aSurface;
  #endif
  uniform sampler2D uGlyphState;
  uniform float uTime, uActive, uReveal, uOpacity, uHasHits, uAspect;
  uniform vec3 uPointer;
  varying vec2 vUv;
  varying vec3 vColor;
  varying float vAlpha;

  float wave(float front, float width) { return exp(-pow(front / width, 2.0)); }
  float gathering(float hit) { return sin(smoothstep(0.0, 0.36, hit) * 3.141593); }
  vec3 inkPosition(float time, float hit, float scan, float attention, float depth) {
    float seed = aParticle.y;
    float locked = smoothstep(0.0, 0.16, hit);
    float front = scan - aFlow.y;
    float scanning = step(-1.0, scan) * uActive * (1.0 - locked);
    float leading = wave(front + 0.075, 0.085) * scanning;
    float combed = smoothstep(0.0, 0.07, front) * (1.0 - smoothstep(0.16, 0.38, front)) * scanning;
    float fringe = aStroke.w * step(0.76, seed);
    float idle = sin(time * 0.85 + seed * 6.283185 + aOrigin.y * 0.3) * 0.012 * fringe;
    float flow = sin(time * 4.0 - aFlow.y * 15.0 + seed * 3.0) * leading * 0.068 * aStroke.w;
    float travel = (idle + flow) * uActive * (1.0 - locked) * (1.0 - combed * 0.8);
    travel *= (0.35 + depth * 0.65) * (1.0 - attention * 0.85);
    float gather = gathering(hit) * aStroke.w * (0.45 + 0.55 * step(0.76, seed)) * 0.035 * uReveal;
    vec3 p = aOrigin + aStroke.xyz * (travel + gather);
    p.z += hit * ${ANALYSIS.lift.toFixed(2)};
    return p;
  }
  #ifdef TRAIL
    float previousHit(float hit, float age) {
      if (hit <= 0.0 || hit >= 1.0) return hit;
      // Inverse smoothstep of CharacterHighlights' 500ms reveal.
      float progress = 0.5 - sin(asin(clamp(1.0 - 2.0 * hit, -1.0, 1.0)) / 3.0);
      return smoothstep(0.0, 1.0, progress - age / 0.5);
    }
  #endif
  void main() {
    vec4 state = texture2D(uGlyphState, vec2((aParticle.x + 0.5) / ${GLYPH_STATE_WIDTH.toFixed(1)}, 0.5));
    float hit = state.x;
    float depth = smoothstep(-33.0, -5.0, aOrigin.z);
    vec4 original = projectionMatrix * modelViewMatrix * vec4(aOrigin, 1.0);
    vec2 pointerDelta = (original.xy / original.w - uPointer.xy) * vec2(uAspect, 1.0);
    float attention = (1.0 - smoothstep(0.04, 0.30, length(pointerDelta))) * uPointer.z;
    float locked = smoothstep(0.0, 0.16, hit);
    float front = state.w - aFlow.y;
    float scanning = step(-1.0, state.w) * uActive * (1.0 - locked);
    float band = wave(front, 0.07) * scanning;
    float leading = wave(front + 0.075, 0.085) * scanning;
    float combed = smoothstep(0.0, 0.07, front) * (1.0 - smoothstep(0.16, 0.38, front)) * scanning;
    vec3 p = inkPosition(uTime, hit, state.w, attention, depth);
    float dye = smoothstep(0.12 + aFlow.x * 0.42, 0.30 + aFlow.x * 0.42, hit);
    float glint = (1.0 - smoothstep(0.10, 0.38, abs(hit - (0.23 + aFlow.x * 0.42)))) * dye * (1.0 - smoothstep(0.72, 1.0, hit));
    vec3 green = mix(vec3(0.43, 0.80, 0.48), vec3(0.78, 1.0, 0.64), glint);
    vec3 ink = mix(vec3(0.45, 0.51, 0.50), vec3(0.88, 0.89, 0.80), depth);
    ink = mix(ink, vec3(1.0, 0.82, 0.38), band * 0.9);
    ink += vec3(0.10, 0.085, 0.05) * combed;
    vColor = mix(ink, green, dye) + vec3(0.08) * max(state.y, attention * 0.65);
    vAlpha = (0.72 + 0.28 * aParticle.z) * state.z * uOpacity;
    vAlpha *= mix(0.60, 1.0, max(depth, max(attention, locked)));
    vAlpha *= 1.0 - uHasHits * (1.0 - hit) * 0.13;
    vUv = uv;

    #ifdef RELIEF
      // Actual shallow extrusion along each letter plane's normal. The source
      // face remains fixed, including hit-testing and text selection geometry.
      p -= aSurface.xyz * aSurface.w;
      float rear = step(0.035, aSurface.w);
      vec3 normal = normalize(mat3(modelViewMatrix) * aSurface.xyz);
      vec3 light = normalize(vec3(-0.45 + uPointer.x * uPointer.z * 0.35, 0.65, 0.70));
      float rim = 0.40 + 0.60 * abs(dot(normal, light));
      vec3 gold = mix(vec3(0.58, 0.34, 0.10), vec3(1.0, 0.80, 0.40), rim);
      vColor = mix(gold, green, dye) * mix(1.0, 0.55, rear);
      vColor += vec3(0.18, 0.15, 0.08) * max(glint, attention * 0.65);
      vAlpha *= mix(0.52, 0.24, rear) * (0.7 + 0.3 * depth);
      vec4 mv = modelViewMatrix * vec4(p, 1.0);
      mv.xy += position.xy * mix(0.031, 0.024, rear);
      gl_Position = projectionMatrix * mv;
    #elif defined(TRAIL)
      // Three short ribbon sections follow 240ms of recent motion. Confirmation
      // shortens the history window until the filament has completely closed.
      float history = 0.24 * (1.0 - smoothstep(0.05, 0.72, hit));
      float age = (1.0 - uv.x) * history;
      float oldHit = previousHit(hit, age);
      float oldScan = state.w > -1.0 ? state.w - age * (1.32 / 3.6) : -2.0;
      vec3 old = inkPosition(uTime - age, oldHit, oldScan, attention, depth);
      vec3 offset = old - p;
      // Never span more than 30% of a glyph width, even during its forward lift.
      float distance = length(offset);
      old = p + offset * min(1.0, 0.108 / max(distance, 0.00001));
      vec4 headView = modelViewMatrix * vec4(p, 1.0);
      vec4 tailView = modelViewMatrix * vec4(old, 1.0);
      vec3 oldest = inkPosition(uTime - history, previousHit(hit, history),
        state.w > -1.0 ? state.w - history * (1.32 / 3.6) : -2.0, attention, depth);
      vec4 oldestView = modelViewMatrix * vec4(oldest, 1.0);
      vec2 direction = headView.xy - oldestView.xy;
      float travel = length(direction);
      direction /= max(travel, 0.00001);
      tailView.xy += vec2(-direction.y, direction.x) * position.y * 0.018 * mix(0.08, 1.0, uv.x);
      gl_Position = projectionMatrix * tailView;
      float energy = max(band, leading * 0.8) + gathering(hit) * uReveal;
      vColor = mix(vec3(1.0, 0.79, 0.30), green, dye);
      vAlpha *= min(1.0, energy) * smoothstep(0.001, 0.018, travel);
      vAlpha *= (1.0 - smoothstep(0.60, 0.95, hit)) * max(uActive, uReveal);
    #else
      vec4 mv = modelViewMatrix * vec4(p, 1.0);
      mv.xy += position.xy * 0.0255 * (1.0 + locked * 0.035 + band * 0.06);
      gl_Position = projectionMatrix * mv;
    #endif
  }
`;

export const particleFragment = `
  varying vec2 vUv;
  varying vec3 vColor;
  varying float vAlpha;
  uniform vec3 uPointer;
  void main() {
    #ifdef TRAIL
      float softEdge = 1.0 - smoothstep(0.18, 0.5, abs(vUv.y - 0.5));
      float alpha = softEdge * pow(vUv.x, 0.75) * vAlpha;
      vec3 color = vColor;
    #else
      vec2 bead = (vUv - 0.5) * 2.0;
      float radius = length(bead);
      float alpha = (1.0 - smoothstep(0.46, 1.0, radius)) * vAlpha;
      vec3 normal = vec3(bead, sqrt(max(0.0, 1.0 - dot(bead, bead))));
      vec3 light = normalize(vec3(-0.45 + uPointer.x * uPointer.z * 0.35, 0.65, 0.70));
      float lit = max(0.0, dot(normal, light));
      vec3 color = vColor * (0.90 + 0.16 * lit) + vec3(0.07, 0.055, 0.025) * pow(lit, 12.0);
    #endif
    if (alpha < 0.01) discard;
    gl_FragColor = vec4(color, alpha);
  }
`;
