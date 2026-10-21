import { TIMES, smooth } from './core.js';

export const REPLACE_DISSOLVE = 8;
export const REPLACE_APPEAR = 8;
export const REPLACE_SWEEP = 12;
export const REPLACE_GROUP = 8;
export const REPLACE_CUE = Object.freeze({ warning: 6, hold: 4, fade: 12, strength: 0.72 });
export const REPLACE_INTERVAL = REPLACE_SWEEP + REPLACE_APPEAR + REPLACE_CUE.hold + REPLACE_CUE.fade + REPLACE_DISSOLVE + REPLACE_CUE.warning;
export const RIPPLE = Object.freeze({ loop: 210, halfWidth: 0.20, amplitude: 0.32, quiet: 0.18 });
export const PATH_SAMPLES = 1024;
const TAU = Math.PI * 2;
const wrap = (v) => ((v % 1) + 1) % 1;
const mix = (a, b, t) => a.map((v, i) => v + (b[i] - v) * t);

function curve(t) {
  const s = Math.sin(t), c = Math.cos(t), d = 1 + s * s;
  return [13 * c / d, 11 * s * c / d, 2.25 * s];
}

// Equal arc-length samples for static row placement and each row's local axis.
const raw = Array.from({ length: 8193 }, (_, i) => curve(i / 8192 * TAU));
const distances = [0];
for (let i = 1; i < raw.length; i++) distances.push(distances[i - 1] + Math.hypot(...raw[i].map((v, j) => v - raw[i - 1][j])));
export const PATH_LENGTH = distances.at(-1);
let cursor = 1;
export const PATH_POINTS = Array.from({ length: PATH_SAMPLES }, (_, i) => {
  const distance = i / PATH_SAMPLES * PATH_LENGTH;
  while (distances[cursor] < distance) cursor++;
  return mix(raw[cursor - 1], raw[cursor], (distance - distances[cursor - 1]) / (distances[cursor] - distances[cursor - 1]));
});

export function infinityPoint(s) {
  const index = wrap(s) * PATH_SAMPLES, a = Math.floor(index);
  return mix(PATH_POINTS[a], PATH_POINTS[(a + 1) % PATH_SAMPLES], index - a);
}

export function infinityFrame(s) {
  const before = infinityPoint(s - 1 / PATH_SAMPLES), after = infinityPoint(s + 1 / PATH_SAMPLES);
  const d = after.map((v, i) => v - before[i]);
  const length = Math.hypot(d[0], d[1]);
  const normal = [-d[1] / length, d[0] / length, 0];
  return { center: infinityPoint(s), normal };
}

function rippleAt(phase, halfWidth) {
  const t = Math.max(0, 1 - Math.abs(phase) / halfWidth);
  const crest = t * t * t * (t * (t * 6 - 15) + 10);
  const breathing = 0.7 * Math.sin(TAU * 2 * phase) + 0.3 * Math.sin(TAU * 3 * phase + 0.6);
  return crest + breathing * RIPPLE.quiet;
}

// Widen the shoulders without increasing the previous waveform's peak displacement.
const peakAt = (width) => Math.max(...Array.from({ length: 8192 }, (_, i) => rippleAt(i / 8192 - 0.5, width)));
const rippleGain = 1.0587980641 / peakAt(RIPPLE.halfWidth);
export const rowPhase = (info) => info.phase ?? info.slot / info.slots;
export function ripple(s, clock) {
  return rippleAt(wrap(s - clock / RIPPLE.loop + 0.5) - 0.5, RIPPLE.halfWidth) * rippleGain;
}

// Text, structural sides and trails use the same phase and displacement.
export const RIPPLE_GLSL = /* glsl */`
  float ringRipple(float s, float clock) {
    float phase = fract(s - clock / ${RIPPLE.loop.toFixed(1)} + 0.5) - 0.5;
    float t = max(0.0, 1.0 - abs(phase) / ${RIPPLE.halfWidth});
    float crest = t * t * t * (t * (t * 6.0 - 15.0) + 10.0);
    float breathing = 0.7 * sin(12.566370614 * phase) + 0.3 * sin(18.849555922 * phase + 0.6);
    return (crest + breathing * ${RIPPLE.quiet}) * ${rippleGain};
  }
`;

