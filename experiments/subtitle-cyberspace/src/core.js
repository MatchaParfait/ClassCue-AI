export const LIMIT = 12000;
export const TIMES = { extract: 1.6, recognize: 3.2, space: 4, asr: 120, build: 124, morph: 2.4, result: 132.4 };
export const WALL_TIME = TIMES.build + TIMES.morph;
export const ASR_PLAYBACK = Object.freeze({ duration: 12, tempo: 10 });
export const MODEL_PROFILES = Object.freeze({ normal: ASR_PLAYBACK, fast: ASR_PLAYBACK, slow: ASR_PLAYBACK });
export const charKey = (id, index) => `${String(id)}:${index}`;
const clean = (text) => Array.from(text).filter((char) => !/\s/u.test(char)).join("");

export function validateData(input) {
  if (!input || !Array.isArray(input.subtitles) || !Array.isArray(input.markers)) throw new Error("JSON 需要包含 subtitles 和 markers 数组。");
  if (!input.subtitles.length || input.subtitles.length > 5000 || input.markers.length > 1000) throw new Error("需要 1–5000 条字幕，考点最多 1000 条。");
  let total = 0;
  const ids = new Set();
  const subtitles = input.subtitles.map((item) => {
    checkItem(item);
    if (ids.has(String(item.id))) throw new Error("字幕 ID 不能重复。");
    ids.add(String(item.id));
    if (typeof item.text !== "string" || !item.text.trim()) throw new Error("字幕 text 不能为空。");
    total += Array.from(item.text).length;
    return { id: item.id, start_ms: item.start_ms, end_ms: item.end_ms, text: item.text };
  }).sort((a, b) => a.start_ms - b.start_ms);
  if (total > 100000) throw new Error("本实验一次最多接收 100,000 个字幕字符，请分段导入。");
  const markerIds = new Set();
  const markers = input.markers.flatMap((item) => {
    checkItem(item);
    if (markerIds.has(String(item.id))) return [];
    markerIds.add(String(item.id));
    if (typeof item.title !== "string" || !item.title.trim() || typeof item.source_quote !== "string") throw new Error("考点需要 title 和 source_quote。");
    return [{ id: item.id, start_ms: item.start_ms, end_ms: item.end_ms, title: item.title, source_quote: item.source_quote, note: typeof item.note === "string" ? item.note : "" }];
  }).sort((a, b) => a.start_ms - b.start_ms);
  return { name: typeof input.name === "string" ? input.name.slice(0, 120) : "本地字幕", subtitles, markers };
}

function checkItem(item) {
  if (!item || !["string", "number"].includes(typeof item.id) || String(item.id).length > 100 ||
    (typeof item.id === "number" && !Number.isFinite(item.id)) || !String(item.id).length ||
    !Number.isFinite(item.start_ms) || !Number.isFinite(item.end_ms) || item.start_ms < 0 || item.end_ms <= item.start_ms) {
    throw new Error("字幕和考点必须有有效 ID 与递增的毫秒时间范围。");
  }
}

export function mapMarkers(data) {
  return data.markers.map((marker) => {
    const candidates = data.subtitles.filter((s) => s.start_ms < marker.end_ms && s.end_ms > marker.start_ms);
    const indexed = candidates.flatMap((s) => Array.from(s.text).flatMap((char, index) => /\s/u.test(char) ? [] : [{ char, key: charKey(s.id, index), subtitleId: s.id }]));
    const quote = clean(marker.source_quote);
    const text = indexed.map((char) => char.char).join("");
    const offset = quote ? text.indexOf(quote) : -1;
    // Convert UTF-16 search offsets back to code-point positions for emoji and CJK extensions.
    const first = offset >= 0 ? Array.from(text.slice(0, offset)).length : 0;
    const selected = offset >= 0 ? indexed.slice(first, first + Array.from(quote).length) : indexed;
    return { ...marker, match: offset >= 0 ? "exact" : candidates.length ? "segment" : "unmapped", keys: selected.map((c) => c.key) };
  });
}

export function allCharacters(data) {
  return data.subtitles.flatMap((subtitle, segment) => Array.from(subtitle.text).flatMap((char, index) =>
    /\s/u.test(char) ? [] : [{ key: charKey(subtitle.id, index), char, index, segment, subtitleId: subtitle.id }]));
}

export function chooseCharacters(data, mappings, focus = null, limit = LIMIT) {
  const all = allCharacters(data);
  if (all.length <= limit) return all;
  const wanted = new Set();
  for (const mapping of [...mappings.filter((m) => String(m.id) === String(focus)), ...mappings]) {
    for (const key of mapping.keys) { if (wanted.size < limit) wanted.add(key); }
  }
  const capacity = limit - wanted.size;
  const rest = all.filter((c) => !wanted.has(c.key));
  for (let i = 0; i < capacity; i++) wanted.add(rest[Math.floor(i * rest.length / capacity)]?.key);
  return all.filter((c) => wanted.has(c.key));
}

