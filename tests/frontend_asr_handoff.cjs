// Isolated live renderer; fixture packets never reach ASR, LLM or user media.
const { chromium } = require("./playwright.cjs");
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const origin = process.env.FRONTEND_URL || 'http://127.0.0.1:5175';
const output = path.resolve(__dirname, '..', '.runtime', `asr-row-handoff-${Date.now()}`);
const modules = '/@fs/' + path.resolve(__dirname, '../experiments/subtitle-cyberspace/src').replaceAll('\\', '/');

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, recordVideo: { dir: output, size: { width: 1440, height: 900 } } });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.route('**/api/**', r => r.abort());
  await page.route('**/qa-asr-rows', r => r.fulfill({ contentType: 'text/html', body: `<!doctype html><style>
    @font-face{font-family:'Noto Experiment';src:url('/fonts/NotoSansSC-Variable.ttf');font-weight:100 900}
    body{margin:0;background:#101112}main{width:100vw;height:100vh;position:relative}canvas{width:100%;height:100%;display:block}
    </style><main><canvas></canvas></main>` }));
  try {
    await page.goto(`${origin}/qa-asr-rows`);
    await page.evaluate(async modules => {
      const { createTaskSpace } = await import('/src/cyberspace/runtime.js');
      const sceneUrl = performance.getEntriesByType('resource').find(entry => new URL(entry.name).pathname.endsWith('/subtitle-cyberspace/src/scene.js')).name;
      const { SubtitleScene } = await import(sceneUrl);
      const { fixture } = await import(modules + '/fixture.js');
      const { liveOpacity } = await import(modules + '/live-layout.js');
      const qa = window.qa = { errors: [], measures: [], measuring: false, packet: null };
      const render = SubtitleScene.prototype.render;
      SubtitleScene.prototype.render = function (...args) {
        qa.scene = this; render.apply(this, args);
        if (!qa.measuring || !this.live) return;
        const clock = this.live.clock, rows = this.records;
        qa.measures.push({ opacity: rows.reduce((sum, r) => sum + liveOpacity(r, clock), 0),
          active: new Set(rows.filter(r => r.departure < 1e6 || clock < r.arrival + r.appear).map(r => r.asr.slot)).size });
      };
      qa.controller = createTaskSpace(document.querySelector('canvas'), { key: 'row-handoff-qa', analysisQuality: 'high', analysisVariant: 'nailong',
        onSelect() {}, onQuality() {}, onError: e => qa.errors.push(e) });
      qa.send = (offset = 0, reduced = false, paused = false, count = 72, phase = 'transcribing') => {
        qa.packet = { subtitles: fixture.subtitles.slice(0, count).map(s => ({ ...s, id: s.id + offset })), markers: [], phase, reduced, paused };
        qa.controller.update(qa.packet);
      };
      qa.send();
    }, modules);
    const settled = () => page.waitForFunction(() => window.qa.scene?.live?.fontReady && !qa.scene.live.pending && !qa.scene.live.transitioning,
      null, { timeout: 45000 });
    const screenshot = name => page.screenshot({ path: path.join(output, name + '.png') });
    await settled();
    await screenshot('01-before');
    const capacity = await page.evaluate(() => qa.scene.records.length);
    assert.equal(capacity, 810);
    await page.evaluate(() => { qa.measuring = true; qa.send(10000); });
    await page.waitForTimeout(240); await screenshot('02-departure');
    await page.waitForTimeout(300); await screenshot('03-gather');
    await page.evaluate(() => qa.controller.update({ ...qa.packet, paused: true }));
    await page.waitForTimeout(100);
    const parked = await page.evaluate(() => ({ clock: qa.scene.live.clock, ids: qa.scene.records.map(r => r.key) }));
    await page.waitForTimeout(450);
    assert.deepEqual(await page.evaluate(() => ({ clock: qa.scene.live.clock, ids: qa.scene.records.map(r => r.key) })), parked);
    await page.evaluate(() => qa.controller.update(qa.packet));
    await settled(); await screenshot('04-complete');
    const metrics = await page.evaluate(() => {
      qa.measuring = false;
      return { minOpacity: Math.min(...qa.measures.map(m => m.opacity)), maxActive: Math.max(...qa.measures.map(m => m.active)),
        current: qa.scene.records.every(r => r.subtitleId >= 10000), uniqueCells: new Set(qa.scene.records.map(r => r.cell)).size };
    });
    assert.ok(metrics.minOpacity > capacity * .9, JSON.stringify(metrics));
    assert.ok(metrics.maxActive <= 6, JSON.stringify(metrics));
    assert.equal(metrics.current, true); assert.equal(metrics.uniqueCells, capacity);
    const revision = await page.evaluate(() => qa.scene.live.layout.revision);
    await page.evaluate(() => qa.controller.update(structuredClone(qa.packet))); await page.waitForTimeout(200);
    assert.equal(await page.evaluate(() => qa.scene.live.layout.revision), revision);
    // Rapid packets replace the queued target, not a backlog of stale batches.
    await page.evaluate(() => { qa.send(20000); qa.send(30000); qa.send(40000, true); });
    await settled();
    assert.ok(await page.evaluate(() => qa.scene.records.every(r => r.subtitleId >= 40000)));
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate(() => qa.send(50000)); await page.waitForTimeout(400);
    await screenshot('05-mobile-handoff'); await settled(); await screenshot('06-mobile-complete');
    const nonblank = await page.evaluate(() => {
      qa.scene.captureFrame();
      const gl = qa.scene.renderer.getContext(), pixels = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4);
      gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      let ink = 0; for (let i = 0; i < pixels.length; i += 4) if (Math.max(pixels[i], pixels[i + 1], pixels[i + 2]) > 60) ink++;
      return ink;
    });
    assert.ok(nonblank > 500, `Canvas ink pixels: ${nonblank}`);
    await page.evaluate(() => { qa.send(60000); qa.send(60000, false, false, 72, 'analyzing'); });
    await page.waitForFunction(() => document.querySelector('canvas').dataset.analysisStyle === 'gravity-subtitles');
    assert.deepEqual(await page.evaluate(() => qa.errors), []); assert.deepEqual(errors, []);
    await page.evaluate(() => qa.controller.dispose());
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ passed: true, metrics, nonblank }, null, 2));
    console.log(JSON.stringify({ passed: true, output, metrics, nonblank }));
  } catch (error) { await screenshotFailure(); throw error; }
  finally { await page.close(); await browser.close(); }
  async function screenshotFailure() { await page.screenshot({ path: path.join(output, 'failure.png') }); console.error({ output, errors }); }
})().catch(error => { console.error(error); process.exitCode = 1; });