export function rowHeight(info, clock, reduced = false) {
  return reduced ? 0 : ripple(rowPhase(info), clock) * info.width * RIPPLE.amplitude;
}

export function rowAxis(info) {
  const { normal } = infinityFrame(rowPhase(info));
  return [normal[0] * Math.cos(Math.PI / 6), normal[1], -normal[0] * 0.5];
}

export function replacementSlots(slots, count) {
  const rounds = Math.ceil(count / REPLACE_GROUP);
  const sector = Math.max(1, Math.floor(slots / REPLACE_GROUP));
  const start = Math.max(0.5, Math.floor((sector - rounds + 1) / 2));
  const available = new Set(Array.from({ length: slots }, (_, slot) => slot));
  const result = [];
  for (let batch = 0; result.length < count && available.size; batch++) {
    // Two separated mirror sets place two simultaneous rows in each quadrant.
    const targets = [0, sector].flatMap((offset) => {
      const phase = (start + offset + batch) / slots;
      return [phase, 0.5 - phase, 0.5 + phase, 1 - phase];
    });
    for (const [lane, target] of targets.entries()) {
      if (result.length >= count || !available.size) break;
      let closest = null, distance = Infinity;
      for (const slot of available) {
        const delta = Math.abs(wrap(slot / slots - target + 0.5) - 0.5);
        if (delta < distance) { closest = slot; distance = delta; }
      }
      available.delete(closest);
      result.push({ slot: closest, lane, batch, arrival: 20 + batch * REPLACE_INTERVAL });
    }
  }
  return result;
}

export function createAsrLayout(chars) {
  const rows = [], layout = new Map();
  const rowLength = Math.min(16, Math.max(1, Math.floor(chars.length / 100)));
  let current = [];
  for (const char of chars) {
    if (current.length && (current[0].segment !== char.segment || current.length === rowLength)) { rows.push(current); current = []; }
    current.push(char);
  }
  if (current.length) rows.push(current);
  const baseSlots = Math.max(1, Math.floor(rows.length * (rows.length < 8 ? 1 : 0.68)));
  const initial = [], baseToSlot = new Map();
  rows.slice(0, baseSlots).forEach((row, slot) => {
    const phase = slot / baseSlots;
    baseToSlot.set(slot, initial.length);
    // Split real source characters at the near lobe; keep mirrored replacement anchors.
    if (infinityPoint(phase)[0] < -3 && row.length >= 8) {
      const middle = Math.ceil(row.length / 2);
      initial.push({ chars: row.slice(0, middle), phase });
      initial.push({ chars: row.slice(middle), phase: phase + 0.5 / baseSlots });
    } else initial.push({ chars: row, phase });
  });
  const slots = initial.length;
  const replacements = replacementSlots(baseSlots, rows.length - baseSlots)
    .map((entry) => ({ ...entry, slot: baseToSlot.get(entry.slot) }));
  const bySlot = new Map(replacements.map((entry) => [entry.slot, entry]));
  const scale = Math.min(1, Math.max(0.22, baseSlots / 70));
  const width = 2.3 * Math.min(1, Math.max(0.08, rowLength / 16));
  const entries = [];
  const arranged = [...initial, ...rows.slice(baseSlots).map((chars) => ({ chars }))];
  arranged.forEach(({ chars: row, phase }, order) => {
    const group = order < slots ? 0 : 1;
    const assignment = group === 0 ? bySlot.get(order) : replacements[order - slots];
    const slot = group === 0 ? order : assignment.slot;
    const info = { slot, slots, phase: phase ?? initial[slot].phase, group, row: slot, length: row.length, scale, width,
      batch: assignment?.batch ?? -1, lane: assignment?.lane ?? -1,
      arrival: group === 0 ? -1 : assignment.arrival,
      departure: group === 0 ? assignment?.arrival ?? 1e6 : 1e6 };
    entries.push(info);
    row.forEach((char, index) => layout.set(char.key, { ...info, index }));
  });
  return { layout, slots, entries };
}

