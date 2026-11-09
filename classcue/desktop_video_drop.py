"""Forward native WebView2 file drops to the library importer."""

from __future__ import annotations

import json
from threading import RLock
from uuid import uuid4


class VideoDropBridge:
    def __init__(self, window, check_origin):
        self.window, self.check_origin = window, check_origin
        self.bindings = {}
        self._lock = RLock()

    def bind(self):
        with self._lock:
            return self._bind()

    def _bind(self):
        from webview.dom import DOMEventHandler

        self.check_origin()
        # pywebview stores one Element per DOM node. Retire it before rebinding.
        for token in list(self.bindings):
            self.unbind(token)
        element = self.window.dom.get_element("#classcue-video-import")
        if element is None:
            raise ValueError("视频导入区域尚未就绪")
        token = uuid4().hex

        def dropped(event):
            self.check_origin()
            files = event.get("dataTransfer", {}).get("files", [])
            payload = {
                "token": token,
                "files": [{"name": file.get("name", ""), "path": file.get("pywebviewFullPath")}
                          for file in files],
            }
            self.window.run_js(
                "window.dispatchEvent(new CustomEvent('classcue-video-drop',"
                f"{{detail:{json.dumps(payload, ensure_ascii=True)}}}))"
            )

        handler = DOMEventHandler(dropped, prevent_default=True)
        element.on("drop", handler)
        self.bindings[token] = (element, handler)
        return token

    def unbind(self, token):
        with self._lock:
            self.check_origin()
            binding = self.bindings.pop(token, None)
            if binding:
                element, handler = binding
                from webview.dom import _dnd_state
                from webview.dom.element import Element

                # pywebview clears this registry on reload, but retains its drop count.
                if isinstance(element, Element) and self.window.dom._elements.get(element._node_id) is not element:
                    _dnd_state["num_listeners"] = max(0, _dnd_state["num_listeners"] - 1)
                    return
                try:
                    element.off("drop", handler.callback)
                except KeyError:
                    if not isinstance(element, Element) or self.window.dom._elements.get(element._node_id) is element:
                        raise
                    _dnd_state["num_listeners"] = max(0, _dnd_state["num_listeners"] - 1)
                    return
                if isinstance(element, Element) and handler.callback in element._event_handler_ids:
                    # React may already have removed the node; off() then skips cleanup.
                    self.window.dom._elements.pop(element._node_id, None)
                    _dnd_state["num_listeners"] = max(0, _dnd_state["num_listeners"] - 1)
