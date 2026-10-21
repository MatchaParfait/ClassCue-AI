const assert = require("node:assert/strict");

// Invoked by frontend_workbench.cjs, which intercepts all API calls.
module.exports = async function checkLibraryAtmosphere({ page, output }) {
  const atmosphere = page.locator(".library-page > .warm-atmosphere");
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.evaluate(() => scrollTo(0, 0));
  const hiddenContent = await page.addStyleTag({
    content: ".library-inner, .library-scene { visibility: hidden !important; }",
  });
  try {
    for (const width of [856, 1316, 1540, 1920]) {
      await page.setViewportSize({ width, height: 949 });
      const state = await atmosphere.evaluate(node => {
        const rect = node.getBoundingClientRect(), style = getComputedStyle(node);
        return {
          left: rect.left, top: rect.top, width: rect.width, bottom: rect.bottom,
          position: style.position, moving: node.dataset.moving,
          children: [...node.children].map(child => ({
            background: getComputedStyle(child).backgroundImage,
            playState: getComputedStyle(child).animationPlayState,
            duration: getComputedStyle(child).animationDuration,
          })),
        };
      });
      assert.equal(state.position, "fixed");
      assert.equal(state.left, 0);
      assert.equal(state.top, 64);
      assert.equal(state.width, width);
      assert.equal(state.bottom, 949);
      assert.equal(state.moving, "true");
      assert.deepEqual(state.children.map(child => child.duration), ["48s", "64s"]);
      assert.ok(state.children.every(child =>
        child.playState === "running" && child.background.startsWith("linear-gradient")));

      const animations = () => atmosphere.evaluate(node =>
        node.getAnimations({ subtree: true }).map(animation => animation.currentTime));
      const before = await animations();
      await page.waitForTimeout(200);
      const after = await animations();
      assert.ok(after.every((time, index) => time > before[index]), "background actually animates");

      // Sample the rendered wash, not just its CSS. All three columns must have color.
      const frame = await page.screenshot({ path: `${output}/library-atmosphere-${width}.png` });
      const sample = async buffer => page.evaluate(async base64 => {
        const image = new Image();
        image.src = `data:image/png;base64,${base64}`;
        await image.decode();
        const canvas = document.createElement("canvas");
        canvas.width = image.width; canvas.height = image.height;
        const context = canvas.getContext("2d");
        context.drawImage(image, 0, 0);
        return [.15, .5, .85].flatMap(x => [.4, .7].map(y =>
          [...context.getImageData(Math.floor(x * image.width), Math.floor(y * image.height), 1, 1).data]));
      }, buffer.toString("base64"));
      const first = await sample(frame);
      assert.ok(first.every(([r, g, b]) => r > 230 && g > 210 && g - b > 12),
        "soft yellow covers both sides and the center without darkening the page");
      await atmosphere.evaluate(node => node.getAnimations({ subtree: true }).forEach(animation => {
        animation.pause();
        animation.currentTime += 16000;
      }));
      const second = await sample(await page.screenshot());
      assert.ok(second.some((pixel, index) =>
        pixel.slice(0, 3).some((value, channel) => Math.abs(value - first[index][channel]) > 2)),
      "rendered color sheets move over time");
      await atmosphere.evaluate(node => node.getAnimations({ subtree: true }).forEach(animation => animation.play()));

      assert.ok(await atmosphere.evaluate(node =>
        Number(getComputedStyle(node).zIndex) >
        Number(getComputedStyle(document.querySelector(".library-scene")).zIndex)),
      "the yellow atmosphere overlays the corner illustration, below page content");
      await hiddenContent.evaluate(node => { node.textContent = ".library-inner { visibility: hidden !important; }"; });
      await page.evaluate(async () => {
        const image = new Image();
        image.src = "/art/library-bauhaus.webp";
        await image.decode();
      });
      const tinted = await page.screenshot();
      await atmosphere.evaluate(node => { node.style.visibility = "hidden"; });
      const untinted = await page.screenshot();
      await atmosphere.evaluate(node => { node.style.visibility = ""; });
      const softenedPixels = await page.evaluate(async ([withWash, withoutWash]) => {
        const read = async base64 => {
          const image = new Image();
          image.src = `data:image/png;base64,${base64}`;
          await image.decode();
          const canvas = document.createElement("canvas");
          canvas.width = image.width; canvas.height = image.height;
          const context = canvas.getContext("2d");
          context.drawImage(image, 0, 0);
          return context.getImageData(0, 0, canvas.width, canvas.height).data;
        };
        const [tinted, original] = await Promise.all([read(withWash), read(withoutWash)]);
        let softened = 0;
        for (let y = Math.floor(innerHeight * .6); y < innerHeight; y += 4) {
          for (let x = Math.floor(innerWidth * .8); x < innerWidth; x += 4) {
            const i = (y * innerWidth + x) * 4;
            if (original[i] < 160 && original[i + 1] > original[i] + 15 &&
                tinted[i] > original[i] + 5) softened++;
          }
        }
        return softened;
      }, [tinted.toString("base64"), untinted.toString("base64")]);
      assert.ok(softenedPixels > 50, "yellow tint visibly softens the illustration's darker green regions");
      await hiddenContent.evaluate(node => { node.textContent = ""; });
      await page.screenshot({ path: `${output}/library-tinted-illustration-${width}.png` });
      await hiddenContent.evaluate(node => {
        node.textContent = ".library-inner, .library-scene { visibility: hidden !important; }";
      });
    }
    await page.evaluate(() => scrollTo(0, document.body.scrollHeight));
    assert.equal((await atmosphere.boundingBox()).y, 64, "scrolling does not move the atmosphere");
    await page.evaluate(() => scrollTo(0, 0));
    await page.emulateMedia({ reducedMotion: "reduce" });
    assert.deepEqual(await atmosphere.locator("span").evaluateAll(nodes =>
      nodes.map(node => getComputedStyle(node).animationName)), ["none", "none"]);
  } finally {
    await hiddenContent.evaluate(node => node.remove());
    await page.emulateMedia({ reducedMotion: "no-preference" });
  }
};