export function ringCoordinates(info) {
  const section = info.length <= 1 ? 0 : (info.index / (info.length - 1) - 0.5) * 2.2;
  return [rowPhase(info), section, info.scale, info.width];
}

export function asrPosition(char, info, copy = 0, clock = 0, reduced = false) {
  const [s, section, scale, width] = ringCoordinates(info);
  const { center, normal } = infinityFrame(s);
  const wave = rowHeight(info, clock, reduced);
  const across = section / 1.1 * width;
  const p = center.map((v) => v * scale);
  p[0] += normal[0] * (across + wave);
  p[1] += normal[1] * (across + wave);
  p[2] += Math.cos(section) * width * 0.64;
  const result = [p[0] * Math.cos(Math.PI / 6) + p[2] * 0.5, p[1] + 3.6,
    -p[0] * 0.5 + p[2] * Math.cos(Math.PI / 6) - 10];
  if (copy > 0) { result[0] *= 0.75; result[1] *= 0.75; result[2] -= 76 + copy * 3; }
  return result;
}

export function replacementState(info, elapsed, reduced = false) {
  if (reduced) return { active: info.group === 0, opacity: info.group === 0 ? 1 : 0, dissolve: 0, gather: 0 };
  const { arrival, departure } = characterLifetime(info);
  const active = elapsed >= arrival && elapsed < departure;
  const dissolve = smooth((elapsed - departure + REPLACE_DISSOLVE) / REPLACE_DISSOLVE);
  const gather = info.group === 0 ? 0 : 1 - smooth((elapsed - arrival) / REPLACE_APPEAR);
  return { active, opacity: active ? (1 - dissolve) * (1 - gather) : 0, dissolve, gather };
}

export function characterLifetime(info) {
  const offset = info.length <= 1 ? 0 : (info.index ?? 0) / (info.length - 1) * REPLACE_SWEEP;
  return { arrival: info.group === 0 ? -1 : info.arrival + offset, departure: info.departure + offset };
}

export function replacementTint(info, elapsed, reduced = false) {
  if (reduced || !replacementState(info, elapsed).active) return [0, 0];
  const { arrival, departure } = characterLifetime(info);
  const red = smooth((elapsed - departure + REPLACE_DISSOLVE + REPLACE_CUE.warning) / REPLACE_CUE.warning)
    * (1 - smooth((elapsed - departure + REPLACE_DISSOLVE) / REPLACE_DISSOLVE));
  const green = info.group === 0 ? 0 : smooth((elapsed - arrival) / REPLACE_APPEAR)
    * (1 - smooth((elapsed - arrival - REPLACE_APPEAR - REPLACE_CUE.hold) / REPLACE_CUE.fade));
  return [red * REPLACE_CUE.strength, green * (1 - red) * REPLACE_CUE.strength];
}

export const REPLACEMENT_TINT_GLSL = /* glsl */`
  vec2 replacementTint(float clock, float arrival, float departure, float group) {
    float red = smoothstep(0.0, 1.0, (clock - departure + ${REPLACE_DISSOLVE.toFixed(1)} + ${REPLACE_CUE.warning.toFixed(1)}) / ${REPLACE_CUE.warning.toFixed(1)});
    red *= 1.0 - smoothstep(0.0, 1.0, (clock - departure + ${REPLACE_DISSOLVE.toFixed(1)}) / ${REPLACE_DISSOLVE.toFixed(1)});
    float green = group < 0.5 ? 0.0 : smoothstep(0.0, 1.0, (clock - arrival) / ${REPLACE_APPEAR.toFixed(1)}) * (1.0 - smoothstep(0.0, 1.0, (clock - arrival - ${REPLACE_APPEAR.toFixed(1)} - ${REPLACE_CUE.hold.toFixed(1)}) / ${REPLACE_CUE.fade.toFixed(1)}));
    return vec2(red, green * (1.0 - red)) * ${REPLACE_CUE.strength} * step(arrival, clock) * (1.0 - step(departure, clock));
  }
`;

export function asrElapsed(time, ambient = time, hold = false) {
  return Math.max(0, (hold ? ambient : Math.min(time, TIMES.build)) - TIMES.space);
}
