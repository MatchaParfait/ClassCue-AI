import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { GravityAtlas, chooseGravityPhrases, subtitlePhrases } from '../../../experiments/subtitle-cyberspace/src/gravity-atlas.js';
import { GravitySubtitleScene } from '../../../experiments/subtitle-cyberspace/src/gravity-scene.js';
import { mapMarkers } from '../../../experiments/subtitle-cyberspace/src/core.js';
import { Color, NoToneMapping, Vector3 } from 'three';
import { movingRibbonEvidence, EVIDENCE_CAMERA_LIMITS } from '../../../experiments/subtitle-cyberspace/src/gravity-focus.js';
import { fixture } from '../../../experiments/subtitle-cyberspace/src/fixture.js';
import { referenceRibbon } from '../../../experiments/subtitle-cyberspace/src/gravity-reference.js';
import { anchorRibbonTail } from '../../../experiments/subtitle-cyberspace/src/gravity-ribbon-tail.js';
import { GravityJourney } from '../../../experiments/subtitle-cyberspace/src/gravity-journey.js';

vi.mock('three/addons/postprocessing/EffectComposer.js', () => ({ EffectComposer: class {
  constructor(renderer) { this.renderer = renderer; this.passes = []; }
  addPass(pass) { this.passes.push(pass); }
  setSize() {}
  setPixelRatio() {}
  render() { this.renderer.render(); }
  dispose() {}
} }));

let context;
beforeEach(() => {
  context = { fillRect: vi.fn(), fillText: vi.fn() };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(context);
});
afterEach(() => vi.restoreAllMocks());

const subtitles = [{ id: 8, start_ms: 0, end_ms: 10000, text: '学习递归算法，需要先理解终止条件。' }];
const marker = { id: -1, start_ms: 0, end_ms: 10000, source_quote: '递归算法' };
const mappings = mapMarkers({ subtitles, markers: [marker] });
const hits = new Map(mappings[0].keys.map(key => [key, 1]));

it('keeps emphasis red through travel, fades to green, and respects pause, replay and reduced motion', () => {
  const journey = new GravityJourney();
  expect(journey.emphasisRed).toBe(0);
  const request = { serial: 1, keys: mappings[0].keys };
  journey.start(request); journey.advance(2);
  expect(journey.emphasisRed).toBe(1);
  journey.advance(0); expect(journey.emphasisRed).toBe(1);
  journey.advance(1.95);
  expect(journey.emphasisRed).toBeGreaterThan(0);
  expect(journey.emphasisRed).toBeLessThan(1);
  journey.advance(1); expect(journey.emphasisRed).toBe(0);
  expect(journey.start(request)).toBe(false);
  expect(journey.emphasisRed).toBe(0);
  journey.start({ ...request, serial: 2 }); journey.advance(0);
  expect(journey.emphasisRed).toBe(1);
  journey.advance(0, true); expect(journey.emphasisRed).toBe(0);
  journey.start({ serial: 3, reset: true }); journey.advance(0);
  expect(journey.emphasisRed).toBe(0);
});

it('anchors ribbon continuations without altering source bodies or breaking text coordinates at their seams', () => {
  for (let index = 0; index < 9; index++) {
    const { geometry } = referenceRibbon(index, false);
    const positions = geometry.getAttribute('position'), flow = geometry.getAttribute('aFlowUv'), uv = geometry.getAttribute('uv');
    const before = positions.array.slice(), beforeFlow = flow.array.slice();
    anchorRibbonTail(geometry);
    let stride = 1;
    while (uv.getX(stride) === uv.getX(0)) stride++;
    const sign = index >= 7 ? -1 : 1;
    let tail = -1;
    for (let i = 0; i < uv.count; i++) {
      if (uv.getX(i) >= 0 && uv.getX(i) <= 1) {
        expect(Array.from(positions.array.subarray(i * 3, i * 3 + 3))).toEqual(Array.from(before.subarray(i * 3, i * 3 + 3)));
        expect(flow.getX(i)).toBe(beforeFlow[i * 2]); expect(flow.getY(i)).toBe(beforeFlow[i * 2 + 1]);
      } else if (tail < 0) tail = i;
    }
    const attachment = index >= 7 ? 0 : tail - stride;
    for (let i = tail; i < uv.count; i++) {
      const prior = i < tail + stride ? attachment + i % stride : i - stride;
      const delta = new Vector3().fromBufferAttribute(positions, i).sub(new Vector3().fromBufferAttribute(positions, prior));
      expect(delta.length()).toBeLessThan(2.5);
      expect((flow.getX(i) - flow.getX(prior)) * sign).toBeGreaterThan(0);
    }
    geometry.dispose();
  }
});

