import { createIcons, Play, Pause, RotateCcw, Upload, Download, PanelRight, X, Send, ArrowUpRight, ScanLine, Check, AlertTriangle } from 'lucide';
import { fixture } from './fixture.js';
import { Timeline, mapMarkers, validateData, formatTime, allCharacters, TIMES, WALL_TIME, MODEL_PROFILES, smooth } from './core.js';
import { SubtitleScene } from './scene.js';
import './style.css';

const iconSet = { Play, Pause, RotateCcw, Upload, Download, PanelRight, X, Send, ArrowUpRight, ScanLine, Check, AlertTriangle };
const icons = () => createIcons({ icons: iconSet, attrs: { 'stroke-width': 1.5, 'aria-hidden': 'true' } });
const $ = (selector) => document.querySelector(selector);
const escape = (text) => String(text).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const timeline = new Timeline();
const reducedMedia = matchMedia('(prefers-reduced-motion: reduce)');
let data = validateData(fixture), mappings = mapMarkers(data), scene = null, selectedId = null;
let ready = false, last = 0, frame = 0, lastUI = 0, lastSignature = '', loading = false;
let fps = 60, slowSeconds = 0, lastStats = 0, frames = 0, hovered = null;
let performanceWarmUntil = 0;
let returnFocus = null;
const labels = { extract: '提取音频', recognize: '启动语音识别', enter: '字幕空间就绪', build: 'ASR 转写', hold: 'ASR 持续演示', morph: '空间转换', wait: '等待分析', reveal: '考点命中', complete: '分析完成', failed: '请求失败' };

