(() => {
  const data = __STARTUP_TRANSITION_DATA__;
  if (window !== window.top || location.origin !== data.origin || location.pathname !== "/") return;
  window.__classroomStartupView = data.view;
  const install = () => {
    if (!document.documentElement || document.getElementById("classroom-startup-cover")) return false;
    const cover = document.createElement("div");
    cover.id = "classroom-startup-cover";
    cover.dataset.libraryId = data.view.library_id;
    cover.setAttribute("aria-hidden", "true");
    Object.assign(cover.style, {
      position: "fixed", top: "0", left: "0", width: "100vw", height: "100vh",
      zIndex: "9990", background: "#f8f5e9", opacity: "1",
    });
    const frame = document.createElement("iframe");
    frame.title = "视频库启动交接";
    frame.tabIndex = -1;
    frame.setAttribute("sandbox", "allow-same-origin");
    Object.assign(frame.style, { width: "100%", height: "100%", border: "0", display: "block", pointerEvents: "none" });
    frame.addEventListener("load", () => {
      frame.contentWindow?.scrollTo(0, data.view.scroll_y);
      const message = frame.contentDocument?.getElementById("message");
      if (message) message.textContent = "正在同步本地视频库…";
    }, { once: true });
    frame.srcdoc = data.html;
    cover.append(frame);
    // This short-lived surface is read-only; do not pass clicks to hidden controls.
    cover.addEventListener("pointerdown", event => { event.preventDefault(); event.stopPropagation(); });
    cover.addEventListener("wheel", event => event.preventDefault(), { passive: false });
    const root = document.documentElement;
    const previousAnchor = root.style.overflowAnchor;
    root.style.overflowAnchor = "none";
    const restoreAnchoring = () => {
      if (root.style.overflowAnchor === "none") root.style.overflowAnchor = previousAnchor;
    };
    document.documentElement.append(cover);
    // A failed bundle must never leave an invisible application permanently covered.
    const fallback = setTimeout(() => { restoreAnchoring(); cover.remove(); }, 10000);
    cover.addEventListener("classroom-startup-released", () => {
      clearTimeout(fallback);
      restoreAnchoring();
    }, { once: true });
    return true;
  };
  if (!install()) {
    const observer = new MutationObserver(() => {
      if (install()) observer.disconnect();
    });
    observer.observe(document, { childList: true, subtree: true });
  }
})();