it('does not fling ribbon geometry when evidence turns cross camera-facing boundaries', () => {
  const scene = new GravitySubtitleScene();
  const renderer = { getSize: v => v.set(1376, 666), render: vi.fn(), getPixelRatio: () => 1,
    getClearColor: c => c.set('#060a0d'), getClearAlpha: () => 0, setPixelRatio: vi.fn(), setClearColor: vi.fn(),
    capabilities: { getMaxAnisotropy: () => 16 } };
  const mapped = mapMarkers(fixture), green = new Map(mapped.flatMap(m => m.keys.map(k => [k, 1])));
  const frame = { data: fixture, mappings: mapped, hits: green, ambient: 0, active: true, aspect: 1376 / 666, motionDelta: 1 / 30 };
  scene.render(renderer, frame);
  const geometry = scene.sheets.map(mesh => mesh.geometry.getAttribute('position').array.slice());
  const initial = scene.camera.position.clone();
  for (let event = 0; event < 3; event++) {
    frame.journey = { serial: event + 1, keys: mapped[event].keys, markerId: mapped[event].id };
    for (let i = 0; i < 156; i++) { frame.ambient += 1 / 30; scene.render(renderer, frame); }
    scene.sheets.forEach((mesh, index) => expect(mesh.geometry.getAttribute('position').array).toEqual(geometry[index]));
  }
  expect(scene.camera.position.distanceTo(initial)).toBeGreaterThan(1);
  expect(scene.uniforms.uTime.value).toBeGreaterThan(15);
  expect(scene.journeyCount).toBe(3);
  scene.dispose();
});

it('splits actual subtitle text without losing Unicode identity or inserting decorative formulas', () => {
  const input = [{ id: '课:1', text: '🙂 这是中文字幕，保留空格与时间关联。'.repeat(8), start_ms: 1234 }];
  const phrases = subtitlePhrases(input);
  expect(phrases.map(p => p.text).join('')).toBe(input[0].text);
  expect(phrases.every(p => Array.from(p.text).length <= 12)).toBe(true);
  expect(phrases.flatMap(p => p.keys)).toEqual(Array.from(input[0].text).map((_, i) => `课:1:${i}`));
  expect(phrases[0].start_ms).toBe(1234);
  expect(subtitlePhrases([])).toEqual([]);
});

it('bounds long lessons and prioritizes the selected real evidence even near the end', () => {
  const phrases = subtitlePhrases(Array.from({ length: 10000 }, (_, id) => ({ id, text: `字幕${id}` })));
  const results = [{ id: 1, keys: ['9998:0'] }, { id: 2, keys: ['9999:0'] }];
  const selected = chooseGravityPhrases(phrases, results, 2, 1);
  expect(selected).toHaveLength(1); expect(selected[0].subtitleId).toBe(9999);
  const normal = chooseGravityPhrases(phrases, results, null);
  expect(normal).toHaveLength(48); expect(new Set(normal.map(p => p.id)).size).toBe(48);
  expect(normal.some(p => p.subtitleId === 9998)).toBe(true);
  expect(normal.some(p => p.subtitleId === 9999)).toBe(true);
});

it('updates only matching character states and never repaints ink for polling or highlight animation', () => {
  const atlas = new GravityAtlas();
  atlas.update(subtitles, [], null, new Map());
  const paints = context.fillText.mock.calls.length, revision = atlas.revision;
  expect(atlas.hitCount).toBe(0);
  atlas.update(subtitles, mappings, null, new Map([['8:2', .5]]));
  expect(atlas.state[2 * 4]).toBe(128); expect(atlas.state[0]).toBe(0);
  atlas.update(subtitles.map(s => ({ ...s })), mappings.map(m => ({ ...m, id: 44 })), 44, hits);
  expect(atlas.hitCount).toBe(4); expect(atlas.entries[0].markerIds).toEqual([44]);
  expect(atlas.state[2 * 4 + 1]).toBe(255);
  expect(context.fillText).toHaveBeenCalledTimes(paints); expect(atlas.revision).toBe(revision);
  atlas.update(subtitles, [], null, new Map());
  expect(atlas.hitCount).toBe(0);
  expect(atlas.state.every((value, index) => index % 4 === 2 || value === 0)).toBe(true);
  atlas.dispose();
});

