const COVER_ID = "classroom-startup-cover";
const previewImages = new WeakMap<HTMLElement, Map<number, string>>();

export function getStartupThumbnail(libraryId: string | undefined, videoId: number): string | null {
  const cover = document.getElementById(COVER_ID);
  if (!libraryId || cover?.dataset.libraryId !== libraryId) return null;
  let images = previewImages.get(cover);
  if (!images) {
    images = new Map();
    const html = cover.querySelector("iframe")?.srcdoc;
    if (html) {
      // Parse the bounded, read-only snapshot once, never fetch or execute its content.
      const preview = new DOMParser().parseFromString(html, "text/html");
      for (const card of preview.querySelectorAll("[data-library-key]")) {
        const match = /^video-(\d+)$/.exec(card.getAttribute("data-library-key") ?? "");
        const src = card.querySelector("img")?.getAttribute("src");
        if (match && src?.startsWith("data:image/jpeg;base64,")) images.set(Number(match[1]), src);
      }
    }
    previewImages.set(cover, images);
  }
  return images.get(videoId) ?? null;
}

// Wait for useful pixels, not an arbitrary minimum animation duration.
export function releaseStartupCover(root: HTMLElement | null, beforeReveal?: () => void) {
  const cover = document.getElementById(COVER_ID);
  if (!cover) return;
  let disposed = false;
  let frame = 0;
  const cleanups: Array<() => void> = [];
  const images = [...(root?.querySelectorAll<HTMLImageElement>(".video-thumbnail img") ?? [])]
    .filter(image => {
      const bounds = image.getBoundingClientRect();
      return bounds.bottom > 0 && bounds.top < window.innerHeight && bounds.right > 0 && bounds.left < window.innerWidth;
    });
  const decode = (image: HTMLImageElement) => image.decode?.().catch(() => {}) ?? Promise.resolve();
  const ready = images.map(image => image.complete ? decode(image) : new Promise<void>(resolve => {
    const done = () => { off(); void decode(image).finally(resolve); };
    const off = () => { image.removeEventListener("load", done); image.removeEventListener("error", done); };
    cleanups.push(off);
    image.addEventListener("load", done, { once: true });
    image.addEventListener("error", done, { once: true });
  }));
  const surface = root?.closest(".library-page") ?? root;
  const backgroundUrls = new Set<string>();
  for (const element of surface?.querySelectorAll<HTMLElement>(".library-scene, .character-art") ?? []) {
    const style = window.getComputedStyle(element);
    const bounds = element.getBoundingClientRect();
    if (style.display === "none" || bounds.bottom <= 0 || bounds.top >= window.innerHeight) continue;
    for (const match of style.backgroundImage.matchAll(/url\((?:"([^"]*)"|'([^']*)'|([^)]*))\)/g)) {
      backgroundUrls.add((match[1] ?? match[2] ?? match[3]).trim());
    }
  }
  const backgrounds = [...backgroundUrls].map(url => new Promise<void>(resolve => {
    const image = new Image();
    const done = () => { image.onload = null; image.onerror = null; void decode(image).finally(resolve); };
    image.onload = done;
    image.onerror = done;
    cleanups.push(() => { image.onload = null; image.onerror = null; });
    image.src = url;
    if (image.complete) done();
  }));
  const cancelPending = () => {
    window.cancelAnimationFrame(frame);
    cleanups.forEach(cleanup => cleanup());
  };
  const remove = () => {
    cover.dispatchEvent(new Event("classroom-startup-released"));
    cover.remove();
  };
  const reveal = () => {
    if (disposed || !cover.isConnected || cover.dataset.phase === "fading") return;
    cancelPending();
    beforeReveal?.();
    cover.dataset.phase = "fading";
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches || !cover.animate) { remove(); return; }
    const animation = cover.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 160, easing: "ease-out", fill: "forwards" });
    void animation.finished.catch(() => {}).finally(remove);
  };
  // The startup surface owns timeout recovery; a timer must not expose half-ready content.
  void Promise.all([...ready, ...backgrounds, document.fonts?.ready]).then(() => {
    if (disposed) return;
    // The scroll handoff has committed. Let its layout and decoded images paint first.
    frame = window.requestAnimationFrame(() => { frame = window.requestAnimationFrame(reveal); });
  });
  return () => { disposed = true; cancelPending(); };
}
