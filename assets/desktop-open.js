// A link preview only reads HTML; desktop navigation starts in a visible page.
const target = JSON.parse(document.querySelector("#target").textContent);
const heading = document.querySelector("#heading");
const message = document.querySelector("#message");
const retry = document.querySelector("#retry");
const web = document.querySelector("#web");
let pending = false;
let attempted = Boolean(target.error);

async function openDesktop() {
  if (pending) return;
  attempted = true;
  pending = true;
  retry.hidden = true;
  web.hidden = true;
  web.removeAttribute("href");
  heading.textContent = "正在打开桌面版";
  message.setAttribute("role", "status");
  message.textContent = "正在连接 ClassCue…首次启动可能需要一些时间。";
  try {
    const response = await fetch("/api/desktop-links/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(target), credentials: "same-origin",
    });
    const result = await response.json();
    if (response.ok && result.navigation === "requested") {
      heading.textContent = "已发送到桌面版";
      message.textContent = "请在 ClassCue 桌面窗口中查看回放。";
      return;
    }
    heading.textContent = "暂时无法打开桌面版";
    message.setAttribute("role", "alert");
    const detail = result.detail ?? result;
    message.textContent = typeof detail === "string" ? detail : detail.message || "请稍后重新打开链接。";
    if (result.code === "desktop_unavailable") {
      retry.hidden = false;
      // Only the validated response may offer a fallback for this library.
      if (typeof result.web_url === "string" && /^\/review\/[1-9]\d*\?/.test(result.web_url)) {
        web.setAttribute("href", result.web_url);
        web.hidden = false;
      }
    }
  } catch {
    heading.textContent = "尚未确认是否打开";
    message.setAttribute("role", "alert");
    message.textContent = "与 ClassCue 的连接中断了，请先检查桌面窗口，再重试打开。";
    retry.hidden = false;
  } finally {
    pending = false;
  }
}

retry.addEventListener("click", () => void openDesktop());
function openWhenVisible() {
  if (!attempted && document.visibilityState === "visible" && window.top === window)
    void openDesktop();
}
document.addEventListener("visibilitychange", openWhenVisible);
openWhenVisible();