it('repaints edited subtitles with the same ID, and clears all old ink for an empty transcript', () => {
  const atlas = new GravityAtlas(); atlas.update(subtitles, mappings, null, hits);
  const revision = atlas.revision;
  atlas.update([{ ...subtitles[0], text: '已经更正的字幕' }], [], null, new Map());
  expect(atlas.revision).toBe(revision + 1); expect(atlas.entries[0].text).toBe('已经更正的字幕');
  atlas.update([], [], null, new Map());
  expect(atlas.entries).toEqual([]); expect(atlas.hitCount).toBe(0);
  expect(context.fillRect.mock.calls.slice(-48)).toEqual(Array.from({ length: 48 }, (_, slot) => [slot % 4 * 1024, Math.floor(slot / 4) * 256, 1024, 256]));
  atlas.dispose();
});

it('keeps an existing hit in its atlas slot when a new result enters a long transcript', () => {
  const source = Array.from({ length: 100 }, (_, id) => ({ id, text: `真实字幕${id}`, start_ms: id * 1000 }));
  const atlas = new GravityAtlas(), first = { id: 1, keys: ['90:0'] }, next = { id: 2, keys: ['99:0'] };
  atlas.update(source, [first], 1, new Map([['90:0', 1]]));
  const slot = atlas.keyToSlot.get('90:0').slot;
  atlas.update(source, [first, next], 2, new Map([['90:0', 1], ['99:0', 1]]));
  expect(atlas.keyToSlot.get('90:0').slot).toBe(slot);
  expect(atlas.tiles[slot].subtitleId).toBe(90);
  atlas.dispose();
});

it('keeps evidence clickable using the rendered camera and releases shared geometry exactly once', () => {
  const scene = new GravitySubtitleScene();
  const renderer = { getSize: v => v.set(1440, 800), render: vi.fn(), getPixelRatio: vi.fn(() => 1),
    getClearColor: color => color.set('#060a0d'), getClearAlpha: () => 0,
    setPixelRatio: vi.fn(), setClearColor: vi.fn(), toneMapping: NoToneMapping, toneMappingExposure: .8,
    capabilities: { getMaxAnisotropy: () => 16 } };
  const frame = { data: { subtitles }, mappings, hits, focus: null, ambient: 4, reduced: false, active: true,
    pointer: null, view: { zoom: 1, yaw: 0, pitch: 0 }, aspect: 1.8 };
  scene.render(renderer, frame);
  const rect = { left: 10, top: 20, width: 1440, height: 800 };
  const point = scene.pointForMarker(-1, rect);
  expect(point).not.toBeNull(); expect(scene.pick(point.x, point.y, rect)).toBe(-1);
  const clock = scene.uniforms.uTime.value;
  scene.render(renderer, { ...frame, active: false, reduced: true, pointer: { x: 0, y: 0 } });
  expect(scene.uniforms.uTime.value).toBe(clock);
  expect(scene.fragments.material.uniforms.uPointerActive.value).toBe(0);
  scene.reduceQuality(); expect(scene.fragments.geometry.instanceCount).toBe(96);
  scene.render(renderer, frame); expect(scene.pointForMarker(-1, rect)).not.toBeNull();
  const resize = vi.spyOn(scene.composer, 'setPixelRatio');
  renderer.getPixelRatio.mockReturnValue(.75);
  scene.render(renderer, frame);
  expect(resize).toHaveBeenCalledWith(.75);
  expect(scene.hole.material.uniforms.uPixelHeight.value).toBe(600);
  const geometry = vi.spyOn(scene.fragments.geometry, 'dispose');
  const pickGeometry = vi.spyOn(scene.pickGeometry, 'dispose');
  const texture = vi.spyOn(scene.atlas.texture, 'dispose');
  scene.dispose(); scene.dispose();
  expect(geometry).toHaveBeenCalledTimes(1); expect(texture).toHaveBeenCalledTimes(1);
  expect(pickGeometry).toHaveBeenCalledTimes(1);
  expect(renderer.toneMapping).toBe(NoToneMapping); expect(renderer.toneMappingExposure).toBe(.8);
  expect(renderer.setPixelRatio).toHaveBeenLastCalledWith(1);
  expect(renderer.setClearColor).toHaveBeenLastCalledWith(new Color('#060a0d'), 0);
  expect(scene.scene.children).toHaveLength(0);
  const calls = renderer.render.mock.calls.length; scene.render(renderer, frame);
  expect(renderer.render).toHaveBeenCalledTimes(calls);
});

