import pytest

from citation_matching import locate_quote, remap_candidate
from core import TranscriptSegment


def segments(*texts):
    return [TranscriptSegment(index, index * 1000, index * 1000 + 900, text)
            for index, text in enumerate(texts, 1)]


def point(quote, **changes):
    return {
        "level": "confirmed", "signal_type": "emphasis", "title": "Original object",
        "reason": "Original reason", "start_sentence_id": 1, "end_sentence_id": 1,
        "evidence_sentence_ids": [1], "evidence_quote": quote, **changes,
    }


def test_exact_quote_restores_all_ids_without_rewriting_the_quote():
    supplied = segments("这个算法的分析步骤很重要你要能够", "掌握这个完整的分析过程")
    original = point("这个算法的分析步骤很重要你要能够掌握这个完整的分析过程")
    fixed, audit = remap_candidate(original, supplied)
    assert fixed["evidence_sentence_ids"] == [1, 2]
    assert fixed["end_sentence_id"] == 2
    assert fixed["evidence_quote"] == original["evidence_quote"]
    assert audit["method"] == "exact" and audit["original_candidate"] == original


def test_oral_normalization_copies_actual_asr_and_preserves_model_wording_in_audit():
    supplied = segments("好吧这个算法的核心思路呃就在这个地方")
    original = point("这个算法的核心思路就在这个地方")
    fixed, audit = remap_candidate(original, supplied)
    assert fixed["evidence_quote"] == supplied[0].text
    assert audit["method"] == "oral_normalized"
    assert audit["original_candidate"]["evidence_quote"] == original["evidence_quote"]
    for key in ("title", "reason", "level", "signal_type"):
        assert fixed[key] == original[key]


def test_unique_ordered_fragments_locate_disjoint_literal_excerpts():
    a = "掌握这个算法的完整推导过程是非常重要的"
    b = "下面插入一段与此无关的背景说明供大家参考"
    c = "一定要能自己推导出它的时间复杂度表达式"
    fixed, audit = remap_candidate(point(a + c), segments(a, b, c))
    assert audit["method"] == "ordered_fragments"
    assert fixed["evidence_sentence_ids"] == [1, 3]
    assert fixed["evidence_quote"] == a + c


@pytest.mark.parametrize("quote,source", [
    ("这个题会考", "这个题不会考"),
    ("这个过程需要掌握", "这个过程不需要掌握"),
    ("复杂度是n减二", "复杂度是n减一"),
    ("复杂度是O(nlogn)", "复杂度是O(n)"),
    ("取值为05", "取值为0.5"),
    ("要求n大于等于m", "要求n大于m"),
])
def test_negation_numbers_formulas_and_conditions_are_not_fuzzily_replaced(quote, source):
    assert locate_quote(quote, segments(source))["status"] != "matched"


def test_omitted_negative_span_is_not_automatically_accepted():
    a = "这个算法的完整分析过程是非常重要的"
    c = "这一段内容一定要把具体的推导过程讲明白"
    outcome = locate_quote(a + c, segments(a, "不过这个证明不考也不要求掌握", c))
    assert outcome["status"] == "needs_review"


def test_multiple_exact_locations_are_ambiguous_even_if_model_provides_an_id():
    repeated = "这个公式很重要"
    fixed, audit = remap_candidate(point(repeated), segments(repeated, repeated))
    assert fixed is None and audit["status"] == "ambiguous"


def test_matching_never_crosses_an_unsupplied_subtitle_gap():
    a = TranscriptSegment(1, 1000, 1900, "这里有一个必须掌握的重要算法")
    b = TranscriptSegment(3, 3000, 3900, "这个完整的推导过程要记住")
    assert locate_quote(a.text + b.text, [a, b])["status"] == "needs_review"


def test_whitespace_is_ignored_but_original_character_offsets_are_retained():
    supplied = segments("重要公式： n log n 需要掌握")
    fixed, audit = remap_candidate(point("nlogn需要掌握"), supplied)
    assert fixed["evidence_quote"] == "nlogn需要掌握"
    assert audit["fragments"][0]["source_fragment"] == "n log n 需要掌握"


def test_short_scattered_character_subsequences_do_not_count_as_quotes():
    outcome = locate_quote("算法很重要要掌握", segments("算这道题", "法是这样", "很有意思",
                                                        "重要的是另一个", "要多看", "掌握了没"))
    assert outcome["status"] != "matched"
