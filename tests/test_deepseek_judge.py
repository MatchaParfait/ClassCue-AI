from types import SimpleNamespace

from classcue.core import TranscriptSegment
from classcue.deepseek_judge import DeepSeekJudge


class FakeCompletions:
    def __init__(self, content: str | list[str]) -> None:
        self.contents = [content] if isinstance(content, str) else content
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.contents) - 1)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.contents[index]))])


class FakeClient:
    def __init__(self, content: str) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(content))


def test_deepseek_judge_parses_json_and_discards_unknown_sentence_ids():
    client = FakeClient(
        '''{"candidates":[
            {"level":"confirmed","start_sentence_id":10,"end_sentence_id":11,"title":"公式","reason":"强调掌握","evidence_quote":"这是重点","confidence":0.9},
            {"level":"possible","start_sentence_id":99,"end_sentence_id":99,"title":"不存在","reason":"","evidence_quote":"","confidence":0.2}
        ]}'''
    )
    judge = DeepSeekJudge("test-key", "deepseek-v4-flash", client=client)

    selection = judge.judge_semantic_chunk(
        (
            TranscriptSegment(10, 0, 1000, "公式要掌握"),
            TranscriptSegment(11, 1000, 2000, "这是重点"),
        )
    )

    assert [candidate.title for candidate in selection.candidates] == ["公式"]
    request = client.chat.completions.calls[0]
    assert request["model"] == "deepseek-v4-flash"
    assert request["response_format"] == {"type": "json_object"}


def test_strict_exam_mode_discards_possible_candidates_and_uses_strict_prompt():
    client = FakeClient(
        '''{"candidates":[
            {"level":"possible","start_sentence_id":10,"end_sentence_id":10,"title":"普通讲解","reason":"","evidence_quote":"","confidence":0.8},
            {"level":"confirmed","start_sentence_id":11,"end_sentence_id":11,"title":"期末重点","reason":"老师说明期末会考","evidence_quote":"期末会考这个","confidence":0.9}
        ]}'''
    )
    judge = DeepSeekJudge("test-key", client=client, mode="strict_exam_intent")
    selection = judge.judge_semantic_chunk((TranscriptSegment(10, 0, 1000, "普通讲解"), TranscriptSegment(11, 1000, 2000, "期末会考这个")))

    assert [candidate.title for candidate in selection.candidates] == ["期末重点"]
    assert "期末考试考点的严格审核器" in client.chat.completions.calls[0]["messages"][1]["content"]


def test_deepseek_judge_repairs_one_malformed_json_response():
    client = FakeClient(
        [
            '{"candidates":[{"level":"confirmed" "start_sentence_id":1}]}',
            '{"candidates":[{"level":"confirmed","start_sentence_id":1,"end_sentence_id":1,"title":"考点","reason":"老师强调","evidence_quote":"会考","confidence":0.9}]}',
        ]
    )
    judge = DeepSeekJudge("test-key", client=client)

    selection = judge.judge_semantic_chunk((TranscriptSegment(1, 0, 1000, "这个会考。"),))

    assert [candidate.title for candidate in selection.candidates] == ["考点"]
    assert len(client.chat.completions.calls) == 2
    assert "只修复下方内容为合法 JSON" in client.chat.completions.calls[1]["messages"][1]["content"]


def test_deepseek_judge_retries_one_empty_response():
    client = FakeClient(
        [
            "",
            '{"candidates":[{"level":"confirmed","start_sentence_id":1,"end_sentence_id":1,"title":"考点","reason":"","evidence_quote":"会考","confidence":0.9}]}',
        ]
    )
    judge = DeepSeekJudge("test-key", client=client)

    selection = judge.judge_semantic_chunk((TranscriptSegment(1, 0, 1000, "这个会考。"),))

    assert [candidate.title for candidate in selection.candidates] == ["考点"]
    assert len(client.chat.completions.calls) == 2