it('keeps the hole visible and camera above the sheets throughout a restrained move while text continues flowing', () => {
  const scene = new GravitySubtitleScene();
  const renderer = { getSize: v => v.set(1440, 800), render: vi.fn(), getPixelRatio: () => 1,
    getClearColor: c => c.set('#060a0d'), getClearAlpha: () => 0, setPixelRatio: vi.fn(), setClearColor: vi.fn(),
    capabilities: { getMaxAnisotropy: () => 16 } };
  const frame = { data: { subtitles }, mappings, hits, ambient: 0, active: true, aspect: 1.8, motionDelta: 1 / 30 };
  scene.render(renderer, frame);
  const children = [...scene.scene.children], opening = scene.camera.position.clone();
  let ceiling = -Infinity;
  for (const sheet of scene.sheets.slice(0, 7)) {
    const vertices = sheet.geometry.getAttribute('position'), uv = sheet.geometry.getAttribute('uv');
    for (let i = 0; i < vertices.count; i++) if (uv.getX(i) <= 1) ceiling = Math.max(ceiling, vertices.getY(i) + .075);
  }
  frame.journey = { serial: 1, keys: mappings[0].keys, markerId: -1, reason: 'automatic' };
  const checkSafeView = () => {
    const local = scene.camera.position.clone().applyQuaternion(scene.inverse);
    expect(local.y).toBeGreaterThanOrEqual(EVIDENCE_CAMERA_LIMITS.minHeight);
    expect(local.y - ceiling).toBeGreaterThan(.3);
    expect(local.length()).toBeGreaterThanOrEqual(EVIDENCE_CAMERA_LIMITS.minRadius);
    expect(local.length()).toBeLessThanOrEqual(EVIDENCE_CAMERA_LIMITS.maxRadius + 1e-6);
    const hole = new Vector3().project(scene.camera);
    const halfHeight = scene.camera.position.length() * Math.tan(scene.camera.fov * Math.PI / 360) / scene.camera.zoom;
    expect(Math.abs(hole.x) + 2.8 / halfHeight / scene.camera.aspect).toBeLessThan(.95);
    expect(Math.abs(hole.y) + 2.8 / halfHeight).toBeLessThan(.95);
  };
  for (let i = 1; i <= 20; i++) { scene.render(renderer, { ...frame, ambient: i / 30 }); checkSafeView(); }
  expect(scene.pathProgress).toBeGreaterThan(0); expect(scene.journeyCount).toBe(1);
  const midway = scene.pathProgress;
  scene.render(renderer, { ...frame, active: false, motionDelta: 0 });
  expect(scene.pathProgress).toBe(midway);
  for (let i = 21; i <= 55; i++) { scene.render(renderer, { ...frame, ambient: i / 30 }); checkSafeView(); }
  expect(scene.journeyPending).toBe(false); expect(scene.journeyCount).toBe(1);
  expect(scene.featuredMarker).toBe(-1); expect(scene.hero).toBeUndefined();
  expect(scene.scene.children).toEqual(children);
  const anchor = scene.focusAnchor;
  expect(scene.sheets).toContain(anchor.mesh);
  const flowTime = anchor.mesh.material.uniforms.uTextTime.value;
  const flowX = movingRibbonEvidence(anchor, scene.uniforms.uTime.value)?.flow.x;
  const offset = anchor.mesh.material.uniforms.uAtlasOffset.value.clone();
  scene.render(renderer, { ...frame, ambient: 8 });
  expect(anchor.mesh.material.uniforms.uTextTime.value).toBeGreaterThan(flowTime);
  expect(movingRibbonEvidence(anchor, scene.uniforms.uTime.value)?.flow.x).toBeLessThan(flowX);
  expect(anchor.mesh.material.uniforms.uAtlasOffset.value).toEqual(offset);
  const rect = { left: 0, top: 0, width: 1440, height: 800 };
  scene.picks = []; // Validate the original curved surface, not a floating fragment.
  const point = scene.pointForMarker(-1, rect);
  expect(point).not.toBeNull(); expect(scene.pick(point.x, point.y, rect)).toBe(-1);
  const beforeRetarget = scene.camera.position.clone();
  const beforeZoom = scene.camera.zoom;
  scene.render(renderer, { ...frame, active: false, motionDelta: 0, journey: { ...frame.journey, serial: 2 } });
  expect(scene.camera.position.distanceTo(beforeRetarget)).toBeLessThan(.0001);
  expect(scene.camera.zoom).toBeCloseTo(beforeZoom, 8);
  scene.render(renderer, { ...frame, reduced: true, journey: { ...frame.journey, serial: 2 } });
  expect(scene.journeyPending).toBe(false); expect(scene.journeyCount).toBe(2);
  expect(scene.focusAnchor.entry.id).toBe(anchor.entry.id);
  checkSafeView();
  scene.render(renderer, { ...frame, aspect: .6, view: { zoom: 1.6 }, reduced: true, journey: { ...frame.journey, serial: 2 } });
  checkSafeView();
  scene.render(renderer, { ...frame, reduced: true, journey: { serial: 3, reset: true } });
  expect(scene.pathProgress).toBe(0); expect(scene.focusAnchor).toBeNull();
  expect(scene.camera.position.distanceTo(opening)).toBeLessThan(.01);
  scene.dispose();
});

