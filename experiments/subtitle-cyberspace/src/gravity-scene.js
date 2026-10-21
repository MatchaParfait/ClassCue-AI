import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { GravityAtlas } from './gravity-atlas.js';
import { GravityJourney } from './gravity-journey.js';
import { anchorRibbonTail } from './gravity-ribbon-tail.js';
import { NailongCore, NAILONG_CORE_SIZE } from './nailong-core.js';
import { DafeiyuCore, DAFEIYU_CORE_SIZE } from './dafeiyu-core.js';
import { Dafeiyu31Core, DAFEIYU31_CORE_SIZE } from './dafeiyu31-core.js';
import { DafeiyuRiceCore, DAFEIYU_RICE_CORE_SIZE } from './dafeiyu-rice-core.js';
import { applyDafeiyuPalette } from './dafeiyu-palette.js';
import { GptCore, GPT_CORE_SIZE } from './gpt-core.js';
import { applyGptPalette } from './gpt-palette.js';
import { ribbonSample, findRibbonEvidence, movingRibbonEvidence, evidenceCameraPose, focusCamera, EVIDENCE_CAMERA_LIMITS } from './gravity-focus.js';
import { referenceCamera, referenceRibbon, referenceHole, referenceFragments, referenceStars, ribbonVertex, ribbonFragment } from './gravity-reference.js';

// Only color actual subtitle ink. Geometry, optical emission and camera retain
// the reference implementation. A zero state is pixel-identical to the source.
const hitShader = `
uniform sampler2D uStates;
uniform float uEmphasisRed;
vec3 subtitleState(vec2 uv){
 vec2 tile=fract(uv)*vec2(4.,12.);
 float row=min(11.,floor((1.-fract(uv.y))*12.));
 float slot=row*4.+floor(tile.x);
 float letter=floor((fract(tile.x)*1024.-42.)/78.);
 if(letter<0. || letter>=12.) return vec3(0.);
 return texture2D(uStates,vec2((letter+.5)/32.,(slot+.5)/48.)).rgb;
}
vec3 subtitleTint(vec3 color,vec2 uv){
 vec3 state=subtitleState(uv);
 vec3 hitColor=mix(vec3(.32,1.,.52),vec3(1.,.035,.055),state.g*uEmphasisRed);
 return mix(color,hitColor*(1.+state.g*.25),state.r)*state.b;
}`;
const fract = n => n - Math.floor(n);
const smooth = THREE.MathUtils.smoothstep;
// The locator is drawn in the original surface's physical UVs. It bends and
// flows with that inscription; it adds no foreground plane or duplicate text.
const evidenceShader = `
uniform float uTextTime,uEvidenceStrength,uEvidenceAge,uNavigationSafety;
uniform vec4 uEvidence;
uniform vec2 uEvidenceLetters;
float evidenceCorners(vec2 p){
 vec2 d=abs(p), aa=max(fwidth(p),vec2(.002));
 vec2 stroke=1.-smoothstep(aa*.6,aa*1.7,abs(d-1.));
 return max(stroke.x*step(.58,d.y)*step(d.y,1.),stroke.y*step(.82,d.x)*step(d.x,1.));
}
`;
const evidenceInk = `
  vec2 evidenceUv=(vFlowUv-uEvidence.xy)/max(uEvidence.zw,vec2(.01));
  float region=(1.-smoothstep(.9,1.05,abs(evidenceUv.x)))*(1.-smoothstep(.9,1.05,abs(evidenceUv.y)));
  vec3 inkState=subtitleState(lettering);
  float order=(floor((fract(lettering.x*4.)*1024.-42.)/78.)-uEvidenceLetters.x)/max(1.,uEvidenceLetters.y-1.);
  float reveal=smoothstep(max(0.,order)*.48,max(0.,order)*.48+.12,uEvidenceAge);
  float evidence=region*inkState.r*inkState.g*uEvidenceStrength*reveal;
  float locator=evidenceCorners(evidenceUv)*uEvidenceStrength*(1.-smoothstep(1.05,1.5,uEvidenceAge));
  float primary=text*((.95*rowAccent+hover*3.5+wave*4.)*pulse+evidence*2.6);
`;

