export const WALL = Object.freeze({ halfWidth: 12, back: -18, front: -2, top: 9.3, bottom: -4 });

// Unfold the three vertical faces into one strip with shared rear corners.
export function wallStrip(distance) {
  const depth = WALL.front - WALL.back;
  const width = WALL.halfWidth * 2;
  if (distance < depth) return { x: -WALL.halfWidth, z: WALL.front - distance, yaw: Math.PI / 2, face: 'left' };
  if (distance <= depth + width) return { x: -WALL.halfWidth + distance - depth, z: WALL.back, yaw: 0, face: 'back' };
  return { x: WALL.halfWidth, z: WALL.back + distance - depth - width, yaw: -Math.PI / 2, face: 'right' };
}

export function wallLayout(index, total) {
  const wallCount = Math.ceil(total * 0.82);
  if (index < wallCount) {
    const rows = Math.min(44, Math.max(1, Math.floor(Math.sqrt(wallCount / 4))));
    const columns = Math.ceil(wallCount / rows);
    const column = Math.floor(index / rows), row = index % rows;
    const perimeter = 2 * (WALL.front - WALL.back + WALL.halfWidth);
    const surface = wallStrip((column + 0.5) / columns * perimeter);
    return { position: [surface.x, WALL.top - (row + 0.5) / rows * (WALL.top - WALL.bottom), surface.z],
      yaw: surface.yaw, pitch: 0, face: surface.face, column };
  }
  const count = total - wallCount;
  const columns = Math.max(1, Math.ceil(Math.sqrt(count * 1.5)));
  const rows = Math.ceil(count / columns);
  const slot = index - wallCount, column = slot % columns, row = Math.floor(slot / columns);
  return { position: [-WALL.halfWidth + (column + 0.5) / columns * WALL.halfWidth * 2, WALL.bottom,
    WALL.back + (row + 0.5) / rows * (WALL.front - WALL.back)],
    yaw: 0, pitch: -Math.PI / 2, face: 'floor', column };
}
