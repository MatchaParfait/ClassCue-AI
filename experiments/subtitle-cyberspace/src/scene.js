import * as THREE from 'three';
import { chooseCharacters, LIMIT, smooth, TIMES } from './core.js';
import { createAsrLayout, asrPosition, ringCoordinates, infinityFrame, rowPhase, rowHeight, rowAxis, RIPPLE, RIPPLE_GLSL, REPLACEMENT_TINT_GLSL, replacementState, characterLifetime, asrElapsed, PATH_LENGTH, REPLACE_GROUP, REPLACE_DISSOLVE, REPLACE_APPEAR } from './asr-layout.js';
import { SignalIntro } from './intro.js';
import { RowVolume } from './row-volume.js';
import { wallLayout } from './wall-layout.js';
import { PreparingField } from './preparing-field.js';
import { LiveSubtitleField } from './live-field.js';
import { LIVE } from './live-layout.js';
import { ANALYSIS, ANALYSIS_PLANES } from './analysis-layout.js';
import { AnalysisActivity } from './analysis-activity.js';
import { ParticleText } from './particle-text.js';
import { GravitySubtitleScene } from './gravity-scene.js';

const vertexShader = /* glsl */`
  attribute vec3 aStair;
  attribute vec3 aWall;
  attribute vec3 aWallFrame;
  attribute vec4 aRing;
  attribute vec4 aGlyph;
  attribute vec4 aMeta;
  attribute vec4 aAsr;
  attribute vec3 aLife;
  attribute vec4 aLive;
  attribute vec2 tileCenter;
  attribute float reliefDepth;
  attribute float aHit;
  attribute float aFocus;
  uniform float uTime;
  uniform float uMotionTime;
  uniform float uAsrElapsed;
  uniform float uTrailLag;
  uniform float uReduced;
  uniform float uDim;
  uniform float uVolume;
  uniform float uGraphite;
  uniform float uLiveMode;
  uniform float uLiveClock;
  uniform float uLiveImmediate;
  uniform float uLiveRibbon;
  uniform float uAnalysisLayers;
  uniform float uAnalysisScan;
  uniform float uScanTime;
  uniform float uParticleBlend;
  varying vec2 vUv;
  varying vec3 vColor;
  varying float vAlpha;
  varying float vDissolve;
  varying vec2 vLocalUv;
  varying float vAnalysis;
  varying float vHit;
  varying float vDust;
  float ease(float v) { return smoothstep(0.0, 1.0, v); }
  ${RIPPLE_GLSL}
  ${REPLACEMENT_TINT_GLSL}
  void main() {
    float birth = aMeta.x;
    float seed = aMeta.y;
    float copy = aMeta.z;
    // ASR can still carry legacy wall columns. Never interpret those as an
    // unbounded layer delay: a negative denominator would morph before AI starts.
    float delay = mix((9.1 - birth) * 0.055, clamp(aWallFrame.z, 0.0, ${(ANALYSIS_PLANES.length - 1).toFixed(1)}) * 0.06, uAnalysisLayers);
    float morph = uTime < ${TIMES.build.toFixed(1)} ? 0.0 : ease((uTime - ${TIMES.build.toFixed(1)} - delay) / (2.4 - delay));
    if (uReduced > 0.5) morph = step(${TIMES.build.toFixed(1)}, uTime);
    float clock = max(0.0, uAsrElapsed - uTrailLag);
    vec2 normal = aAsr.zw;
    float ripple = ringRipple(aRing.x, clock) * aRing.w * ${RIPPLE.amplitude} * (1.0 - uReduced);
    vec3 p = aStair;
    vec3 axis = vec3(normal.x * 0.866025404, normal.y, -normal.x * 0.5);
    vec3 offset = axis * ripple;
    if (copy > 0.5) offset.xy *= 0.75;
    p += offset;
    // The flow clock freezes on completion, preserving the first frame of the wall transition.
    p = mix(p, aWall, morph);
    p.y += sin(morph * 3.14159265) * (1.5 + copy * 0.08) * (1.0 - uAnalysisLayers);
    p.z += aHit * morph * uAnalysisLayers * ${ANALYSIS.lift};
    float motionTime = uReduced > 0.5 ? 0.0 : uMotionTime;
    float column = aWallFrame.z;
    float wallMotion = morph * sin(motionTime * 0.65 + column) * 0.24 * (1.0 - uReduced) * (1.0 - uAnalysisLayers);
    p.y += wallMotion;
    float fault = uReduced > 0.5 ? 0.0 : pow(max(0.0, sin(motionTime * 0.6 + column * 1.71)), 40.0) * (1.0 - uAnalysisLayers);
    p.x += fault * 0.07 * morph;
    float size = mix(aMeta.w, mix(0.30, 0.36, uAnalysisLayers), morph);
    float occupied = step(aLife.x, clock) * (1.0 - step(aLife.y, clock));
    float dissolve = ease((clock - aLife.y + ${REPLACE_DISSOLVE.toFixed(1)}) / ${REPLACE_DISSOLVE.toFixed(1)});
    float appear = aAsr.y < 0.5 ? 1.0 : ease((clock - aLife.x) / ${REPLACE_APPEAR.toFixed(1)});
    if (uReduced > 0.5) { occupied = 1.0 - step(0.5, aAsr.y); dissolve = 0.0; appear = 1.0; }
    if (copy > 0.5) { occupied = 1.0; dissolve = 0.0; appear = 1.0; }
    if (uLiveMode > 0.5) {
      occupied = step(aLive.x, uLiveClock);
      appear = ease((uLiveClock - aLive.x) / aLive.z);
      dissolve = ease((uLiveClock - aLive.y) / aLive.w);
      if (uReduced > 0.5 || uLiveImmediate > 0.5) { occupied = 1.0; appear = 1.0; dissolve = 0.0; }
    }
    float gather = (1.0 - appear) * occupied;
    float disruption = (1.0 - morph) * max(dissolve, gather) * (1.0 - uReduced);
    float tilt = (1.0 - morph) * sin(aRing.y) * 0.22;
    float rotation = normal.y * normal.x * 0.2 * (1.0 - morph);
    vec2 glyph = mat2(cos(rotation), sin(rotation), -sin(rotation), cos(rotation)) * position.xy;
    vec3 corner = vec3(glyph.x * size, glyph.y * size * cos(tilt), -glyph.y * size * sin(tilt));
    float yaw = aWallFrame.x * morph;
    float pitch = aWallFrame.y * morph;
    corner.yz = mat2(cos(pitch), sin(pitch), -sin(pitch), cos(pitch)) * corner.yz;
    corner.xz = mat2(cos(yaw), -sin(yaw), sin(yaw), cos(yaw)) * corner.xz;
    float volume = (1.0 - morph) * (copy < 0.5 ? 1.0 : 0.0) * uVolume;
    corner += vec3(-0.5, -0.18, -0.866025404) * reliefDepth * size * 1.05 * volume;
    float fragmentSeed = fract(sin(dot(tileCenter + seed, vec2(17.13, 51.78))) * 43758.5);
    if (uLiveRibbon > 0.5 && morph < 0.001) {
      // Keep the glyph's scale and dense stroke layers. Only sparse outer
      // fragments travel a short distance along the local ribbon tangent.
      vec3 tangent = vec3(normal.y * 0.866025404, -normal.x, -normal.y * 0.5);
      float edge = step(0.28, max(abs(tileCenter.x - 0.5), abs(tileCenter.y - 0.5))) * step(0.35, fragmentSeed);
      float motion = (1.0 - uReduced) * (1.0 - uLiveImmediate);
      corner += tangent * size * (dissolve * (0.035 + edge * 0.28) - gather * (0.10 + edge * 0.08)) * motion;
      corner += axis * size * sin(fragmentSeed * 27.0) * max(dissolve, gather) * edge * 0.035 * motion;
    } else {
      corner *= 1.0 - disruption * 0.28;
      float radius = disruption * mix(0.55, 0.8, gather);
      corner.x += cos(fragmentSeed * 32.0) * radius;
      corner.y += sin(fragmentSeed * 27.0) * radius + (dissolve - gather) * disruption * 0.25;
      corner.z += sin(fragmentSeed * 13.0) * radius * 0.7;
    }
    corner.y *= 1.0 + fault * 2.8 * morph * (1.0 - uReduced);
    // Reuse the second glyph layer as sparse fragments of the actual strokes.
    // The first layer remains intact and is still the only hit-test surface.
    float dust = step(0.9, reliefDepth) * uAnalysisLayers * step(0.995, morph);
    float dustPhase = fract(uScanTime / 3.8 + seed + fragmentSeed * 0.28);
    vec3 dustOffset = vec3(cos(fragmentSeed * 24.0) * (0.12 + dustPhase * 0.65),
      0.12 + dustPhase * 1.05, 0.2 + dustPhase * 0.85);
    corner = mix(corner, corner * 0.55 + dustOffset, dust);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(p + corner, 1.0);
    vUv = aGlyph.xy + uv * aGlyph.zw;
    vLocalUv = uv;
    vAnalysis = morph * uAnalysisLayers;
    vHit = aHit;
    vDust = dust;
    vDissolve = (1.0 - morph) * max(dissolve, gather * 0.86) * (1.0 - uReduced);
    float crest = clamp(ripple / max(0.001, aRing.w * ${RIPPLE.amplitude}), 0.0, 1.0);
    vec3 silver = mix(vec3(0.72, 0.78, 0.76), vec3(0.42, 0.69, 0.67), crest * 0.65);
    vec3 cyan = mix(vec3(0.28, 0.91, 0.98), silver, uGraphite);
    vec3 red = mix(vec3(0.91, 0.075, 0.13), vec3(0.70, 0.20, 0.22), uGraphite);
    vec3 green = mix(vec3(0.12, 0.98, 0.46), vec3(0.38, 0.78, 0.51), uGraphite);
    vec2 cue = replacementTint(clock, aLife.x, aLife.y, aAsr.y) * occupied * (1.0 - uReduced) * (copy < 0.5 ? 1.0 : 0.0);
    if (uLiveMode > 0.5) {
      cue = vec2(ease((uLiveClock - aLive.y + 0.1) / 0.2) * (1.0 - dissolve),
        appear * (1.0 - ease((uLiveClock - aLive.x - 0.65) / 0.8))) * 0.26 * (1.0 - uReduced) * (1.0 - uLiveImmediate);
    }
    if (uLiveRibbon > 0.5) cue = vec2(0.0);
    vec3 outgoing = mix(vec3(0.94, 0.25, 0.30), vec3(0.77, 0.43, 0.42), uGraphite);
    vec3 incoming = mix(vec3(0.25, 0.92, 0.50), vec3(0.53, 0.76, 0.60), uGraphite);
    vec3 asrColor = mix(mix(cyan, outgoing, cue.x), incoming, cue.y);
    // A slow light pass signals activity only, never claims a model reading position.
    float scanX = -23.0 + mod(uScanTime, 10.0) * 4.6;
    float scan = exp(-pow((aWall.x + aWall.y * 0.24 - scanX) / 3.2, 2.0)) * uAnalysisScan;
    float depth = smoothstep(-33.0, -5.0, aWall.z);
    vec3 paper = mix(vec3(0.43, 0.48, 0.46), vec3(0.79, 0.82, 0.77), depth);
    paper = mix(paper, vec3(0.99, 0.85, 0.46), scan * 0.55);
    float shimmer = pow(0.5 + 0.5 * sin(uScanTime * 1.4 - aWall.x * 0.55 + aWall.y * 0.7), 5.0) * uAnalysisScan;
    paper = mix(paper, vec3(1.0, 0.93, 0.70), shimmer * 0.28);
    vColor = mix(mix(asrColor, mix(red, paper, uAnalysisLayers), morph), green, aHit);
    // Hold a readable gold phase before settling into green; never flash white.
    vec3 hitGold = vec3(1.0, 0.88, 0.48);
    vec3 hitGreen = vec3(0.52, 0.91, 0.58);
    vec3 hitColor = mix(hitGold, hitGreen, smoothstep(0.20, 0.95, aHit));
    vColor = mix(vColor, hitColor, smoothstep(0.0, 0.20, aHit) * morph * uAnalysisLayers);
    float surfaceLight = 0.52 + 0.48 * pow(max(0.0, cos(aRing.y + 0.22)), 2.0);
    float faceLight = reliefDepth > 0.0 ? 0.34 - reliefDepth * 0.12 : 1.0;
    float wallLight = 1.0 - abs(aWallFrame.x) * 0.10 - abs(aWallFrame.y) * 0.18;
    vColor *= mix(surfaceLight * faceLight, wallLight, morph);
    float wave = mix(0.97, mix(0.78 + 0.22 * sin(aStair.z * 0.35 + motionTime * 0.9),
      mix(0.38, 0.90, depth), uAnalysisLayers), morph);
    float born = uReduced > 0.5 ? step(${TIMES.recognize.toFixed(1)}, uTime) : ease((uTime - ${TIMES.recognize.toFixed(1)}) / 0.8);
    float visible = mix(0.68, 1.0, seed);
    visible = mix(visible, 1.0, morph * uAnalysisLayers);
    visible *= mix(copy > 0.5 ? 0.04 : aLife.z, 1.0, morph);
    born *= uLiveMode > 0.5 ? mix(occupied * appear, 1.0, morph) * (1.0 - dissolve)
      : mix(occupied * appear * (1.0 - dissolve), 1.0, morph);
    vAlpha = born * visible * wave * mix(1.0 - uDim * 0.35, 1.0, aHit);
    vAlpha = mix(vAlpha, born * 0.98, aHit * morph * uAnalysisLayers);
    vAlpha = min(1.0, vAlpha + aFocus * 0.35);
    if (reliefDepth > 0.0) vAlpha *= volume;
    if (dust > 0.5) {
      float sparse = step(0.64, seed) * step(0.73, fragmentSeed);
      vAlpha = sin(dustPhase * 3.14159265) * sparse * uAnalysisScan * (1.0 - aHit * 0.8) * 0.72;
      vColor = mix(vec3(0.72, 0.83, 0.88), vec3(1.0, 0.83, 0.43), fragmentSeed);
    }
    if (uTrailLag > 0.0) vAlpha *= (uTrailLag < 1.0 ? 0.19 : 0.08) * (1.0 - morph);
    vAlpha *= 1.0 - uParticleBlend;
  }
`;
const fragmentShader = /* glsl */`
  uniform sampler2D uAtlas;
  varying vec2 vUv;
  varying vec3 vColor;
  varying float vAlpha;
  varying float vDissolve;
  varying vec2 vLocalUv;
  varying float vAnalysis;
  varying float vHit;
  varying float vDust;
  void main() {
    float alpha = texture2D(uAtlas, vUv).a;
    if (vDissolve > 0.0) {
      vec2 cell = floor(vLocalUv * 14.0);
      float noise = fract(sin(dot(cell, vec2(12.9898, 78.233))) * 43758.5453);
      alpha *= smoothstep(vDissolve - 0.06, vDissolve + 0.06, noise);
    }
    // Underline and evidence wash share the glyph quad and atlas.
    float underline = smoothstep(0.07, 0.09, vLocalUv.y) * (1.0 - smoothstep(0.11, 0.13, vLocalUv.y));
    float wash = 0.10 * smoothstep(0.01, 0.06, vLocalUv.y) * (1.0 - smoothstep(0.92, 0.99, vLocalUv.y));
    alpha = max(alpha, max(underline * 0.8, wash) * vHit * vAnalysis * (1.0 - vDust));
    if (alpha * vAlpha < 0.01) discard;
    gl_FragColor = vec4(vColor, alpha * vAlpha);
  }
`;