$('#app').innerHTML = `
  <main class="experiment" data-stage="build">
    <div class="viewport"><canvas id="scene" aria-label="由字幕字符构成的三维空间"></canvas></div>
    <header class="topbar">
      <div class="brand"><i data-lucide="scan-line"></i><h1>字幕空间</h1><span class="experiment-label">实验演示</span></div>
      <div class="source"><span class="source-dot"></span><span id="source-name"></span></div>
      <div class="top-actions">
        <button class="icon-button" id="download" title="下载示例 JSON" aria-label="下载示例 JSON"><i data-lucide="download"></i></button>
        <button class="quiet-button" id="import"><i data-lucide="upload"></i><span>导入 JSON</span></button>
        <input type="file" id="file" accept=".json,application/json" hidden />
      </div>
    </header>
    <nav class="stages" aria-label="动画阶段">
      <button data-phase="build"><span class="stage-number">01</span><span>转写<span class="stage-en">TRANSCRIBE</span></span></button>
      <button data-phase="morph"><span class="stage-number">02</span><span>转换<span class="stage-en">TRANSFORM</span></span></button>
      <button data-phase="wait"><span class="stage-number">03</span><span>分析<span class="stage-en">ANALYZE</span></span></button>
      <button data-phase="result"><span class="stage-number">04</span><span>命中<span class="stage-en">EXTRACT</span></span></button>
    </nav>
    <div class="scene-meta"><span id="phase-caption">字幕构筑</span><span id="clock">00:00</span></div>
    <div class="loading" id="loading" role="status"><i data-lucide="scan-line"></i><span>正在构建字幕空间</span></div>
    <section class="asr-intro" id="asr-intro" aria-label="转写前奏">
      <h2 id="intro-title">正在从视频中提取音频</h2>
      <p id="intro-subtitle">VIDEO / AUDIO EXTRACTION</p>
    </section>
    <div class="error-banner" id="error" role="alert" hidden><i data-lucide="alert-triangle"></i><span></span><button id="dismiss-error" aria-label="关闭提示" title="关闭提示"><i data-lucide="x"></i></button></div>
    <div class="fallback" id="fallback" hidden><i data-lucide="alert-triangle"></i><strong>当前窗口无法显示三维空间</strong><p>字幕与考点仍可在结果栏查看。</p><button class="quiet-button" id="fallback-results">查看考点</button></div>
    <div class="hover-label" id="hover-label" hidden></div>
    <div class="caption" id="caption"><span class="caption-rule"></span><time id="subtitle-time"></time><p id="subtitle-text"></p></div>
    <aside class="results" id="results" hidden aria-label="考点结果">
      <div class="results-heading"><div><span class="eyebrow">ANALYSIS OUTPUT</span><h2>考点 <span id="result-count">0</span></h2></div><button id="close-results" class="icon-button" title="关闭结果栏" aria-label="关闭结果栏"><i data-lucide="x"></i></button></div>
      <section class="detail" id="detail" hidden aria-label="考点详情"></section>
      <div class="result-empty" id="result-empty">等待分析结果</div>
      <div class="result-list" id="result-list"></div>
    </aside>
    <footer class="console">
      <div class="console-status"><span class="status-light"></span><span id="status-text">字幕构筑</span><span class="console-meta" id="glyph-stats"></span><div class="completion-controls"><button id="finish-asr" class="icon-button" title="模拟 ASR 完成，立即结束转写阶段" aria-label="模拟 ASR 完成"><i data-lucide="check"></i></button></div><span class="console-meta fps" id="fps"></span><span class="console-meta" id="reduced-note" hidden>减少动态效果</span></div>
      <div class="console-controls">
        <div class="transport"><button class="icon-button primary-control" id="play" aria-label="暂停" title="暂停"><i data-lucide="pause"></i></button><button class="icon-button" id="replay" title="重新播放" aria-label="重新播放"><i data-lucide="rotate-ccw"></i></button></div>
        <label class="select-control"><span>阶段</span><select id="phase" aria-label="动画阶段"><option value="intro">提取音频 / 启动 ASR</option><option value="build">ASR 文字空间</option><option value="hold">ASR 持续演示</option><option value="morph">空间转换</option><option value="wait">等待分析</option><option value="result">考点命中</option></select></label>
        <label class="manual-control"><input type="checkbox" id="manual" /><span>手动返回</span></label>
        <div class="return-controls"><button id="return" class="quiet-button"><i data-lucide="send"></i><span>返回考点</span></button><select id="scenario" aria-label="模拟返回类型"><option value="normal">正常结果</option><option value="empty">零考点</option><option value="failure">请求失败</option></select></div>
        <button class="quiet-button" id="retry" hidden><i data-lucide="rotate-ccw"></i>重试</button>
        <button class="icon-button results-toggle" id="toggle-results" aria-expanded="false" aria-controls="results" aria-label="查看考点" title="查看考点"><i data-lucide="panel-right"></i><span id="badge" hidden></span></button>
      </div>
    </footer>
  </main>`;
icons();

function updateIntro() {
  const t = timeline.time;
  const intro = $('#asr-intro');
  intro.hidden = t >= TIMES.space || !scene;
  if (intro.hidden) return;
  const recognizing = t >= TIMES.extract;
  intro.dataset.phase = recognizing ? 'asr' : 'audio';
  intro.style.opacity = reducedMedia.matches ? (t < TIMES.recognize ? 1 : 0) : 1 - smooth((t - TIMES.recognize) / 0.75);
  $('#intro-title').textContent = recognizing ? '自动语音识别已启动' : '正在从视频中提取音频';
  $('#intro-subtitle').textContent = recognizing ? 'ASR / AUTOMATIC SPEECH RECOGNITION' : 'VIDEO / AUDIO EXTRACTION';
}

