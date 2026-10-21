import * as THREE from 'three';
import { asrPosition, infinityFrame, ringCoordinates, rowPhase, PATH_LENGTH } from './asr-layout.js';
import { RowVolume } from './row-volume.js';
import { SignalIntro } from './intro.js';
import { wallLayout } from './wall-layout.js';
import { LIVE, ASR_RIBBON, LiveLayout, liveOpacity } from './live-layout.js';
import { TIMES } from './core.js';
import { ANALYSIS, AnalysisLayout, analysisSurface } from './analysis-layout.js';

const ATTRIBUTES = { aStair: 3, aWall: 3, aWallFrame: 3, aRing: 4, aGlyph: 4, aMeta: 4, aAsr: 4, aLife: 3, aLive: 4, aHit: 1, aFocus: 1 };

export class LiveSubtitleField {
  constructor(owner, vertexShader, fragmentShader, quad) {
    this.owner = owner; this.shaders = { vertexShader, fragmentShader }; this.quad = quad;
    this.layout = new LiveLayout(Math.min(owner.options.maxCharacters ?? 4000, owner.options.instanceLimit ?? 6000), owner.options.asrRibbon);
    this.analysis = owner.options.analysisLayers ? new AnalysisLayout() : null;
    this.analysisRevision = -1;
    this.glyphs = new Map(); this.unused = new Map(); this.pages = []; this.dirty = new Set();
    this.clock = 0; this.coverage = 0; this.immediate = false; this.fontReady = false; this.disposed = false;
    owner.intro = new SignalIntro(owner.scene);
    owner.rowVolume = new RowVolume(owner.scene, this.layout.entries, owner.options.graphite);
    owner.asrSlots = this.layout.entries.length;
    owner.present = new Set(); owner.uniqueShown = owner.total = 0;
    this.structureDirty = false;
    let programReady;
    if (owner.renderer?.compileAsync) {
      // Compile the real glyph material on an empty instance pool during audio
      // preparation, before the first transcript batch needs it.
      this.createPage();
      programReady = owner.renderer.compileAsync(owner.scene, owner.camera);
    }
    // Font loading must not stop the particle silhouette or the page atmosphere.
    Promise.all([document.fonts.load('500 48px "Noto Experiment"'), programReady]).then(() => {
      if (!this.disposed) { this.fontReady = true; owner.options.onInvalidate?.(); }
    }).catch(() => {
      if (!this.disposed) { this.fontError = new Error('字幕渲染资源无法载入，后台任务不受影响。'); owner.options.onInvalidate?.(); }
    });
  }

  update(data, mappings, focus) {
    this.owner.data = data;
    this.owner.setMappings(mappings);
    this.layout.update(data, mappings, focus, this.clock, record => this.writeLifetime(record),
      this.analysisStarted ? ANALYSIS.maxCharacters : this.layout.limit);
  }

  createPage() {
    const atlas = document.createElement('canvas'); atlas.width = atlas.height = 768;
    const ctx = atlas.getContext('2d');
    ctx.fillStyle = '#fff'; ctx.font = '500 35px "Noto Experiment"'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    const texture = new THREE.CanvasTexture(atlas);
    texture.minFilter = texture.magFilter = THREE.LinearFilter; texture.generateMipmaps = false;
    const geometry = new THREE.InstancedBufferGeometry();
    for (const [name, attr] of Object.entries(this.quad.attributes)) geometry.setAttribute(name, attr);
    for (const [name, size] of Object.entries(ATTRIBUTES)) geometry.setAttribute(name,
      new THREE.InstancedBufferAttribute(new Float32Array(this.layout.limit * size), size).setUsage(THREE.DynamicDrawUsage));
    geometry.instanceCount = 0;
    const material = new THREE.ShaderMaterial({ ...this.shaders,
      uniforms: { uAtlas: { value: texture }, uVolume: { value: 1 }, uGraphite: { value: this.owner.options.graphite ? 1 : 0 }, uTime: { value: 0 }, uMotionTime: { value: 0 },
        uAsrElapsed: { value: 0 }, uTrailLag: { value: 0 }, uReduced: { value: 0 }, uDim: { value: 0 },
        uLiveMode: { value: 1 }, uLiveClock: { value: 0 }, uLiveImmediate: { value: 0 },
        uLiveRibbon: { value: this.owner.options.asrRibbon ? 1 : 0 },
        uAnalysisLayers: { value: this.analysis ? 1 : 0 }, uAnalysisScan: { value: 0 }, uScanTime: { value: 0 }, uParticleBlend: { value: 0 } },
      transparent: true, depthWrite: true, depthTest: true, blending: THREE.NormalBlending, side: THREE.DoubleSide,
    });
    const mesh = new THREE.Mesh(geometry, material); mesh.frustumCulled = false;
    const page = { atlas, ctx, texture, mesh, records: [], count: 0, low: Infinity, high: -1, names: new Set(), textureDirty: false };
    this.pages.push(page); this.owner.meshes.push(page); this.owner.scene.add(mesh);
    return page;
  }

