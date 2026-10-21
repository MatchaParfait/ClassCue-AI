import * as THREE from 'three';

const grids = new WeakMap();
// Invert the actual ribbon's physical text UVs. Only use its permanent body;
// the reference's camera-following tail must never become a camera target.
export function ribbonSample(mesh, x, y, time = 0) {
  const geometry = mesh.geometry, flow = geometry.getAttribute('aFlowUv');
  const positions = geometry.getAttribute('position'), uv = geometry.getAttribute('uv');
  let grid = grids.get(geometry);
  if (!grid) {
    let stride = 1;
    while (stride < uv.count && uv.getX(stride) === uv.getX(0)) stride++;
    let last = 0;
    while ((last + 1) * stride < uv.count && uv.getX((last + 1) * stride) <= 1) last++;
    grid = { stride, last }; grids.set(geometry, grid);
  }
  const { stride, last } = grid;
  if (x < flow.getX(0) || x >= flow.getX(last * stride)) return null;
  let lo = 0, hi = last;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (flow.getX(mid * stride) <= x) lo = mid; else hi = mid; }
  const row = lo * stride, next = hi * stride;
  const t = (x - flow.getX(row)) / (flow.getX(next) - flow.getX(row));
  const ymin = THREE.MathUtils.lerp(flow.getY(row), flow.getY(next), t);
  const ymax = THREE.MathUtils.lerp(flow.getY(row + stride - 1), flow.getY(next + stride - 1), t);
  const cross = (y - ymin) / (ymax - ymin);
  if (!Number.isFinite(cross) || cross < .05 || cross > .95) return null;
  const column = Math.min(stride - 2, Math.floor(cross * (stride - 1)));
  const a = row + column, b = a + 1, c = next + column, d = c + 1;
  const query = new THREE.Vector3(x, y, 0);
  for (const indices of [[a, b, c], [c, b, d]]) {
    const tex = indices.map(i => new THREE.Vector3(flow.getX(i), flow.getY(i), 0));
    const bary = THREE.Triangle.getBarycoord(query, ...tex, new THREE.Vector3());
    if (!bary || Math.min(bary.x, bary.y, bary.z) < -1e-5) continue;
    const points = indices.map(i => {
      const p = new THREE.Vector3().fromBufferAttribute(positions, i);
      p.y += Math.sin(p.x * .35 + p.z * .24 + time * .15) * .075;
      return p.applyMatrix4(mesh.matrixWorld);
    });
    const center = points[0].clone().multiplyScalar(bary.x).addScaledVector(points[1], bary.y).addScaledVector(points[2], bary.z);
    const ab = points[1].clone().sub(points[0]), ac = points[2].clone().sub(points[0]);
    const du1 = tex[1].x - tex[0].x, dv1 = tex[1].y - tex[0].y;
    const du2 = tex[2].x - tex[0].x, dv2 = tex[2].y - tex[0].y, det = du1 * dv2 - du2 * dv1;
    if (Math.abs(det) < 1e-8) continue;
    const dx = ab.clone().multiplyScalar(dv2).addScaledVector(ac, -dv1).divideScalar(det);
    const dy = ac.clone().multiplyScalar(du1).addScaledVector(ab, -du2).divideScalar(det);
    const right = dx.clone().normalize(), normal = new THREE.Vector3().crossVectors(dx, dy).normalize();
    const up = new THREE.Vector3().crossVectors(normal, right).normalize();
    return { center, right, up, normal, dx, dy, flow: new THREE.Vector2(x, y), mesh };
  }
  return null;
}