it('retargets different evidence without leaving the camera over the previous green inscription', () => {
  const scene = new GravitySubtitleScene();
  const renderer = { getSize: v => v.set(1440, 800), render: vi.fn(), getPixelRatio: () => 1,
    getClearColor: c => c.set('#060a0d'), getClearAlpha: () => 0, setPixelRatio: vi.fn(), setClearColor: vi.fn(),
    capabilities: { getMaxAnisotropy: () => 16 } };
  const mapped = mapMarkers({ subtitles: fixture.subtitles, markers: fixture.markers.slice(0, 2) });
  const green = new Map(mapped.flatMap(m => m.keys.map(key => [key, 1])));
  const frame = { data: fixture, mappings: mapped, hits: green, ambient: 3, aspect: 1.8, reduced: true };
  scene.render(renderer, frame);
  for (const [i, mapping] of [mapped[1], mapped[0], mapped[0]].entries()) {
    scene.render(renderer, { ...frame, ambient: 8 + i, journey: { serial: i + 1, keys: mapping.keys, markerId: mapping.id } });
    const anchor = scene.focusAnchor, u = anchor.mesh.material.uniforms;
    const ink = scene.atlas.characterAt(anchor.flow.x * u.uRepeat.value.x + u.uAtlasOffset.value.x + u.uTextTime.value * u.uFlow.value,
      anchor.flow.y * u.uRepeat.value.y + u.uAtlasOffset.value.y);
    expect(mapping.keys).toContain(ink.key);
    expect(ink.markerId).toBe(mapping.id);
    const projected = anchor.center.clone().project(scene.camera);
    expect(Math.abs(projected.x)).toBeLessThan(.9); expect(Math.abs(projected.y)).toBeLessThan(.9);
    expect(Math.abs(projected.z)).toBeLessThan(1);
  }
  scene.dispose();
});