  glyph(char) {
    let glyph = this.glyphs.get(char);
    if (glyph) { this.unused.delete(char); glyph.references++; return glyph; }
    let page, index;
    if (this.glyphs.size >= LIVE.maxGlyphs) {
      const evicted = this.unused.values().next().value;
      if (!evicted) throw new Error('字幕字形缓存已满，后台任务不受影响。');
      ({ page, index } = evicted); this.unused.delete(evicted.char); this.glyphs.delete(evicted.char);
    } else {
      page = this.pages.at(-1);
      if (!page || page.count === 256) page = this.createPage();
      index = page.count++;
    }
    const col = index % 16, row = Math.floor(index / 16);
    page.ctx.clearRect(col * 48, row * 48, 48, 48);
    page.ctx.fillText(char, col * 48 + 24, row * 48 + 25);
    page.textureDirty = true;
    glyph = { char, page, index, uv: [col / 16, 1 - (row + 1) / 16, 1 / 16, 1 / 16], references: 1 };
    this.glyphs.set(char, glyph);
    return glyph;
  }

  install(record) {
    record.atlas = this.glyph(record.char);
    const { page } = record.atlas, asr = record.asr;
    record.gpuIndex = page.records.length;
    record.copy = 0; record.birth = rowPhase(asr) * 9.1;
    record.seed = ((record.cell * 7919) % 997) / 997;
    record.size = Math.min(this.owner.options.asrRibbon ? ASR_RIBBON.fontSize : .36, 2.5 * asr.width / asr.length, PATH_LENGTH / asr.slots * .85);
    record.stair = asrPosition(record, asr, 0, 0, true);
    const cell = this.analysis?.cells.get(record.key);
    // Before analysis, keep an inert target at the ASR source position. The old
    // enclosure's column indices are not compatible with the five analysis layers.
    const surface = cell !== undefined ? analysisSurface(cell) : this.analysis
      ? { position: record.stair, yaw: 0, pitch: 0, column: 0, face: 'asr' }
      : wallLayout(record.cell, this.layout.limit);
    record.wall = surface.position; record.wallFace = surface.face;
    record.wallFrame = [surface.yaw, surface.pitch, surface.column];
    record.markers = this.owner.charToMarkers.get(record.key) ?? [];
    page.records.push(record); page.mesh.geometry.instanceCount = page.records.length;
    this.structureDirty = true;
    this.owner.present.add(record.key);
    this.write(record);
  }

  write(record) {
    if (!record.atlas) return;
    const { page } = record.atlas, i = record.gpuIndex, asr = record.asr;
    const frame = infinityFrame(rowPhase(asr));
    const values = {
      aStair: record.stair, aWall: record.wall, aWallFrame: record.wallFrame, aRing: ringCoordinates(asr), aGlyph: record.atlas.uv,
      aMeta: [record.birth, record.seed, 0, record.size], aAsr: [asr.slots, 0, ...frame.normal.slice(0, 2)],
      aLife: [-1, 1e6, 1], aLive: [record.arrival, record.departure, record.appear, record.disappear], aHit: [0], aFocus: [0],
    };
    for (const [name, value] of Object.entries(values)) page.mesh.geometry.attributes[name].array.set(value, i * ATTRIBUTES[name]);
    for (const name of Object.keys(ATTRIBUTES)) page.names.add(name);
    page.low = Math.min(page.low, i); page.high = Math.max(page.high, i); this.dirty.add(page);
  }

  writeLifetime(record) {
    const page = record.atlas.page, i = record.gpuIndex;
    page.mesh.geometry.attributes.aLive.array.set([record.arrival, record.departure, record.appear, record.disappear], i * 4);
    page.names.add('aLive'); page.low = Math.min(page.low, i); page.high = Math.max(page.high, i); this.dirty.add(page);
  }

