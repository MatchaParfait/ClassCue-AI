import { Vector3 } from 'three';

// The source's offscreen continuation follows the camera. Its screen-space
// front/behind branches can whip visible vertices across the scene during our
// wider evidence turns. Build a world-space continuation once instead. Leave
// every original body vertex and its physical text coordinates untouched.
export function anchorRibbonTail(geometry) {
  const positions = geometry.getAttribute('position'), uv = geometry.getAttribute('uv');
  const flow = geometry.getAttribute('aFlowUv'), normals = geometry.getAttribute('normal');
  let stride = 1;
  while (stride < uv.count && uv.getX(stride) === uv.getX(0)) stride++;
  let tailStart = stride;
  while (tailStart < uv.count && uv.getX(tailStart) >= 0 && uv.getX(tailStart) <= 1) tailStart += stride;
  if (tailStart >= uv.count) return;
  const atStart = uv.getX(tailStart) < 0;
  const anchor = atStart ? 0 : tailStart - stride;
  const neighbor = atStart ? stride : anchor - stride;
  const mid = Math.floor(stride / 2);
  const origin = new Vector3().fromBufferAttribute(positions, anchor + mid);
  const tangent = origin.clone().sub(new Vector3().fromBufferAttribute(positions, neighbor + mid)).normalize();
  const outward = new Vector3(origin.x, 0, origin.z).normalize();
  const edge = new Vector3().fromBufferAttribute(positions, anchor + stride - 1)
    .sub(new Vector3().fromBufferAttribute(positions, anchor));
  const anchorNormal = new Vector3().fromBufferAttribute(normals, anchor + mid);
  const rows = (uv.count - tailStart) / stride;
  let arc = 0;
  const previous = origin.clone(), center = new Vector3(), normal = new Vector3(), point = new Vector3();
  for (let row = 1; row <= rows; row++) {
    const distance = 48 * row / rows;
    center.copy(origin).addScaledVector(tangent, distance).addScaledVector(outward, .005 * distance * distance);
    arc += center.distanceTo(previous); previous.copy(center);
    normal.copy(tangent).addScaledVector(outward, .01 * distance).cross(edge).normalize();
    if (normal.dot(anchorNormal) < 0) normal.negate();
    for (let column = 0; column < stride; column++) {
      const index = tailStart + (row - 1) * stride + column;
      // Retain the exact attachment cross-section, including its center offset.
      point.fromBufferAttribute(positions, anchor + column).sub(origin).add(center);
      positions.setXYZ(index, point.x, point.y, point.z);
      normals.setXYZ(index, normal.x, normal.y, normal.z);
      flow.setXY(index, flow.getX(anchor + column) + (atStart ? -arc : arc), flow.getY(anchor + column));
    }
  }
  positions.needsUpdate = normals.needsUpdate = flow.needsUpdate = true;
  geometry.computeBoundingSphere();
}
