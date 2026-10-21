import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it } from "vitest";
import { SubtitleReveal } from "./SubtitleReveal";

let host: HTMLDivElement, root: Root;
beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); });
const render = (text: string) => act(() => root.render(<SubtitleReveal text={text} />));
const glyphs = () => [...host.querySelectorAll<HTMLElement>(".task-space-reveal-glyph")];

it("reveals whole graphemes and supplies one complete accessible sentence", () => {
  render("学习👩‍💻e\u0301");
  expect(glyphs().map(node => node.textContent)).toEqual(["学", "习", "👩‍💻", "e\u0301"]);
  expect(host.querySelector(".task-space-accessible-text")?.textContent).toBe("学习👩‍💻e\u0301");
  expect(host.querySelector(".task-space-subtitle-ink")?.getAttribute("aria-hidden")).toBe("true");
});
it("keeps existing glyph DOM and delay when a sentence grows or is polled again", () => {
  render("这一句已经完成识别".repeat(5));
  const old = glyphs(); const delay = old.at(-1)!.style.getPropertyValue("--glyph-delay");
  render("这一句已经完成识别".repeat(5) + "新文字");
  expect(glyphs()[0]).toBe(old[0]);
  expect(glyphs()[old.length - 1].style.getPropertyValue("--glyph-delay")).toBe(delay);
  render("这一句已经完成识别".repeat(5) + "新文字");
  expect(glyphs()[0]).toBe(old[0]);
});
it("replaces only corrected characters", () => {
  render("学习递归"); const old = glyphs();
  render("学习递推");
  expect(glyphs()[0]).toBe(old[0]); expect(glyphs()[2]).toBe(old[2]);
  expect(glyphs()[3]).not.toBe(old[3]);
});
it("bounds decorative nodes and stagger while retaining the complete original text", () => {
  const text = "长句测试".repeat(1000); render(text);
  expect(glyphs()).toHaveLength(161);
  expect(glyphs().at(-1)?.textContent).toBe("…");
  expect(Math.max(...glyphs().map(node => parseFloat(node.style.getPropertyValue("--glyph-delay"))))).toBeLessThanOrEqual(1100);
  expect(host.querySelector(".task-space-accessible-text")?.textContent).toBe(text);
});
it("does not emit particles for blank characters", () => {
  render("字 空"); expect(glyphs()[1].dataset.ink).toBe("false");
});
