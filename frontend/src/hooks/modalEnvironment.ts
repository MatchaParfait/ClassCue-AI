// Dialogs may close out of order when navigation removes
// their underlying page. Release shared page locks only after the last owner.
let owners = 0;
let originalOverflow = "";
let originalPadding = "";
let originalScrollbarWidth = "";
let originalModalLockAttribute: string | null = null;
const backgrounds = new WeakMap<HTMLElement, { owners: number; inert: boolean }>();

export function lockModalEnvironment(background?: HTMLElement | null) {
  if (owners++ === 0) {
    originalOverflow = document.body.style.overflow;
    originalPadding = document.body.style.paddingRight;
    const scrollbar = document.documentElement.clientWidth > 0 ? window.innerWidth - document.documentElement.clientWidth : 0;
    originalScrollbarWidth = document.body.style.getPropertyValue("--modal-scrollbar-width");
    originalModalLockAttribute = document.body.getAttribute("data-modal-locked");
    document.body.style.overflow = "hidden";
    document.body.style.setProperty("--modal-scrollbar-width", `${Math.max(0, scrollbar)}px`);
    document.body.setAttribute("data-modal-locked", "true");
    if (scrollbar > 0) document.body.style.paddingRight = `${(parseFloat(getComputedStyle(document.body).paddingRight) || 0) + scrollbar}px`;
  }
  if (background) {
    const state = backgrounds.get(background) ?? { owners: 0, inert: background.inert ?? false };
    state.owners++;
    backgrounds.set(background, state);
    background.inert = true;
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    if (background) {
      const state = backgrounds.get(background)!;
      if (--state.owners === 0) { background.inert = state.inert; backgrounds.delete(background); }
    }
    if (--owners === 0) {
      document.body.style.overflow = originalOverflow;
      document.body.style.paddingRight = originalPadding;
      if (originalScrollbarWidth) document.body.style.setProperty("--modal-scrollbar-width", originalScrollbarWidth);
      else document.body.style.removeProperty("--modal-scrollbar-width");
      if (originalModalLockAttribute === null) document.body.removeAttribute("data-modal-locked");
      else document.body.setAttribute("data-modal-locked", originalModalLockAttribute);
    }
  };
}