function message(text) { $('#error span').textContent = text; $('#error').hidden = !text; }
function setResults(open) {
  if (open && $('#results').hidden) returnFocus = document.activeElement;
  $('#results').hidden = !open;
  $('.experiment').classList.toggle('results-open', open);
  $('#toggle-results').setAttribute('aria-expanded', String(open));
  if (!open && $('#results').contains(document.activeElement)) (returnFocus?.isConnected ? returnFocus : $('#toggle-results')).focus();
}
function selectMarker(id) {
  selectedId = id;
  if (scene) {
    scene.focusId = id;
    const mapping = mappings.find((m) => String(m.id) === String(id));
    if (mapping?.keys.some((key) => !scene.present.has(key)) && !loading) {
      loading = true;
      scene.load(data, mappings, id).then(() => { scene.focusId = id; }).catch((error) => message(error.message)).finally(() => { loading = false; });
    }
  }
  setResults(true);
  renderResults();
  $('#results').scrollTop = 0;
}
function renderResults() {
  const results = timeline.mappings;
  $('#result-count').textContent = results.length;
  $('#badge').textContent = results.length;
  $('#badge').hidden = !results.length;
  $('#result-empty').hidden = results.length > 0;
  $('#result-empty').textContent = timeline.received ? '未识别到考点' : '等待分析结果';
  $('#result-list').innerHTML = results.map((m, i) => `<button class="result-item ${String(m.id) === String(selectedId) ? 'selected' : ''}" data-marker="${escape(m.id)}" aria-pressed="${String(m.id) === String(selectedId)}"><span class="result-index">${String(i + 1).padStart(2, '0')}</span><span><time>${formatTime(m.start_ms)}</time><strong>${escape(m.title)}</strong><small>${m.match === 'exact' ? '原文匹配' : m.match === 'segment' ? '对应字幕段' : '未定位到字幕'}</small></span><i data-lucide="arrow-up-right"></i></button>`).join('');
  const selected = results.find((m) => String(m.id) === String(selectedId));
  $('#detail').hidden = !selected;
  if (selected) $('#detail').innerHTML = `<span class="eyebrow">${formatTime(selected.start_ms)} — ${formatTime(selected.end_ms)}</span><h3>${escape(selected.title)}</h3><blockquote>${escape(selected.source_quote || '未提供原文摘录')}</blockquote><p>${escape(selected.note)}</p>${selected.match !== 'exact' ? `<span class="match-note">${selected.match === 'segment' ? '对应字幕段：原文未精确匹配，按时间范围标记。' : '未定位到字幕：此结果没有染绿场景文字。'}</span>` : ''}`;
  icons();
}

function seek(phase) {
  timeline.seek(phase, mappings);
  selectedId = null;
  if (scene) scene.focusId = null;
  $('#phase').value = phase;
  $('#error').hidden = true;
  lastSignature = '';
  renderResults();
}
$('#play').onclick = () => { timeline.playing = !timeline.playing; updateUI(); };
$('#replay').onclick = () => { timeline.hold = false; seek('intro'); };
$('#phase').onchange = (event) => seek(event.target.value);
$('.stages').onclick = (event) => { const button = event.target.closest('[data-phase]'); if (button) seek(button.dataset.phase); };
$('#manual').onchange = (event) => { timeline.manual = event.target.checked; };
$('#finish-asr').onclick = () => { timeline.completeAsr(); updateUI(); };
$('#return').onclick = () => {
  if ($('#scenario').value === 'failure') timeline.fail();
  else timeline.receive($('#scenario').value === 'empty' ? [] : mappings);
  renderResults();
  updateUI();
};
$('#retry').onclick = () => { timeline.retry(); $('#scenario').value = 'normal'; updateUI(); };
$('#toggle-results').onclick = () => setResults($('#results').hidden);
$('#close-results').onclick = () => setResults(false);
$('#fallback-results').onclick = () => { timeline.seek('result', mappings); renderResults(); setResults(true); };
$('#result-list').onclick = (event) => { const item = event.target.closest('[data-marker]'); if (item) selectMarker(item.dataset.marker); };
$('#dismiss-error').onclick = () => message('');
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') setResults(false); });
$('#import').onclick = () => $('#file').click();
$('#download').onclick = () => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(fixture, null, 2)], { type: 'application/json' }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = 'subtitle-cyberspace-example.json'; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$('#file').onchange = async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  const wasPlaying = timeline.playing;
  timeline.playing = false;
  try {
    if (file.size > 8 * 1024 * 1024) throw new Error('JSON 文件不能超过 8 MB。');
    const next = validateData(JSON.parse((await file.text()).replace(/^\uFEFF/, '')));
    await load(next);
    message('');
  } catch (error) { message(error instanceof SyntaxError ? '无法读取 JSON，请检查文件格式。' : error.message); timeline.playing = wasPlaying; }
  event.target.value = '';
};

