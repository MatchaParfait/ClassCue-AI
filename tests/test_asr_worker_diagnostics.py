import io
import json
import os
from contextlib import nullcontext
from contextvars import ContextVar
from queue import Empty, Queue
from types import SimpleNamespace

import pytest

import qwen_vad
import transcriber
from processing_control import ProcessingCancelled


def worker(tmp_path, line):
    value = transcriber.DolphinWorker.__new__(transcriber.DolphinWorker)
    value.device = "cpu"
    value.log_path = tmp_path / "worker.log"
    value.process = SimpleNamespace(pid=123, stdin=io.StringIO(), poll=lambda: 7)
    value.events = Queue()
    value.events.put(line)
    return value


@pytest.mark.parametrize("line", [
    "not json", "[]", "{}", '{"event":"progress"}',
    '{"event":"result"}', '{"event":"error"}', '{"event":"unknown"}',
])
def test_invalid_worker_protocol_has_structured_diagnostics(tmp_path, monkeypatch, line):
    logs = []
    monkeypatch.setattr(transcriber, "log_exception", lambda *_a, **k: logs.append(k))
    value = worker(tmp_path, line)
    with pytest.raises(transcriber.ASRSetupError, match="PID 123") as error:
        value._request({}, None)
    assert isinstance(error.value.__cause__, ValueError)
    assert logs[0]["stage"] == "event_decode"
    assert logs[0]["model"] == "Dolphin"
    assert logs[0]["device"] == "cpu"
    assert logs[0]["pid"] == 123 and logs[0]["exitcode"] == 7
    assert logs[0]["error_type"]
    assert "line" not in logs[0]


def test_progress_callback_exception_is_not_mislabeled_as_protocol_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(transcriber, "log_exception", lambda *_a, **_k: pytest.fail("Not a protocol error"))
    value = worker(tmp_path, '{"event":"progress","fraction":0.5,"message":"ready"}')
    failure = ValueError("progress handler failed")

    def progress(_fraction, _message):
        raise failure

    with pytest.raises(ValueError) as error:
        value._request({}, progress)
    assert error.value is failure


def test_broken_pipe_keeps_os_error_context_without_exposing_payload(tmp_path, monkeypatch):
    value = worker(tmp_path, None)
    failure = BrokenPipeError(32, "pipe closed")

    def write(_data):
        raise failure

    value.process.stdin = SimpleNamespace(write=write)
    logs = []
    monkeypatch.setattr(transcriber, "log_exception", lambda *_a, **k: logs.append(k))
    with pytest.raises(transcriber.ASRSetupError, match="BrokenPipeError") as error:
        value._request({"secret": "sensitive-request-value"}, None)
    assert error.value.__cause__ is failure
    assert logs[0]["stage"] == "request_write" and logs[0]["errno"] == 32
    assert "sensitive-request-value" not in str(error.value)


@pytest.mark.parametrize("with_stop", [False, True])
def test_vad_always_uses_owned_runner(tmp_path, monkeypatch, with_stop):
    python = tmp_path / "python.exe"
    python.touch()
    calls = []

    def run(command, **kwargs):
        calls.append(kwargs)
        output = command[command.index("--output") + 1]
        from pathlib import Path

        Path(output).write_text(json.dumps({"backend": "firered", "speech_intervals": []}))
        return 0

    monkeypatch.setattr("qwen_process.run_process", run)
    stop = (lambda: False) if with_stop else None
    result = qwen_vad.detect_speech(
        tmp_path / "audio.wav", "firered", model_path=tmp_path / "model",
        python_path=python, should_stop=stop,
    )
    assert result["speech_intervals"] == []
    assert calls[0]["should_stop"] is stop
    assert calls[0]["timeout"] == 7200