it('predicts flowing ink, emphasizes it only after arrival, and restores context without replaying', () => {
  const scene = new GravitySubtitleScene();
  const renderer = { getSize: v => v.set(1440, 800), render: vi.fn(), getPixelRatio: () => 1,
    getClearColor: c => c.set('#060a0d'), getClearAlpha: () => 0, setPixelRatio: vi.fn(), setClearColor: vi.fn(),
    capabilities: { getMaxAnisotropy: () => 16 } };
  const frame = { data: { subtitles }, mappings, hits, ambient: 0, active: true, aspect: 1.8, motionDelta: 1 / 30 };
  scene.render(renderer, frame);
  frame.journey = { serial: 1, keys: mappings[0].keys, markerId: -1 };
  scene.render(renderer, { ...frame, ambient: 1 / 30 });
  const anchor = scene.focusAnchor, destination = scene.focusDestination.center.clone();
  const predicted = movingRibbonEvidence(anchor, scene.uniforms.uTime.value + 1.6, anchor.clock + 1.6);
  expect(predicted.center.distanceTo(destination)).toBeLessThan(1e-6);
  expect(scene.evidenceCue.phase).toBe('travel'); expect(scene.evidenceStrength).toBe(0);
  for (let i = 2; i <= 66; i++) scene.render(renderer, { ...frame, ambient: i / 30 });
  expect(scene.evidenceCue.phase).toBe('arrival'); expect(scene.evidenceStrength).toBe(1);
  expect(scene.navigationZoom).toBeCloseTo(1.3);
  expect(scene.presentationPending).toBe(true); expect(scene.journeyPending).toBe(false);
  expect(scene.focusDestination.center).toEqual(destination);
  const u = anchor.mesh.material.uniforms, box = u.uEvidence.value.clone();
  const camera = scene.camera.position.clone(), age = u.uEvidenceAge.value;
  scene.render(renderer, { ...frame, ambient: 66 / 30, active: false, motionDelta: 0 });
  expect(u.uEvidenceAge.value).toBe(age);
  for (let i = 67; i <= 75; i++) scene.render(renderer, { ...frame, ambient: i / 30 });
  expect(u.uEvidence.value.x).toBeLessThan(box.x);
  expect(scene.camera.position.distanceTo(camera)).toBeGreaterThan(.01);
  for (let i = 76; i <= 145; i++) scene.render(renderer, { ...frame, ambient: i / 30 });
  expect(scene.evidenceStrength).toBe(0); expect(scene.evidenceCue).toBeNull();
  expect(scene.presentationPending).toBe(false); expect(scene.journeyCount).toBe(1);
  expect(scene.navigationZoom).toBe(1);
  expect(scene.fragments.material.uniforms.uContextDim.value).toBe(1);
  scene.render(renderer, { ...frame, reduced: true, journey: { ...frame.journey, serial: 2 } });
  expect(scene.evidenceCue.phase).toBe('still'); expect(scene.presentationPending).toBe(false);
  expect(scene.evidenceStrength).toBe(0);
  scene.render(renderer, { ...frame, reduced: true, journey: { serial: 3, reset: true } });
  expect(scene.evidenceCue).toBeNull(); expect(scene.focusAnchor).toBeNull();
  scene.dispose();
});