function updateUI() {
  const stage = timeline.stage;
  $('.experiment').dataset.stage = stage;
  $('#status-text').textContent = stage === 'failed' ? '模拟请求失败 · 可重试' : !timeline.playing ? '已暂停' : labels[stage];
  $('#phase-caption').textContent = labels[stage];
  $('#clock').textContent = `${String(Math.floor(timeline.ambientTime / 60)).padStart(2, '0')}:${String(Math.floor(timeline.ambientTime % 60)).padStart(2, '0')}`;
  $('#finish-asr').disabled = timeline.time >= TIMES.build || timeline.failed;
  $('#retry').hidden = !timeline.failed;
  $('#return').disabled = timeline.failed;
  const phase = timeline.hold ? 'hold' : ({ extract: 'intro', recognize: 'intro', enter: 'intro', build: 'build', morph: 'morph', wait: 'wait', reveal: 'result', complete: 'result' })[stage];
  if (phase && document.activeElement !== $('#phase')) $('#phase').value = phase;
  const stageIndex = ({ extract: 0, recognize: 0, enter: 0, build: 0, hold: 0, morph: 1, wait: 2, reveal: 3, complete: 3 })[stage] ?? 2;
  document.querySelectorAll('.stages button').forEach((button, index) => { button.classList.toggle('active', index === stageIndex); button.classList.toggle('passed', index < stageIndex); });
  $('#reduced-note').hidden = !reducedMedia.matches;
  $('#glyph-stats').textContent = scene ? `${scene.uniqueShown.toLocaleString()} 源字符${scene.degraded ? ' · 已降低效果' : ''}` : '文字模式';
  $('#fps').textContent = scene ? `${Math.round(fps)} FPS` : '';
  const playLabel = timeline.playing ? '暂停' : '播放';
  if ($('#play').getAttribute('aria-label') !== playLabel) {
    $('#play').innerHTML = `<i data-lucide="${timeline.playing ? 'pause' : 'play'}"></i>`;
    $('#play').setAttribute('aria-label', playLabel); $('#play').title = playLabel; icons();
  }
  const activeSubtitle = scene?.currentAsrSubtitle();
  const subtitle = activeSubtitle ?? data.subtitles[0];
  $('#caption').hidden = timeline.time < TIMES.space || timeline.time >= TIMES.build;
  $('#subtitle-time').textContent = formatTime(subtitle.start_ms);
  $('#subtitle-text').textContent = subtitle.text;
  const signature = `${timeline.received}:${timeline.mappings.map((m) => m.id).join(',')}`;
  if (signature !== lastSignature) { lastSignature = signature; renderResults(); }
  const hover = timeline.mappings.find((m) => String(m.id) === String(hovered));
  $('#hover-label').hidden = !hover || !$('#results').hidden;
  if (hover) $('#hover-label').textContent = `${formatTime(hover.start_ms)}  ${hover.title}`;
}

async function load(next) {
  if (loading) throw new Error('正在读取字幕，请稍后再试。');
  loading = true; ready = false;
  $('#loading').hidden = false;
  $('#import').disabled = true;
  try {
    const nextMappings = mapMarkers(next);
    if (scene) await scene.load(next, nextMappings);
    data = next; mappings = nextMappings;
    timeline.reset(); timeline.hold = false; selectedId = null;
    $('#source-name').textContent = data.name;
    $('#source-name').title = data.name;
    $('#detail').hidden = true;
    setResults(false); renderResults(); updateUI();
    ready = true;
  } finally {
    $('#loading').hidden = true;
    $('#import').disabled = false;
    loading = false; last = 0;
    frames = 0; slowSeconds = 0; lastStats = performance.now(); performanceWarmUntil = lastStats + 2500;
  }
}

