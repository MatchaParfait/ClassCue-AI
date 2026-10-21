const assert = require("node:assert/strict");

module.exports = async function checkSettingsHerons({ page, scope, width }) {
  const decoration = scope.locator(".settings-herons");
  assert.equal(await decoration.getAttribute("aria-hidden"), "true");
  await decoration.locator("img").evaluateAll(images => Promise.all(images.map(image => image.decode())));
  assert.deepEqual(await decoration.locator("img").evaluateAll(images => images.map(image =>
    [image.naturalWidth, image.naturalHeight])), [[445, 453], [445, 1], [445, 1042], [511, 461], [511, 1], [511, 1029]]);
  if (width < 1500) {
    assert.equal(await decoration.isVisible(), false, "narrow PC windows reserve all space for settings");
    return;
  }
  assert.equal(await decoration.isVisible(), true);
  assert.equal(await decoration.evaluate(node => getComputedStyle(node).pointerEvents), "none");
  const content = await scope.locator(".settings-page-inner").boundingBox();
  const main = await scope.locator(".settings-page").boundingBox();
  for (const side of ["left", "right"]) {
    const bird = decoration.locator(`.settings-heron-${side}`);
    const [head, neck, body] = await Promise.all(["head", "neck", "body"].map(part =>
      bird.locator(`.settings-heron-${part}`).boundingBox()));
    assert.ok(head.width >= 90, "art remains large enough to recognize");
    if (side === "left") assert.ok(head.x > 0 && head.x + head.width < content.x - 16);
    else assert.ok(head.x > content.x + content.width + 16 && head.x + head.width < width);
    assert.ok(Math.abs(head.y + head.height - neck.y) < 1, "head meets the neck without a gap");
    assert.ok(Math.abs(neck.y + neck.height - body.y) < 1, "neck meets the body without a gap");
    assert.ok(Math.abs(body.y + body.height - (main.y + main.height - 24)) < 1);
    const expected = side === "left" ? [445 / 453, 445 / 1042] : [511 / 461, 511 / 1029];
    assert.ok(Math.abs(head.width / head.height - expected[0]) < .01);
    assert.ok(Math.abs(body.width / body.height - expected[1]) < .01);
    assert.ok(neck.height > 0, "only the neck occupies the remaining height");
  }
  const initial = await decoration.locator(".settings-heron-left .settings-heron-neck").boundingBox();
  const inner = scope.locator(".settings-page-inner");
  const oldPadding = await inner.evaluate(node => {
    const original = node.style.paddingBottom;
    node.style.paddingBottom = "360px";
    return original;
  });
  try {
    const longer = await decoration.locator(".settings-heron-left .settings-heron-neck").boundingBox();
    const longerPage = await scope.locator(".settings-page").boundingBox();
    assert.ok(longerPage.height > main.height, "the fixture makes the page longer");
    assert.ok(Math.abs(longer.height - initial.height - (longerPage.height - main.height)) < 1,
      "additional content extends only the neck");
    assert.equal(longer.width, initial.width);
    assert.equal(longer.y, initial.y);
  } finally {
    await inner.evaluate((node, padding) => { node.style.paddingBottom = padding; }, oldPadding);
  }
};