it.each([1.8, .6])('continues a safe orbit long after evidence settles at aspect %s and freezes only when inactive', aspect => {
  const scene = new GravitySubtitleScene();
  const renderer = { getSize: v => v.set(1440, 800), render: vi.fn(), getPixelRatio: () => 1,
    getClearColor: c => c.set('#060a0d'), getClearAlpha: () => 0, setPixelRatio: vi.fn(), setClearColor: vi.fn(),
    capabilities: { getMaxAnisotropy: () => 16 } };
  const frame = { data: { subtitles }, mappings, hits, ambient: 0, active: true, aspect, view: { zoom: aspect < 1 ? 1.6 : 1 }, motionDelta: .1 };
  scene.render(renderer, frame);
  frame.journey = { serial: 1, keys: mappings[0].keys, markerId: -1 };
  let previous;
  const angles = [], elevations = [];
  for (let i = 1; i <= 700; i++) {
    scene.render(renderer, { ...frame, ambient: i / 10 });
    const local = scene.camera.position.clone().applyQuaternion(scene.inverse);
    if (i > 50) {
      angles.push(Math.atan2(local.x, local.z));
      elevations.push(Math.asin(local.y / local.length()));
    }
    expect(local.y).toBeGreaterThanOrEqual(EVIDENCE_CAMERA_LIMITS.minHeight);
    expect(local.length()).toBeGreaterThanOrEqual(EVIDENCE_CAMERA_LIMITS.minRadius - 1e-6);
    expect(local.length()).toBeLessThanOrEqual(EVIDENCE_CAMERA_LIMITS.maxRadius + 1e-6);
    const hole = new Vector3().project(scene.camera);
    const halfHeight = local.length() * Math.tan(scene.camera.fov * Math.PI / 360) / scene.camera.zoom;
    expect(Math.abs(hole.y) + 2.8 / halfHeight).toBeLessThan(.95);
    expect(Math.abs(hole.x) + 2.8 / halfHeight / scene.camera.aspect).toBeLessThan(.95);
    if (i % 50 === 0) {
      if (previous) expect(scene.camera.position.distanceTo(previous)).toBeGreaterThan(.1);
      previous = scene.camera.position.clone();
    }
  }
  expect(scene.journeyCount).toBe(1); expect(scene.presentationPending).toBe(false);
  expect(Math.max(...angles) - Math.min(...angles)).toBeGreaterThan(1.1);
  expect(Math.max(...elevations) - Math.min(...elevations)).toBeGreaterThan(.1);
  const position = scene.camera.position.clone(), flow = scene.uniforms.uTime.value;
  scene.render(renderer, { ...frame, ambient: 71, active: false, motionDelta: 0 });
  expect(scene.camera.position.distanceTo(position)).toBeLessThan(1e-6);
  expect(scene.uniforms.uTime.value).toBe(flow);
  scene.render(renderer, { ...frame, ambient: 71, active: false, motionDelta: 0, journey: { ...frame.journey, serial: 2 } });
  expect(scene.camera.position.distanceTo(position)).toBeLessThan(1e-6);
  scene.dispose();
});

it('makes a substantial event turn and transports real ink outward without changing its identity or jumping on interruption', () => {
  const scene = new GravitySubtitleScene();
  const renderer = { getSize: v => v.set(1376, 666), render: vi.fn(), getPixelRatio: () => 1,
    getClearColor: c => c.set('#060a0d'), getClearAlpha: () => 0, setPixelRatio: vi.fn(), setClearColor: vi.fn(),
    capabilities: { getMaxAnisotropy: () => 16 } };
  const mapped = mapMarkers(fixture), green = new Map(mapped.flatMap(m => m.keys.map(k => [k, 1])));
  const frame = { data: fixture, mappings: mapped, hits: green, ambient: 0, active: true, aspect: 1376 / 666, motionDelta: 1 / 30 };
  const step = () => { frame.ambient += 1 / 30; scene.render(renderer, frame); };
  let transported = false;
  for (let index = 0; index < mapped.length && !transported; index++) {
    for (let i = 0; i < 90; i++) step();
    const local = scene.camera.position.clone().applyQuaternion(scene.inverse);
    const theta = Math.atan2(local.x, local.z);
    frame.journey = { serial: index + 1, keys: mapped[index].keys, markerId: mapped[index].id };
    scene.render(renderer, { ...frame, motionDelta: 0 });
    const anchor = scene.focusAnchor;
    expect(anchor).not.toBeNull();
    const angle = Math.abs(Math.atan2(Math.sin(anchor.cameraTheta - theta), Math.cos(anchor.cameraTheta - theta)));
    expect(angle).toBeGreaterThanOrEqual(.59); expect(angle).toBeLessThanOrEqual(.86);
    if (!anchor.clockShift) { for (let i = 0; i < 66; i++) step(); continue; }
    transported = true;
    expect(scene.focusDestination.center.length()).toBeGreaterThan(anchor.center.length() + .5);
    const u = anchor.mesh.material.uniforms;
    for (let i = 0; i < 24; i++) {
      step();
      const moving = movingRibbonEvidence(anchor, scene.uniforms.uTime.value);
      const ink = scene.atlas.characterAt(moving.flow.x * u.uRepeat.value.x + u.uAtlasOffset.value.x + u.uTextTime.value * u.uFlow.value,
        moving.flow.y * u.uRepeat.value.y + u.uAtlasOffset.value.y);
      expect(mapped[index].keys).toContain(ink.key);
    }
    const pose = scene.camera.position.clone(), clock = u.uTextTime.value;
    scene.render(renderer, { ...frame, active: false, motionDelta: 0 });
    expect(u.uTextTime.value).toBe(clock);
    scene.render(renderer, { ...frame, active: false, motionDelta: 0, journey: { ...frame.journey, serial: 99 } });
    expect(scene.camera.position.distanceTo(pose)).toBeLessThan(1e-6);
    expect(u.uTextTime.value).toBe(clock);
  }
  expect(transported).toBe(true);
  scene.dispose();
});

