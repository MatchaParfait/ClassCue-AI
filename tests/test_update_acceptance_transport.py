import json
from urllib.request import Request, build_opener

import pytest

from tests.update_transport import install


def test_test_transport_preserves_urls_and_range_resumption(tmp_path, monkeypatch):
    from urllib.request import OpenerDirector
    original = OpenerDirector.open
    path = tmp_path / "fixture.zip"
    path.write_bytes(b"fixture-package")
    mapping = tmp_path / "transport.json"
    url = "https://github.com/owner/project/releases/download/v1.0.0/package.zip"
    mapping.write_text(json.dumps({url: {"path": str(path)}}), encoding="utf-8")
    monkeypatch.setenv("CLASSCUE_QA_TRANSPORT", str(mapping))
    monkeypatch.setattr(OpenerDirector, "open", original)
    install()
    with build_opener().open(Request(url, headers={"Range": "bytes=3-"})) as response:
        assert response.status == 206 and response.url == url
        assert response.headers["Content-Range"] == "bytes 3-14/15"
        assert response.read() == b"ture-package"
    with pytest.raises(OSError, match="External network blocked"):
        build_opener().open("https://unused.example")


def test_transport_without_opt_in_is_a_noop(monkeypatch):
    from urllib.request import OpenerDirector
    monkeypatch.delenv("CLASSCUE_QA_TRANSPORT", raising=False)
    original = OpenerDirector.open
    install()
    assert OpenerDirector.open is original
