// Isolated renderer with synthetic batches. No backend or user files are accessed.
const { chromium } = require("./playwright.cjs");
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const assert = require('node:assert/strict');
const origin = process.env.FRONTEND_URL || 'http://127.0.0.1:5175';
const output = path.resolve(__dirname, '..', '.runtime', `cyberspace-stream-${Date.now()}`);

(async () => {
  fs.mkdirSync(output, { recursive: true });
  const { fixture } = await import(pathToFileURL(path.resolve(__dirname, '../experiments/subtitle-cyberspace/src/fixture.js')));
  const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
  const page = await browser.newPage({ viewport: { width: 1316, height: 900 } });
  const errors = [], samples = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.route('**/api/**', route => route.abort());
  await page.route('**/stream-harness', route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: `<!doctype html><html><head><meta charset="utf-8"><style>
    @font-face{font-family:'Noto Experiment';src:url('/fonts/NotoSansSC-Variable.ttf');font-weight:100 900}
    *{box-sizing:border-box}body{margin:0;background:#070f17;color:#dce8ee;font:14px sans-serif}
    header{padding:20px;border-bottom:1px solid #30404c}#view{height:760px;width:100%}canvas{width:100%;height:100%;display:block}
    </style></head><body><header>字幕增量更新验证 · 模拟数据</header><div id="view"><canvas></canvas></div></body></html>` }));
  try {
    await page.goto(`${origin}/stream-harness`);
    await page.evaluate(async () => {
      const { createTaskSpace } = await import('/src/cyberspace/runtime.js');
      const modulePath = performance.getEntriesByType('resource').find(entry => new URL(entry.name).pathname.endsWith('/subtitle-cyberspace/src/scene.js')).name;
      const { SubtitleScene } = await import(modulePath);
      const originalLoad = SubtitleScene.prototype.load, originalClear = SubtitleScene.prototype.clear, originalRender = SubtitleScene.prototype.render;
      window.qa = { errors: [], clears: 0, timings: [] };
      SubtitleScene.prototype.load = function(...args) { window.qa.scene = this; return originalLoad.apply(this, args); };
      SubtitleScene.prototype.clear = function(...args) { window.qa.clears++; return originalClear.apply(this, args); };
      SubtitleScene.prototype.render = function(...args) {
        const start = performance.now(), updating = this.live?.pending;
        originalRender.apply(this, args);
        window.qa.timings.push({ updating, ms: performance.now() - start });
      };
      const canvas = document.querySelector('canvas');
      window.qa.controller = createTaskSpace(canvas, { key: 'stream-isolated', onSelect: id => { window.qa.selected = id; },
        onError: message => window.qa.errors.push(message), onQuality: message => { window.qa.quality = message; } });
      window.qa.controller.update({ subtitles: [], markers: [], phase: 'preparing', reduced: false });
    });
    const snapshot = () => page.evaluate(() => {
      const scene = window.qa.scene, live = scene.live;
      return { ...live.stats(), frames: Number(document.querySelector('canvas').dataset.frames), phase: document.querySelector('canvas').dataset.phase,
        time: scene.time, count: scene.records.length, clearCount: window.qa.clears, fontReady: live.fontReady,
        textures: live.pages.map(p => p.texture.uuid), geometry: live.pages.map(p => p.mesh.geometry.uuid),
        records: scene.records.map(r => ({ key: r.key, char: r.char, arrival: r.arrival, stair: r.stair, cell: r.cell })) };
    });
    const send = (subtitles, phase = 'transcribing', markers = [], reduced = false) => page.evaluate(({ subtitles, phase, markers, reduced }) => {
      window.qa.controller.update({ subtitles, markers, phase, reduced });
    }, { subtitles, phase, markers, reduced });
    const settled = () => page.waitForFunction(() => {
      const live = window.qa.scene.live;
      return live.fontReady && !live.pending && !live.transitioning;
    });
    await page.waitForFunction(() => Number(document.querySelector('canvas').dataset.frames) > 0);
    const smallZoom = await page.evaluate(() => window.qa.scene.camera.zoom);
    await page.waitForFunction(() => document.querySelector('canvas').dataset.entranceScale === '1.0000');
    assert.ok(await page.evaluate(zoom => window.qa.scene.camera.zoom > zoom * 1.25, smallZoom), 'Actual projected ring expands, not the full-page background canvas');
    await page.waitForTimeout(4300);
    assert.equal((await snapshot()).count, 0);
    await page.screenshot({ path: path.join(output, '01-empty-skeleton.png') });

    await send(fixture.subtitles.slice(0, 8));
    await page.waitForTimeout(120);
    const early = await snapshot(); samples.push({ label: 'first-120ms', ...early, records: undefined });
    assert.ok(early.gathering > 0 && early.opacity < early.count, 'New words gather instead of appearing fully opaque');
    await page.waitForTimeout(250);
    await page.screenshot({ path: path.join(output, '02-first-gather.png') });
    await settled();
    const first = await snapshot();
    assert.ok(first.count > 0 && first.opacity >= first.count - 1);
    await page.screenshot({ path: path.join(output, '03-first-settled.png') });

    await send(fixture.subtitles.slice(0, 48));
    await page.waitForTimeout(150);
    const secondEarly = await snapshot();
    assert.ok(secondEarly.frames > first.frames && secondEarly.gathering > 0);
    await settled();
    const second = await snapshot();
    for (const old of first.records) {
      const current = second.records.find(r => r.key === old.key);
      assert.deepEqual(current, old, `Existing character remains stable: ${old.key}`);
    }
    assert.deepEqual(second.textures.slice(0, first.textures.length), first.textures, 'Atlas pages are reused');
    assert.deepEqual(second.geometry.slice(0, first.geometry.length), first.geometry, 'GPU geometry is reused');
    assert.equal(second.clearCount, 0, 'No full scene rebuild');
    await page.screenshot({ path: path.join(output, '04-appended-settled.png') });

    await send(fixture.subtitles.slice(20, 70));
    await page.waitForTimeout(180);
    const rolling = await snapshot(); assert.ok(rolling.dissolving > 0, 'Old preview words fade out');
    await page.screenshot({ path: path.join(output, '05-window-handoff.png') });
    await settled();
    const rollEnd = await snapshot(); assert.equal(rollEnd.dissolving, 0);
    assert.equal(new Set(rollEnd.records.map(r => r.cell)).size, rollEnd.count);

    // Burst coalescing and a stage change while new words are still gathering.
    await send(fixture.subtitles.slice(0, 32));
    await send(fixture.subtitles.slice(0, 60));
    await send(fixture.subtitles);
    await page.waitForTimeout(100);
    await send(fixture.subtitles, 'analyzing');
    await page.waitForFunction(() => document.querySelector('canvas').dataset.phase === 'analyzing');
    assert.ok((await snapshot()).time >= 124, 'AI transition does not wait for the subtitle queue');
    await page.waitForTimeout(900);
    assert.ok((await snapshot()).time >= 126.4);
    await page.screenshot({ path: path.join(output, '06-wall.png') });
    await send(fixture.subtitles, 'completed', fixture.markers);
    await page.waitForTimeout(5500); await settled();
    const complete = await snapshot();
    await page.waitForTimeout(300); assert.equal((await snapshot()).frames, complete.frames);
    await page.screenshot({ path: path.join(output, '07-complete.png') });

    // The same real camera used to draw the wall must also locate clickable hits.
    const originalZoom = await page.evaluate(() => window.qa.scene.camera.zoom);
    await page.mouse.move(850, 300); await page.mouse.wheel(0, -120); await page.waitForTimeout(1500);
    assert.ok(await page.evaluate(zoom => window.qa.scene.camera.zoom > zoom * 1.15, originalZoom), 'Wheel changes actual projection');
    assert.ok(await page.evaluate(() => Math.abs(window.qa.scene.camera.position.x) > .1), 'Pointer moves actual viewing angle');
    const hit = await page.evaluate(() => {
      const scene = window.qa.scene;
      for (const id of scene.hits.size ? scene.mappings.map(m => m.id) : []) {
        const p = scene.pointForMarker(id);
        if (p && scene.pick(p.x, p.y) !== null) return p;
      }
    });
    assert.ok(hit, 'Mapped characters can be located after zoom and tilt');
    await page.mouse.click(hit.x, hit.y);
    assert.ok(await page.evaluate(() => window.qa.selected != null), 'Transformed hit opens its marker');
    await page.screenshot({ path: path.join(output, '08-inspected-wall.png') });
    await page.mouse.move(2, 2);
    await page.evaluate(() => window.qa.controller.resetView()); await page.waitForTimeout(1800);
    assert.ok(await page.evaluate(zoom => Math.abs(window.qa.scene.camera.zoom - zoom) < .001, originalZoom));
    const parked = (await snapshot()).frames; await page.waitForTimeout(300);
    assert.equal((await snapshot()).frames, parked, 'View easing stops requesting frames once settled');

    // More unique glyphs than the cache budget, all in synthetic temporary data.
    for (const offset of [0, 4000]) {
      const text = Array.from({ length: 4000 }, (_, i) => String.fromCodePoint(0x4e00 + offset + i)).join('');
      await send([{ id: offset + 1000, start_ms: 0, end_ms: 10000, text }], 'transcribing', [], true);
      await settled();
      const sample = await snapshot();
      assert.ok(sample.count <= sample.capacity && sample.glyphs <= 4096 && sample.atlasPages <= 16, 'Bounded glyph cache and instances');
    }
    const staticSample = await snapshot(); await page.waitForTimeout(300); assert.equal((await snapshot()).frames, staticSample.frames);
    const metrics = await page.evaluate(() => {
      const times = window.qa.timings.filter(t => t.updating).map(t => t.ms).sort((a, b) => a - b);
      return { errors: window.qa.errors, updates: times.length, updateP95Ms: times[Math.floor(times.length * .95)], maxUpdateMs: Math.max(...times), quality: window.qa.quality };
    });
    assert.deepEqual(errors, []); assert.deepEqual(metrics.errors, []);
    await page.evaluate(() => window.qa.controller.dispose());
    fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify({ passed: true, samples, metrics, firstCount: first.count, secondCount: second.count, cache: { glyphs: staticSample.glyphs, pages: staticSample.atlasPages } }, null, 2));
    console.log(JSON.stringify({ passed: true, output, metrics }));
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png') });
    console.error({ output, errors, error: error.stack }); process.exitCode = 1;
  } finally { await browser.close(); }
})();
