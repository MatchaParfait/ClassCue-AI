import { SubtitleScene } from '../../../experiments/subtitle-cyberspace/src/scene.js';
import { Timeline, mapMarkers, smooth, TIMES, WALL_TIME } from '../../../experiments/subtitle-cyberspace/src/core.js';
import { TaskAurora } from './aurora.js';
import { CharacterHighlights } from './highlights.js';
import { TaskViewControls } from './view-controls.js';
import { EvidenceJourney } from './evidence-journey.js';
import { StageHandoff } from './stage-handoff.js';
import { AsrStage, TranscriptArrivals } from './asr-stage.js';
import { AsrPreparation } from './asr-preparation.js';

const clocks = new Map();
const stationary = new Set(['waiting', 'paused', 'failed', 'stopped']);

// The job owns all stage transitions. No demo clock or simulated results run here.
export function createTaskSpace(canvas, { key, onSelect, onError, onQuality, onReady, onEvidence, analysisQuality = 'low', analysisVariant = 'gravity', analysisPlaybackRate = 1 }) {
  let disposed = false, loading = false, frame = 0, last = 0, ready = false;
  let requested = null, loadedText = null, loadedMarkers = null, mappings = [];
  let reduced = false, focus = null, lastDraw = 0, slow = 0;
  let lastEvidence = '';
  const timeline = new Timeline();
  const highlights = new CharacterHighlights({ wave: analysisQuality === 'high' });
  timeline.manual = true; timeline.hold = true;
  timeline.finishRate = 4; // 2.4 scene seconds = a 600 ms analysis entrance.
  const previous = clocks.get(key);
  const journeys = new EvidenceJourney(previous?.evidenceSeen);
  const arrivals = new TranscriptArrivals(previous?.arrivals);
  const gravityEnabled = analysisQuality === 'high' && ['gravity', 'nailong', 'dafeiyu', 'dafeiyu31', 'dafeiyu-rice', 'gpt'].includes(analysisVariant);
  const handoff = new StageHandoff(canvas, { high: gravityEnabled, variant: analysisVariant, seen: previous?.analysisEntered ?? false });
  let presentedPhase = null;
  const playbackRate = Number.isFinite(analysisPlaybackRate) ? Math.max(.1, Math.min(1, analysisPlaybackRate)) : 1;
  if (previous) Object.assign(timeline, previous);
  // Production enters the live ring directly; the standalone demo keeps its flowchart intro.
  timeline.time = Math.max(timeline.time, TIMES.space);
  let entrance = null, entranceElapsed = 0, announced = false;
  const scene = new SubtitleScene(canvas, (id) => { onSelect(id); invalidate(); }, () => invalidate(), {
    preserveDrawingBuffer: false, transparent: true, graphite: true, preparingScale: 1.15, viewScale: 1.85, fitAspect: 1.2,
    compositionOffset: .09, wallScale: .88, streaming: true, analysisLayers: true, pixelRatio: 1, maxCharacters: 4000, instanceLimit: 6000, trailLayers: 0,
    analysisQuality, analysisVariant, asrRibbon: gravityEnabled, onParticleFallback: () => onQuality('粒子显示不可用，已切回轻量版'),
    onGravityFallback: () => onQuality('引力字幕显示不可用，已切回轻量版'),
    onAssetError: onQuality,
    onInvalidate: () => invalidate(),
  });
  const aurora = new TaskAurora(scene.scene, analysisVariant);
  const stage = gravityEnabled ? new AsrStage(scene.scene, analysisVariant) : null;
  const preparation = gravityEnabled ? new AsrPreparation(scene.scene, { done: previous?.preparationDone,
    invalidate, onError: onQuality }) : null;
  const view = new TaskViewControls(canvas, invalidate, () => ready && !disposed && !document.hidden && (!entrance || entranceElapsed >= 1.05));

  function remember() {
    clocks.set(key, { time: timeline.time, flowTime: timeline.flowTime, ambientTime: timeline.ambientTime, hold: timeline.hold, evidenceSeen: journeys.seen, analysisEntered: handoff.seen, arrivals: arrivals.snapshot(), preparationDone: preparation?.lifecycle.state === 'done' });
    if (clocks.size > 12) clocks.delete(clocks.keys().next().value);
  }
  function animate() {
    if (!requested || requested.paused || reduced || document.hidden || stationary.has(requested.phase)) return false;
    return requested.phase !== 'completed' || highlights.pending(timeline.time) || scene.live?.transitioning;
  }
  function invalidate() {
    if (!disposed && !document.hidden && !frame) frame = requestAnimationFrame(tick);
  }
  function journeyAnimating() {
    return gravityEnabled && (scene.gravity?.presentationPending ?? scene.gravity?.journeyPending) && !reduced && !requested?.paused && !stationary.has(requested?.phase);
  }
  function fail(error) {
    if (disposed) return;
    onError(error instanceof Error ? error.message : String(error));
    dispose();
  }
  function tick(now) {
    frame = 0;
    if (disposed || document.hidden || !ready) return;
    const interval = 1000 / (gravityEnabled && ['analyzing', 'completed'].includes(requested.phase) ? 60 : 30);
    // Preserve the fractional frame remainder. Resetting the budget to each
    // actual draw quantized a 30 Hz target to irregular 33/50 ms on 60 Hz screens.
    if (lastDraw && (animate() || journeyAnimating() || scene.live?.pending || view.pending)
      && now - lastDraw < interval - .75) { invalidate(); return; }
    const elapsed = last ? Math.max(0, (now - last) / 1000) : 0;
    const dt = Math.min(elapsed, .1);
    const motionDt = dt * (['analyzing', 'completed'].includes(requested.phase) ? playbackRate : 1);
    last = now;
    lastDraw = lastDraw ? Math.min(now, lastDraw + Math.max(1, Math.floor((now - lastDraw + .75) / interval)) * interval) : now;
    try {
      if (animate()) timeline.advance(motionDt, []);
      if (entrance && !requested.paused) entranceElapsed = Math.min(1.05, entranceElapsed + elapsed);
      const entryProgress = entrance ? Math.min(1, entranceElapsed / 1.05) : 1;
      scene.entranceScale = .48 + .52 * (1 - (1 - entryProgress) ** 3);
      scene.userView = view.update(dt);
      const still = reduced || stationary.has(requested.phase);
      const hits = highlights.values(timeline.time, still);
      scene.gravityMotionDelta = requested.paused || stationary.has(requested.phase) ? 0 : motionDt;
      if (gravityEnabled && !requested.paused && !stationary.has(requested.phase)) {
        const request = journeys.take(hits, mappings, Boolean(scene.gravity?.presentationPending ?? scene.gravity?.journeyPending));
        if (request) scene.gravityJourneyRequest = request;
      }
      scene.focusId = focus;
      scene.analysisScanning = requested.phase === 'analyzing' && !requested.paused && !reduced;
      scene.analysisArrival = handoff.active && gravityEnabled ? Math.min(1, handoff.elapsed / .9) : 1;
      scene.analysisRevealing = !still && !requested.paused && highlights.pending(timeline.time);
      if (scene.live) scene.live.immediate = reduced || stationary.has(requested.phase) || requested.phase === 'completed';
      const asrActive = ['preparing', 'transcribing'].includes(requested.phase) || requested.phase === 'paused' && timeline.time < TIMES.build;
      const preparing = preparation?.update(timeline.ambientTime, {
        active: asrActive, reduced,
        hasText: Boolean(loadedText?.some(s => s.text.trim())), drawable: scene.uniqueShown > 0,
      }) ?? 0;
      scene.asrPresence = 1 - preparing;
      if (preparing > 0) scene.entranceScale = 1;
      aurora.update(timeline.ambientTime, scene.camera.aspect, reduced,
        smooth((timeline.time - TIMES.build) / TIMES.morph), scene.degraded, analysisQuality === 'high' && !scene.particleFailure);
      stage?.update(timeline.ambientTime, { active: asrActive, reduced,
        records: scene.records, positionOf: record => scene.positionOf(record), presence: 1 - preparing });
      scene.render(scene.live || loadedText?.length ? timeline.time : Math.min(timeline.time, TIMES.space), hits, reduced,
        requested.phase === 'completed' ? Math.max(WALL_TIME, highlights.endAt) : Infinity,
        timeline.ambientTime, timeline.time < TIMES.build, timeline.flowTime);
      presentedPhase = requested.phase;
      handoff.advance(dt, { reduced, paused: requested.paused || stationary.has(requested.phase),
        frameReady: !gravityEnabled || scene.gravityActive || scene.gravityFailure,
        ready: !gravityEnabled || scene.gravityActive && (!scene.gravity?.character || scene.gravity.character.mesh.visible),
        anchor: scene.gravity?.coreScreenAnchor });
      remember();
      canvas.dataset.asrArrivals = String(stage?.bursts ?? 0);
      canvas.dataset.preparation = preparation?.lifecycle.state ?? 'done';
      canvas.dataset.preparationOpacity = preparing.toFixed(3);
      canvas.dataset.preparationReady = String(Boolean(preparation?.ready));
      canvas.dataset.asrInflow = String(Boolean(stage?.flow.visible));
      canvas.dataset.phase = requested.phase;
      canvas.dataset.characters = String(scene.uniqueShown);
      canvas.dataset.frames = String(Number(canvas.dataset.frames ?? 0) + 1);
      canvas.dataset.entranceScale = scene.entranceScale.toFixed(4);
      canvas.dataset.highlighted = String(hits.size);
      canvas.dataset.analysisStyle = scene.gravityActive ? 'gravity-subtitles' : scene.particleText?.mesh.visible ? 'particle-glyphs' : 'layered';
      canvas.dataset.gravityPhrases = String(scene.gravity?.atlas.entries.length ?? 0);
      canvas.dataset.gravityRevision = String(scene.gravity?.atlas.revision ?? 0);
      canvas.dataset.gravityHits = String(scene.gravity?.atlas.hitCount ?? 0);
      canvas.dataset.gravityQuality = scene.gravity ? scene.gravity.simplified ? 'simplified' : 'full' : '';
      canvas.dataset.gravityJourney = String(scene.gravity?.journeyCount ?? 0);
      canvas.dataset.gravityJourneyMoving = String(Boolean(scene.gravity?.journeyPending));
      canvas.dataset.gravityPresenting = String(Boolean(scene.gravity?.presentationPending));
      canvas.dataset.gravityEmphasis = (scene.gravity?.evidenceStrength ?? 0).toFixed(3);
      canvas.dataset.gravityZoom = (scene.gravity?.navigationZoom ?? 1).toFixed(4);
      canvas.dataset.gravityPath = (scene.gravity?.pathProgress ?? 0).toFixed(4);
      canvas.dataset.gravityFeatured = String(scene.gravity?.featuredMarker ?? '');
      const cue = scene.gravityActive ? scene.gravity?.evidenceCue ?? null : null;
      const cueKey = cue ? `${cue.serial}:${cue.id}:${cue.phase}` : '';
      if (cueKey !== lastEvidence) { lastEvidence = cueKey; onEvidence?.(cue); }
      canvas.dataset.analysisQuality = analysisQuality;
      canvas.dataset.analysisVariant = analysisVariant;
      canvas.dataset.analysisCore = scene.gravity?.character?.mesh.visible ? analysisVariant : scene.gravityActive ? 'black-hole' : '';
      canvas.dataset.particleCount = String(scene.particleText?.totalCount ?? 0);
      canvas.dataset.particleFaceCount = String(scene.particleText?.count ?? 0);
      canvas.dataset.particleRelief = String(scene.particleText?.accents?.reliefCount ?? 0);
      canvas.dataset.particleTrails = String(scene.particleText?.accents?.trails.visible ? scene.particleText.accents.visibleTrails : 0);
      canvas.dataset.particleScan = scene.analysisScanning ? scene.particleText?.scanGroup ?? '' : '';
      canvas.dataset.particleFocus = String(scene.particleText?.material.uniforms.uPointer.value.z ?? 0);
      canvas.dataset.analysisScanning = String(scene.analysisScanning);
      canvas.dataset.analysisParticles = String(Boolean(scene.analysisParticlesActive));
      canvas.dataset.viewZoom = view.current.zoom.toFixed(4);
      canvas.dataset.viewYaw = view.current.yaw.toFixed(4);
      canvas.dataset.viewPitch = view.current.pitch.toFixed(4);
      if (!announced) { announced = true; onReady?.(Boolean(entrance)); }
      if (scene.live) {
        canvas.dataset.pending = String(scene.live.layout.pending.size);
        canvas.dataset.atlasPages = String(scene.live.pages.length);
      }
      if (animate() && dt > .06) slow += dt; else slow = Math.max(0, slow - dt);
      if (slow > 3 && scene.gravity && !scene.gravity.simplified) {
        scene.gravity.reduceQuality(); slow = 0; onQuality('已减少远景字幕与星点，保留考点显示');
      } else if (slow > 3 && scene.particleText?.accents) {
        scene.particleText.reduceAccents(); slow = 0; onQuality('已简化拖尾与立体细节');
      } else if (slow > 5 && !scene.degraded) { scene.degrade(); onQuality('已降低效果'); }
    } catch (error) { fail(error); return; }
    if (animate() || journeyAnimating() || scene.live?.pending || view.pending || handoff.active && !requested.paused && !stationary.has(requested.phase)) invalidate();
  }
  function applyStage() {
    if (!requested) return;
    const { phase } = requested;
    if (reduced && phase === 'transcribing') timeline.time = Math.max(timeline.time, TIMES.space);
    if (['analyzing', 'completed', 'waiting'].includes(phase)) {
      timeline.completeAsr(); timeline.finishRate = 4; timeline.hold = false;
    }
    if ((reduced || stationary.has(phase) || phase === 'completed') && timeline.time >= TIMES.build) timeline.time = Math.max(timeline.time, WALL_TIME);
    if (phase === 'analyzing' || phase === 'completed' || stationary.has(phase)) {
      if (stationary.has(phase) && timeline.time < WALL_TIME && mappings.length) timeline.time = WALL_TIME;
      if (loadedMarkers === requested.markers) {
        highlights.sync(mappings, Math.max(timeline.time, WALL_TIME), requested.resultScope ?? 'default');
        if (gravityEnabled && journeys.sync(mappings, requested.resultScope ?? 'default', phase === 'analyzing')) {
          scene.gravityJourneyRequest = { ...journeys.reset(), immediate: true };
        }
      }
    }
  }
  async function refresh() {
    if (disposed || loading || !requested) return;
    loading = true;
    const snapshot = requested;
    try {
      const data = { subtitles: snapshot.subtitles, markers: snapshot.markers };
      const nextMappings = mapMarkers(data);
      const textChanged = loadedText !== snapshot.subtitles;
      const missingHit = nextMappings.some((mapping) => mapping.keys.some((char) => !scene.present?.has(char)));
      if (textChanged || (loadedMarkers !== snapshot.markers && missingHit) || !ready) {
        await scene.load(data, nextMappings, focus);
        if (disposed) return;
      } else scene.setMappings(nextMappings);
      loadedText = snapshot.subtitles; loadedMarkers = snapshot.markers; mappings = nextMappings;
      ready = true;
      applyStage();
    } catch (error) { fail(error); }
    finally {
      loading = false;
      if (!disposed && requested !== snapshot) void refresh();
      else invalidate();
    }
  }
  function visibility() {
    cancelAnimationFrame(frame); frame = 0; last = lastDraw = 0;
    if (document.hidden) { view.suspend(); scene.particlePointer = null; handoff.finish(); }
    if (!document.hidden) invalidate();
  }
  function blur() { view.suspend(); scene.particlePointer = null; invalidate(); }
  function contextLost(event) { event.preventDefault(); fail(new Error('三维显示已中断，后台任务不受影响。')); }
  document.addEventListener('visibilitychange', visibility);
  window.addEventListener('blur', blur);
  canvas.addEventListener('webglcontextlost', contextLost);
  function dispose() {
    if (disposed) return;
    remember(); disposed = true; cancelAnimationFrame(frame);
    document.removeEventListener('visibilitychange', visibility);
    window.removeEventListener('blur', blur);
    canvas.removeEventListener('webglcontextlost', contextLost);
    handoff.dispose(); view.dispose(); preparation?.dispose(); stage?.dispose(); aurora.dispose(); scene.dispose();
  }
  return {
    update(snapshot) {
      const incoming = arrivals.observe(snapshot.subtitles, snapshot.phase === 'transcribing' && !snapshot.reduced && !snapshot.paused && !document.hidden);
      stage?.enqueue(incoming);
      handoff.observe(snapshot, presentedPhase, () => scene.captureFrame?.() ?? false);
      if (!requested || requested.phase !== snapshot.phase || requested.paused !== snapshot.paused || reduced !== snapshot.reduced) {
        last = lastDraw = 0;
      }
      requested = snapshot; reduced = snapshot.reduced;
      view.setReduced(reduced);
      if (entrance === null) entrance = !previous && !reduced && ['preparing', 'transcribing'].includes(snapshot.phase);
      if (reduced || !['preparing', 'transcribing'].includes(snapshot.phase)) { entrance = false; entranceElapsed = 1.05; }
      // Stage changes are never held behind a font, glyph batch or reveal queue.
      applyStage();
      if (loadedText !== snapshot.subtitles || loadedMarkers !== snapshot.markers || !ready) void refresh();
      else { applyStage(); invalidate(); }
    },
    select(id, { travel = false } = {}) {
      if (travel) handoff.finish();
      focus = id;
      const mapping = mappings.find((item) => String(item.id) === String(id));
      if (travel && gravityEnabled) journeys.select(mapping);
      if (mapping?.keys.some((char) => !scene.present.has(char))) void refresh();
      else invalidate();
    },
    resetView() { handoff.finish(); if (gravityEnabled) scene.gravityJourneyRequest = journeys.reset(); view.reset(); },
    dispose,
  };
}
