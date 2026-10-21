// All API traffic is intercepted. This test never starts ASR, calls AI or touches user media.
const { chromium } = require("./playwright.cjs");
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { pathToFileURL } = require('node:url');
const { execFileSync } = require('node:child_process');
const origin = process.env.FRONTEND_URL || 'http://127.0.0.1:5175';
const output = path.resolve(__dirname, '..', '.runtime', `analysis-layers-qa-${Date.now()}`);
const key = 'classroom.experimental.subtitle-space.v1';
const quality = 'high';
const variant = ['gravity', 'dafeiyu', 'dafeiyu31', 'dafeiyu-rice', 'gpt'].includes(process.env.ANALYSIS_VARIANT) ? process.env.ANALYSIS_VARIANT : 'nailong';

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
  const page = await browser.newPage({ viewport: { width: 1440, height: 949 }, recordVideo: { dir: output, size: { width: 1440, height: 949 } } });
  const errors = [], writes = [], snapshots = [], requests = [];
  const video = { id: 7, filename: '临时测试课程.mp4', display_name: '临时测试课程', status: 'processing', duration_ms: 100000,
    library_id: 'visual-test', video_path: 'X:/test-only/video.mp4', has_transcript: false };
  let job = { id: 'space-job', job_id: 'space-job', video_id: 7, job_type: 'video_process', status: 'running', stage: 'transcribing', progress: 20, analysis_modes: ['medium_recall'] };
  let noResults = false, started = true, liveCount = 72, completedChunks = 0;
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

  try {
    await page.addInitScript(key => localStorage.setItem(key, 'dynamic'), key);
    await page.addInitScript(quality => localStorage.setItem('classroom.experimental.subtitle-space.quality.v1', quality), quality);
    await page.addInitScript(variant => localStorage.setItem('classroom.experimental.subtitle-space.variant.v1', variant), variant);
    await page.goto(`${origin}/settings?section=experimental`);
    assert.equal(await page.locator('input[name="analysis-quality"]').count(), 0);
    const enabled = page.getByRole('switch', { name: '启用实验性字幕空间' });
    await enabled.uncheck();
    await enabled.check();
    if (quality === 'high') {
      await page.locator('input[name="analysis-variant"][value="gravity"]').check();
      await page.locator(`input[name="analysis-variant"][value="${variant}"]`).check();
      assert.equal(await page.evaluate(() => localStorage.getItem('classroom.experimental.subtitle-space.variant.v1')), variant);
    }
    await page.screenshot({ path: path.join(output, '00-settings-quality.png'), fullPage: true });
    await page.getByRole('button', { name: '观看演示', exact: true }).click();
    await page.waitForFunction(() => {
      const canvas = document.querySelector('.subtitle-demo-scene canvas');
      return Number(canvas?.dataset.characters) > 500 && canvas?.dataset.pending === '0';
    });
    await page.getByRole('button', { name: '暂停演示', exact: true }).click();
    assert.equal(await page.locator('.subtitle-demo-scene canvas').getAttribute('data-phase'), 'transcribing');
    assert.equal(await page.locator('.subtitle-demo-scene canvas').getAttribute('data-particle-count'), '0', 'ASR does not allocate particle text');
    assert.equal(await page.locator('.subtitle-demo-scene canvas').getAttribute('data-gravity-phrases'), '0', 'ASR does not allocate gravity textures');
    await page.screenshot({ path: path.join(output, '00-demo-asr.png') });
    await page.getByRole('group', { name: '演示阶段' }).getByRole('button', { name: /AI 分析/ }).click();
    await page.waitForFunction(() => document.querySelector('.subtitle-demo-scene canvas')?.dataset.analysisParticles === 'true');
    if (quality === 'high') await page.waitForFunction(() => document.querySelector('.subtitle-demo-scene canvas')?.dataset.analysisStyle === 'gravity-subtitles');
    await page.waitForTimeout(900);
    await page.screenshot({ path: path.join(output, '00-demo-analysis.png') });
    if (quality === 'high') {
      await page.waitForFunction(() => Number(document.querySelector('.subtitle-demo-scene canvas')?.dataset.gravityPhrases) > 0);
      await page.screenshot({ path: path.join(output, '00-demo-combing.png') });
      const demoCanvas = page.locator('.subtitle-demo-scene canvas');
      await page.waitForFunction(variant => document.querySelector('.subtitle-demo-scene canvas')?.dataset.analysisCore ===
        (variant === 'gravity' ? 'black-hole' : variant), variant);
      const demoBounds = await demoCanvas.boundingBox();
      await page.mouse.move(demoBounds.x + demoBounds.width * .54, demoBounds.y + demoBounds.height * .45);
      for (let i = 0; i < 3; i++) { await page.mouse.wheel(0, -120); await page.waitForTimeout(100); }
      await page.waitForTimeout(400);
      await page.screenshot({ path: path.join(output, '00-demo-depth-focus.png') });
      assert.equal(await demoCanvas.getAttribute('data-particle-count'), '0', 'The preserved particle implementation stays inactive');
      await page.getByRole('button', { name: '暂停演示', exact: true }).click();
      await page.waitForFunction(() => document.querySelector('.subtitle-demo-scene canvas')?.dataset.particleTrails === '0');
      await page.getByRole('group', { name: '演示阶段' }).getByRole('button', { name: /考点命中/ }).click();
      await page.waitForFunction(() => document.querySelector('.subtitle-demo-scene canvas')?.dataset.analysisStyle === 'gravity-subtitles');
      await page.waitForTimeout(1100);
      await page.screenshot({ path: path.join(output, '00-demo-confirmed.png') });
      await page.waitForFunction(() => document.querySelector('.subtitle-demo-scene canvas')?.dataset.phase === 'completed');
      await page.waitForFunction(() => document.querySelector('.subtitle-demo-scene canvas')?.dataset.gravityJourneyMoving === 'false');
      const demoMoves = Number(await demoCanvas.getAttribute('data-gravity-journey'));
      await page.locator('.subtitle-demo-results > button').first().click();
      await page.waitForFunction(before => Number(document.querySelector('.subtitle-demo-scene canvas')?.dataset.gravityJourney) === before + 1, demoMoves);
      assert.equal(await demoCanvas.getAttribute('data-analysis-scanning'), 'false', 'Inspecting a completed result does not restart analysis');
      await page.waitForFunction(() => document.querySelector('.subtitle-demo-scene canvas')?.dataset.gravityPresenting === 'false');
      await page.waitForTimeout(250);
      const finished = await demoCanvas.getAttribute('data-frames');
      await page.waitForTimeout(600);
      assert.equal(await demoCanvas.getAttribute('data-frames'), finished, 'Completed scene stops rendering after the finite camera and arrival effects');
      await page.screenshot({ path: path.join(output, '00-completed-evidence.png') });
    }
    await page.getByRole('group', { name: '演示阶段' }).getByRole('button', { name: /字幕形成/ }).click();
    await page.waitForFunction(() => {
      const canvas = document.querySelector('.subtitle-demo-scene canvas');
      return canvas?.dataset.phase === 'transcribing' && Number(canvas?.dataset.characters) > 500 && canvas?.dataset.pending === '0';
    });
    await page.getByRole('button', { name: '暂停演示', exact: true }).click();
    await page.screenshot({ path: path.join(output, '00-demo-asr-replay.png') });
    assert.equal(await page.locator('.subtitle-demo-scene canvas').getAttribute('data-gravity-phrases'), '0', 'Replaying ASR releases gravity resources');
    await page.getByRole('button', { name: '关闭演示', exact: true }).click();
    await page.goto(`${origin}/process?videoId=7`);
    const canvas = page.locator('.task-space canvas');
    await page.waitForFunction(() => Number(document.querySelector('.task-space canvas')?.dataset.characters) > 100);
    await page.waitForTimeout(2500);
    await page.screenshot({ path: path.join(output, '01-asr.png') });
    job = { ...job, stage: 'analyzing', progress: 65 };
    await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.phase === 'analyzing');
    assert.equal(await canvas.getAttribute('data-stage-transition-count'), '1', 'ASR hands off once to analysis');
    if (quality === 'high') {
      await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.stageTransition === 'running');
      await page.screenshot({ path: path.join(output, '01-handoff-start.png') });
      await page.waitForTimeout(260);
      await page.screenshot({ path: path.join(output, '01-handoff-middle.png') });
    }
    await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.stageTransition === 'idle');
    assert.equal(await page.locator('.subtitle-stage-handoff').count(), 0);
    await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.pending === '0');
    await page.waitForTimeout(1500);
    assert.equal(await canvas.getAttribute('data-analysis-scanning'), 'true');
    assert.equal(await canvas.getAttribute('data-analysis-particles'), 'true');
    assert.equal(await canvas.getAttribute('data-analysis-quality'), quality);
    assert.equal(await canvas.getAttribute('data-analysis-style'), quality === 'high' ? 'gravity-subtitles' : 'layered');
    const particleCount = Number(await canvas.getAttribute('data-particle-count'));
    if (quality === 'high') {
      assert.equal(particleCount, 0);
      assert.equal(await canvas.getAttribute('data-analysis-variant'), variant);
      assert.equal(await canvas.getAttribute('data-analysis-core'), variant === 'gravity' ? 'black-hole' : variant);
      assert.ok(Number(await canvas.getAttribute('data-gravity-phrases')) > 0);
      assert.ok(Number(await canvas.getAttribute('data-gravity-phrases')) <= 48);
      assert.equal(await canvas.getAttribute('data-gravity-hits'), '0', 'Decorative motion invents no hits');
    }
    else assert.equal(particleCount, 0);
    assert.ok(Number(await canvas.getAttribute('data-characters')) <= 1800);
    await page.screenshot({ path: path.join(output, '02-analysis.png') });
    const beforeFrames = Number(await canvas.getAttribute('data-frames')), beforeTime = Date.now();
    await page.waitForTimeout(2000);
    const fps = (Number(await canvas.getAttribute('data-frames')) - beforeFrames) * 1000 / (Date.now() - beforeTime);
    await page.screenshot({ path: path.join(output, '02-analysis-loop.png') });
    if (quality === 'high') {
      const revision = await canvas.getAttribute('data-gravity-revision');
      const bounds = await canvas.boundingBox();
      await page.mouse.move(bounds.x + bounds.width * .70, bounds.y + bounds.height * .28);
      await page.waitForTimeout(250);
      await page.screenshot({ path: path.join(output, '02-analysis-focus.png') });
      await page.mouse.move(5, 5);
      assert.equal(await canvas.getAttribute('data-gravity-revision'), revision, 'Pointer motion does not rebuild subtitle textures');
    }
    completedChunks = 2; // Two results arrive together: only one automatic journey.
    await page.waitForFunction(() => Number(document.querySelector('.task-space canvas')?.dataset.highlighted) > 0);
    const quote = page.locator('.task-space-evidence-quote');
    const arrivingColor = await quote.evaluate(el => getComputedStyle(el).color);
    await page.screenshot({ path: path.join(output, '03-evidence-arriving.png') });
    await page.waitForTimeout(1400);
    if (quality === 'high') assert.ok(Number(await canvas.getAttribute('data-gravity-hits')) > 0, 'Real evidence colors subtitle ink');
    assert.notEqual(await quote.evaluate(el => getComputedStyle(el).color), arrivingColor, 'Returned evidence transitions from gold to green');
    if (quality === 'high') {
      await page.waitForFunction(() => {
        const d = document.querySelector('.task-space canvas')?.dataset;
        return d?.gravityJourney === '1' && d.gravityJourneyMoving === 'false';
      });
      assert.equal(await canvas.getAttribute('data-gravity-featured'), '-2', 'One representative from the packet is presented forward');
      assert.ok(Number(await canvas.getAttribute('data-gravity-path')) > 0);
      await page.waitForFunction(() => Number(document.querySelector('.task-space canvas')?.dataset.gravityEmphasis) > .9);
      assert.equal(await page.locator('.task-space-evidence .evidence-feedback').getAttribute('data-evidence-phase'), 'arrival');
      await page.waitForTimeout(450);
      assert.ok(Number(await canvas.getAttribute('data-gravity-zoom')) > 1.20, 'Evidence arrival visibly pushes in');
      assert.ok(Number(await canvas.getAttribute('data-gravity-zoom')) <= 1.301, 'Automatic zoom stays within its framing limit');
    }
    await page.screenshot({ path: path.join(output, '03-evidence.png') });
    if (quality === 'high') {
      // Moving reference fragments no longer occupy a fabricated fixed shelf.
      // Scene picking is covered with real camera/projection in gravity tests;
      // the production evidence control provides a stable, accessible entry.
      await page.locator('.task-space-evidence').click();
      await page.locator('.task-space-detail blockquote').waitFor();
      assert.ok(await page.locator('.task-space-detail blockquote').innerText(), 'Confirmed evidence opens its real quote');
      await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.gravityJourney === '2');
      await page.waitForTimeout(400);
      await page.screenshot({ path: path.join(output, '03-journey-moving.png') });
      await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.gravityJourneyMoving === 'false');
      await page.waitForFunction(() => Number(document.querySelector('.task-space canvas')?.dataset.gravityEmphasis) > .9);
      assert.equal(await page.locator('.task-space-result-list .evidence-feedback').count(), 1);
      await page.waitForTimeout(450);
      await page.screenshot({ path: path.join(output, '03-journey-manual.png') });
      await page.keyboard.press('Escape');
    }
    await page.getByRole('button', { name: '本次考点预览', exact: true }).click();
    await page.locator('.task-space-result-list button').first().click();
    assert.ok(await page.locator('.task-space-detail blockquote').innerText());
    if (quality === 'high') {
      await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.gravityJourney === '3');
      await page.locator('.task-space-result-list button').first().click();
      await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.gravityJourney === '4');
      await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.gravityJourneyMoving === 'false');
      assert.equal(await canvas.getAttribute('data-gravity-featured'), '-1');
    }
    await page.keyboard.press('Escape');
    const hits = await canvas.getAttribute('data-highlighted');
    await page.waitForTimeout(3000);
    assert.equal(await canvas.getAttribute('data-highlighted'), hits);
    if (quality === 'high') assert.equal(await canvas.getAttribute('data-gravity-journey'), '4', 'Polling does not replay the same result packet');
    assert.equal(await canvas.getAttribute('data-stage-transition-count'), '1', 'Polling does not replay the stage transition');
    if (quality === 'high') {
      // Recording can slow presentation time; wait for bounded completion rather than a fixed wall-clock delay.
      await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.gravityPresenting === 'false', null, { timeout: 15000 });
      assert.equal(await canvas.getAttribute('data-gravity-emphasis'), '0.000', 'Context brightness restores after the finite emphasis');
      assert.equal(await canvas.getAttribute('data-gravity-zoom'), '1.0000', 'Camera eases back after inspecting evidence');
      assert.equal(await page.locator('.task-space .evidence-feedback').count(), 0);
    }
    if (quality === 'high') {
      await page.evaluate(() => {
        Object.defineProperty(document, 'hidden', { configurable: true, value: true });
        document.dispatchEvent(new Event('visibilitychange'));
      });
      await page.waitForTimeout(150);
      const hiddenFrames = await canvas.getAttribute('data-frames');
      await page.waitForTimeout(600);
      assert.equal(await canvas.getAttribute('data-frames'), hiddenFrames, 'Hidden gravity scene stops rendering');
      await page.evaluate(() => {
        Object.defineProperty(document, 'hidden', { configurable: true, value: false });
        document.dispatchEvent(new Event('visibilitychange'));
      });
    }
    for (const width of [1024, 390]) {
      await page.setViewportSize({ width, height: 949 });
      await page.waitForTimeout(300);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await page.screenshot({ path: path.join(output, `analysis-${width}.png`), fullPage: true });
    }
    await page.setViewportSize({ width: 1440, height: 949 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.analysisScanning === 'false');
    assert.equal(await canvas.getAttribute('data-analysis-particles'), 'false');
    assert.equal(await canvas.getAttribute('data-particle-focus'), '0');
    assert.equal(await canvas.getAttribute('data-particle-scan'), '');
    assert.equal(await canvas.getAttribute('data-particle-trails'), '0');
    if (quality === 'high') {
      const before = Number(await canvas.getAttribute('data-gravity-journey'));
      await page.getByRole('button', { name: '本次考点预览', exact: true }).click();
      await page.locator('.task-space-result-list button').first().click();
      await page.waitForFunction(before => Number(document.querySelector('.task-space canvas')?.dataset.gravityJourney) === before + 1, before);
      assert.equal(await canvas.getAttribute('data-gravity-journey-moving'), 'false', 'Reduced motion settles the evidence view without a flight');
      assert.equal(await canvas.getAttribute('data-gravity-zoom'), '1.0000', 'Reduced motion has no automatic lens animation');
      await page.keyboard.press('Escape');
    }
    await page.waitForTimeout(1000);
    const still = await canvas.getAttribute('data-frames');
    await page.waitForTimeout(650);
    assert.equal(await canvas.getAttribute('data-frames'), still, 'Reduced motion stops continuous rendering');
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    job = { ...job, stage: 'awaiting_api', status: 'awaiting_api' };
    await page.waitForFunction(() => document.querySelector('.task-space canvas')?.dataset.phase === 'waiting');
    assert.equal(await canvas.getAttribute('data-analysis-scanning'), 'false');
    assert.equal(await canvas.getAttribute('data-analysis-particles'), 'false');
    assert.equal(await canvas.getAttribute('data-particle-trails'), '0');
    await page.waitForTimeout(500);
    const frames = await canvas.getAttribute('data-frames');
    await page.waitForTimeout(600);
    assert.equal(await canvas.getAttribute('data-frames'), frames);
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
    console.log(JSON.stringify({ output, quality, particleCount, fps, passed: true }));
  } finally { await page.close(); await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