export function findRibbonEvidence(sheets, atlas, keys, camera, time, previous, turn = 0, lookAhead = 0) {
  const wanted = new Set(keys), candidates = [];
  atlas.tiles.forEach((entry, slot) => {
    const matched = entry?.keys.map((key, index) => wanted.has(key) ? index : -1).filter(i => i >= 0) ?? [];
    if (matched.length) candidates.push({ slot, entry, matched });
  });
  const inverse = sheets[0].parent.quaternion.clone().invert();
  const eye = camera.position.clone().applyQuaternion(inverse);
  const fromTheta = Math.atan2(eye.x, eye.z), direction = turn % 2 ? -1 : 1;
  let best = null, bestScore = -Infinity;
  const evaluate = (base, visible) => {
    const u = base.mesh.material.uniforms;
    // Slide the existing ribbon's ink, rather than making a foreground copy.
    // Nonzero offsets are only accepted if they move the real phrase outward.
    for (const shift of lookAhead ? [0, -4, 4, 8] : [0]) {
      const clockShift = -shift * u.uRepeat.value.x / u.uFlow.value;
      const future = movingRibbonEvidence(base, time + lookAhead, base.clock + lookAhead + clockShift);
      if (!future || future.center.length() > 18 || (shift && future.center.length() < base.center.length() + .5)) continue;
      for (let poseOffset = 0; poseOffset < 4; poseOffset++) {
        const cameraTheta = fromTheta + direction * [.60, -.60, .85, -.85][poseOffset];
        const candidate = { ...base, poseOffset, cameraTheta, clockShift };
        const pose = evidenceCameraPose({ ...future, cameraTheta, poseOffset }, camera.aspect, camera.zoom, turn);
        const preview = camera.clone(); preview.position.copy(pose.position); preview.up.copy(pose.up);
        preview.lookAt(pose.target); preview.updateMatrixWorld();
        const arriving = future.center.clone().project(preview);
        if (Math.abs(arriving.x) > .85 || Math.abs(arriving.y) > .8 || Math.abs(arriving.z) > 1) continue;
        const facing = Math.abs(future.normal.dot(pose.position.clone().sub(future.center).normalize()));
        if (facing < .25) continue;
        const sight = new THREE.Line3(pose.position, future.center);
        if (sight.closestPointToPoint(new THREE.Vector3(), true, new THREE.Vector3()).length() < 4) continue;
        const end = future.center.clone().addScaledVector(future.dx, base.flowWidth * base.matched.length * 78 / 1024).project(preview);
        const top = future.center.clone().addScaledVector(future.dy, base.flowHeight * .28).project(preview);
        const inkX = new THREE.Vector2((end.x - arriving.x) * camera.aspect, end.y - arriving.y);
        const inkY = new THREE.Vector2((top.x - arriving.x) * camera.aspect, top.y - arriving.y);
        const width = inkX.length(), height = Math.abs(inkX.cross(inkY)) / Math.max(.001, width);
        if (shift) {
          const natural = movingRibbonEvidence(base, time + lookAhead, base.clock + lookAhead);
          if (!natural || height < .012) continue;
          const p = natural.center.clone().project(preview);
          const x = natural.center.clone().addScaledVector(natural.dx, base.flowWidth * base.matched.length * 78 / 1024).project(preview);
          const y = natural.center.clone().addScaledVector(natural.dy, base.flowHeight * .28).project(preview);
          const baseline = new THREE.Vector2((x.x - p.x) * camera.aspect, x.y - p.y);
          const glyph = new THREE.Vector2((y.x - p.x) * camera.aspect, y.y - p.y);
          const naturalHeight = Math.abs(baseline.cross(glyph)) / Math.max(.001, baseline.length());
          // An outer location on the far side may look smaller. Only relocate
          // when the actual projected strokes also become noticeably larger.
          if (height < naturalHeight * 1.15) continue;
        }
        const halfX = Math.abs(end.x - arriving.x) * .55;
        if (camera.aspect > 1.2 && (arriving.x - halfX < -.42 || arriving.x + halfX > .48)) continue;
        const outward = Math.min(3, Math.max(0, future.center.length() - base.center.length()));
        const score = (visible ? 1 : 0) + base.matched.length * .1 + Math.min(1.2, width) * 2
          + Math.min(.075, height) * 500 + facing + outward * 1.5
          - Math.hypot(arriving.x - .12, arriving.y + .3) * 4 - poseOffset * .15;
        if (score <= bestScore) continue;
        bestScore = score; best = candidate;
      }
    }
  };
  // A repeated click first tries to transport the same physical inscription.
  if (previous && candidates.some(c => c.slot === previous.slot && c.entry.id === previous.entry.id
    && c.matched.join() === previous.matched.join())) {
    const moving = movingRibbonEvidence(previous, time);
    if (moving) evaluate(moving, true);
    if (best) return best;
  }
  for (const { slot, entry, matched } of candidates) {
    const textX = (42 + (matched[Math.floor(matched.length / 2)] + .5) * 78) / 1024;
    const tileX = (slot % 4 + textX) / 4, tileY = (11 - Math.floor(slot / 4) + .5) / 12;
    for (const mesh of sheets.slice(0, 7)) {
      const u = mesh.material.uniforms, repeat = u.uRepeat.value, offset = u.uAtlasOffset.value;
      const clock = u.uTextTime.value;
      for (let rx = -1; rx <= 12; rx++) for (let ry = -1; ry <= 1; ry++) {
        const fx = (tileX + rx - offset.x - clock * u.uFlow.value) / repeat.x;
        const fy = (tileY + ry - offset.y) / repeat.y;
        const sample = ribbonSample(mesh, fx, fy, time);
        if (!sample || sample.center.length() < 8 || sample.center.length() > 18) continue;
        const screen = sample.center.clone().project(camera);
        const visible = Math.abs(screen.x) < .95 && Math.abs(screen.y) < .85 && Math.abs(screen.z) < 1;
        evaluate({ ...sample, slot, entry, matched, textX, clock, width: sample.dx.length() * .25 / repeat.x,
          flowWidth: .25 / repeat.x, flowHeight: 1 / 12 / repeat.y }, visible);
      }
    }
  }
  return best;
}

