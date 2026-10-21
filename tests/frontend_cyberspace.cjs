// All API traffic is intercepted. This test never starts ASR, calls AI or touches user media.
const { chromium } = require("./playwright.cjs");
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { pathToFileURL } = require('node:url');
const { execFileSync } = require('node:child_process');
const origin = process.env.FRONTEND_URL || 'http://127.0.0.1:5175';
const output = path.resolve(__dirname, '..', '.runtime', `cyberspace-qa-${Date.now()}`);
const key = 'classroom.experimental.subtitle-space.v1';

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const media = path.join(output, 'mock-video.mp4');
  execFileSync(path.resolve(__dirname, '../.venv/Scripts/python.exe'), ['-c', `
import av, numpy as np, sys
with av.open(sys.argv[1], 'w') as container:
    stream = container.add_stream('libx264', rate=10)
    stream.width, stream.height, stream.pix_fmt = 320, 180, 'yuv420p'
    for i in range(100):
        pixels = np.full((180, 320, 3), (25, 70 + i, 100), dtype=np.uint8)
        for packet in stream.encode(av.VideoFrame.from_ndarray(pixels, format='rgb24')):
            container.mux(packet)
    for packet in stream.encode(): container.mux(packet)
`, media], { windowsHide: true });
  const { fixture } = await import(pathToFileURL(path.resolve(__dirname, '../experiments/subtitle-cyberspace/src/fixture.js')));
  const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
  const page = await browser.newPage({ viewport: { width: 1540, height: 949 } });
  const errors = [], writes = [], snapshots = [], requests = [];
  const video = { id: 7, filename: '临时测试课程.mp4', display_name: '临时测试课程', status: 'processing', duration_ms: 100000,
    library_id: 'visual-test', video_path: 'X:/test-only/video.mp4', has_transcript: false };
  let job = { id: 'space-job', job_id: 'space-job', video_id: 7, job_type: 'video_process', status: 'running', stage: 'transcribing', progress: 20, analysis_modes: ['medium_recall'] };
  let noResults = false, started = false, liveCount = 0, completedChunks = 0;
  job.message = 'FireRedASR2-AED 转写 390/1406 · BS6';
  job.resource = { engine: 'firered', initial_batch_size: 6, current_batch_size: 6, completed_segments: 390, total_segments: 1406 };
  const asr = { id: 'whisper-turbo', name: 'Test ASR', engine: 'whisper', selected: true, downloaded: true };
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('request', (request) => requests.push(request.url()));
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url()), p = url.pathname;
    if (!p.startsWith('/api/')) return route.continue();
    const json = (data, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (route.request().method() !== 'GET') {
      writes.push(p);
      if (p === '/api/videos/7/process') { started = true; return json(job); }
      if (p === '/api/videos/7/retry') { job = { ...job, id: 'restarted-job', job_id: 'restarted-job', status: 'running', stage: 'transcribing', error_message: null }; return json(job); }
      if (p.endsWith('/cancel')) { job = { ...job, status: 'interrupted', stage: 'interrupted', error_message: '用户已请求停止处理' }; return json(job); }
      return json({ detail: 'Unexpected write' }, 500);
    }
    if (p === '/api/library/status') return json({ library_id: 'visual-test', data_root: 'X:/test-only', database_path: 'X:/test-only/test.db', temp_audio_dir: 'X:/test-only/audio', video_count: 1, active_job_id: ['running','queued'].includes(job.status) ? job.id : null });
    if (p === '/api/library') return json({ folder: null, parent_id: null, sort: 'custom', order: 'asc', items: [{ type: 'video', video }] });
    if (p === '/api/videos') return json([video]);
    if (p === '/api/videos/7') return json({ ...video, status: job.status === 'completed' ? 'review_ready' : video.status });
    if (p.endsWith('/analysis-runs')) return json([]);
    if (p === '/api/videos/7/processing-options') return json({ asr_model: asr, active_job: started ? job : null });
    if (p.endsWith('/subtitles')) return json(['transcribing','starting'].includes(job.stage) ? [] : fixture.subtitles);
    if (p.endsWith('/markers')) return json(noResults ? [] : fixture.markers.map((m, i) => ({ ...m, id: i + 1, video_id: 7, analysis_mode: 'medium_recall', review_status: 'active', importance: 'standard' })));
    if (p === '/api/jobs/paused') return json([]);
    if (p.endsWith('/transcript')) return json({ video_id: 7, revision: liveCount, settled_until_ms: 10000, subtitles: fixture.subtitles.slice(0, liveCount) });
    if (p.endsWith('/analysis-preview')) return json({ video_id: 7, revision: completedChunks, active_mode: 'medium_recall', modes: completedChunks ? {
      medium_recall: { completed_chunks: completedChunks, total_chunks: 3, current_chunk: completedChunks < 3 ? completedChunks + 1 : null,
        candidate_count: completedChunks, truncated: false, markers: fixture.markers.slice(0, completedChunks).map((m, i) => ({
          ...m, id: -i - 1, video_id: 7, analysis_mode: 'medium_recall', review_status: 'active', preview: true,
        })) },
    } : {} });
    if (p.startsWith('/api/jobs/')) return json(job);
    if (p === '/api/batch/latest') return json(null);
    if (p === '/api/asr/selection' || p.startsWith('/api/asr/models/')) return json(asr);
    if (p === '/api/asr/models') return json([asr]);
    if (p === '/api/settings/ai') return json({ provider: 'openai_compatible', model: 'mock-model', has_api_key: true });
    if (p.endsWith('/stream')) return route.fulfill({ contentType: 'video/mp4', path: media });
    if (p.endsWith('/thumbnail')) return route.fulfill({ status: 204 });
    return json({});
  });
  const waitCanvas = () => page.waitForFunction(() => Number(document.querySelector('.task-space canvas')?.dataset.frames) > 0);
  const frames = () => page.locator('.task-space canvas').getAttribute('data-frames').then(Number);
  async function exerciseView(label) {
    const point = await page.evaluate(() => {
      const canvas = document.querySelector('.task-space canvas'), rect = canvas.getBoundingClientRect();
      for (const y of [.4, .5, .3, .6]) for (const x of [.65, .55, .45, .35]) {
        const p = { x: rect.x + rect.width * x, y: rect.y + rect.height * y };
        if (document.elementFromPoint(p.x, p.y) === canvas) return p;
      }
    });
    assert.ok(point, `Scene has an interactive exposed region ${label}`);
    const scroll = await page.evaluate(() => scrollY);
    const panel = await page.locator('.task-space-dossier').boundingBox();
    await page.mouse.move(point.x, point.y); await page.mouse.wheel(0, -120);
    await page.waitForFunction(() => Number(document.querySelector('.task-space canvas').dataset.viewZoom) > 1.15);
    await page.waitForTimeout(700);
    const view = await page.locator('.task-space canvas').evaluate(el => ({ zoom: +el.dataset.viewZoom, yaw: +el.dataset.viewYaw, pitch: +el.dataset.viewPitch }));
    assert.ok(Math.abs(view.yaw) + Math.abs(view.pitch) > .001, `Pointer changes the scene angle ${label}`);
    assert.ok(Math.abs(view.yaw) < .053 && Math.abs(view.pitch) < .036, 'Tilt stays subtle');
    assert.equal(await page.evaluate(() => scrollY), scroll, 'Scene wheel does not scroll the page');
    assert.deepEqual(await page.locator('.task-space-dossier').boundingBox(), panel, 'UI does not scale with the scene');
    await page.screenshot({ path: path.join(output, `view-zoom-tilt-${label}.png`) });
    await page.mouse.wheel(0, 120);
    await page.waitForFunction(() => Math.abs(Number(document.querySelector('.task-space canvas').dataset.viewZoom) - 1) < .001);
    await page.locator('.task-space-dossier').hover(); await page.mouse.wheel(0, 150); await page.waitForTimeout(1200);
    assert.equal(await page.locator('.task-space canvas').getAttribute('data-view-zoom'), '1.0000', 'Panel scrolling does not zoom');
    assert.equal(await page.locator('.task-space canvas').getAttribute('data-view-yaw'), '0.0000', 'Leaving the scene restores angle');
    await page.locator('.task-space-dossier').evaluate(el => { el.scrollTop = 0; });
    await page.getByRole('button', { name: '重置视角', exact: true }).click();
    await page.mouse.move(2, 2);
  }
  // Screenshot pixels are sampled through a 2D image, so production needs no preserved WebGL buffer.
  async function pixels(name) {
    // The scene now runs behind the UI; exclude foreground labels and cards
    // from pixel assertions about mapped green characters.
    const isolate = await page.addStyleTag({ content: '.task-space-visual, .task-space-dossier { visibility: hidden !important; }' });
    let png;
    try { png = await page.locator('.task-space canvas').screenshot({ path: path.join(output, `${name}.png`) }); }
    finally { await isolate.evaluate(el => el.remove()); }
    return page.evaluate(async (src) => {
      const image = new Image(); image.src = src; await image.decode();
      const canvas = document.createElement('canvas'); canvas.width = 480; canvas.height = 320;
      const ctx = canvas.getContext('2d'); ctx.drawImage(image, 0, 0, 480, 320);
      const data = ctx.getImageData(0, 0, 480, 320).data;
      let red = 0, green = 0, cyan = 0, silver = 0, hash = 0, glyphHash = 0;
      const points = [];
      for (let i = 0; i < data.length; i += 4) {
        const [r,g,b] = data.slice(i,i+3);
        if (r > 30 && r > g * 1.5 && r > b * 1.3) red++;
        if (g > 30 && g > r * 1.5 && g > b * 1.3) {
          green++;
          if (green % 4 === 0 && points.length < 200) points.push({ x: (i / 4 % 480 + .5) / 480, y: (Math.floor(i / 4 / 480) + .5) / 320 });
        }
        if (g > 85 && g > r * 1.5 && b > r * 1.5) {
          cyan++;
        }
        if (Math.min(r, g, b) > 75 && Math.max(r, g, b) < Math.min(r, g, b) * 1.45) {
          silver++; glyphHash = (glyphHash * 31 + i + r + g + b) >>> 0;
        }
        hash = (hash * 31 + r + 3*g + 7*b) >>> 0;
      }
      return {red,green,cyan,silver,hash,glyphHash,points};
    }, `data:image/png;base64,${png.toString('base64')}`);
  }
  try {
    await page.goto(`${origin}/settings?section=experimental`);
    const toggle = page.getByRole('switch', { name: '启用实验性字幕空间' });
    await toggle.waitFor(); assert.equal(await toggle.isChecked(), false);
    assert.equal(requests.some((u) => u.includes('/cyberspace/runtime') || u.includes('/deps/three')), false, 'Default off does not load the renderer');
    const watch = page.getByRole('button', { name: '观看演示', exact: true });
    await watch.click();
    await page.waitForFunction(() => Number(document.querySelector('.subtitle-demo-scene canvas')?.dataset.characters) > 0);
    const demoCanvas = page.locator('.subtitle-demo-scene canvas');
    await page.getByRole('button', { name: '暂停演示', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('.subtitle-demo-scene canvas')?.dataset.pending === '0');
    await page.waitForTimeout(1000);
    const pausedFrames = await demoCanvas.getAttribute('data-frames');
    await page.waitForTimeout(600); assert.equal(await demoCanvas.getAttribute('data-frames'), pausedFrames, 'Paused preview parks its renderer');
    assert.equal(await toggle.isChecked(), false, 'Watching does not enable the experiment');
    await page.getByRole('button', { name: '继续演示', exact: true }).click();
    const demoBefore = await demoCanvas.screenshot(); await page.waitForTimeout(700);
    assert.notDeepEqual(await demoCanvas.screenshot(), demoBefore, 'Preview contains live animation, not a poster');
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      await page.getByRole('group', { name: '演示阶段' }).getByRole('button', { name: /字幕形成/ }).click();
      await page.waitForFunction(() => Number(document.querySelector('.subtitle-demo-scene canvas')?.dataset.characters) > 0);
      await page.waitForTimeout(1800);
      await page.screenshot({ path: path.join(output, `settings-demo-asr-${width}.png`) });
      const fits = await page.getByRole('dialog').evaluate(el => {
        const r = el.getBoundingClientRect();
        return r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight && el.scrollWidth <= el.clientWidth;
      });
      assert.ok(fits, `Demo fits PC window ${width}`);
      await page.getByRole('group', { name: '演示阶段' }).getByRole('button', { name: /考点命中/ }).click();
      await page.waitForFunction(() => Number(document.querySelector('.subtitle-demo-scene canvas')?.dataset.highlighted) > 0);
      await page.waitForTimeout(1800);
      await page.locator('.subtitle-demo-results > button').first().click();
      assert.ok(await page.locator('.subtitle-demo-detail blockquote').innerText());
      await page.screenshot({ path: path.join(output, `settings-demo-hits-${width}.png`) });
    }
    await page.getByRole('button', { name: '重播演示', exact: true }).click();
    await page.getByText('演示结束', { exact: true }).waitFor({ timeout: 35000 });
    assert.equal(await page.locator('.subtitle-demo-results > button').count(), fixture.markers.length, 'Full demo reaches all example results');
    await page.keyboard.press('Escape');
    assert.equal(await page.getByRole('dialog').count(), 0);
    assert.ok(await watch.evaluate(el => el === document.activeElement), 'Demo returns focus to settings');
    assert.deepEqual(writes, [], 'Demo never submits processing or settings mutations');
    assert.equal(requests.filter(url => /\/api\/jobs\/[^/]+\/(transcript|analysis-preview)/.test(url)).length, 0, 'Demo never polls real task previews');
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await watch.click();
    await page.waitForFunction(() => Number(document.querySelector('.subtitle-demo-scene canvas')?.dataset.characters) > 0);
    await page.getByRole('group', { name: '演示阶段' }).getByRole('button', { name: /考点命中/ }).click();
    await page.waitForFunction(() => Number(document.querySelector('.subtitle-demo-scene canvas')?.dataset.highlighted) > 0);
    assert.equal(await page.locator('.subtitle-demo-results > button').count(), fixture.markers.length);
    assert.ok(await page.getByRole('button', { name: '暂停演示', exact: true }).isDisabled());
    await page.getByRole('button', { name: '关闭演示', exact: true }).click();
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await toggle.check(); await page.reload(); await toggle.waitFor(); assert.equal(await toggle.isChecked(), true);
    assert.equal(await page.locator('.analysis-quality-picker input[type="radio"]').count(), 2, 'Light and particle variants are available');
    await page.screenshot({ path: path.join(output, 'settings.png') });
    await page.goto(`${origin}/process?videoId=7`);
    await page.getByRole('button', { name: '开始处理', exact: true }).waitFor();
    assert.equal(await page.locator('.is-space-cooling').count(), 0);
    await page.screenshot({ path: path.join(output, 'before-start-warm.png') });
    await page.evaluate(() => {
      window.__entrySamples = [];
      window.__entryTimer = setInterval(() => {
        const root = document.querySelector('.task-space'); if (!root) return;
        const surface = selector => { const style = getComputedStyle(root.querySelector(selector));
          return { visibility: style.visibility, opacity: Number(style.opacity), clip: style.clipPath }; };
        window.__entrySamples.push({ time: performance.now(), state: root.dataset.entrance,
          scale: Number(root.querySelector('canvas').dataset.entranceScale ?? 0),
          right: surface('.task-space-dossier'), top: surface('.task-space-title-entry'), bottom: surface('.task-space-transcript') });
      }, 30);
    });
    await page.getByRole('button', { name: '开始处理', exact: true }).click();
    await page.waitForSelector('.is-space-cooling');
    const opacity = () => page.locator('.workbench-screen:not([hidden])').evaluate(el => Number(getComputedStyle(el, '::before').opacity));
    const initialOpacity = await opacity();
    assert.ok(await page.getByRole('button', { name: '停止处理', exact: true }).isVisible(), 'Stop is available throughout entry');
    assert.ok(await page.getByRole('button', { name: '停止处理', exact: true }).evaluate(el => {
      const r = el.getBoundingClientRect(); return el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2));
    }), 'The submitted-task toast does not cover the stop button');
    await waitCanvas();
    await page.screenshot({ path: path.join(output, 'transition-start.png') });
    await page.waitForTimeout(650);
    await page.screenshot({ path: path.join(output, 'entrance-right-unroll.png') });
    await page.waitForTimeout(500);
    await page.screenshot({ path: path.join(output, 'entrance-top-bottom-unroll.png') });
    await page.waitForFunction(() => document.querySelector('.task-space')?.dataset.entrance === 'done');
    const entry = await page.evaluate(() => { clearInterval(window.__entryTimer); return window.__entrySamples; });
    const growing = entry.filter(sample => sample.scale > 0 && sample.state === 'running');
    assert.ok(growing.length > 5 && growing[0].scale < .8 && entry.at(-1).scale === 1, 'Ring grows from small to its normal framing');
    const visibleAt = key => entry.find(sample => sample[key].visibility === 'visible' && sample[key].opacity > .1)?.time;
    assert.ok(visibleAt('right') < visibleAt('top') && visibleAt('right') < visibleAt('bottom'), 'Right rail opens before the two left surfaces');
    assert.ok(Math.abs(visibleAt('top') - visibleAt('bottom')) < 150, 'Top and bottom unfold together');
    for (const region of ['right', 'top', 'bottom']) {
      assert.ok(new Set(entry.map(sample => sample[region].clip)).size > 4, `${region} reveals continuously through a directional mask`);
    }
    fs.writeFileSync(path.join(output, 'entrance-sequence.json'), JSON.stringify(entry, null, 2));
    await page.screenshot({ path: path.join(output, 'entrance-settled.png') });
    assert.ok((await opacity()) > initialOpacity, 'Warm page crossfades into cool page');
    await waitCanvas();
    assert.equal(await page.locator('.process-status-section').count(), 0, 'No separate status card');
    assert.equal(await page.locator('.task-space-telemetry [role="progressbar"]').getAttribute('aria-valuenow'), '20');
    assert.ok(await page.locator('.task-space-telemetry').getByText('已保存 390/1406 个片段').isVisible());
    await page.waitForTimeout(4600);
    assert.equal(await page.locator('.task-space canvas').getAttribute('data-characters'), '0', 'No example subtitles used for ASR');
    liveCount = 48;
    await page.waitForFunction(() => Number(document.querySelector('.task-space canvas')?.dataset.characters) > 0);
    assert.ok(await page.getByText('本次转写片段', { exact: true }).isVisible());
    assert.equal(await page.locator('.task-space').getAttribute('data-entrance'), 'done', 'New subtitles do not replay entry');
    await page.waitForSelector('.task-space-transcript li[data-reveal="active"]');
    // Seek only the finite text animations for reproducible particle/reveal evidence.
    const reveal = await page.evaluate(() => {
      const row = document.querySelector('.task-space-transcript li:last-child');
      const animations = row.getAnimations({ subtree: true });
      for (const animation of animations) { animation.pause(); animation.currentTime = 220; }
      const glyphs = [...row.querySelectorAll('.task-space-reveal-glyph')];
      return { first: Number(getComputedStyle(glyphs[0].firstElementChild).opacity),
        last: Number(getComputedStyle(glyphs.at(-1).firstElementChild).opacity),
        particles: glyphs.some(g => Number(getComputedStyle(g, '::before').opacity) > .1) };
    });
    assert.ok(reveal.first > .5 && reveal.last === 0 && reveal.particles, 'Per-character reveal leaves the ungenerated right side blank, with local particles');
    await page.screenshot({ path: path.join(output, 'subtitle-gathering.png') });
    await page.evaluate(() => { for (const a of document.querySelector('.task-space-transcript').getAnimations({ subtree: true })) a.play(); });
    await page.waitForFunction(() => document.querySelector('.task-space-transcript-window')?.dataset.feedPhase === 'idle');
    await page.evaluate(() => {
      window.__retainedGlyph = document.querySelector('.task-space-transcript li:last-child .task-space-reveal-glyph');
      window.__tickerSamples = [];
      window.__tickerSampleTimer = setInterval(() => {
        const panel = document.querySelector('.task-space-transcript-window');
        const active = [...panel.querySelectorAll('li[data-reveal="active"]')];
        const last = panel.querySelector('li:last-child');
        window.__tickerSamples.push({ phase: panel.dataset.feedPhase, active: active.length,
          key: last.dataset.subtitleKey, y: last.getBoundingClientRect().y,
          text: last.querySelector('.task-space-accessible-text').textContent });
      }, 35);
    });
    liveCount = 51;
    await page.waitForFunction(() => document.querySelector('.task-space-transcript .task-space-strip-heading b')?.textContent === '51');
    assert.ok(await page.evaluate(() => window.__retainedGlyph.isConnected && Number(getComputedStyle(window.__retainedGlyph.firstElementChild).opacity) === 1), 'New rows do not replay surviving glyphs');
    await page.waitForFunction(() => document.querySelector('.task-space-transcript-window')?.dataset.feedPhase === 'idle');
    const ticker = await page.evaluate(() => {
      clearInterval(window.__tickerSampleTimer);
      return { samples: window.__tickerSamples, backgrounds: [...document.querySelectorAll('.task-space-transcript li')].map(el => getComputedStyle(el).backgroundColor) };
    });
    assert.ok(ticker.samples.every(sample => sample.active <= 1), 'Only one sentence reveals at any instant');
    const revealed = [...new Set(ticker.samples.filter(sample => sample.active === 1).map(sample => sample.text))];
    assert.deepEqual(revealed, fixture.subtitles.slice(48, 51).map(item => item.text), 'A new batch plays in sentence order, never as three simultaneous rows');
    const scrolling = ticker.samples.filter(sample => sample.phase === 'scrolling');
    assert.ok(scrolling.some((sample, index) => scrolling.slice(index + 1).some(next => sample.key === next.key && Math.abs(sample.y - next.y) > 2)), 'Rows visibly scroll between sampled frames');
    assert.ok(ticker.backgrounds.every(color => color === 'rgba(0, 0, 0, 0)'), 'No white highlighted row');
    fs.writeFileSync(path.join(output, 'transcript-sequence.json'), JSON.stringify(ticker, null, 2));
    await page.screenshot({ path: path.join(output, 'subtitle-connected.png') });
    await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.pending === '0');
    await page.waitForTimeout(1500);
    const a = await pixels('asr-geometric-1'); await page.waitForTimeout(900); const b = await pixels('asr-geometric-2');
    assert.ok(a.silver > 20, 'Silver glyphs are visible against the graphite background');
    assert.ok(a.silver > a.cyan, 'Neutral glyphs dominate instead of saturated cyan');
    assert.notEqual(a.glyphHash, b.glyphHash, 'Bright glyphs move, not only the dark background');
    const surface = await page.locator('.task-space-float-face').first().evaluate(el => getComputedStyle(el).backgroundImage);
    assert.ok(surface.includes('41, 44, 43'), 'Floating panels use a neutral charcoal surface');
    assert.equal(await page.locator('.workbench-topbar .task-indicator').evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(37, 42, 39)', 'Task status matches the dark surface');
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 }); await page.waitForTimeout(200);
      await page.screenshot({ path: path.join(output, `asr-page-${width}.png`) });
      const stop = await page.getByRole('button', { name: '停止处理', exact: true }).boundingBox();
      assert.ok(stop && stop.y + stop.height <= 949, `Task controls in first viewport ${width}`);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      const composition = await page.evaluate(() => {
        const canvas = document.querySelector('.task-space canvas').getBoundingClientRect();
        const rail = document.querySelector('.task-space-dossier').getBoundingClientRect();
        const transcript = document.querySelector('.task-space-transcript li:last-child').getBoundingClientRect();
        const latest = document.elementFromPoint(transcript.x + transcript.width / 2, transcript.y + transcript.height / 2);
        const heading = document.querySelector('.task-space-title h2');
        return { behindRail: canvas.right >= rail.right, behindText: canvas.bottom >= transcript.bottom,
          foregroundText: Boolean(latest?.closest('.task-space-transcript')), weight: Number(getComputedStyle(heading).fontWeight),
          configFits: document.querySelector('.task-space-source dl').getBoundingClientRect().bottom <= rail.bottom };
      });
      assert.ok(composition.behindRail && composition.behindText && composition.foregroundText, `Layered composition ${width}`);
      assert.ok(composition.weight >= 650 && composition.configFits, `Text hierarchy and complete configuration ${width}`);
      await exerciseView(`asr-${width}`);
      for (const selector of ['.task-space-status-surface', '.task-space-transcript .task-space-float']) {
        const panel = page.locator(selector), face = panel.locator(':scope > .task-space-float-face');
        const resting = await face.evaluate(el => getComputedStyle(el).transform);
        const bounds = await panel.boundingBox();
        await page.mouse.move(bounds.x + bounds.width * .85, bounds.y + bounds.height * .2);
        await page.waitForTimeout(300);
        assert.equal(await panel.getAttribute('data-hovered'), 'true');
        assert.notEqual(await face.evaluate(el => getComputedStyle(el).transform), resting, `Pointer tilts ${selector} at ${width}`);
        const fit = await face.evaluate(el => {
          const rect = el.getBoundingClientRect(), parent = el.closest('.task-space-body').getBoundingClientRect();
          return rect.left >= parent.left && rect.right <= parent.right && rect.top >= parent.top && rect.bottom <= parent.bottom;
        });
        assert.ok(fit, `Tilt is not clipped ${selector} at ${width}`);
        await page.screenshot({ path: path.join(output, `floating-${selector.includes('transcript') ? 'transcript' : 'status'}-${width}.png`) });
        await page.mouse.move(2, 2); await page.waitForTimeout(300);
        assert.equal(await panel.getAttribute('data-hovered'), null);
        assert.equal(await face.evaluate(el => getComputedStyle(el).transform), resting, 'Panel returns to rest');
      }
    }
    const before = await frames(); await page.waitForTimeout(1200); const fps = ((await frames()) - before) / 1.2;
    assert.ok(fps <= 31, `Capped FPS ${fps}`);
    job = { ...job, stage: 'analyzing', progress: 65 };
    await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.phase === 'analyzing');
    await page.waitForFunction(() => Number(document.querySelector('.task-space canvas')?.dataset.characters) > 0);
    await page.waitForTimeout(700);
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 }); await page.waitForTimeout(250);
      const wall = await pixels(`layers-${width}`);
      assert.ok(wall.silver > 20, `Neutral subtitle layers visible ${width}`);
      assert.ok(wall.silver > wall.red, `Analysis no longer uses the red distortion wall ${width}`);
      assert.equal(await page.locator('.task-space canvas').getAttribute('data-analysis-scanning'), 'true');
      await page.screenshot({ path: path.join(output, `process-${width}.png`) });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `No overflow ${width}`);
      await exerciseView(`wall-${width}`);
      const { points: _, ...wallPixels } = wall;
      snapshots.push({ width, wall: wallPixels });
    }
    completedChunks = 1;
    await page.getByText('正在分析第 2 / 3 块', { exact: true }).waitFor();
    await page.waitForFunction(() => Number(document.querySelector('.task-space canvas')?.dataset.highlighted) > 0);
    await page.waitForTimeout(1400);
    const firstHits = Number(await page.locator('.task-space canvas').getAttribute('data-highlighted'));
    const firstChunk = await pixels('chunk-1');
    assert.ok(firstChunk.green > 0 && firstChunk.silver > 20, 'First chunk highlights evidence while other text stays neutral');
    await page.getByRole('button', { name: '本次考点预览', exact: true }).click();
    await page.locator('.task-space-result-list button').first().click();
    assert.equal(await page.locator('.task-space-detail a').count(), 0, 'Preview is not a saved review record');
    await page.keyboard.press('Escape');
    completedChunks = 2;
    await page.getByText('正在分析第 3 / 3 块', { exact: true }).waitFor();
    await page.waitForTimeout(1600);
    assert.ok(Number(await page.locator('.task-space canvas').getAttribute('data-highlighted')) > firstHits, 'Next chunk adds hits while analysis is still running');
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 }); await page.waitForTimeout(200);
      await page.screenshot({ path: path.join(output, `chunk-progress-${width}.png`) });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Chunk status fits ${width}`);
    }
    await page.reload(); await waitCanvas();
    await page.getByText('正在分析第 3 / 3 块', { exact: true }).waitFor();
    await page.waitForFunction(() => Number(document.querySelector('.task-space canvas')?.dataset.highlighted) > 0);
    completedChunks = 3;
    await page.waitForTimeout(5200);
    const { points: greenPoints, ...result } = await pixels('result'); assert.ok(result.green > 20, 'Only mapped results turn green while analysis continues');
    const canvasBounds = await page.locator('.task-space canvas').boundingBox();
    for (const point of greenPoints) {
      const screen = { x: canvasBounds.x + point.x * canvasBounds.width, y: canvasBounds.y + point.y * canvasBounds.height };
      const exposed = await page.evaluate(p => document.elementFromPoint(p.x, p.y)?.tagName === 'CANVAS', screen);
      if (!exposed) continue;
      await page.mouse.click(screen.x, screen.y);
      if (await page.locator('.task-space-detail').count()) break;
    }
    assert.ok(await page.locator('.task-space-detail').count(), 'Exposed green characters remain clickable behind foreground panels');
    await page.locator('.task-space-result-list button').first().click();
    assert.ok(await page.locator('.task-space-detail blockquote').innerText());
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 }); await page.waitForTimeout(150);
      await page.screenshot({ path: path.join(output, `results-${width}.png`) });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    await page.keyboard.press('Escape'); assert.equal(await page.locator('.task-space-results').count(), 0);
    job = { ...job, status: 'awaiting_api', stage: 'awaiting_api', progress: 75 };
    await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.phase === 'waiting');
    assert.equal(await page.locator('.task-space canvas').getAttribute('data-analysis-scanning'), 'false');
    assert.ok(page.url().includes('/process?videoId=7'), 'Waiting for API does not open review');
    await page.waitForTimeout(500); const settled = await frames(); await page.waitForTimeout(500);
    assert.equal(await frames(), settled, 'Waiting scene has no permanent render loop');
    // Leaving the processing view never sends a cancellation request.
    await page.getByRole('button', { name: '查看配置' }).click(); await page.waitForSelector('.task-space', { state: 'detached' });
    assert.deepEqual(writes, ['/api/videos/7/process']);
    await page.getByRole('button', { name: '打开字幕空间' }).click(); await waitCanvas();
    await page.emulateMedia({ reducedMotion: 'reduce' });
    job = { ...job, id: 'reduced-job', job_id: 'reduced-job', status: 'running', stage: 'transcribing' };
    await page.goto(`${origin}/process?videoId=7`); await waitCanvas();
    await page.waitForSelector('.task-space-reveal-glyph');
    assert.ok(await page.locator('.task-space-reveal-glyph > span').evaluateAll(nodes => nodes.every(el => getComputedStyle(el).animationName === 'none' && getComputedStyle(el).opacity === '1')), 'Reduced motion shows complete text immediately');
    const reducedPanel = page.locator('.task-space-status-surface');
    await reducedPanel.hover(); await page.waitForTimeout(250);
    assert.equal(await reducedPanel.getAttribute('data-hovered'), null);
    assert.equal(await reducedPanel.locator(':scope > .task-space-float-face').evaluate(el => getComputedStyle(el).transform), 'none');
    await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.pending === '0');
    await page.waitForTimeout(100);
    const staticFrame = await frames(); await page.waitForTimeout(600); assert.equal(await frames(), staticFrame, 'Static mode has no permanent render loop');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    await page.getByRole('button', { name: '停止处理', exact: true }).click();
    await page.waitForSelector('.is-space-returning');
    await page.screenshot({ path: path.join(output, 'stop-transition.png') });
    await page.waitForSelector('.task-space', { state: 'detached' });
    await page.waitForTimeout(1000);
    assert.equal(await page.locator('.is-space-cooling').count(), 0);
    assert.equal(await page.locator('.job-error').count(), 0, 'User stop is not an error');
    await page.screenshot({ path: path.join(output, 'stop-restored-warm.png') });
    assert.deepEqual(writes, ['/api/videos/7/process', '/api/jobs/reduced-job/cancel']);
    await page.locator('.process-start-button').click(); await waitCanvas();
    await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.phase === 'transcribing');
    assert.ok(writes.includes('/api/videos/7/retry'));
    job = { ...job, status: 'completed', stage: 'completed', progress: 100 };
    await page.waitForURL(`${origin}/review/7?library_id=visual-test`);
    await page.waitForSelector('.video-player');
    assert.equal(await page.locator('.task-space').count(), 0, 'Completion leaves the animation immediately');
    assert.equal(await page.locator('.video-player').getAttribute('autoplay'), '');
    await page.waitForFunction(() => { const video = document.querySelector('.video-player'); return video && !video.paused && video.currentTime > 0; });
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 }); await page.waitForTimeout(150);
      await page.screenshot({ path: path.join(output, `completed-review-${width}.png`) });
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    noResults = true; completedChunks = 0; job = { ...job, id: 'empty-job', job_id: 'empty-job', status: 'completed', stage: 'completed' };
    await page.goto(`${origin}/process?videoId=7`);
    await page.waitForURL(`${origin}/review/7?library_id=visual-test`);
    await page.waitForSelector('.video-player');
    assert.equal(await page.locator('.marker-item').count(), 0, 'Zero results still open the playable video');
    await page.goto(`${origin}/settings?section=experimental`); await toggle.uncheck();
    started = false;
    await page.goto(`${origin}/process?videoId=7`); await page.getByRole('heading', { name: '处理选项' }).waitFor();
    assert.equal(await page.locator('.task-space').count(), 0);
    assert.equal(await page.locator('.is-space-cooling').count(), 0);
    assert.equal(await page.evaluate((key) => localStorage.getItem(key), key), 'off');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ snapshots, fps, result, writes, errors }, null, 2));
    console.log(JSON.stringify({ output, passed: true, fps, result }));
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png') });
    console.error({ output, errors, message: error.stack }); process.exitCode = 1;
  } finally { await browser.close(); }
})();