export class GravitySubtitleScene {
  // Reference texture is used by the parity fixture only; production always
  // builds the atlas from this task's real subtitles, with no remote requests.
  constructor({ referenceTexture = null, mobile = globalThis.innerWidth < 700, variant = 'gravity', onInvalidate, onAssetError } = {}) {
    this.mobile = mobile; this.referenceTexture = referenceTexture;
    this.bloomStrength = variant === 'gpt' && !referenceTexture ? .28 : .42;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(46, 1, .08, 220);
    this.field = new THREE.Group();
    this.field.quaternion.setFromAxisAngle(new THREE.Vector3(Math.cos(1.08), 0, -Math.sin(1.08)), -.47);
    this.inverse = this.field.quaternion.clone().invert();
    this.diskUp = new THREE.Vector3(0, 1, 0).applyQuaternion(this.field.quaternion);
    this.scene.add(this.field);
    this.atlas = new GravityAtlas();
    const texture = referenceTexture ?? this.atlas.texture;
    this.uniforms = { uTime: { value: 0 } };
    this.stars = referenceStars(mobile, this.uniforms.uTime, 1);
    this.scene.add(this.stars.group);
    this.occluder = new THREE.Mesh(new THREE.SphereGeometry(1.94, mobile ? 48 : 80, 48), new THREE.MeshBasicMaterial({ colorWrite: false }));
    this.field.add(this.occluder);
    this.hole = referenceHole(mobile, this.uniforms.uTime, texture); this.scene.add(this.hole);
    const characterPreset = {
      nailong: { Core: NailongCore, coreSize: NAILONG_CORE_SIZE, coreName: '奶龙' },
      dafeiyu: { Core: DafeiyuCore, coreSize: DAFEIYU_CORE_SIZE, coreName: '大肥鱼1' },
      dafeiyu31: { Core: Dafeiyu31Core, coreSize: DAFEIYU31_CORE_SIZE, coreName: '大肥鱼2' },
      'dafeiyu-rice': { Core: DafeiyuRiceCore, coreSize: DAFEIYU_RICE_CORE_SIZE, coreName: '大肥鱼吃白饭' },
      gpt: { Core: GptCore, coreSize: GPT_CORE_SIZE, coreName: 'GPT' },
    }[variant];
    if (characterPreset && !referenceTexture) {
      const { Core, coreSize, coreName } = characterPreset;
      // Keep the original ray-marched gold emission and lensing. Only replace
      // its opaque sphere with the character silhouette once the asset is ready.
      this.hole.material.uniforms.uCharacterReady = { value: 0 };
      this.hole.material.uniforms.uCharacterMap = { value: null };
      this.hole.material.fragmentShader = 'uniform float uCharacterReady;\nuniform sampler2D uCharacterMap;\n' + this.hole.material.fragmentShader
        .replace('vec3 sphereNormal=normalize(uEye+rd*sphereDistance);', `
          if(uCharacterReady>.5){
            sphereDistance=-dot(uEye,uForward)/dot(rd,uForward);
            vec3 characterPoint=uEye+rd*sphereDistance;
            vec2 characterUv=vec2(dot(characterPoint,uRight),dot(characterPoint,uUp))/${coreSize.toFixed(2)}+.5;
            float inside=step(0.,characterUv.x)*step(characterUv.x,1.)*step(0.,characterUv.y)*step(characterUv.y,1.);
            sphereHit=inside*step(.08,texture2D(uCharacterMap,characterUv).a)*step(0.,sphereDistance);
          }
          vec3 sphereNormal=normalize(uEye+rd*sphereDistance);`)
        .replace('light+=bodyLight*sphereHit*(1.-opacity);', 'light+=bodyLight*sphereHit*(1.-opacity)*(1.-uCharacterReady);')
        .replace('captured=max(captured,sphereHit);', 'captured=max(captured,sphereHit)*(1.-uCharacterReady);');
      this.character = new Core({ onReady: () => {
        this.hole.material.uniforms.uCharacterMap.value = this.character.texture;
        this.hole.material.uniforms.uCharacterReady.value = 1;
        this.occluder.visible = false; onInvalidate?.();
      }, onError: () => onAssetError?.(`${coreName}素材加载失败，已显示黑洞核心`) });
      this.scene.add(this.character.mesh);
    }
    this.fragments = referenceFragments(mobile ? 96 : 168, this.uniforms.uTime, texture).mesh;
    this.field.add(this.fragments);
    this.sheets = []; this.ribbons = [];
    for (let i = 0; i < 9; i++) {
      const ribbon = referenceRibbon(i, mobile);
      if (!referenceTexture) anchorRibbonTail(ribbon.geometry);
      const material = new THREE.ShaderMaterial({
        uniforms: {
          uTime: this.uniforms.uTime, uAtlas: { value: texture }, uColor: { value: new THREE.Color(i >= 7 ? '#e8d5b4' : '#dec79b') },
          uHole: { value: new THREE.Vector3() }, uRepeat: { value: new THREE.Vector2(i >= 7 ? 1 / 6 : i === 3 ? 1 / 10 : 1 / 12, i >= 7 ? 1 / 4.5 : i === 3 ? 1 / 7.5 : 1 / 9) },
          uAtlasOffset: { value: new THREE.Vector2(i * .173, (i * 5 % 12) / 12) }, uFlow: { value: i >= 7 ? -.055 : .055 },
          uVisibility: { value: i >= 7 ? 0 : 1 }, uPointer: { value: new THREE.Vector2() }, uClickPointer: { value: new THREE.Vector2() },
          uPointerActive: { value: 0 }, uClickAge: { value: 100 }, uAspect: { value: 1 }, uInteraction: { value: 1 },
          uHover: { value: new THREE.Vector3(1000, 1000, 1000) }, uHoverStrength: { value: 0 }, uPulse: { value: 1 },
        },
        vertexShader: ribbonVertex, fragmentShader: ribbonFragment,
        transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide, forceSinglePass: true,
      });
      const mesh = new THREE.Mesh(ribbon.geometry, material); mesh.frustumCulled = false;
      this.field.add(mesh); this.sheets.push(mesh); this.ribbons.push(ribbon);
    }
    if (!referenceTexture) {
      this.emphasisRed = { value: 0 };
      this.hole.material.uniforms.uArrival = { value: 1 };
      this.hole.material.fragmentShader = 'uniform float uArrival;\n' + this.hole.material.fragmentShader.replace(
        'gl_FragColor=vec4(light,max(opacity,captured));', 'gl_FragColor=vec4(light*uArrival,max(opacity,captured)*uArrival);');
      for (const mesh of this.sheets) {
        mesh.material.uniforms.uArrival = { value: 1 };
        mesh.material.uniforms.uStates = { value: this.atlas.states };
        mesh.material.uniforms.uEmphasisRed = this.emphasisRed;
        mesh.material.uniforms.uTextTime = { value: 0 };
        mesh.material.uniforms.uEvidence = { value: new THREE.Vector4() };
        mesh.material.uniforms.uEvidenceStrength = { value: 0 };
        mesh.material.uniforms.uEvidenceAge = { value: 0 };
        mesh.material.uniforms.uEvidenceLetters = { value: new THREE.Vector2() };
        mesh.material.uniforms.uNavigationSafety = { value: 0 };
        mesh.material.fragmentShader = 'uniform float uArrival;\n' + hitShader + evidenceShader + ribbonFragment
          .replace('uTime*uFlow', 'uTextTime*uFlow')
          .replace('float primary=text*(.95*rowAccent+hover*3.5+wave*4.)*pulse;', evidenceInk)
          .replace('  gl_FragColor = vec4(tint * luminosity, 1.0);',
            '  gl_FragColor = vec4(tint * luminosity, uArrival);\n  gl_FragColor.rgb *= 1. - smoothstep(.45, 1., max(-vUv.x, vUv.x - 1.));')
          .replace('tint * luminosity',
          '((mix(subtitleTint(tint,lettering),mix(vec3(.08,1.,.28),vec3(1.,.035,.055),uEmphasisRed)*inkState.b,evidence)*primary + subtitleTint(tint,lettering*vec2(3.,4.)+vec2(.137,.319))*fineInk*(.10+coreLight*.16) + subtitleTint(tint,lettering*vec2(9.,11.)+vec2(.593,.721))*microInk*(.025+glint*.35*coreLight) + tint*(edge*(1.+wave*2.)+sheet))*uVisibility*mix(1.,.7,uEvidenceStrength*(1.-region)) + mix(vec3(.24,.95,.5),vec3(1.,.035,.055),uEmphasisRed)*locator*1.2)*mix(1.,smoothstep(1.2,3.2,length(cameraPosition-vWorld)),uNavigationSafety)');
      }
      this.fragments.material.uniforms.uStates = { value: this.atlas.states };
      this.fragments.material.uniforms.uEmphasisRed = this.emphasisRed;
      this.fragments.material.uniforms.uArrival = { value: 1 };
      this.fragments.material.uniforms.uNavigationSafety = { value: 0 };
      this.fragments.material.uniforms.uContextDim = { value: 1 };
      this.fragments.material.vertexShader = 'uniform float uNavigationSafety;\n' + this.fragments.material.vertexShader
        .replace('vFade=smoothstep(0.,.08,age);', 'vFade=smoothstep(0.,.08,age)*mix(1.,smoothstep(1.2,3.2,clip.w),uNavigationSafety);');
      this.fragments.material.fragmentShader = hitShader + 'uniform float uContextDim,uArrival;\n' + this.fragments.material.fragmentShader.replace('vColor*ink', 'subtitleTint(vColor,vAtlas)*ink*uContextDim').replace('*fade,1.);', '*fade,uArrival);');
    }
    if (['dafeiyu', 'dafeiyu31', 'dafeiyu-rice'].includes(variant) && !referenceTexture) applyDafeiyuPalette(this);
    if (variant === 'gpt' && !referenceTexture) applyGptPalette(this);
    if (variant === 'dafeiyu31' && !referenceTexture) {
      // Its opaque circular backdrop also conceals the distant sky, while
      // foreground subtitle ribbons and lens emission still cross in front.
      this.stars.group.children.forEach(node => { node.material.depthTest = true; });
    }
    this.pointer = new THREE.Vector2(); this.smoothedPointer = new THREE.Vector2();
    this.size = new THREE.Vector2(); this.lastSize = new THREE.Vector2();
    this.right = new THREE.Vector3(); this.up = new THREE.Vector3(); this.target = new THREE.Vector3();
    this.raycaster = new THREE.Raycaster(); this.picks = [];
    this.pickGeometry = new THREE.PlaneGeometry(1, 1, 8, 1);
    const vertices = this.pickGeometry.getAttribute('position');
    for (let i = 0; i < vertices.count; i++) vertices.setZ(i, vertices.getX(i) ** 2 * .1);
    this.pickGeometry.computeBoundingSphere();
    this.pickMaterial = new THREE.MeshBasicMaterial({ side: THREE.DoubleSide });
    this.proxies = Array.from({ length: mobile ? 96 : 168 }, () => new THREE.Mesh(this.pickGeometry, this.pickMaterial));
    this.simplified = false; this.disposed = false;
    this.journey = new GravityJourney(); this.featuredMarker = null;
    this.ribbonClockOffsets = new Map();
  }