it('retains background lines and fades only newly required excerpts without restarting on polling', () => {
  const source = Array.from({ length: 200 }, (_, id) => ({ id, text: `课程内容${id}`, start_ms: id * 1000 }));
  const atlas = new GravityAtlas(); atlas.update(source, [], null, new Map());
  const old = atlas.tiles.map(e => e.id);
  const missing = atlas.phrases.find(p => !old.includes(p.id));
  const mapping = { id: 1, keys: missing.keys }, green = new Map(missing.keys.map(key => [key, 1]));
  const version = atlas.texture.version;
  atlas.update(source, [mapping], 1, green, { delta: .08, immediate: false });
  expect(atlas.pending).toBe(true); expect(atlas.changes.size).toBe(1);
  const slot = [...atlas.changes.keys()][0];
  expect(atlas.opacity[slot]).toBeCloseTo(.5); expect(atlas.tiles.map(e => e.id)).toEqual(old);
  expect(atlas.texture.version).toBe(version); expect(atlas.ready(missing.keys)).toBe(false);
  atlas.update(source, [{ ...mapping }], 1, green, { delta: 0, immediate: false });
  expect(atlas.opacity[slot]).toBeCloseTo(.5);
  atlas.update(source, [mapping], 1, green, { delta: .08, immediate: false });
  expect(atlas.tiles[slot].id).toBe(missing.id); expect(atlas.opacity[slot]).toBe(0);
  expect(atlas.texture.version).toBe(version + 1);
  expect(atlas.tiles.filter((entry, i) => entry.id !== old[i])).toHaveLength(1);
  atlas.update(source, [mapping], 1, green, { delta: .11, immediate: false });
  expect(atlas.opacity[slot]).toBeCloseTo(.5);
  atlas.update(source, [mapping], 1, green, { delta: 0, immediate: true });
  expect(atlas.pending).toBe(false); expect(atlas.ready(missing.keys)).toBe(true);
  expect(atlas.texture.version).toBe(version + 1);
  atlas.dispose();
});

it('waits for newly inserted evidence ink to fade in before moving, without freezing the subtitle flow', () => {
  const scene = new GravitySubtitleScene();
  const renderer = { getSize: v => v.set(1440, 800), render: vi.fn(), getPixelRatio: () => 1,
    getClearColor: c => c.set('#060a0d'), getClearAlpha: () => 0, setPixelRatio: vi.fn(), setClearColor: vi.fn(),
    capabilities: { getMaxAnisotropy: () => 16 } };
  const frame = { data: fixture, mappings: [], hits: new Map(), ambient: 0, active: true, aspect: 1.8, motionDelta: 1 / 60 };
  scene.render(renderer, frame);
  const entry = scene.atlas.phrases.find(p => !scene.atlas.entries.some(e => e.id === p.id));
  frame.mappings = [{ id: 99, keys: entry.keys }]; frame.hits = new Map(entry.keys.map(key => [key, 1]));
  frame.journey = { serial: 1, keys: entry.keys, markerId: 99 };
  scene.render(renderer, { ...frame, ambient: 1 / 60 });
  expect(scene.waitingEvidence).toBe(true); expect(scene.journeyCount).toBe(0);
  expect(scene.presentationPending).toBe(true);
  const clock = scene.uniforms.uTime.value;
  for (let i = 2; i <= 35; i++) scene.render(renderer, { ...frame, ambient: i / 60 });
  expect(scene.uniforms.uTime.value).toBeGreaterThan(clock);
  expect(scene.atlas.pending).toBe(false); expect(scene.waitingEvidence).toBe(false);
  expect(scene.journeyCount).toBe(1); expect(scene.focusAnchor).not.toBeNull();
  scene.dispose();
});