@pytest.mark.parametrize("primary", [None, RuntimeError, ProcessingCancelled])
def test_resident_close_continues_cleanup_and_preserves_primary(tmp_path, monkeypatch, primary):
    value = worker(tmp_path, None)
    alive, stages, logs = [True], [], []
    terminate_error = OSError("terminate failed")

    def terminate():
        stages.append("terminate")
        raise terminate_error

    def wait(**_kwargs):
        stages.append("wait")
        alive[0] = False

    def stdin_close():
        stages.append("stdin")
        raise OSError("stdin close failed")

    def join(**_kwargs):
        stages.append("reader")
        raise RuntimeError("reader join failed")

    value.process.poll = lambda: None if alive[0] else 0
    value.process.terminate, value.process.wait = terminate, wait
    value.process.kill = lambda: stages.append("kill")
    value.process.stdin = SimpleNamespace(close=stdin_close)
    value.process.stdout = io.StringIO()
    value.reader = SimpleNamespace(join=join)
    monkeypatch.setattr(transcriber.subprocess, "run", lambda *_a, **_k: None)
    monkeypatch.setattr(transcriber, "log_exception", lambda *_a, **k: logs.append(k))
    failure = primary("main operation failed") if primary else None
    with pytest.raises(primary or OSError) as error:
        try:
            if failure:
                raise failure
        finally:
            value.close(primary_error=failure)
    assert error.value is (failure or terminate_error)
    assert stages == ["terminate", "wait", "stdin", "reader"]
    assert value.process.stdout.closed
    assert [log["stage"] for log in logs] == ["terminate", "stdin_close", "reader_join"]


@pytest.mark.parametrize("primary", [None, RuntimeError, ProcessingCancelled])
def test_session_cleanup_failure_preserves_primary_and_resets_owner(monkeypatch, primary):
    calls = []
    cleanup_error = OSError("worker close failed")

    class Model:
        def close(self):
            calls.append("close")
            raise cleanup_error

    monkeypatch.setattr(transcriber, "QwenWorker", Model)
    monkeypatch.setattr(transcriber, "_SESSION", ContextVar("diagnostic_scope", default=None))
    monkeypatch.setattr(transcriber, "_loaded_model", None)
    monkeypatch.setattr(transcriber, "_loaded_key", None)
    monkeypatch.setattr(transcriber, "_loaded_session", None)
    monkeypatch.setattr(transcriber.gc, "collect", lambda: calls.append("gc"))
    monkeypatch.setattr(transcriber, "log_exception", lambda *_a, **_k: None)
    failure = primary("operation failed") if primary else None
    with pytest.raises(primary or OSError) as error:
        with transcriber.asr_session():
            transcriber._loaded_model = Model()
            transcriber._loaded_key = ("test", "worker", "cpu")
            transcriber._loaded_session = transcriber._SESSION.get()
            if failure:
                raise failure
    assert error.value is (failure or cleanup_error)
    assert calls == ["close", "gc"]
    assert transcriber._loaded_model is transcriber._loaded_key is transcriber._loaded_session is None
    assert transcriber._SESSION.get() is None