function createGlyphQuad() {
  const quad = new THREE.BufferGeometry();
  const positions = [], uvs = [], centers = [], depths = [];
  for (let layer = 0; layer < 4; layer++) for (let y = 0; y < 4; y++) for (let x = 0; x < 4; x++) {
    for (const [dx, dy] of [[0, 0], [1, 0], [1, 1], [0, 0], [1, 1], [0, 1]]) {
      positions.push((x + dx) / 4 - 0.5, (y + dy) / 4 - 0.5, 0);
      uvs.push((x + dx) / 4, (y + dy) / 4);
      centers.push((x + 0.5) / 4, (y + 0.5) / 4);
      depths.push([0, 1, 1 / 3, 2 / 3][layer]);
    }
  }
  quad.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  quad.setAttribute('uv', new THREE.Float32BufferAttribute(uvs, 2));
  quad.setAttribute('tileCenter', new THREE.Float32BufferAttribute(centers, 2));
  quad.setAttribute('reliefDepth', new THREE.Float32BufferAttribute(depths, 1));
  return quad;
}

export class SubtitleScene {
  constructor(canvas, onSelect, onHover, options = {}) {
    this.options = options;
    this.disposed = false;
    this.canvas = canvas;
    this.onSelect = onSelect;
    this.onHover = onHover;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 130);
    this.camera.position.set(0, 2, 22);
    this.camera.lookAt(0, 0.5, -10);
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: options.transparent ?? false, powerPreference: 'low-power', preserveDrawingBuffer: options.preserveDrawingBuffer ?? true });
    this.renderer.setClearColor(0x060a0d, options.transparent ? 0 : 1);
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, options.pixelRatio ?? 1.5));
    this.meshes = [];
    this.trails = [];
    this.records = [];
    this.reduced = false;
    this.degraded = false;
    this.focusId = null;
    this.hoverId = null;
    this.time = 0;
    this.hits = new Map();
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(canvas.parentElement);
    this.move = (event) => {
      if (event.pointerType !== 'touch' && !event.buttons && !this.reduced && this.options.analysisQuality === 'high') {
        const rect = canvas.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) this.particlePointer = {
          x: (event.clientX - rect.left) / rect.width * 2 - 1,
          y: 1 - (event.clientY - rect.top) / rect.height * 2,
        };
      } else this.particlePointer = null;
      if (performance.now() - (this.lastPick ?? 0) < 65) return;
      this.lastPick = performance.now();
      this.hoverId = this.pick(event.clientX, event.clientY);
      canvas.style.cursor = this.hoverId !== null ? 'pointer' : 'default';
      onHover(this.hoverId);
    };
    this.click = (event) => { const id = this.pick(event.clientX, event.clientY); if (id !== null) onSelect(id); };
    this.leave = () => { this.hoverId = null; this.particlePointer = null; onHover(null); };
    canvas.addEventListener('pointermove', this.move);
    canvas.addEventListener('click', this.click);
    canvas.addEventListener('pointerleave', this.leave);
    this.resize();
  }

  captureFrame() {
    if (this.disposed || this.gravityActive || !this.renderer) return false;
    this.renderer.render(this.scene, this.camera);
    return true;
  }

  resize() {
    const { width, height } = this.canvas.parentElement.getBoundingClientRect();
    if (this.disposed || width <= 0 || height <= 0) return;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.baseZoom = Math.min(this.options.viewScale ?? 1, Math.max(1, this.camera.aspect / (this.options.fitAspect ?? 1.85)));
    this.camera.zoom = this.baseZoom;
    if (this.options.compositionOffset) this.camera.setViewOffset(width, height, width * (this.options.asrRibbon ? 0 : this.options.compositionOffset), this.options.asrRibbon ? -height * .04 : 0, width, height);
    // Fit the wall into narrow PC windows without resizing individual glyphs.
    this.cameraDistance = 27 + Math.max(0, 1.38 - this.camera.aspect) * 28;
    this.camera.position.set(0, 2, this.cameraDistance);
    this.camera.lookAt(0, 2, -15);
    this.camera.updateProjectionMatrix();
    this.fitComposition(this.time, this.reduced);
    this.fitPreparingField();
    this.options.onInvalidate?.();
  }

  fitPreparingField() {
    if (!this.preparingField) return;
    // The near lobe needs extra horizontal margin in narrower desktop windows.
    const scale = Math.min(this.options.preparingScale ?? 1, Math.max(1, this.camera.aspect * .9));
    this.preparingField.mesh.scale.setScalar(scale);
    this.preparingField.mesh.position.set(0, this.options.preparingScale ? .8 : 0, 15 * (scale - 1));
  }

  fitComposition(time, reduced) {
    if (!this.options.wallScale && !this.userView) return;
    // The page overlays only part of the ASR ring. Ease back enough to keep
    // both enclosure sides visible when those same glyphs become the wall.
    const morph = reduced ? Number(time >= TIMES.build) : smooth((time - TIMES.build) / TIMES.morph);
    const zoom = this.baseZoom * (this.options.asrRibbon ? .80 : 1) * (1 + ((this.options.wallScale ?? 1) - 1) * morph) * (this.entranceScale ?? 1) * (this.userView?.zoom ?? 1);
    if (this.camera.zoom !== zoom) {
      this.camera.zoom = zoom;
      this.camera.updateProjectionMatrix();
    }
    if (this.userView) {
      const { yaw, pitch } = this.userView;
      const radius = this.cameraDistance + 15;
      this.camera.position.set(Math.sin(yaw) * Math.cos(pitch) * radius, 2 + Math.sin(pitch) * radius,
        -15 + Math.cos(yaw) * Math.cos(pitch) * radius);
      this.camera.lookAt(0, 2, -15);
      this.camera.updateMatrixWorld();
    }
  }

  async load(data, mappings, focus = null) {
    this.gravityData = data;
    if (this.options.streaming) {
      if (!this.live) this.live = new LiveSubtitleField(this, vertexShader, fragmentShader, createGlyphQuad());
      this.live.update(data, mappings, focus);
      return;
    }
    await document.fonts.load('500 48px "Noto Experiment"');
    if (this.disposed) return;
    this.clear();
    this.intro = new SignalIntro(this.scene);
    this.data = data;
    this.mappings = mappings;
    this.charToMarkers = new Map();
    mappings.forEach((m) => m.keys.forEach((key) => {
      const matches = this.charToMarkers.get(key) ?? [];
      if (!matches.includes(m.id)) matches.push(m.id);
      this.charToMarkers.set(key, matches);
    }));
    const chars = chooseCharacters(data, mappings, focus, this.options.maxCharacters ?? LIMIT);
    if (!chars.length) {
      this.preparingField = new PreparingField(this.scene);
      this.fitPreparingField();
      this.total = this.uniqueShown = 0;
      this.present = new Set();
      return;
    }
    const unique = [...new Set(chars.map((c) => c.char))];
    const atlasMap = new Map();
    const textures = [];
    const cell = 48, cells = 32;
    for (let first = 0; first < unique.length; first += cells * cells) {
      const atlas = document.createElement('canvas');
      atlas.width = atlas.height = cell * cells;
      const ctx = atlas.getContext('2d');
      ctx.fillStyle = '#ffffff';
      ctx.font = '500 35px "Noto Experiment"';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      unique.slice(first, first + cells * cells).forEach((char, index) => {
        const col = index % cells, row = Math.floor(index / cells);
        ctx.fillText(char, col * cell + cell / 2, row * cell + cell / 2 + 1);
        atlasMap.set(char, { page: textures.length, uv: [col / cells, 1 - (row + 1) / cells, 1 / cells, 1 / cells] });
      });
      const texture = new THREE.CanvasTexture(atlas);
      texture.minFilter = THREE.LinearFilter;
      texture.magFilter = THREE.LinearFilter;
      texture.generateMipmaps = false;
      textures.push(texture);
    }
    const budget = this.options.instanceLimit ?? LIMIT;
    const trailLayers = Math.min(this.options.trailLayers ?? 2, Math.max(0, Math.floor(budget / chars.length) - 1));
    const copies = Math.max(1, Math.min(18, Math.floor(budget / chars.length) - trailLayers));
    const total = Math.min(budget, chars.length * copies);
    const groups = textures.map(() => []);
    const segments = [...new Set(chars.map((c) => c.segment))];
    const { layout, slots, entries } = createAsrLayout(chars, data);
    this.asrSlots = slots;
    this.rowVolume = new RowVolume(this.scene, entries, this.options.graphite);
    const segmentPosition = new Map(segments.map((s, i) => [s, i]));
    for (let copy = 0; copy < copies; copy++) {
      chars.forEach((char, ci) => {
        const slot = copy * chars.length + ci;
        if (slot >= total) return;
        const segment = segmentPosition.get(char.segment);
        const fraction = segment / Math.max(1, segments.length - 1);
        const birth = fraction * 9.1;
        const asr = layout.get(char.key);
        const stair = asrPosition(char, asr, copy, 0, true);
        const surface = wallLayout(slot, total);
        const wall = surface.position;
        const record = {
          ...char, copy, birth, stair, wall, asr,
          wallFrame: [surface.yaw, surface.pitch, surface.column], wallFace: surface.face,
          seed: ((slot * 7919) % 997) / 997, size: copy === 0 ? Math.min(0.36, 2.5 * asr.width / Math.max(1, asr.length), PATH_LENGTH * asr.scale / asr.slots * 0.85) : 0.19,
          atlas: atlasMap.get(char.char), markers: this.charToMarkers.get(char.key) ?? []
        };
        groups[record.atlas.page].push(record);
        this.records.push(record);
      });
    }
    // Independent glyph tiles separate locally during dissolution; no sentence-level travel.
    const quad = createGlyphQuad();
    groups.forEach((records, page) => {
      // Keep all primary identities first; thin background copies across all faces, not one whole wall.
      records.sort((a, b) => a.copy === 0 ? (b.copy === 0 ? 0 : -1) : b.copy === 0 ? 1 : a.seed - b.seed);
      const geometry = new THREE.InstancedBufferGeometry();
      geometry.setAttribute('position', quad.attributes.position.clone());
      geometry.setAttribute('uv', quad.attributes.uv.clone());
      geometry.setAttribute('tileCenter', quad.attributes.tileCenter.clone());
      geometry.setAttribute('reliefDepth', quad.attributes.reliefDepth.clone());
      const attribute = (name, size, values) => geometry.setAttribute(name, new THREE.InstancedBufferAttribute(new Float32Array(values), size));
      attribute('aStair', 3, records.flatMap((r) => r.stair));
      attribute('aWall', 3, records.flatMap((r) => r.wall));
      attribute('aWallFrame', 3, records.flatMap((r) => r.wallFrame));
      attribute('aRing', 4, records.flatMap((r) => ringCoordinates(r.asr)));
      attribute('aGlyph', 4, records.flatMap((r) => r.atlas.uv));
      attribute('aMeta', 4, records.flatMap((r) => [r.birth, r.seed, r.copy, r.size]));
      attribute('aAsr', 4, records.flatMap((r) => [r.asr.slots, r.asr.group, ...infinityFrame(rowPhase(r.asr)).normal.slice(0, 2)]));
      attribute('aLife', 3, records.flatMap((r) => {
        const center = infinityFrame(rowPhase(r.asr)).center;
        const life = characterLifetime(r.asr);
        return [life.arrival, life.departure, center[2] < 0 ? 0.2 + 0.8 * smooth((Math.abs(center[0]) - 0.4) / 2.2) : 1];
      }));
      attribute('aHit', 1, records.map(() => 0));
      attribute('aLive', 4, records.flatMap(() => [-1, 1e6, LIVE.appear, LIVE.disappear]));
      attribute('aFocus', 1, records.map(() => 0));
      geometry.instanceCount = records.length;
      const material = new THREE.ShaderMaterial({
        vertexShader, fragmentShader,
        uniforms: { uAtlas: { value: textures[page] }, uVolume: { value: 1 }, uGraphite: { value: this.options.graphite ? 1 : 0 }, uTime: { value: 0 }, uMotionTime: { value: 0 }, uAsrElapsed: { value: 0 }, uTrailLag: { value: 0 }, uReduced: { value: 0 }, uDim: { value: 0 }, uLiveMode: { value: 0 }, uLiveClock: { value: 0 }, uLiveImmediate: { value: 0 } },
        transparent: true, depthWrite: true, depthTest: true, blending: THREE.NormalBlending, side: THREE.DoubleSide
      });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.frustumCulled = false;
      this.scene.add(mesh);
      this.meshes.push({ mesh, records });
      for (let trail = 0; trail < trailLayers; trail++) {
        const trailGeometry = new THREE.InstancedBufferGeometry();
        for (const [name, attr] of Object.entries(geometry.attributes)) trailGeometry.setAttribute(name, attr);
        trailGeometry.instanceCount = records.filter((r) => r.copy === 0).length;
        trailGeometry.setDrawRange(0, 96);
        const trailMaterial = material.clone();
        trailMaterial.depthWrite = false;
        trailMaterial.uniforms = { ...material.uniforms, uTrailLag: { value: (trail + 1) * 0.8 } };
        const trailMesh = new THREE.Mesh(trailGeometry, trailMaterial); trailMesh.frustumCulled = false;
        this.scene.add(trailMesh); this.trails.push(trailMesh);
      }
    });
    quad.dispose();
    this.textures = textures;
    this.total = total;
    this.uniqueShown = chars.length;
    this.present = new Set(chars.map((c) => c.key));
  }

  setMappings(mappings) {
    this.mappings = mappings;
    this.charToMarkers = new Map();
    for (const mapping of mappings) for (const key of mapping.keys) {
      const ids = this.charToMarkers.get(key) ?? [];
      ids.push(mapping.id); this.charToMarkers.set(key, ids);
    }
    for (const record of this.records) record.markers = this.charToMarkers.get(record.key) ?? [];
  }

  render(time, hits, reduced = false, settledAt = Infinity, ambient = time, hold = false, flowTime = null) {
    this.time = time;
    this.fitComposition(time, reduced);
    this.live?.render(time, ambient, reduced);
    this.asrFloat = this.options.asrRibbon && time < TIMES.build && !reduced ? Math.sin(ambient * .48) * .14 : 0;
    for (const { mesh } of this.meshes) mesh.position.y = this.asrFloat;
    if (this.rowVolume) this.rowVolume.mesh.position.y = this.asrFloat;
    const after = Math.max(0, time - settledAt);
    // Integrate a decaying speed so completion never changes the current phase abruptly.
    this.motionTime = hold ? ambient : Math.min(time, settledAt) + after * 0.35 + 0.65 * (1 - Math.exp(-after));
    this.asrClock = flowTime ?? asrElapsed(time, ambient, hold);
    this.reduced = reduced;
    this.hits = hits;
    this.gravityActive = ['gravity', 'nailong', 'dafeiyu', 'dafeiyu31', 'dafeiyu-rice', 'gpt'].includes(this.options.analysisVariant) && this.options.analysisQuality === 'high'
      && time >= TIMES.build && this.live?.fontReady && !this.gravityFailure;
    if (this.gravityActive) {
      try {
        this.gravity ??= new GravitySubtitleScene({ variant: this.options.analysisVariant,
          onInvalidate: this.options.onInvalidate, onAssetError: this.options.onAssetError });
        this.gravity.render(this.renderer, { data: this.gravityData, mappings: this.mappings ?? [], hits,
          focus: this.focusId ?? this.hoverId, ambient, reduced, active: this.analysisScanning,
          pointer: this.particlePointer, view: this.userView, aspect: this.camera.aspect,
          journey: this.gravityJourneyRequest, motionDelta: this.gravityMotionDelta, arrival: this.analysisArrival ?? 1 });
        this.analysisParticlesActive = this.analysisScanning && !reduced;
        return;
      } catch (error) {
        this.gravity?.dispose(); this.gravity = null; this.gravityFailure = true; this.gravityActive = false;
        this.options.onGravityFallback?.(error);
      }
    } else if (this.gravity) { this.gravity.dispose(); this.gravity = null; }
    this.intro?.render(time, reduced);
    this.preparingField?.render(time, this.asrClock, reduced);
    this.rowVolume?.render(time, this.asrClock, reduced);
    const high = this.options.analysisQuality === 'high' && !['gravity', 'nailong', 'dafeiyu', 'dafeiyu31', 'dafeiyu-rice', 'gpt'].includes(this.options.analysisVariant) && !this.degraded && !this.particleFailure;
    let particleBlend = 0;
    if (high && time >= TIMES.build + TIMES.morph && this.live?.fontReady) {
      try {
        this.particleText ??= new ParticleText(this.scene);
        this.particleEnteredAt ??= ambient;
        particleBlend = reduced || !this.analysisScanning ? 1 : smooth((ambient - this.particleEnteredAt) / .2);
        this.particleText.update(this.records, hits, this.focusId ?? this.hoverId, ambient, this.analysisScanning && !reduced,
          particleBlend, this.particlePointer, this.camera.aspect, !reduced && (this.analysisRevealing || this.analysisScanning));
        if (!this.particleText.count) particleBlend = 0;
      } catch {
        this.particleText?.dispose(); this.particleText = null; this.particleFailure = true;
        particleBlend = 0; this.options.onParticleFallback?.();
      }
    } else if (this.particleText) {
      this.particleText.dispose(); this.particleText = null; this.particleEnteredAt = null;
    }
    this.trails.forEach((mesh) => { mesh.visible = !reduced && !this.degraded && time >= TIMES.recognize && time < TIMES.build + TIMES.morph; });
    this.analysisParticlesActive = Boolean(this.options.analysisLayers && this.analysisScanning && !reduced && !this.degraded && time >= TIMES.build + TIMES.morph);
    for (const { mesh, records } of this.meshes) {
      mesh.visible = time >= TIMES.recognize;
      mesh.geometry.setDrawRange(0, time < TIMES.build + TIMES.morph ? (this.degraded ? 192 : 384) : this.analysisParticlesActive && !high ? 192 : 96);
      const uniforms = mesh.material.uniforms;
      uniforms.uTime.value = time;
      uniforms.uMotionTime.value = this.motionTime;
      uniforms.uAsrElapsed.value = this.asrClock;
      uniforms.uReduced.value = reduced ? 1 : 0;
      uniforms.uDim.value = hits.size ? 1 : 0;
      if (uniforms.uAnalysisLayers) {
        uniforms.uParticleBlend.value = particleBlend;
        uniforms.uAnalysisScan.value = this.analysisScanning && !reduced ? 1 : 0;
        uniforms.uScanTime.value = ambient;
        mesh.material.depthWrite = !(this.options.analysisLayers && time >= TIMES.build);
      }
      const hit = mesh.geometry.attributes.aHit;
      const focus = mesh.geometry.attributes.aFocus;
      records.forEach((record, i) => {
        hit.array[i] = this.live && record.departure < 1e6 ? 0 : hits.get(record.key) ?? 0;
        focus.array[i] = record.markers.some((id) => String(id) === String(this.focusId ?? this.hoverId)) && hit.array[i] > 0 ? 1 : 0;
      });
      hit.needsUpdate = focus.needsUpdate = true;
    }
    if (this.options.analysisLayers && time >= TIMES.build) {
      this.analysisActivity ??= new AnalysisActivity(this.scene);
      this.analysisActivity.update(this.records, hits, ambient, this.analysisScanning && !reduced,
        reduced ? 1 : smooth((time - TIMES.build - 1.2) / 1.2), !high);
    }
    this.renderer.render(this.scene, this.camera);
  }

  positionOf(record) {
    const layered = this.options.analysisLayers;
    const delay = layered ? Math.min(ANALYSIS_PLANES.length - 1, Math.max(0, record.wallFrame[2])) * .06 : (9.1 - record.birth) * 0.055;
    const morph = this.time < TIMES.build ? 0 : this.reduced ? 1 : smooth((this.time - TIMES.build - delay) / (2.4 - delay));
    const p = new THREE.Vector3(...(record.asr ? asrPosition(record, record.asr, record.copy, this.asrClock ?? 0, this.reduced) : record.stair));
    p.lerp(new THREE.Vector3(...record.wall), morph);
    p.y += this.asrFloat ?? 0;
    if (layered) {
      p.z += (record.departure < 1e6 ? 0 : this.hits.get(record.key) ?? 0) * morph * ANALYSIS.lift;
      return p;
    }
    p.y += Math.sin(morph * Math.PI) * (1.5 + record.copy * 0.08);
    const t = this.reduced ? 0 : (this.motionTime ?? this.time);
    p.y += morph * Math.sin(t * 0.65 + (record.wallFrame?.[2] ?? Math.floor(record.wall[0] * 5))) * 0.24 * (this.reduced ? 0 : 1);
    return p;
  }

  asrSnapshot() {
    const visible = this.records.filter((r) => r.copy === 0 && replacementState(r.asr, this.asrClock ?? 0, this.reduced).active);
    const rows = [...new Map(visible.map((r) => [r.asr.slot, r.asr])).values()];
    const strengths = rows.map((info) => Math.abs(rowHeight(info, this.asrClock ?? 0, this.reduced)) / (info.width * RIPPLE.amplitude));
    return { slots: this.asrSlots, primaryCharacters: visible.length, primaryKeys: visible.map((r) => r.key),
      gathering: visible.filter((r) => replacementState(r.asr, this.asrClock ?? 0, this.reduced).gather > 0).length,
      dissolving: visible.filter((r) => replacementState(r.asr, this.asrClock ?? 0, this.reduced).dissolve > 0).length,
      exchanging: false,
      replacementGroupSize: REPLACE_GROUP,
      replacementCharacters: this.records.filter((r) => r.copy === 0 && r.asr.batch === 0).map((r) => ({ key: r.key, slot: r.asr.slot, lane: r.asr.lane, index: r.asr.index, group: r.asr.group, ...replacementState(r.asr, this.asrClock ?? 0, this.reduced) })),
      replacementRegions: [...new Map(this.records.filter((r) => r.copy === 0 && r.asr.group > 0 && r.asr.batch === 0).map((r) => [r.asr.slot, { slot: r.asr.slot, lane: r.asr.lane, center: infinityFrame(rowPhase(r.asr)).center }])).values()],
      activeReplacementRows: [...new Set(visible.filter((r) => { const state = replacementState(r.asr, this.asrClock ?? 0, this.reduced); return state.gather > 0 || state.dissolve > 0; }).map((r) => r.asr.slot))],
      rowHeights: [...new Map(visible.map((r) => [r.asr.slot, rowHeight(r.asr, this.asrClock ?? 0, this.reduced)])).entries()],
      rowMotion: rows.map((info, i) => ({ phase: rowPhase(info), strength: strengths[i] })).sort((a, b) => a.phase - b.phase),
      motionDirection: 'local-axis',
      activeWaveRows: strengths.filter((s) => s > 0.35).length,
      quietWaveRows: strengths.filter((s) => s < 0.22).length,
      volumeLayers: this.time < TIMES.build + TIMES.morph ? (this.degraded ? 2 : 4) : 1,
      volumeRibs: this.rowVolume?.mesh.visible ? this.rowVolume.mesh.geometry.instanceCount : 0,
      volumeStyle: 'particle-fibers',
      trails: this.trails.filter((m) => m.visible).length,
      renderedInstances: this.total + this.trails.filter((m) => m.visible).reduce((n, m) => n + m.geometry.instanceCount, 0),
      primarySentences: [...new Set(visible.map((r) => r.subtitleId))],
      nearestDuplicateZ: Math.max(-Infinity, ...this.records.filter((r) => r.copy > 0).map((r) => r.stair[2])),
      shape: 'infinity', tiltDegrees: 30,
      samplePositions: visible.slice(0, 8).map((r) => this.positionOf(r).toArray()),
      sampleAxes: visible.slice(0, 8).map((r) => rowAxis(r.asr)),
      bounds: visible.reduce((b, r) => {
        const p = this.positionOf(r).project(this.camera);
        return { left: Math.min(b.left, p.x), right: Math.max(b.right, p.x), top: Math.max(b.top, p.y), bottom: Math.min(b.bottom, p.y) };
      }, { left: 1, right: -1, top: -1, bottom: 1 }) };
  }

  currentAsrSubtitle() {
    const active = this.records.filter((r) => r.copy === 0 && r.index === 0 && replacementState(r.asr, this.asrClock ?? 0, this.reduced).active);
    const latest = active.reduce((best, r) => !best || r.asr.arrival > best.asr.arrival ? r : best, null);
    return latest ? this.data.subtitles[latest.segment] : null;
  }

  wallSnapshot() {
    const visible = this.meshes.flatMap(({ mesh, records }) => records.slice(0, mesh.geometry.instanceCount));
    return Object.fromEntries(['left', 'back', 'right', 'floor'].map((face) => {
      const records = visible.filter((r) => r.wallFace === face);
      const projected = records.map((r) => this.positionOf(r).project(this.camera));
      return [face, { count: records.length, hits: records.filter((r) => (this.hits.get(r.key) ?? 0) > 0.5).length,
        bounds: { left: Math.min(...projected.map((p) => p.x)), right: Math.max(...projected.map((p) => p.x)),
          top: Math.max(...projected.map((p) => p.y)), bottom: Math.min(...projected.map((p) => p.y)) } }];
    }));
  }

  pointForMarker(id, face = null) {
    const rect = this.canvas.getBoundingClientRect();
    if (this.gravityActive) return this.gravity.pointForMarker(id, rect);
    const visible = this.meshes.flatMap(({ mesh, records }) => records.slice(0, mesh.geometry.instanceCount));
    for (const record of visible) {
      if (face && record.wallFace !== face) continue;
      if (!record.markers.some((marker) => String(marker) === String(id)) || (this.hits.get(record.key) ?? 0) <= 0) continue;
      const p = this.positionOf(record).project(this.camera);
      const point = { x: rect.left + (p.x + 1) * rect.width / 2, y: rect.top + (1 - p.y) * rect.height / 2 };
      if (point.x > 40 && point.x < rect.width - 40 && point.y > 180 && point.y < rect.height - 180) return point;
    }
    return null;
  }

  pick(x, y) {
    if (this.gravityActive) return this.gravity.pick(x, y, this.canvas.getBoundingClientRect());
    if (!this.hits.size) return null;
    const rect = this.canvas.getBoundingClientRect();
    let best = 12 * 12, found = null;
    for (const { records, mesh } of this.meshes) {
      for (let i = 0; i < mesh.geometry.instanceCount; i++) {
        const record = records[i];
        if (this.live && record.departure < 1e6) continue;
        if (!record.markers.length || (this.hits.get(record.key) ?? 0) <= 0) continue;
        const p = this.positionOf(record).project(this.camera);
        if (p.z < -1 || p.z > 1) continue;
        const distance = (rect.left + (p.x + 1) * rect.width / 2 - x) ** 2 + (rect.top + (1 - p.y) * rect.height / 2 - y) ** 2;
        if (distance < best) { best = distance; found = record.markers[0]; }
      }
    }
    return found;
  }

  degrade() {
    if (this.degraded) return;
    this.degraded = true;
    this.gravity?.reduceQuality();
    this.trails.forEach((m) => { m.visible = false; });
    for (const { mesh, records } of this.meshes) {
      // Primary characters (including every represented hit) precede decorative copies.
      const primary = records.filter((r) => r.copy === 0).length;
      mesh.geometry.instanceCount = Math.max(primary, Math.floor(records.length * 0.6));
      mesh.geometry.setDrawRange(0, this.time < TIMES.build + TIMES.morph ? 192 : 96);
    }
    this.renderer.setPixelRatio(1);
  }

  clear() {
    this.gravity?.dispose(); this.gravity = null; this.gravityActive = false;
    this.particleText?.dispose(); this.particleText = null; this.particleEnteredAt = null;
    this.analysisActivity?.dispose(); this.analysisActivity = null;
    this.live?.dispose(); this.live = null;
    this.preparingField?.dispose(); this.preparingField = null;
    this.intro?.dispose(); this.intro = null;
    this.rowVolume?.dispose(); this.rowVolume = null;
    for (const mesh of this.trails ?? []) { mesh.removeFromParent(); mesh.geometry.dispose(); mesh.material.dispose(); }
    this.trails = [];
    for (const { mesh } of this.meshes) { this.scene.remove(mesh); mesh.geometry.dispose(); mesh.material.dispose(); }
    for (const texture of this.textures ?? []) texture.dispose();
    this.meshes = [];
    this.records = [];
    this.textures = [];
    this.degraded = false;
  }

  dispose() {
    this.disposed = true;
    this.clear(); this.observer.disconnect(); this.renderer.dispose();
    this.canvas.removeEventListener('pointermove', this.move);
    this.canvas.removeEventListener('click', this.click);
    this.canvas.removeEventListener('pointerleave', this.leave);
  }
}
