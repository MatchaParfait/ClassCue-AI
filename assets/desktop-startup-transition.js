(() => {
  const data = __STARTUP_TRANSITION_DATA__;
  if (window !== window.top || location.origin !== data.origin || location.pathname !== "/") return;
  if (data.view) window.__classroomStartupView = data.view;
  const install = () => {
    if (!document.documentElement || document.getElementById("classroom-startup-cover")) return false;
    const cover = document.createElement("div");
    cover.id = "classroom-startup-cover";
    if (data.view) cover.dataset.libraryId = data.view.library_id;
    cover.setAttribute("aria-hidden", "true");
    Object.assign(cover.style, {
      position: "fixed", top: "0", left: "0", width: "100vw", height: "100vh",
      zIndex: "9990", background: data.background ?? "#eef6f8", opacity: "1",
    });
    const frame = document.createElement("iframe");
    frame.title = "应用启动画面";
    frame.tabIndex = -1;
    frame.setAttribute("sandbox", "allow-same-origin");
    Object.assign(frame.style, { width: "100%", height: "100%", border: "0", display: "block", pointerEvents: "none" });
    let failure = null;
    const updateFailure = () => {
      const page = frame.contentDocument;
      if (!page || !failure) return;
      const message = page.getElementById("message");
      if (message) message.textContent = failure;
      const status = page.getElementById("sync");
      if (status) { status.dataset.error = "true"; status.setAttribute("role", "alert"); }
      const screen = page.querySelector(".startup-screen");
      if (screen) screen.dataset.error = "true";
      const actions = page.getElementById("actions");
      if (actions) actions.hidden = false;
      cover.removeAttribute("aria-hidden");
      frame.style.pointerEvents = "auto";
      frame.tabIndex = 0;
    };
    const fail = message => { failure = message; updateFailure(); };
    const invoke = async method => {
      const api = window.pywebview?.api;
      if (typeof api?.[method] !== "function") {
        fail("桌面连接尚未就绪，请稍后重试。");
        return;
      }
      try {
        const buttons = frame.contentDocument?.querySelectorAll("button") ?? [];
        buttons.forEach(button => { button.disabled = true; });
        await api[method]();
      } catch (error) {
        fail(String(error));
      } finally {
        frame.contentDocument?.querySelectorAll("button").forEach(button => { button.disabled = false; });
      }
    };
    frame.addEventListener("load", () => {
      if (data.view) frame.contentWindow?.scrollTo(0, data.view.scroll_y);
      const page = frame.contentDocument;
      const artwork = page?.querySelector(".startup-art");
      if (artwork?.complete && !artwork.naturalWidth) artwork.remove();
      else artwork?.addEventListener("error", event => event.currentTarget.remove(), { once: true });
      page?.querySelector('[data-startup-action="retry"]')?.addEventListener("click", () => void invoke("retry"));
      page?.querySelector('[data-startup-action="logs"]')?.addEventListener("click", () => void invoke("open_logs"));
      page?.querySelector("#dialog button")?.addEventListener("click", () => {
        page.getElementById("dialog").close();
        void window.pywebview?.api.respond("cancel");
      });
      updateFailure();
    }, { once: true });
    frame.srcdoc = data.html;
    cover.append(frame);
    // This short-lived surface is read-only; do not pass clicks to hidden controls.
    cover.addEventListener("pointerdown", event => { event.preventDefault(); event.stopPropagation(); });
    cover.addEventListener("wheel", event => event.preventDefault(), { passive: false });
    const root = document.documentElement;
    const previousAnchor = root.style.overflowAnchor;
    root.style.overflowAnchor = "none";
    let application = null, previousInert = false;
    const blockApplication = () => {
      const candidate = document.getElementById("root");
      if (!candidate || application) return;
      application = candidate;
      previousInert = Boolean(application.inert);
      application.inert = true;
    };
    blockApplication();
    const rootObserver = new MutationObserver(blockApplication);
    rootObserver.observe(document, { childList: true, subtree: true });
    const restoreAnchoring = () => {
      if (root.style.overflowAnchor === "none") root.style.overflowAnchor = previousAnchor;
    };
    const dialog = event => {
      const page = frame.contentDocument;
      const message = page?.getElementById("dialog-message");
      const modal = page?.getElementById("dialog");
      if (!message || !modal) return;
      message.textContent = event.detail.message;
      frame.style.pointerEvents = "auto";
      cover.removeAttribute("aria-hidden");
      if (!modal.open) modal.showModal();
    };
    window.addEventListener("classroom-desktop-dialog", dialog);
    document.documentElement.append(cover);
    // Keep the artwork and offer recovery instead of exposing an empty/partial page.
    const fallback = setTimeout(() => fail("首页加载时间较长。可以重试启动，或打开日志查看原因。"), 10000);
    cover.addEventListener("classroom-startup-released", () => {
      clearTimeout(fallback);
      rootObserver.disconnect();
      window.removeEventListener("classroom-desktop-dialog", dialog);
      if (application) application.inert = previousInert;
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
