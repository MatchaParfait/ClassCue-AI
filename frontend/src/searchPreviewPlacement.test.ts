import { expect, it } from "vitest";
import { placeSearchPreview } from "./searchPreviewPlacement";

const dialog = { left: 600, right: 1320, top: 100, bottom: 800 };
const row = { left: 616, right: 1304, top: 400, bottom: 450 };

it("uses the outside of a search dialog when a large preview fits there", () => {
  expect(placeSearchPreview(row, dialog, { width: 1920, height: 1080 })).toMatchObject({ side: "right", width: 560, left: 1332 });
});
it.each([1099, 1024, 390])("fits a preview at width %i without covering the selected row", width => {
  const anchor = { left: 16, right: width - 16, top: 390, bottom: 440 };
  const result = placeSearchPreview(anchor, { left: 10, right: width - 10, top: 50, bottom: 700 }, { width, height: 728 })!;
  const height = result.width * 9 / 16 + 92;
  expect(result.left).toBeGreaterThanOrEqual(16);
  expect(result.left + result.width).toBeLessThanOrEqual(width - 16);
  expect(result.top).toBeGreaterThanOrEqual(16);
  expect(result.top + height).toBeLessThanOrEqual(728 - 16);
  expect(result.top + height <= anchor.top || result.top >= anchor.bottom).toBe(true);
});
it("avoids showing a preview when there is too little height around the row", () => {
  expect(placeSearchPreview({ left: 16, right: 700, top: 130, bottom: 180 }, { left: 10, right: 710, top: 10, bottom: 300 }, { width: 720, height: 300 })).toBeNull();
});
