"""Synthetic multi-source transfers, ordered resume and cancellation."""

from contextlib import contextmanager
import hashlib
import re
from threading import Event

import httpx
import pytest

from classcue import semantic_embeddings as embeddings
from classcue.preparation_progress import DownloadProgress, preparation_events
from classcue.processing_control import ProcessingCancelled, ResourcePaused


def item(content):
    return {"Size": len(content), "Sha256": hashlib.sha256(content).hexdigest(),
            "Revision": "a" * 40}


def range_client(monkeypatch, respond):
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        @contextmanager
        def stream(self, method, url, *, headers):
            start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", headers["Range"]).groups())
            yield respond(start, end, httpx.Request(method, url))

    monkeypatch.setattr(httpx, "Client", Client)


def test_parallel_resume_orders_out_of_order_chunks_and_reports_on_caller_thread(tmp_path, monkeypatch):
    from threading import get_ident

    content = bytes(range(256)) * (100 * 1024)
    partial = tmp_path / "model.part"
    partial.write_bytes(content[:123])
    later_chunk = Event()
    caller = get_ident()
    reports = []

    def respond(start, end, request):
        if start == 123:
            assert later_chunk.wait(5)
        else:
            later_chunk.set()
        return httpx.Response(206, headers={
            "Content-Range": f"bytes {start}-{end}/{len(content)}",
        }, content=content[start:end + 1], request=request)

    range_client(monkeypatch, respond)

    def report(size):
        assert get_ident() == caller
        reports.append(size)

    embeddings._parallel_weight("https://example.invalid/model", partial,
                                len(content), report, None)
    assert partial.read_bytes() == content
    assert reports[0] == 123 and reports[-1] == len(content)


@pytest.mark.parametrize("mode", ["ignored", "wrong_total", "oversized", "short"])
def test_parallel_invalid_ranges_never_modify_existing_prefix(tmp_path, monkeypatch, mode):
    content = b"x" * (16 * 1024 * 1024)
    partial = tmp_path / "model.part"
    partial.write_bytes(b"prefix")

    def respond(start, end, request):
        total = len(content) + (1 if mode == "wrong_total" else 0)
        payload = content[start:end + 1]
        if mode == "oversized":
            payload += b"x"
        elif mode == "short":
            payload = payload[:-1]
        return httpx.Response(200 if mode == "ignored" else 206, headers={
            "Content-Range": f"bytes {start}-{end}/{total}",
        }, content=payload, request=request)

    range_client(monkeypatch, respond)
    with pytest.raises((ValueError, httpx.ReadError)):
        embeddings._parallel_weight("https://example.invalid/model", partial,
                                    len(content), lambda _: None, None)
    assert partial.read_bytes() == b"prefix"


def test_parallel_cancellation_waits_for_workers_and_keeps_only_contiguous_prefix(tmp_path, monkeypatch):
    content = b"x" * (20 * 1024 * 1024)
    partial = tmp_path / "model.part"
    partial.write_bytes(b"prefix")
    called = Event()

    def respond(start, end, request):
        called.set()
        return httpx.Response(206, headers={
            "Content-Range": f"bytes {start}-{end}/{len(content)}",
        }, content=content[start:end + 1], request=request)

    range_client(monkeypatch, respond)
    with pytest.raises(ProcessingCancelled):
        embeddings._parallel_weight("https://example.invalid/model", partial, len(content),
                                    lambda _: None, called.is_set)
    size = partial.stat().st_size
    assert partial.read_bytes() == b"prefix" + b"x" * (size - 6)
    assert size < len(content)


@pytest.mark.parametrize("failure", ["network", "hash"])
def test_source_fallback_checks_official_digest_and_reports_actual_mirror(tmp_path, monkeypatch, failure):
    content = b"verified-public-model"
    partial = tmp_path / "model.part"
    urls, events = [], []
    tracker = DownloadProgress(len(content))

    def transfer(url, path, size, report, stop):
        urls.append(url)
        if len(urls) == 1:
            if failure == "network":
                path.write_bytes(content[:5])
                raise RuntimeError("network interrupted")
            path.write_bytes(b"incorrect")
        else:
            if failure == "network":
                assert path.read_bytes() == content[:5]
            else:
                assert not path.exists()
            path.write_bytes(content)
        report(size)

    monkeypatch.setattr(embeddings, "_stream_weight", transfer)
    with preparation_events(events.append):
        tracker.file("embedding", embeddings.MODEL_ID, "model.safetensors", len(content))
        embeddings._download_weight("model.safetensors", item(content), partial, tracker, None)
    assert partial.read_bytes() == content
    assert urls[0].startswith("https://modelscope.cn/")
    assert urls[1].startswith("https://hf-mirror.com/")
    assert any(event.get("source") == "HF 镜像" for event in events)


def test_mirror_hash_mismatch_never_promotes_download(tmp_path, monkeypatch):
    content = b"correct"
    partial = tmp_path / "model.part"
    tracker = DownloadProgress(len(content))

    def wrong(url, path, size, report, stop):
        path.write_bytes(b"invalid")

    monkeypatch.setattr(embeddings, "_stream_weight", wrong)
    with preparation_events(lambda _: None):
        tracker.file("embedding", embeddings.MODEL_ID, "model.safetensors", len(content))
        with pytest.raises(RuntimeError, match="校验失败"):
            embeddings._download_weight("model.safetensors", item(content), partial, tracker, None)
    assert not partial.exists()


@pytest.mark.parametrize("error", [ProcessingCancelled("stopped"), ResourcePaused("paused")])
def test_download_control_signal_does_not_try_another_source(tmp_path, monkeypatch, error):
    content = b"correct"
    partial = tmp_path / "model.part"
    tracker = DownloadProgress(len(content))
    calls = []

    def cancel(url, *args):
        calls.append(url)
        raise error

    monkeypatch.setattr(embeddings, "_stream_weight", cancel)
    with preparation_events(lambda _: None):
        tracker.file("embedding", embeddings.MODEL_ID, "model.safetensors", len(content))
        with pytest.raises(type(error)):
            embeddings._download_weight("model.safetensors", item(content), partial, tracker, None)
    assert len(calls) == 1


def test_unsupported_parallel_ranges_fall_back_to_safe_sequential_transfer(tmp_path, monkeypatch):
    partial = tmp_path / "model.part"
    content = b"x" * (16 * 1024 * 1024)
    tracker = DownloadProgress(len(content))
    calls = []

    def no_range(*args):
        raise ValueError("range unsupported")

    def sequential(url, path, *args):
        calls.append(url)
        path.write_bytes(content)

    monkeypatch.setattr(embeddings, "_parallel_weight", no_range)
    monkeypatch.setattr(embeddings, "_stream_weight", sequential)
    with preparation_events(lambda _: None):
        tracker.file("embedding", embeddings.MODEL_ID, "model.safetensors", len(content))
        embeddings._download_weight("model.safetensors", item(content), partial, tracker, None)
    assert partial.read_bytes() == content
    assert len(calls) == 1