export const smooth = (v) => { const x = Math.max(0, Math.min(1, v)); return x * x * (3 - 2 * x); };
export function phaseAt(time) {
  return time < TIMES.extract ? 'extract' : time < TIMES.recognize ? 'recognize' : time < TIMES.space ? 'enter'
    : time < TIMES.build ? "build" : time < WALL_TIME ? "morph" : "wait";
}

export class Timeline {
  constructor() { this.model = 'normal'; this.manual = false; this.hold = false; this.reset(); }
  reset() { this.time = 0; this.ambientTime = 0; this.flowTime = 0; this.finishRate = 8; this.playing = true; this.failed = false; this.received = false; this.mappings = []; this.receivedAt = null; this.revealAt = null; this.empty = false; }
  advance(dt, available) {
    if (!this.playing || this.failed) return;
    const step = Math.min(dt, 0.1);
    this.ambientTime += step;
    let remaining = step;
    // Wall choreography and subtitle motion use separate clocks; completion never waits for text.
    while (remaining > 1e-8) {
      const inAsr = this.time >= TIMES.space && this.time < TIMES.build;
      const profile = MODEL_PROFILES[this.model];
      const rate = inAsr ? (this.hold ? 1 : TIMES.asr / profile.duration)
        : this.time >= TIMES.build && this.time < WALL_TIME ? this.finishRate : 1;
      const boundary = this.time < TIMES.space ? TIMES.space : inAsr ? TIMES.build : this.time < WALL_TIME ? WALL_TIME : Infinity;
      const chunk = this.hold && inAsr ? remaining : Math.min(remaining, (boundary - this.time) / rate);
      if (inAsr) this.flowTime += chunk * profile.tempo;
      this.time += chunk * rate;
      if (this.hold && inAsr) this.time = Math.min(this.time, TIMES.build - 0.01);
      remaining -= chunk;
    }
    if (!this.manual && !this.received && this.time >= TIMES.result) this.receive(available);
    if (this.received && this.revealAt === null && this.time >= WALL_TIME) this.revealAt = this.time;
  }
  receive(mappings) {
    const previous = new Map(this.mappings.map((m) => [String(m.id), m]));
    for (const mapping of mappings) if (!previous.has(String(mapping.id))) previous.set(String(mapping.id), mapping);
    this.mappings = [...previous.values()].sort((a, b) => a.start_ms - b.start_ms);
    this.received = true;
    this.empty = this.mappings.length === 0;
    this.receivedAt ??= this.time;
    if (this.time >= WALL_TIME) this.revealAt ??= this.time;
  }
  seek(phase, available) {
    this.reset();
    this.hold = phase === "hold";
    this.time = ({ intro: 0, build: TIMES.space, hold: TIMES.space, morph: TIMES.build, wait: WALL_TIME, result: TIMES.result })[phase] ?? 0;
    this.ambientTime = this.time < TIMES.space ? this.time : TIMES.space + Math.min(TIMES.asr, this.time - TIMES.space) / TIMES.asr * MODEL_PROFILES[this.model].duration + Math.max(0, this.time - TIMES.build);
    this.flowTime = Math.min(TIMES.asr, Math.max(0, this.time - TIMES.space)) / TIMES.asr * MODEL_PROFILES[this.model].duration * MODEL_PROFILES[this.model].tempo;
    if (phase === 'morph') this.finishRate = 1;
    if (phase === "result") this.receive(available);
  }
  setModel(model) { if (MODEL_PROFILES[model]) this.model = model; }
  completeAsr() {
    if (this.time >= TIMES.build) return;
    this.hold = false; this.time = TIMES.build; this.finishRate = 8;
  }
  retry() { this.failed = false; this.playing = true; }
  fail() { this.failed = true; }
  get stage() {
    if (this.failed) return "failed";
    if (this.hold) return "hold";
    if (this.revealAt !== null) return this.time - this.revealAt >= this.revealDuration ? "complete" : "reveal";
    return phaseAt(this.time);
  }
  get revealDuration() { return this.mappings.length ? (Math.min(this.mappings.length, 12) - 1) * 0.5 + 1.2 + (this.mappings.length > 12 ? 1.2 : 0) : 0; }
  highlights(reduced = false) {
    const values = new Map();
    if (this.revealAt === null) return values;
    this.mappings.forEach((mapping, order) => {
      const delay = order < 12 ? order * 0.5 : 6.7;
      const elapsed = this.time - this.revealAt - delay;
      mapping.keys.forEach((key, index) => {
        const amount = reduced ? 1 : smooth((elapsed - (order < 12 ? index / Math.max(1, mapping.keys.length - 1) * 0.8 : 0)) / (order < 12 ? 0.4 : 1.2));
        if (amount > (values.get(key) ?? 0)) values.set(key, amount);
      });
    });
    return values;
  }
}

export function formatTime(ms) {
  const seconds = Math.floor(ms / 1000);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
