from core import TranscriptSegment
from judge import LocalJudge


class RepairingJudge(LocalJudge):
    def __init__(self):
        super().__init__("unused")
        self.outputs = [
            '{"candidates": [{"level": "possible" "start_sentence_id": 1}]}',
            '{"candidates": [{"level": "possible", "start_sentence_id": 1, "end_sentence_id": 1, "title": "修复结果"}]}',
        ]

    def _generate(self, prompt, max_new_tokens):
        return self.outputs.pop(0)


def test_semantic_judge_retries_malformed_json():
    selection = RepairingJudge().judge_semantic_chunk((TranscriptSegment(1, 0, 1000, "内容"),))
    assert selection.candidates[0].title == "修复结果"
