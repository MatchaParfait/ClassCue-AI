import { expect, it } from "vitest";
import { lockModalEnvironment } from "./modalEnvironment";

it("releases nested dialogs out of order without leaving the page locked", () => {
  const page = document.createElement("div");
  document.body.style.overflow = "auto";
  document.body.style.paddingRight = "7px";
  const first = lockModalEnvironment(page);
  const second = lockModalEnvironment(page);
  expect(document.body.dataset.modalLocked).toBe("true");
  first(); first();
  expect(document.body.style.overflow).toBe("hidden");
  expect(page.inert).toBe(true);
  second();
  expect(document.body.style.overflow).toBe("auto");
  expect(document.body.style.paddingRight).toBe("7px");
  expect(page.inert).toBe(false);
  expect(document.body.dataset.modalLocked).toBeUndefined();
  document.body.style.overflow = "";
  document.body.style.paddingRight = "";
});

it("preserves an existing inert state and releases independent backgrounds", () => {
  const page = document.createElement("div"), folder = document.createElement("div");
  page.inert = true;
  const first = lockModalEnvironment(page), second = lockModalEnvironment(folder);
  second();
  expect(folder.inert).toBe(false);
  expect(page.inert).toBe(true);
  expect(document.body.style.overflow).toBe("hidden");
  first();
  expect(page.inert).toBe(true);
  expect(document.body.style.overflow).toBe("");
});
