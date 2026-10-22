"""Opt-in test-process transport; never shipped or imported by the application."""

from email.message import Message
import io
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from urllib.request import OpenerDirector


class LocalResponse(io.BytesIO):
    def __init__(self, data, url, offset, delay):
        super().__init__(data[offset:])
        self.url = url
        self.status = self.code = 206 if offset else 200
        self.headers = Message()
        self.headers["Content-Length"] = str(len(data) - offset)
        if offset:
            self.headers["Content-Range"] = f"bytes {offset}-{len(data) - 1}/{len(data)}"
        self.delay = delay

    def read(self, *args):
        if self.delay:
            time.sleep(self.delay)
        return super().read(*args)

    def info(self):
        return self.headers

    def getcode(self):
        return self.code

    def geturl(self):
        return self.url


def install():
    manifest = os.environ.get("CLASSCUE_QA_TRANSPORT")
    if not manifest:
        return
    original = OpenerDirector.open
    def open_local(self, fullurl, data=None, timeout=None):
        url = getattr(fullurl, "full_url", fullurl)
        mapping = json.loads(Path(manifest).read_text(encoding="utf-8"))
        entry = mapping.get(url)
        if entry:
            headers = dict(fullurl.header_items()) if hasattr(fullurl, "header_items") else {}
            range_value = next((v for k, v in headers.items() if k.lower() == "range"), "")
            offset = int(re.fullmatch(r"bytes=(\d+)-", range_value).group(1)) if range_value else 0
            return LocalResponse(Path(entry["path"]).read_bytes(), url, offset, entry.get("delay", 0))
        if urlsplit(url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise OSError("External network blocked in isolated update acceptance")
        if timeout is None:
            return original(self, fullurl, data)
        return original(self, fullurl, data, timeout)
    OpenerDirector.open = open_local
