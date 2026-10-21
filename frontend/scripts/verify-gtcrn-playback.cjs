const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

async function main() {
  const base = process.argv[2] || "http://127.0.0.1:5175";
  const root = path.resolve(__dirname, "../..");
  const artifact = path.join(root, ".runtime", "gtcrn-playback");
  await fs.mkdir(artifact, { recursive: true });
  const media = await fs.readFile(path.join(root, "gtcrn-realtime-experiment/static/demo.mp4"));
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    const frames = { input: 0, output: 0, nonzero: 0 };
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("websocket", (socket) => {
      if (!socket.url().includes("/api/audio/denoise")) return;
      socket.on("framesent", ({ payload }) => { if (Buffer.isBuffer(payload)) frames.input++; });
      socket.on("framereceived", ({ payload }) => {
        if (!Buffer.isBuffer(payload)) return;
        frames.output++;
        for (let i = 8; i < payload.length; i += 4) {
          if (Math.abs(payload.readFloatLE(i)) > 0.00001) { frames.nonzero++; break; }
        }
      });
    });
    await page.addInitScript(() => {
      window.audioCheck = { stats: [], captures: 0, sockets: [], analysers: [], pauses: 0 };
      const NativeContext = window.AudioContext;
      window.AudioContext = class extends NativeContext {
        createMediaElementSource(element) {
          window.audioCheck.captures++;
          element.addEventListener("pause", () => window.audioCheck.pauses++);
          return super.createMediaElementSource(element);
        }
      };
      const NativeNode = window.AudioWorkletNode;
      window.AudioWorkletNode = class extends NativeNode {
        constructor(context, ...args) {
          super(context, ...args);
          const analyser = context.createAnalyser();
          this.connect(analyser);
          window.audioCheck.analysers.push(analyser);
          this.port.addEventListener("message", ({ data }) => {
            if (data.type === "stats") window.audioCheck.stats.push(data);
          });
        }
      };
      const NativeSocket = window.WebSocket;
      window.WebSocket = class extends NativeSocket {
        constructor(...args) { super(...args); window.audioCheck.sockets.push(this); }
      };
    });
    await page.route("https://interactive-examples.mdn.mozilla.net/media/cc0-videos/flower.mp4", (route) =>
      route.fulfill({ status: 200, contentType: "video/mp4", headers: { "access-control-allow-origin": "*" }, body: media }));
    await page.goto(`${base}/demo`, { waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: "播放", exact: true }).waitFor();
    await page.evaluate(() => { document.querySelector("video").loop = true; });
    await page.getByRole("button", { name: "播放", exact: true }).click();
    await page.waitForFunction(() => document.querySelector("video").currentTime > 0.1);
    const control = page.getByRole("button", { name: "实时降噪", exact: true });
    await control.click();
    await page.waitForFunction(() => document.querySelector('[aria-label="实时降噪"]').getAttribute("aria-pressed") === "true");
    await page.waitForFunction(() => window.audioCheck.stats.some((s) => s.wetSamples > 4096));
    assert.ok(frames.nonzero > 20, "Expected actual nonzero GTCRN output");
    await page.waitForFunction(() => {
      const analyser = window.audioCheck.analysers[0];
      const pcm = new Float32Array(analyser.fftSize);
      analyser.getFloatTimeDomainData(pcm);
      return pcm.some((v) => Math.abs(v) > 0.00001);
    });
    const pausesBefore = await page.evaluate(() => window.audioCheck.pauses);
    await control.click();
    await page.waitForFunction(() => document.querySelector('[aria-label="实时降噪"]').getAttribute("aria-pressed") === "false");
    const count = frames.input;
    await page.waitForTimeout(400);
    assert.equal(frames.input, count, "Off must stop inference input");
    assert.equal(await page.locator("video").evaluate((video) => video.paused), false);
    await control.click();
    await page.waitForFunction(() => document.querySelector('[aria-label="实时降噪"]').getAttribute("aria-pressed") === "true");
    assert.equal(await page.evaluate(() => window.audioCheck.pauses), pausesBefore, "Toggle must not pause playback");
    await page.getByRole("button", { name: "声道映射", exact: true }).click();
    await page.getByRole("radio", { name: "左声道 → 双声道", exact: true }).click();
    assert.equal(await page.evaluate(() => window.audioCheck.captures), 1, "Channel/denoise must share one media source");
    await page.getByRole("combobox", { name: "播放速度", exact: true }).selectOption("2");
    await page.evaluate(() => { document.querySelector("video").currentTime = 1; });
    await page.waitForTimeout(1200);
    assert.equal(await control.getAttribute("aria-pressed"), "true");
    await page.getByRole("button", { name: "暂停", exact: true }).click();
    await page.waitForTimeout(150);
    const pausedCount = frames.input;
    await page.waitForTimeout(250);
    assert.equal(frames.input, pausedCount, "Pause must stop inference input");
    await page.getByRole("button", { name: "播放", exact: true }).click();
    await page.waitForTimeout(1000);
    assert.ok(frames.input > pausedCount);
    await page.evaluate(() => window.audioCheck.sockets.filter((s) => s.url.includes("/api/audio/denoise")).at(-1).close());
    await page.waitForFunction(() => document.querySelector('[aria-label="实时降噪"]').getAttribute("aria-pressed") === "false");
    assert.equal(await page.locator("video").evaluate((video) => video.paused), false);
    assert.match(await page.locator(".player-denoise-error").innerText(), /原声/);
    await control.click();
    await page.waitForFunction(() => document.querySelector('[aria-label="实时降噪"]').getAttribute("aria-pressed") === "true");
    await page.waitForTimeout(1200);
    assert.equal(await page.locator(".player-denoise-error").count(), 0);
    const soakSeconds = Number(process.env.GTCRN_SOAK_SECONDS || 0);
    if (soakSeconds > 0) await page.waitForTimeout(soakSeconds * 1000);
    assert.equal(await control.getAttribute("aria-pressed"), "true");
    await page.screenshot({ path: path.join(artifact, "desktop-1440.png"), fullPage: true });
    await page.setViewportSize({ width: 1100, height: 800 });
    await page.screenshot({ path: path.join(artifact, "desktop-1100.png"), fullPage: true });
    const result = await page.evaluate(() => ({
      stats: window.audioCheck.stats, captures: window.audioCheck.captures,
      time: document.querySelector("video").currentTime,
      paused: document.querySelector("video").paused,
      overflow: document.documentElement.scrollWidth > innerWidth,
    }));
    assert.equal(result.overflow, false);
    assert.equal(result.captures, 1);
    assert.deepEqual(errors, []);
    assert.ok(result.stats.every((s) => s.underruns < 6));
    await control.click();
    await fs.writeFile(path.join(artifact, "report.json"), JSON.stringify({ frames, errors, soakSeconds, ...result }, null, 2));
    console.log(JSON.stringify({ passed: true, frames, errors, soakSeconds, captures: result.captures, artifact }, null, 2));
  } finally {
    await browser.close();
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