  get journeyPending() { return this.journey.pending; }
  get coreScreenAnchor() {
    const center = new THREE.Vector3().project(this.camera);
    const edge = new THREE.Vector3(1.94, 0, 0).applyQuaternion(this.camera.quaternion).project(this.camera);
    return { x: (center.x + 1) / 2, y: (1 - center.y) / 2, radius: Math.abs(edge.x - center.x) / 2 };
  }
  get presentationPending() { return this.waitingEvidence || this.atlas.pending || this.journey.pending || Boolean(this.focusAnchor && this.journey.presentationPending); }
  get evidenceCue() {
    if (!this.focusAnchor || this.journey.resetting || this.featuredMarker == null) return null;
    const phase = this.journey.immediate ? 'still' : this.journey.pending ? 'travel' : this.journey.presentationPending ? 'arrival' : null;
    return phase ? { id: this.featuredMarker, serial: this.journey.serial, phase } : null;
  }
  get journeyCount() { return this.journey.count; }
  get pathProgress() { return this.journey.progress; }

  attachRenderer(renderer) {
    this.renderer = renderer;
    this.previous = { pixelRatio: renderer.getPixelRatio(), color: renderer.getClearColor(new THREE.Color()), alpha: renderer.getClearAlpha(),
      toneMapping: renderer.toneMapping, exposure: renderer.toneMappingExposure };
    this.pixelRatio = Math.min(globalThis.devicePixelRatio || 1, this.mobile ? 1.25 : 1.5);
    renderer.setPixelRatio(this.pixelRatio); renderer.setClearColor(0, 1);
    renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1;
    (this.referenceTexture ?? this.atlas.texture).anisotropy = renderer.capabilities.getMaxAnisotropy();
    this.composer = new EffectComposer(renderer);
    this.renderPass = new RenderPass(this.scene, this.camera);
    this.bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), this.bloomStrength, .3, 1.25);
    this.output = new OutputPass();
    this.composer.addPass(this.renderPass); this.composer.addPass(this.bloom); this.composer.addPass(this.output);
    this.stars.group.children[0].material.uniforms.uPixelRatio.value = this.pixelRatio;
  }

  render(renderer, { data, mappings = [], hits = new Map(), focus = null, ambient = 0, reduced = false, active = true, pointer, view = {}, aspect = 1, journey, motionDelta, arrival = 1 }) {
    if (this.disposed) return;
    if (!this.renderer) this.attachRenderer(renderer);
    const dt = this.lastAmbient === undefined ? 1 / 60 : THREE.MathUtils.clamp(ambient - this.lastAmbient, 0, .15);
    this.lastAmbient = ambient;
    const featureKeys = new Set(journey?.reset ? [] : journey?.keys ?? this.journey.keys);
    const featureCandidates = mappings.filter(m => m.keys.some(key => featureKeys.has(key)));
    const featureMapping = featureCandidates.find(m => String(m.id) === String(journey?.markerId))
      ?? featureCandidates.sort((a, b) => b.keys.filter(key => featureKeys.has(key)).length - a.keys.filter(key => featureKeys.has(key)).length)[0];
    if (!this.referenceTexture) this.atlas.update(data?.subtitles ?? [], mappings, featureMapping?.id ?? focus, hits,
      { delta: motionDelta ?? (active ? dt : 0), immediate: reduced || Boolean(journey?.immediate) });
    // The shared owner can lower DPR after sustained slow frames. Keep the
    // offscreen composer and the lens's pixel footprint in sync with that size.
    if (renderer.getPixelRatio() !== this.pixelRatio) {
      this.pixelRatio = renderer.getPixelRatio(); this.composer.setPixelRatio(this.pixelRatio);
      this.stars.group.children[0].material.uniforms.uPixelRatio.value = this.pixelRatio;
    }
    renderer.getSize(this.size);
    if (!this.size.equals(this.lastSize)) {
      this.composer.setSize(this.size.x, this.size.y); this.lastSize.copy(this.size);
    }
    if (active && !reduced) this.uniforms.uTime.value += dt;
    for (const mesh of this.sheets) if (mesh.material.uniforms.uTextTime) {
      mesh.material.uniforms.uTextTime.value = this.uniforms.uTime.value + (this.ribbonClockOffsets.get(mesh) ?? 0);
    }
    this.waitingEvidence = Boolean(journey && journey.serial !== this.journey.serial && !journey.reset && !this.atlas.ready(journey.keys));
    if (!this.waitingEvidence && this.journey.start(journey)) this.beginFocus(journey, active && !reduced && !journey.immediate ? this.journey.duration : 0, view, aspect);
    this.journey.advance(motionDelta ?? (active ? dt : 0), reduced || Boolean(journey?.immediate));
    if (this.emphasisRed) this.emphasisRed.value = this.journey.emphasisRed;
    if (this.focusTransport && !this.journey.resetting) {
      const { mesh, from, to } = this.focusTransport;
      const offset = THREE.MathUtils.lerp(from, to, this.journey.amount);
      this.ribbonClockOffsets.set(mesh, offset);
      mesh.material.uniforms.uTextTime.value = this.uniforms.uTime.value + offset;
    }
    // Keep the opening composition, but do not use the reference orbit as an
    // evidence destination: that route aims at the hole and can pull away.
    const progress = THREE.MathUtils.clamp(view.progress ?? 0, 0, 1);
    const camera = this.camera, pose = referenceCamera(progress, aspect < .8);
    const userZoom = view.zoom ?? 1;
    const peakZoom = 1 + (aspect < 1 ? .16 : .30) * (1 - smooth(userZoom, 1.2, 1.6));
    this.navigationZoom = this.journey.cameraScale(this.focusFromZoom ?? 1, peakZoom);
    const entry = reduced ? 1 : THREE.MathUtils.clamp(arrival, 0, 1);
    camera.aspect = aspect; camera.fov = aspect < .8 ? 65 : 46;
    camera.zoom = userZoom * this.navigationZoom * (this.focusAnchor ? 1 : .96 + .04 * smooth(entry, 0, 1));
    camera.position.fromArray(pose.position).applyQuaternion(this.field.quaternion);
    this.target.fromArray(pose.target).applyQuaternion(this.field.quaternion);
    camera.up.set(0, 1, 0).lerp(this.diskUp, Math.exp(-(((progress - .65) / .1) ** 4))).normalize();
    const axis = this.target.clone().sub(camera.position).normalize();
    camera.up.lerp(this.diskUp.clone().applyAxisAngle(axis, .43), smooth(progress, .73, .9)).normalize();
    camera.lookAt(this.target);
    const interactive = active && !reduced && pointer && !this.focusAnchor;
    this.pointer.set(interactive ? pointer.x : 0, interactive ? pointer.y : 0);
    if (reduced || !active) this.smoothedPointer.set(0, 0);
    else this.smoothedPointer.lerp(this.pointer, 1 - Math.exp(-dt * 8));
    this.right.setFromMatrixColumn(camera.matrix, 0);
    camera.position.addScaledVector(this.right, this.smoothedPointer.x * .3).addScaledVector(new THREE.Vector3(0, 1, 0), this.smoothedPointer.y * .3);
    camera.lookAt(this.target); camera.updateProjectionMatrix(); camera.updateMatrixWorld(); this.field.updateMatrixWorld(true);
    if (this.focusFrom) {
      const destination = this.focusAnchor && !this.journey.resetting
        ? evidenceCameraPose(this.focusDestination ?? this.focusAnchor, aspect, camera.zoom, this.journey.count - 1)
        : { position: camera.position.clone(), target: this.target.clone(), up: camera.up.clone() };
      // The evidence event ends, but an active analysis keeps touring. Use a
      // separate clock: the finite journey clock deliberately stops at 4.2 s.
      // Start at zero velocity and retain the offset when paused/completed.
      if (this.focusAnchor && !this.journey.resetting) {
        if (reduced) this.focusOrbitTime = 0;
        else if (active && !this.journey.pending) this.focusOrbitTime += Math.max(0, motionDelta ?? dt);
        const t = this.focusOrbitTime, ramp = smooth(t, 0, 2);
        const local = destination.position.clone().applyQuaternion(this.inverse);
        const radius = local.length();
        // Broader, slower arcs with two periods avoid repeating a tiny sway.
        // Alternate the departure direction between evidence events; a new
        // event still interpolates from the actual current camera pose.
        const direction = this.journey.count % 2 ? 1 : -1;
        const theta = Math.atan2(local.x, local.z)
          + direction * (.66 * Math.sin(t * .10) + .12 * Math.sin(t * .17)) * ramp;
        const elevation = Math.asin(local.y / radius)
          + (.06 * Math.sin(t * .11) + .035 * Math.sin(t * .067)) * ramp;
        const dolly = radius + (EVIDENCE_CAMERA_LIMITS.maxRadius - radius) * .65 * (1 - Math.cos(t * .30)) / 2;
        destination.position.set(Math.sin(theta) * Math.cos(elevation), Math.sin(elevation), Math.cos(theta) * Math.cos(elevation))
          .multiplyScalar(dolly).applyQuaternion(this.field.quaternion);
      }
      focusCamera(camera, this.focusFrom, destination, this.journey.amount, this.field.quaternion);
      if (!this.journey.pending && this.journey.resetting) { this.releaseFocus(); this.focusFrom = null; }
    }
    const viewDelta = Math.max(dt, motionDelta ?? 0);
    this.character?.update(camera);
    if (this.character) {
      // Preserve opaque depth/order so the foreground lens still crosses the
      // portrait; changing transparency here would also recompile its shader.
      this.character.mesh.material.color.setScalar(this.character.brightness * smooth(entry, .28, .78));
    }
    if (this.referenceTexture) for (const ribbon of this.ribbons) ribbon.update(camera, this.field.matrixWorld, viewDelta);
    this.stars.update(camera, pose.theta, pose.elevation, viewDelta);
    this.right.setFromMatrixColumn(camera.matrixWorld, 0).applyQuaternion(this.inverse);
    this.up.setFromMatrixColumn(camera.matrixWorld, 1).applyQuaternion(this.inverse);
    const h = this.hole.material.uniforms, f = this.fragments.material.uniforms;
    if (h.uArrival) h.uArrival.value = smooth(entry, 0, .45);
    if (f.uArrival) f.uArrival.value = smooth(entry, .4, .95);
    h.uEye.value.copy(camera.position).applyQuaternion(this.inverse);
    h.uRight.value.copy(this.right); h.uUp.value.copy(this.up);
    h.uForward.value.setFromMatrixColumn(camera.matrixWorld, 2).negate().applyQuaternion(this.inverse);
    h.uAspect.value = aspect; h.uTanFov.value = Math.tan(THREE.MathUtils.degToRad(camera.fov * .5)) / camera.zoom;
    h.uPixelHeight.value = this.size.y * this.pixelRatio; h.uPulse.value = 0;
    f.uPixelHeight.value = this.size.y; f.uTanFov.value = h.uTanFov.value;
    f.uRight.value.copy(this.right); f.uUp.value.copy(this.up); f.uPulse.value = 0;
    f.uPointer.value.copy(this.pointer);
    f.uPointerActive.value = reduced || !active ? 0 : THREE.MathUtils.lerp(f.uPointerActive.value, interactive ? 1 : 0, 1 - Math.exp(-dt * 13));
    h.uPointer.value.copy(this.pointer); h.uPointerActive.value = f.uPointerActive.value; h.uClickAge.value = 100; h.uInteraction.value = reduced ? 0 : 1;
    this.focusStrength = this.focusAnchor
      ? THREE.MathUtils.lerp(this.focusFromStrength ?? 0, this.journey.resetting ? 0 : 1, this.journey.amount) : 0;
    if (f.uNavigationSafety) f.uNavigationSafety.value = this.focusStrength;
    const movingAnchor = movingRibbonEvidence(this.focusAnchor, this.uniforms.uTime.value);
    const age = this.journey.emphasisAge;
    const emphasis = movingAnchor && !this.journey.pending && !this.journey.resetting && !reduced
      ? smooth(age, 0, .16) * (1 - smooth(age, 1.9, 2.6)) : 0;
    this.evidenceStrength = emphasis;
    if (f.uContextDim) f.uContextDim.value = 1 - emphasis * .3;
    this.sheets.forEach((mesh, i) => {
      const u = mesh.material.uniforms;
      if (u.uArrival) u.uArrival.value = smooth(entry, .25 + i * .018, .95);
      u.uVisibility.value = i >= 7 ? smooth(progress, .68, .78) * .85 : i === 3 ? .85 : i % 2 === 0 ? .42 : .18;
      if (mesh === movingAnchor?.mesh) {
        const first = movingAnchor.matched[0], last = movingAnchor.matched.at(-1);
        const center = (42 + (first + last + 1) * 39) / 1024;
        const shift = (center - movingAnchor.textX) * movingAnchor.flowWidth;
        u.uVisibility.value = THREE.MathUtils.lerp(u.uVisibility.value, .8, emphasis);
        u.uEvidence.value.set(movingAnchor.flow.x + shift, movingAnchor.flow.y,
          movingAnchor.flowWidth * ((last - first + 1) * 78 + 22) / 2048, movingAnchor.flowHeight * .21);
        u.uEvidenceLetters.value.set(first, last - first + 1);
      } else u.uVisibility.value *= 1 - emphasis * .3;
      if (u.uEvidenceStrength) u.uEvidenceStrength.value = mesh === movingAnchor?.mesh ? emphasis : 0;
      if (u.uEvidenceAge) u.uEvidenceAge.value = age;
      if (u.uNavigationSafety) u.uNavigationSafety.value = this.focusStrength;
      u.uPointer.value.copy(this.pointer); u.uPointerActive.value = f.uPointerActive.value;
      u.uAspect.value = aspect; u.uInteraction.value = reduced ? 0 : 1;
    });
    this.scene.updateMatrixWorld(true);
    this.updatePicks(hits);
    this.featuredMarker = this.focusAnchor ? featureMapping?.id ?? null : null;
    this.composer.render(dt);
  }

  releaseFocus() {
    this.focusAnchor = null; this.focusDestination = null;
  }

  beginFocus(request, lookAhead = 0, view = {}, aspect = this.camera.aspect) {
    this.focusOrbitTime = 0;
    this.focusFrom = { position: this.camera.position.clone(),
      target: this.camera.getWorldDirection(new THREE.Vector3()).multiplyScalar(this.camera.position.length()).add(this.camera.position),
      up: new THREE.Vector3(0, 1, 0).applyQuaternion(this.camera.quaternion) };
    this.focusFromStrength = this.focusStrength ?? 0;
    this.focusFromZoom = this.navigationZoom ?? 1;
    this.focusTransport = null;
    if (request.reset) return;
    this.field.updateMatrixWorld(true);
    const selectionCamera = this.camera.clone(), userZoom = view.zoom ?? 1;
    selectionCamera.aspect = aspect; selectionCamera.fov = aspect < .8 ? 65 : 46;
    selectionCamera.zoom = userZoom * (lookAhead ? 1 + (aspect < 1 ? .16 : .30) * (1 - smooth(userZoom, 1.2, 1.6)) : 1);
    selectionCamera.updateProjectionMatrix();
    const anchor = findRibbonEvidence(this.sheets, this.atlas, request.keys, selectionCamera, this.uniforms.uTime.value, this.focusAnchor, this.journey.count - 1, lookAhead);
    if (anchor !== this.focusAnchor) {
      this.releaseFocus();
      this.focusAnchor = anchor;
    }
    this.focusDestination = movingRibbonEvidence(anchor, this.uniforms.uTime.value + lookAhead,
      (anchor?.clock ?? this.uniforms.uTime.value) + lookAhead + (anchor?.clockShift ?? 0)) ?? anchor;
    if (anchor) {
      const from = this.ribbonClockOffsets.get(anchor.mesh) ?? 0;
      this.focusTransport = { mesh: anchor.mesh, from, to: from + anchor.clockShift };
    }
    // No matching inscription means no substitute or fabricated front layer.
    if (!anchor) this.focusFrom = null;
  }

  // CPU counterpart of the source floating-fragment vertex shader. These meshes
  // are never rendered: they only make actual moving text clickable, rather than
  // adding a fixed foreground text shelf that changes the reference composition.
  updatePicks(hits) {
    this.hits = hits;
    this.picks = [];
    if (this.referenceTexture || !this.atlas.hitCount) return;
    const time = this.uniforms.uTime.value, u = this.fragments.material.uniforms;
    for (let seed = 0; seed < this.fragments.geometry.instanceCount; seed++) {
      const cycle = Math.floor(seed * .61803398875 + time / 28), index = (seed * 17 + cycle * 13) % 48;
      const slot = (11 - Math.floor(index / 4)) * 4 + index % 4;
      const entry = this.atlas.tiles[slot];
      if (this.atlas.opacity[slot] <= .1 || !entry?.keys.some(key => hits.get(key) > 0)) continue;
      const age = fract(seed * .61803398875 + time / 28), lane = seed % 7, q = (1 - age) ** .92;
      if (age < .025) continue;
      const angle = lane * .898 + .78 - q * (2.85 + .28 * Math.sin(lane * 2.1));
      const baseRadius = .8 + (21.3 + lane * .8) * q ** .85;
      const band = (1.4 + baseRadius * .32) * (lane % 2 === 0 || lane === 3 ? 1 : .48) * Math.min(1, q * 5);
      const across = (fract(seed * .3234) - .5) * .6;
      const bank = (lane === 3 ? 1.3 : lane === 4 ? 1.12 : lane === 6 ? .7 : .22) * Math.sin(Math.min(1, q * 1.8) * 3.14159 * .5);
      let r = baseRadius + across * band * Math.cos(bank);
      let y = -.55 * Math.min(1, q * 5) - q * (1.5 + lane * .18) + Math.sin(q * 3.14159 * 1.6 + lane) * q * .7;
      y = (y + across * band * Math.sin(bank) + (lane === 3 ? .55 * Math.sin(q * 3.14159) : 0)) * smooth(baseRadius, 1.94, 5) + .18 * smooth(r, 1.3, 5);
      const center = new THREE.Vector3(Math.cos(angle) * r, y, Math.sin(angle) * r);
      r = center.length(); center.y += Math.sin(seed * 1.713 + angle) * Math.sin(3.14159 * smooth(r, 1.35, 5)) * .9; r = center.length();
      const world = center.clone().applyMatrix4(this.field.matrixWorld), screen = world.clone().project(this.camera);
      const distance = screen.distanceTo(new THREE.Vector3(this.pointer.x, this.pointer.y, screen.z));
      const clipW = -world.clone().applyMatrix4(this.camera.matrixWorldInverse).z;
      if (this.focusStrength > .99 && clipW < 1.2) continue;
      const hover = Math.exp(-distance * distance * 25) * u.uPointerActive.value * (clipW > 0 ? 1 : 0);
      const away = new THREE.Vector2(screen.x - this.pointer.x, screen.y - this.pointer.y); away.divideScalar(Math.max(.01, away.length()));
      center.addScaledVector(this.right, away.x * hover * .65 * smooth(r, 1.94, 5)).addScaledVector(this.up, away.y * hover * .65 * smooth(r, 1.94, 5));
      const hero = fract(seed * .713) >= .76 ? 1 : 0, baseWidth = .7 + fract(seed * .517) * .65 + hero * .8;
      const minimum = (24 + hero * 30) * 2 * Math.max(1, clipW) * u.uTanFov.value / Math.max(1, this.size.y);
      const width = Math.max(baseWidth, Math.min(baseWidth * 1.6, minimum)) * (.18 + .82 * smooth(r, 1.3, 5));
      const right = this.right.clone().multiplyScalar(.92).addScaledVector(new THREE.Vector3(-Math.sin(angle), .08, Math.cos(angle)).normalize(), .08).normalize();
      const normal = new THREE.Vector3().crossVectors(right, this.up).normalize();
      const proxy = this.proxies[seed];
      proxy.matrixAutoUpdate = false;
      proxy.matrix.makeBasis(right.multiplyScalar(width), this.up.clone().multiplyScalar(width * .25), normal.multiplyScalar(width)).setPosition(center);
      proxy.matrixWorld.multiplyMatrices(this.field.matrixWorld, proxy.matrix);
      proxy.userData = { entry, index, slot, hits };
      this.picks.push(proxy);
    }
  }

  pick(x, y, rect) {
    if (!rect.width || !rect.height || this.disposed) return null;
    this.raycaster.setFromCamera(new THREE.Vector2((x - rect.left) / rect.width * 2 - 1, 1 - (y - rect.top) / rect.height * 2), this.camera);
    const obstruction = this.character?.mesh.visible ? this.character.obstruction(this.raycaster)
      : this.raycaster.intersectObject(this.occluder)[0]?.distance ?? Infinity;
    // Resolve the emphasized ink against its rippled surface. Raycasting only
    // the unperturbed base mesh can miss a thin glyph on a strongly banked fold.
    const moving = movingRibbonEvidence(this.focusAnchor, this.uniforms.uTime.value);
    if (moving && this.atlas.opacity[moving.slot] > .1) for (const index of moving.matched) {
      const key = moving.entry.keys[index], markerId = this.atlas.markerByKey.get(key);
      if (!(this.hits.get(key) > 0) || markerId === undefined) continue;
      const cx = moving.flow.x + ((42 + (index + .5) * 78) / 1024 - moving.textX) * moving.flowWidth;
      const dx = moving.flowWidth * 35 / 1024, dy = moving.flowHeight * .14;
      const corners = [[-1, -1], [1, -1], [1, 1], [-1, 1]].map(([sx, sy]) =>
        ribbonSample(moving.mesh, cx + dx * sx, moving.flow.y + dy * sy, this.uniforms.uTime.value)?.center);
      if (corners.some(point => !point)) continue;
      for (const [a, b, c] of [[0, 1, 2], [0, 2, 3]]) {
        const point = this.raycaster.ray.intersectTriangle(corners[a], corners[b], corners[c], false, new THREE.Vector3());
        if (!point) continue;
        const distance = point.distanceTo(this.raycaster.ray.origin);
        if (distance > 1.2 && distance < obstruction) return markerId;
      }
    }
    for (const hit of this.raycaster.intersectObjects(this.picks)) {
      if (hit.distance >= obstruction || !hit.uv || hit.uv.y < .25 || hit.uv.y > .75) continue;
      const { index, hits } = hit.object.userData;
      const char = this.atlas.characterAt((index % 4 + hit.uv.x) / 4, (Math.floor(index / 4) + hit.uv.y) / 12);
      if (char && this.atlas.opacity[char.slot] > .1 && hits.get(char.key) > 0 && char.markerId !== undefined) return char.markerId;
    }
    // Reference hover picking also raycasts the base ribbon geometry; its small
    // shader ripple remains a visual offset. Recover physical flow UVs instead
    // of interpreting the sheet's longitudinal UV as a subtitle index.
    for (const hit of this.raycaster.intersectObjects(this.sheets.filter(mesh => mesh.material.uniforms.uVisibility.value > .1))) {
      if (hit.distance >= obstruction || !hit.face || (this.focusStrength > .99 && hit.distance < 1.2)) continue;
      const geometry = hit.object.geometry, positions = geometry.getAttribute('position'), flow = geometry.getAttribute('aFlowUv');
      const local = hit.object.worldToLocal(hit.point.clone()), { a, b, c } = hit.face;
      const bary = THREE.Triangle.getBarycoord(local, new THREE.Vector3().fromBufferAttribute(positions, a),
        new THREE.Vector3().fromBufferAttribute(positions, b), new THREE.Vector3().fromBufferAttribute(positions, c), new THREE.Vector3());
      if (!bary) continue;
      const uv = new THREE.Vector2().fromBufferAttribute(flow, a).multiplyScalar(bary.x)
        .addScaledVector(new THREE.Vector2().fromBufferAttribute(flow, b), bary.y).addScaledVector(new THREE.Vector2().fromBufferAttribute(flow, c), bary.z);
      const u = hit.object.material.uniforms;
      uv.multiply(u.uRepeat.value).add(u.uAtlasOffset.value); uv.x += (u.uTextTime?.value ?? this.uniforms.uTime.value) * u.uFlow.value;
      const rowY = fract(uv.y * 12);
      if (rowY < .32 || rowY > .68) continue;
      const char = this.atlas.characterAt(uv.x, uv.y);
      if (char && this.atlas.opacity[char.slot] > .1 && this.hits.get(char.key) > 0 && char.markerId !== undefined) return char.markerId;
    }
    return null;
  }

  pointForMarker(id, rect) {
    if (this.focusAnchor) {
      const moving = movingRibbonEvidence(this.focusAnchor, this.uniforms.uTime.value);
      const { mesh, flow, entry } = moving ?? this.focusAnchor, u = mesh.material.uniforms;
      for (let i = 0; i < entry.keys.length; i++) {
        if (String(this.atlas.markerByKey.get(entry.keys[i])) !== String(id) || !(this.hits.get(entry.keys[i]) > 0)) continue;
        const x = flow.x + ((42 + (i + .5) * 78) / 1024 - this.focusAnchor.textX) / 4 / u.uRepeat.value.x;
        const sample = ribbonSample(mesh, x, flow.y, this.uniforms.uTime.value);
        if (!sample) continue;
        const point = sample.center.project(this.camera);
        const screen = { x: rect.left + (point.x + 1) * rect.width / 2, y: rect.top + (1 - point.y) * rect.height / 2 };
        if (Math.abs(point.x) < .95 && Math.abs(point.y) < .9 && Math.abs(point.z) < 1 && String(this.pick(screen.x, screen.y, rect)) === String(id)) return screen;
      }
    }
    for (const proxy of this.picks) {
      const { entry, hits } = proxy.userData;
      for (let i = 0; i < entry.keys.length; i++) {
        const key = entry.keys[i];
        if (String(this.atlas.markerByKey.get(key)) !== String(id) || !(hits.get(key) > 0)) continue;
        const x = (42 + (i + .5) * 78) / 1024 - .5;
        const point = new THREE.Vector3(x, 0, x * x * .1).applyMatrix4(proxy.matrixWorld).project(this.camera);
        if (Math.abs(point.x) > .95 || Math.abs(point.y) > .9 || Math.abs(point.z) > 1) continue;
        const screen = { x: rect.left + (point.x + 1) * rect.width / 2, y: rect.top + (1 - point.y) * rect.height / 2 };
        if (String(this.pick(screen.x, screen.y, rect)) === String(id)) return screen;
      }
    }
    return null;
  }

  reduceQuality() {
    if (this.simplified) return;
    this.simplified = true;
    this.fragments.geometry.instanceCount = 96;
    this.stars.group.children[0].geometry.setDrawRange(0, 24000);
    // Keep all nine source ribbons, the lens and bloom. Only reduce peripheral
    // density after sustained slow frames, never on initial entry.
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.character?.dispose();
    const geometries = new Set([this.pickGeometry]), materials = new Set([this.pickMaterial]);
    this.scene.traverse(node => { if (node.geometry) geometries.add(node.geometry); if (node.material) materials.add(node.material); });
    geometries.forEach(g => g.dispose()); materials.forEach(m => m.dispose());
    this.atlas.dispose(); this.bloom?.dispose(); this.output?.dispose(); this.renderPass?.dispose(); this.composer?.dispose();
    if (this.renderer) {
      const p = this.previous;
      this.renderer.setPixelRatio(p.pixelRatio); this.renderer.setClearColor(p.color, p.alpha);
      this.renderer.toneMapping = p.toneMapping; this.renderer.toneMappingExposure = p.exposure;
    }
    this.scene.clear(); this.picks = []; this.proxies = [];
  }
}