@pytest.mark.parametrize("failure_type", [RuntimeError, ProcessingCancelled])
def test_transcribe_failure_is_not_replaced_by_worker_close_error(monkeypatch, failure_type):
    failure = failure_type("inference failed")
    closed = []

    class Model:
        def __init__(self, **_kwargs):
            pass

        def transcribe(self, *_args):
            raise failure

        def close(self):
            closed.append(True)
            raise OSError("worker close failed")

    monkeypatch.setattr(transcriber, "QwenWorker", Model)
    monkeypatch.setattr(transcriber, "DolphinWorker", Model)
    monkeypatch.setattr(transcriber, "_loaded_model", None)
    monkeypatch.setattr(transcriber, "_loaded_key", None)
    monkeypatch.setattr(transcriber, "_loaded_session", None)
    monkeypatch.setattr(transcriber, "_SESSION", ContextVar("failure_scope", default=None))
    monkeypatch.setattr(transcriber, "model_operation", lambda *_a: nullcontext())
    monkeypatch.setattr(transcriber, "prepare_asr_model", lambda *_a: None)
    monkeypatch.setattr(transcriber, "selected_dolphin_device", lambda: "cpu")
    monkeypatch.setattr(transcriber, "component_paths", lambda *_a: {})
    monkeypatch.setattr(transcriber, "log_exception", lambda *_a, **_k: None)
    with pytest.raises(failure_type) as error:
        transcriber.transcribe_audio("unused.wav", model_id="dolphin-small-cn")
    assert error.value is failure
    assert closed == [True]
    assert transcriber._loaded_model is None and transcriber._SESSION.get() is None


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended worker construction")
@pytest.mark.parametrize("stage", ["popen", "assign", "resume"])
def test_owned_resident_constructor_failure_closes_created_resources(tmp_path, monkeypatch, stage):
    value = transcriber.DolphinWorker.__new__(transcriber.DolphinWorker)
    value.device = "cpu"
    alive, calls = [True], []
    failure = OSError(f"{stage} failed")
    process = SimpleNamespace(
        pid=123, stdin=io.StringIO(), stdout=io.StringIO(),
        poll=lambda: None if alive[0] else 0,
    )

    def popen(_command, **kwargs):
        assert kwargs["creationflags"] & 4
        assert kwargs["start_new_session"] is False
        if stage == "popen":
            raise failure
        return process

    def stop(*_args, **_kwargs):
        alive[0] = False
        calls.append("stopped")

    class Job:
        def __init__(self, _process):
            if stage == "assign":
                raise failure

        def resume(self, _process):
            raise failure

        def close(self):
            stop()

    monkeypatch.setattr(transcriber.subprocess, "Popen", popen)
    monkeypatch.setattr(transcriber.subprocess, "run", stop)
    monkeypatch.setattr(transcriber, "WindowsJob", Job)
    monkeypatch.setattr(transcriber, "log_exception", lambda *_a, **_k: None)
    with pytest.raises(OSError) as error:
        transcriber._start_owned_worker(
            value, ["owned-model-worker"], log=None, env={}, device="cpu", reader_name="test-owned")
    assert error.value is failure
    if stage != "popen":
        assert calls == ["stopped"]
        assert process.stdin.closed and process.stdout.closed
        assert value.reader.ident is None


@pytest.mark.skipif(os.name != "nt", reason="Native Windows job-owned resident descendants")
def test_resident_close_reaps_child_after_parent_exits_while_stdout_is_held(tmp_path):
    import sys
    import time
    from scripts.launcher_process import windows_process_is_alive

    value = transcriber.DolphinWorker.__new__(transcriber.DolphinWorker)
    value.device = "cpu"
    child_pid_file = tmp_path / "child.pid"
    command = [
        sys.executable, "-u", "-c",
        "import subprocess,sys,os,pathlib;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "pathlib.Path(sys.argv[1]).write_text(str(p.pid));"
        "print('child started',flush=True);os._exit(7)",
        str(child_pid_file),
    ]
    with (tmp_path / "worker.log").open("w") as log:
        transcriber._start_owned_worker(
            value, command, log=log, env=os.environ.copy(),
            device="cpu", reader_name="test-owned-native")
        try:
            assert value.process.wait(timeout=10) == 7
            child_pid = int(child_pid_file.read_text())
            value.reader.join(timeout=.1)
            assert value.reader.is_alive()
            started = time.monotonic()
            value.close()
            assert time.monotonic() - started < 10
            assert not value.reader.is_alive()
            assert not windows_process_is_alive(child_pid)
            assert value.process.stdin.closed and value.process.stdout.closed
        finally:
            value.close()


@pytest.mark.parametrize("worker_type", [transcriber.DolphinWorker, transcriber.FireRedWorker])
def test_empty_events_with_exited_parent_fail_and_cleanup_without_waiting(tmp_path, worker_type):
    value = worker_type.__new__(worker_type)
    value.device = "cpu"
    value.log_path = tmp_path / "worker.log"
    value.process = SimpleNamespace(pid=123, stdin=io.StringIO(), poll=lambda: 7)
    polls, closed = [], []

    def get(**_kwargs):
        polls.append(True)
        if len(polls) > 1:
            pytest.fail("An exited parent must not wait again for child-held stdout")
        raise Empty

    value.events = SimpleNamespace(get=get)
    value.close = lambda **kwargs: closed.append(kwargs["primary_error"])
    with pytest.raises(transcriber.ASRSetupError) as error:
        if worker_type is transcriber.DolphinWorker:
            value._request({}, None)
        else:
            value.transcribe(
                "source.wav", {"recognizer": tmp_path, "vad": tmp_path}, None)
    assert polls == [True]
    assert closed == [error.value]
    assert "PID 123" in str(error.value) and "7" in str(error.value)
    assert str(value.log_path) in str(error.value)
