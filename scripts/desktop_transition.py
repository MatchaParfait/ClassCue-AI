"""One-navigation WebView2 injection, without changing API origins or routing."""

import logging


def install_startup_transition(window, script):
    try:
        from System import Func, Object
        task = window.native.Invoke(Func[Object](
            lambda: window.native.browser.webview.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(script)))
        script_id = task.GetAwaiter().GetResult()

        def remove():
            try:
                window.native.Invoke(Func[Object](
                    lambda: window.native.browser.webview.CoreWebView2.RemoveScriptToExecuteOnDocumentCreated(script_id)))
            except Exception:
                logging.getLogger(__name__).debug("Startup transition cleanup unavailable", exc_info=True)

        return remove
    except Exception:
        # Unsupported hosts retain the ordinary navigation and scroll handoff.
        logging.getLogger(__name__).warning("Startup transition unavailable; using ordinary navigation", exc_info=True)
        return None
