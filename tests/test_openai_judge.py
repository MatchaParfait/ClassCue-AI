import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest

from core import TranscriptSegment
from openai_judge import OpenAICompatibleJudge


def response(content='{"candidates":[]}', finish_reason="stop"):
    return SimpleNamespace(choices=[
        SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)
    ])


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def judge_with(outcomes, **kwargs):
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(outcomes)))
    return OpenAICompatibleJudge(
        "test-key", "relay-gpt", base_url="https://relay.example/v1", client=client, **kwargs
    )


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status_code = status


def test_response_format_fallback_only_removes_unsupported_parameter():
    judge = judge_with([
        ApiError(400, "response_format is not supported"), response(),
    ], token_parameter="max_completion_tokens", max_tokens=4096)
    assert not judge.judge_semantic_chunk((TranscriptSegment(1, 0, 1000, "普通讲解"),)).candidates
    first, second = judge.client.chat.completions.calls
    assert first["response_format"] == {"type": "json_object"}
    assert "response_format" not in second
    assert second["max_completion_tokens"] == 4096
    assert "max_tokens" not in second
    assert first["messages"] == second["messages"]


@pytest.mark.parametrize("error", [
    ApiError(401, "response_format unauthorized"),
    ApiError(429, "too many requests"),
    ApiError(400, "model not found"),
    ApiError(500, "response_format is unsupported"),
    ApiError(400, "response_format json_object requires JSON in messages"),
])
def test_other_api_errors_are_not_silently_downgraded(error):
    judge = judge_with([error])
    with pytest.raises(ApiError):
        judge.test_connection()
    assert len(judge.client.chat.completions.calls) == 1


def test_json_mode_can_be_disabled_without_disabling_parsing():
    judge = judge_with([response()], json_mode=False)
    judge.test_connection()
    assert "response_format" not in judge.client.chat.completions.calls[0]


def test_api_connection_uses_a_plain_text_probe():
    judge = judge_with([response("OK")])
    assert judge.test_api_connection() == "OK"
    request = judge.client.chat.completions.calls[0]
    assert "response_format" not in request
    assert request["messages"][0]["content"].startswith("You are a connectivity test")
    assert request["reasoning_effort"] == "high"
    assert request["max_tokens"] == 16384


def test_truncated_result_is_not_repaired_or_returned_as_a_valid_selection():
    judge = judge_with([response(finish_reason="length")])
    with pytest.raises(RuntimeError, match="长度上限"):
        judge.judge_semantic_chunk((TranscriptSegment(1, 0, 1000, "期末会考"),))
    assert len(judge.client.chat.completions.calls) == 1
    assert judge.client.chat.completions.calls[0]["max_tokens"] == 16384
    assert judge.profile.timeout_seconds == 120 and judge.profile.max_retries == 5


def test_relay_repairs_json_and_filters_reverse_or_unknown_ranges():
    payload = json.dumps({"candidates": [
        {"level": "confirmed", "start_sentence_id": 1, "end_sentence_id": 2, "title": "valid"},
        {"level": "confirmed", "start_sentence_id": 2, "end_sentence_id": 1},
        {"level": "confirmed", "start_sentence_id": 99, "end_sentence_id": 99},
        {"level": "possible", "start_sentence_id": 1, "end_sentence_id": 1},
    ]})
    judge = judge_with([response("invalid JSON"), response(payload)], mode="strict_exam_intent")
    result = judge.judge_semantic_chunk((
        TranscriptSegment(1, 0, 1000, "这个会考"), TranscriptSegment(2, 1000, 2000, "公式"),
    ))
    assert [candidate.title for candidate in result.candidates] == ["valid"]
    assert judge.client.chat.completions.calls[1]["max_tokens"] == 16384
    assert all(request["reasoning_effort"] == "high" for request in judge.client.chat.completions.calls)


def test_cache_identity_separates_endpoints_and_parameters_but_not_credentials():
    def identity(**kwargs):
        return OpenAICompatibleJudge(
            kwargs.pop("key", "key-one"), "same-model",
            base_url=kwargs.pop("base_url", "https://one.example/v1"),
            client=object(), **kwargs,
        ).cache_identity

    default = identity()
    assert default == identity(key="key-two", base_url="https://one.example/v1/")
    for kwargs in [
        {"base_url": "https://two.example/v1"}, {"max_tokens": 4096},
        {"json_mode": False}, {"mode": "strict_exam_intent"},
        {"token_parameter": "max_completion_tokens"},
        {"reasoning_effort": "low"}, {"reasoning_effort": "medium"},
    ]:
        assert identity(**kwargs) != default
    assert "key-one" not in default


def test_adapter_does_not_close_a_caller_owned_client():
    closed = []
    client = SimpleNamespace(close=lambda: closed.append(True))
    with OpenAICompatibleJudge("key", "model", base_url="https://relay.example/v1", client=client):
        pass
    assert not closed


@pytest.fixture
def local_relay():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            requests.append((self.path, self.headers["Authorization"], None))
            self.send_json(200, {
                "object": "list", "data": [
                    {"id": "relay-gpt", "object": "model"},
                    {"id": "relay-gpt", "object": "model"},
                    {"id": "other-model", "object": "model"},
                ],
            })

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, self.headers["Authorization"], body))
            if "response_format" in body:
                self.send_json(400, {
                    "error": {"message": "response_format is unsupported", "type": "invalid_request_error"},
                })
            else:
                self.send_json(200, {
                    "id": "local-test", "object": "chat.completion", "created": 0,
                    "model": body["model"], "choices": [{
                        "index": 0, "message": {"role": "assistant", "content": '{"candidates":[]}'},
                        "finish_reason": "stop",
                    }],
                })

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_actual_sdk_sends_auth_and_correct_paths_to_local_relay(local_relay):
    pytest.importorskip("openai")
    base_url, requests = local_relay
    judge = OpenAICompatibleJudge(
        "local-test-key", "relay-gpt", base_url=base_url,
        token_parameter="max_completion_tokens", max_retries=0,
    )
    try:
        assert judge.list_remote_models() == ["other-model", "relay-gpt"]
        judge.test_connection()
    finally:
        judge.close()
    assert [item[0] for item in requests] == [
        "/api/v1/models", "/api/v1/chat/completions", "/api/v1/chat/completions",
    ]
    assert all(item[1] == "Bearer local-test-key" for item in requests)
    assert requests[-1][2]["max_completion_tokens"] == 16384
    assert requests[-1][2]["reasoning_effort"] == "high"
    assert "max_tokens" not in requests[-1][2]