  remove(record) {
    const glyph = record.atlas, page = glyph.page;
    if (--glyph.references === 0) this.unused.set(glyph.char, glyph);
    const last = page.records.pop();
    if (last !== record) { last.gpuIndex = record.gpuIndex; page.records[record.gpuIndex] = last; this.write(last); }
    page.mesh.geometry.instanceCount = page.records.length;
    this.structureDirty = true;
    this.owner.present.delete(record.key);
  }

  render(time, clock, reduced) {
    if (this.fontError) throw this.fontError;
    const delta = Math.min(.1, Math.max(0, clock - this.clock)); this.clock = clock;
    const immediate = reduced || this.immediate;
    if (this.analysis && time >= TIMES.build && !this.analysisStarted) {
      this.analysisStarted = true;
      this.layout.rowTransitions = false;
      // The source transcript stays intact. Only the decorative field becomes
      // less dense, preserving real evidence before recent background text.
      this.layout.update(this.owner.data, this.owner.mappings ?? [], this.owner.focusId, clock,
        record => this.writeLifetime(record), ANALYSIS.maxCharacters);
    }
    if (this.analysis && time >= TIMES.build && this.analysisRevision !== this.layout.revision) {
      this.analysis.sync([...this.layout.desired.values()]);
      this.analysisRevision = this.layout.revision;
      for (const record of this.layout.records.values()) {
        const cell = this.analysis.cells.get(record.key);
        if (cell === undefined) continue;
        const surface = analysisSurface(cell);
        record.wall = surface.position; record.wallFace = surface.face;
        record.wallFrame = [surface.yaw, surface.pitch, surface.column];
        this.write(record);
      }
    }
    if (this.fontReady) this.layout.advance(clock, immediate, {
      add: record => this.install(record), remove: record => this.remove(record),
    });
    for (const page of this.pages) {
      page.mesh.material.uniforms.uLiveClock.value = clock;
      page.mesh.material.uniforms.uLiveImmediate.value = immediate ? 1 : 0;
      if (page.textureDirty) { page.texture.needsUpdate = true; page.textureDirty = false; }
    }
    for (const page of this.dirty) {
      for (const name of page.names) {
        const size = ATTRIBUTES[name];
        const attr = page.mesh.geometry.attributes[name];
        attr.addUpdateRange(page.low * size, (page.high - page.low + 1) * size); attr.needsUpdate = true;
      }
      page.low = Infinity; page.high = -1;
      page.names.clear();
    }
    this.dirty.clear();
    if (this.structureDirty) { this.owner.records = this.pages.flatMap(p => p.records); this.structureDirty = false; }
    this.owner.uniqueShown = this.owner.total = this.owner.records.length;
    const occupancy = this.owner.total / this.layout.limit;
    this.coverage = immediate ? occupancy : this.coverage + (occupancy - this.coverage) * (1 - Math.exp(-delta * 3));
    this.owner.rowVolume.mesh.material.uniforms.uOpacity.value = (.24 + this.coverage * .65) * (this.owner.asrPresence ?? 1);
    // The planner clock must progress even while waiting for the local font.
    this.layout.clock = clock;
  }

  get pending() { return Boolean(this.fontError) || this.fontReady && this.layout.hasPending; }
  get transitioning() { return !this.immediate && this.layout.transitioning; }

  stats() {
    return { pending: this.layout.pending.size, revision: this.layout.revision, atlasPages: this.pages.length,
      glyphs: this.glyphs.size, gathering: this.owner.records.filter(r => this.clock < r.arrival + r.appear).length,
      dissolving: this.owner.records.filter(r => r.departure <= this.clock).length,
      opacity: this.owner.records.reduce((sum, r) => sum + liveOpacity(r, this.clock), 0), capacity: this.layout.limit };
  }

  dispose() {
    this.disposed = true;
    for (const page of this.pages) { page.mesh.removeFromParent(); page.mesh.geometry.dispose(); page.mesh.material.dispose(); page.texture.dispose(); }
    this.quad.dispose(); this.pages = []; this.glyphs.clear(); this.unused.clear(); this.dirty.clear();
    this.layout.records.clear(); this.layout.pending.clear(); this.owner.meshes = [];
  }
}