// Camera coordinates are bounded in the tilted field's local space. Its seven
// permanent text sheets top out at y=2.56 (plus a 0.075 shader ripple).
export const EVIDENCE_CAMERA_LIMITS = Object.freeze({ minRadius: 18.2, maxRadius: 20.5, minHeight: 3.05 });
const wrap = angle => Math.atan2(Math.sin(angle), Math.cos(angle));

export function movingRibbonEvidence(anchor, time, predictedClock) {
  if (!anchor) return null;
  const u = anchor.mesh.material.uniforms, clock = predictedClock ?? u.uTextTime.value;
  const x = anchor.flow.x - (clock - anchor.clock) * u.uFlow.value / u.uRepeat.value.x;
  const sample = ribbonSample(anchor.mesh, x, anchor.flow.y, time);
  return sample ? { ...anchor, ...sample, clock } : null;
}

export function evidenceCameraPose(anchor, aspect, zoom, turn) {
  turn += anchor.poseOffset ?? 0;
  const rotation = anchor.mesh.parent.quaternion, inverse = rotation.clone().invert();
  const local = anchor.center.clone().applyQuaternion(inverse);
  const home = -.55;
  const bias = THREE.MathUtils.clamp(wrap(Math.atan2(local.x, local.z) - home) * .28, -.34, .34);
  const theta = anchor.cameraTheta ?? home + bias + [-.14, .17, -.07, .22][turn % 4];
  const radius = aspect < 1 ? 20.5 : [18.6, 19.2, 18.2, 19][turn % 4];
  const elevation = [.28, .32, .30, .27][turn % 4];
  const position = new THREE.Vector3(Math.sin(theta) * Math.cos(elevation), Math.sin(elevation), Math.cos(theta) * Math.cos(elevation))
    .multiplyScalar(radius).applyQuaternion(rotation);
  const fov = aspect < .8 ? 65 : 46;
  // Frame the hole and its surrounding ribbons together. The hit supplies a
  // restrained look-at bias, never a camera destination on the text surface.
  const maxBias = Math.min(1.4, radius * Math.tan(THREE.MathUtils.degToRad(fov / 2)) * Math.min(aspect, 1) / zoom * .14);
  const target = anchor.center.clone().multiplyScalar(.16).clampLength(0, maxBias);
  return { position, target, up: new THREE.Vector3(0, 1, 0), rotation };
}

export function focusCamera(camera, from, to, amount, rotation) {
  // Follow a spherical arc rather than a chord through the text sheets.
  const inverse = rotation.clone().invert();
  const a = from.position.clone().applyQuaternion(inverse), b = to.position.clone().applyQuaternion(inverse);
  const ar = Math.max(.1, a.length()), br = b.length();
  const radius = THREE.MathUtils.lerp(ar, br, amount);
  const theta = Math.atan2(a.x, a.z) + wrap(Math.atan2(b.x, b.z) - Math.atan2(a.x, a.z)) * amount;
  const elevation = THREE.MathUtils.lerp(Math.asin(a.y / ar), Math.asin(b.y / br), amount);
  const local = new THREE.Vector3(Math.sin(theta) * Math.cos(elevation), Math.sin(elevation), Math.cos(theta) * Math.cos(elevation)).multiplyScalar(radius);
  local.y = Math.max(EVIDENCE_CAMERA_LIMITS.minHeight, local.y);
  camera.position.copy(local.applyQuaternion(rotation));
  camera.up.copy(from.up).lerp(to.up, amount).normalize();
  camera.lookAt(from.target.clone().lerp(to.target, amount));
  camera.updateMatrixWorld();
}