function tick(now) {
  frame = 0;
  if (document.hidden) return;
  const dt = last ? Math.min((now - last) / 1000, 0.1) : 0;
  last = now;
  if (ready && !loading) {
    timeline.advance(dt, mappings);
    scene?.render(timeline.time, timeline.highlights(reducedMedia.matches), reducedMedia.matches,
      timeline.revealAt === null ? Infinity : timeline.revealAt + timeline.revealDuration, timeline.ambientTime, timeline.hold, timeline.flowTime);
    updateIntro();
    frames++;
    if (now - lastStats > 1500) {
      fps = frames * 1000 / Math.max(1, now - lastStats);
      const measurePlayback = now >= performanceWarmUntil && timeline.playing && !timeline.failed && timeline.time >= TIMES.space;
      slowSeconds = measurePlayback && fps < 32 ? slowSeconds + (now - lastStats) / 1000 : 0;
      if (slowSeconds > 5) scene?.degrade();
      frames = 0; lastStats = now;
    }
    if (now - lastUI > 120) { updateUI(); lastUI = now; }
  }
  frame = requestAnimationFrame(tick);
}
document.addEventListener('visibilitychange', () => {
  cancelAnimationFrame(frame); last = 0; frames = 0; lastStats = performance.now();
  slowSeconds = 0; performanceWarmUntil = lastStats + 2000;
  if (!document.hidden) frame = requestAnimationFrame(tick);
});
$('#scene').addEventListener('webglcontextlost', (event) => {
  event.preventDefault(); scene = null; $('#fallback').hidden = false;
  message('三维显示已中断，结果仍可查看。刷新页面可重新初始化。');
});

try {
  if (new URLSearchParams(location.search).has('noWebGL')) throw new Error('Fallback test');
  scene = new SubtitleScene($('#scene'), selectMarker, (id) => { hovered = id; });
} catch { $('#fallback').hidden = false; }
await load(data).catch((error) => { message(error.message); ready = true; });
frame = requestAnimationFrame(tick);

// Explicit local QA hooks; no remote requests, credentials, or production task state.
window.__experiment = {
  timings: { ...TIMES, wall: WALL_TIME },
  models: MODEL_PROFILES,
  timeline,
  seek: (phase) => seek(phase),
  setTime: (time, flowTime) => {
    timeline.time = time;
    const elapsed = Math.max(0, Math.min(TIMES.asr, time - TIMES.space)) / TIMES.asr * MODEL_PROFILES[timeline.model].duration;
    timeline.flowTime = flowTime ?? elapsed * MODEL_PROFILES[timeline.model].tempo;
    timeline.ambientTime = time < TIMES.space ? time : TIMES.space + elapsed + Math.max(0, time - TIMES.build);
  },
  completeAsr: () => timeline.completeAsr(),
  load: async (input) => load(validateData(input)),
  receive: () => { timeline.receive(mappings); renderResults(); },
  fail: () => timeline.fail(), retry: () => timeline.retry(),
  snapshot: () => ({ ready, stage: timeline.stage, time: timeline.time, flowTime: timeline.flowTime, model: timeline.model, elapsed: timeline.ambientTime, resultCount: timeline.mappings.length,
    greenCharacters: timeline.highlights(reducedMedia.matches).size, particles: scene?.total ?? 0,
    visibleParticles: scene?.meshes.reduce((n, m) => n + m.mesh.geometry.instanceCount, 0) ?? 0,
    fps, degraded: scene?.degraded ?? false, reduced: reducedMedia.matches, selectedId,
    sourceCharacters: allCharacters(data).length, renderer: scene?.renderer.info.render }),
  markerPoint: (id, face) => scene?.pointForMarker(id, face),
  wallSnapshot: () => scene?.wallSnapshot(),
  asrSnapshot: () => scene?.asrSnapshot(),
  degrade: () => scene?.degrade(),
  fixture
};
