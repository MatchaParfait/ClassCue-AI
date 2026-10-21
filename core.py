"""Core domain logic for the local classroom knowledge-point demo."""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator


DEFAULT_TRIGGER_GROUPS: dict[str, dict[str, list[str]]] = {
    "strong": {
        "考试明确": [
            "会考", "汇考", "考试会出", "考试会考到", "考试要考", "考试考", "卷子上会有", "卷子会有",
            "考卷会有", "试卷上有", "必考", "必靠", "必须考", "一定考", "肯定考", "必定考", "高频考点",
            "高频必考", "重点考", "常考点", "出题点", "考查点", "真题出现过", "这题会出", "这一类会出",
            "考试重点", "期中会考", "期末会考", "中考会考", "高考会考", "考研会考", "考证会考", "考核会考",
            "考试必出", "肯定会出", "大概率会出", "可能会出", "爱考", "喜欢考", "考得很多", "反复考",
            "常出题", "出过题", "出题概率高", "命题点", "命题角度", "考察内容", "考察重点", "考试范围",
            "划重点", "划考点", "这里考", "这里要考", "这个要考", "这个必考", "这是必考", "这是考点",
        ],
    },
    "medium": {
        "掌握强调": [
            "重点", "重点内容", "重点考点", "重中之重", "核心", "核心内容", "关键", "关键点", "关键步骤",
            "要记住", "牢记", "一定要会", "一定得会", "必须掌握", "务必记住", "需要掌握", "要求掌握",
            "必须会", "都要会", "这一点要会", "这一步要会", "要弄懂", "要吃透", "熟练掌握", "学会这个",
            "一定要写", "必须写", "一定要记", "特别重要", "非常重要", "很重要",
            "相当重要", "比较重要", "尤其重要", "最重要", "首要", "关键核心", "核心考点", "关键考点",
            "核心方法", "关键方法", "核心公式", "关键公式", "核心结论", "关键结论", "基础中的基础", "重中之重",
            "务必掌握", "务必要会", "必须弄懂", "必须理解", "一定理解", "一定掌握", "需要理解", "重点掌握",
            "重点理解", "重点记忆", "重点背诵", "必须背", "需要背", "记牢", "牢牢记住", "烂熟于心",
        ],
        "得分易错": [
            "常考", "经常考", "高频", "容易出错", "容易错", "最容易错", "容易丢分", "容易失分", "容易扣分",
            "经常问", "常见错误", "错误点", "易错点", "陷阱", "坑", "注意这里", "注意这个", "特别注意",
            "千万注意", "不要弄错", "不能错", "这步不能错", "别搞错", "得分点", "拿分", "步骤分", "过程分",
            "失分点", "扣分点", "送分题", "送分", "保分", "保底分", "拉分", "拉开差距", "容易混淆",
            "最容易混", "常常出错", "特别容易错", "千万别错", "一定别错", "务必注意", "切记", "要小心",
            "容易遗漏", "容易漏", "容易忘", "别漏", "不要漏", "漏写", "漏解", "漏掉条件", "符号别错",
            "单位别忘", "格式要对", "书写规范", "答题规范", "审题", "看清条件", "隐含条件", "陷阱题",
        ],
        "题型提示": [
            "典型题", "经典题", "典型例题", "经典例题", "重点题型", "常见题型", "这一类题", "这种题",
            "选择题", "填空题", "判断题", "计算题", "解答题", "大题", "证明题", "实验题", "综合题",
            "压轴题", "基础题", "中档题", "拔高题", "易考题", "高频题", "必做题", "例题", "例题很重要",
            "典型方法", "典型套路", "通法", "通用方法", "模板", "答题模板", "解题模板", "标准写法",
            "固定套路", "常用结论", "常用公式", "必用公式", "这类问题", "这一型", "这种模型", "常见模型",
        ],
    },
    "weak": {
        "学习指令": [
            "建议背下来", "背下来", "大家要会", "同学们要会", "记下来", "写下来", "记笔记", "做笔记",
            "圈起来", "划下来", "标出来", "不要忘", "别忘了", "这里要注意", "这个结论", "这个公式",
            "这个定理", "复习一下", "考前看看", "课后复习", "回去看看", "留意一下", "好好看看",
            "课后要看", "回去要看", "回去复习", "复习重点", "整理一下", "整理笔记", "抄下来", "写在本上",
            "标个记号", "画个重点", "打个星号", "记在脑子里", "反复看", "多看几遍", "多练", "多做几遍",
            "务必", "一定", "必须", "重要", "注意", "留心", "听清楚", "听好了", "看好了", "大家听",
        ],
        "结论步骤": [
            "所以结论是", "结论就是", "最终结论", "最后结果", "记住结论", "得到结论", "由此可见", "因此",
            "第一步", "第二步", "第三步", "关键在于", "先要", "然后要", "最后要", "标准步骤", "解题步骤",
            "证明思路", "解题思路", "方法总结", "规律总结", "归纳一下", "总结一下", "这里小结", "小结一下",
        ],
    },
}

EXAM_BOUNDARY_RE = re.compile(
    r"^(好[，,。]?|接下来|下面|然后|再看|我们来看看|这一题讲完|今天就讲到|另外)"
)


@dataclass(frozen=True)
class TranscriptSegment:
    id: int
    start_ms: int
    end_ms: int
    text: str
    words: list[dict[str, Any]] | None = None

    def as_prompt_line(self) -> str:
        return f"[{self.id}] {format_time(self.start_ms)}-{format_time(self.end_ms)} {self.text}"


@dataclass(frozen=True)
class TriggerMatch:
    segment_id: int
    phrase: str
    strength: str
    category: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class CandidateWindow:
    segments: tuple[TranscriptSegment, ...]
    triggers: tuple[TriggerMatch, ...]

    @property
    def start_ms(self) -> int:
        return self.segments[0].start_ms

    @property
    def end_ms(self) -> int:
        return self.segments[-1].end_ms


class CandidateDecision(BaseModel):
    is_exam_point: bool = False
    trigger_sentence_ids: list[int] = Field(default_factory=list)
    start_sentence_id: int | None = None
    end_sentence_id: int | None = None
    knowledge_point: str = ""
    summary: str = ""
    exam_signal_quote: str = ""
    reason: str = ""
    confidence: float = 0.0

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))


class SemanticCandidate(BaseModel):
    level: Literal["confirmed", "possible"] = "possible"
    start_sentence_id: int
    end_sentence_id: int
    title: str = "疑似重点"
    reason: str = ""
    evidence_quote: str = ""
    confidence: float = 0.5

    @field_validator("confidence")
    @classmethod
    def clamp_confidence(cls, value: float) -> float:
        return max(0.0, min(1.0, float(value)))


class SemanticSelection(BaseModel):
    candidates: list[SemanticCandidate] = Field(default_factory=list)


class SemanticResponseError(ValueError):
    """A semantic judge ran but did not return a usable structured response."""


def format_time(milliseconds: int) -> str:
    total_seconds = max(0, milliseconds) // 1000
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def format_vtt_time(milliseconds: int) -> str:
    """Format a non-negative timestamp for a WebVTT subtitle cue."""
    total_milliseconds = max(0, milliseconds)
    hours, remainder = divmod(total_milliseconds, 60 * 60 * 1000)
    minutes, remainder = divmod(remainder, 60 * 1000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def transcript_to_vtt(segments: Iterable[TranscriptSegment]) -> str:
    """Create an in-memory WebVTT track without modifying the source video."""
    cues = ["WEBVTT", ""]
    for segment in segments:
        text = " ".join(segment.text.replace("-->", "→").split())
        if not text or segment.end_ms <= segment.start_ms:
            continue
        cues.extend(
            [
                str(segment.id),
                f"{format_vtt_time(segment.start_ms)} --> {format_vtt_time(segment.end_ms)}",
                text,
                "",
            ]
        )
    return "\n".join(cues)


def flatten_trigger_groups(groups: dict[str, dict[str, list[str]]] | None = None) -> list[tuple[str, str, str, str]]:
    groups = groups or DEFAULT_TRIGGER_GROUPS
    flattened: list[tuple[str, str, str, str]] = []
    for strength, categories in groups.items():
        for category, phrases in categories.items():
            for phrase in phrases:
                normalized = normalize_text(phrase)
                if normalized:
                    flattened.append((normalized, strength, category, phrase))
    return flattened


def normalize_text(text: str) -> str:
    """Normalize common ASR punctuation/spacing without changing Chinese text."""
    return re.sub(r"[\s，,。.!！？?；;：:、]+", "", text or "").lower()


def validate_trigger_groups(value: Any) -> dict[str, dict[str, list[str]]]:
    """Validate user-edited trigger groups and drop duplicate/empty phrases."""
    if not isinstance(value, dict):
        raise ValueError("词库必须是 JSON 对象")
    validated: dict[str, dict[str, list[str]]] = {}
    for strength, categories in value.items():
        if not isinstance(strength, str) or not strength.strip() or not isinstance(categories, dict):
            raise ValueError("每个信号强度必须对应一个分类对象")
        cleaned_categories: dict[str, list[str]] = {}
        for category, phrases in categories.items():
            if not isinstance(category, str) or not category.strip() or not isinstance(phrases, list):
                raise ValueError("每个分类必须包含短语数组")
            seen: set[str] = set()
            cleaned_phrases: list[str] = []
            for phrase in phrases:
                if not isinstance(phrase, str):
                    raise ValueError("提示词必须是字符串")
                display = phrase.strip()
                normalized = normalize_text(display)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    cleaned_phrases.append(display)
            if cleaned_phrases:
                cleaned_categories[category.strip()] = cleaned_phrases
        if cleaned_categories:
            validated[strength.strip()] = cleaned_categories
    if not validated:
        raise ValueError("词库至少需要一个非空分类和提示词")
    return validated


def find_trigger_matches(
    segments: Iterable[TranscriptSegment],
    query: str,
    expanded: bool = True,
    groups: dict[str, dict[str, list[str]]] | None = None,
) -> list[TriggerMatch]:
    query_normalized = normalize_text(query)
    if not query_normalized:
        return []
    all_phrase_rows = flatten_trigger_groups(groups)
    if expanded:
        # Expand only the semantic group containing the query, rather than turning a
        # search for "重点" into a match for every unrelated signal such as "会考".
        query_categories = {
            category
            for phrase, _strength, category, _display_phrase in all_phrase_rows
            if phrase == query_normalized or normalize_text(category) == query_normalized
        }
        phrase_rows = [row for row in all_phrase_rows if row[2] in query_categories]
        if not phrase_rows:
            phrase_rows = [(query_normalized, "query", query, query)]
    else:
        phrase_rows = [(query_normalized, "query", query, query)]
    return _find_phrase_rows(segments, phrase_rows)


def find_all_trigger_matches(
    segments: Iterable[TranscriptSegment],
    groups: dict[str, dict[str, list[str]]] | None = None,
) -> list[TriggerMatch]:
    """High-recall search across every configured exam/learning signal."""
    return _find_phrase_rows(segments, flatten_trigger_groups(groups))


_FORMULA_OPERATOR_TERMS = (
    "加",
    "减",
    "乘",
    "除",
    "等于",
    "平方",
    "立方",
    "次方",
    "根号",
    "分母",
    "分子",
)
_FORMULA_CONTEXT_TERMS = (
    "公式",
    "定理",
    "定义",
    "结论",
    "性质",
    "方法",
    "步骤",
    "证明",
    "推导",
    "展开",
    "化简",
    "解方程",
    "求导",
    "积分",
    "条件",
    "注意",
    "关键",
    "例题",
    "题型",
    "模型",
    "规律",
    "为什么",
    "原因",
    "因此",
    "所以",
    "得到",
    "表示",
)
_ASR_FILLER_RE = re.compile(r"^(?:嗯|呃|啊|这个|那个|然后|就是|对|好){2,}$")
_FORMULA_VOCAB_RE = re.compile(
    r"等于|平方|立方|次方|根号|分母|分子|分之|括号|加上|减去|乘以|除以"
    r"|加|减|乘|除|正|负|的|次|项"
    r"|零|一|二|三|四|五|六|七|八|九|十|百|千|点"
    r"|这个|那个|然后|就是|是|再|嗯|呃|啊"
)
_FORMULA_SYMBOL_RE = re.compile(r"[+\-*/=^()]")


def is_low_information_analysis_segment(
    segment: TranscriptSegment,
    groups: dict[str, dict[str, list[str]]] | None = None,
) -> bool:
    """Identify isolated formula-like or filler ASR text before an AI request.

    This is deliberately conservative. Formula-shaped text with an exam signal
    or an explanatory/teaching term is kept because it may itself be evidence.
    """
    normalized = normalize_text(segment.text)
    if not normalized:
        return True
    if _ASR_FILLER_RE.fullmatch(normalized):
        return True
    if find_all_trigger_matches([segment], groups):
        return False
    if any(term in normalized for term in _FORMULA_CONTEXT_TERMS):
        return False
    operator_count = sum(normalized.count(term) for term in _FORMULA_OPERATOR_TERMS)
    operator_count += len(_FORMULA_SYMBOL_RE.findall(normalized))
    has_formula_token = bool(re.search(r"[a-z]|\d|[零一二三四五六七八九十百千万]", normalized))
    # Unknown words keep the whole sentence; a Latin word plus "加" is not math.
    remainder = re.sub(r"(?<![a-z])[a-z](?![a-z])|\d+", "", normalized)
    remainder = _FORMULA_VOCAB_RE.sub("", remainder)
    remainder = re.sub(r"[+\-*/=^()（）\[\]{}]", "", remainder)
    return has_formula_token and operator_count >= 1 and not remainder


def filter_analysis_segments(
    segments: list[TranscriptSegment],
    groups: dict[str, dict[str, list[str]]] | None = None,
    context_seconds: int = 20,
) -> list[TranscriptSegment]:
    """Remove isolated low-information ASR spans while preserving nearby context.

    The original transcript remains untouched. Kept neighboring segments are
    restored around meaningful text so a formula explanation is not detached
    from the sentence that gives it educational or exam context.
    """
    if not segments:
        return []
    noise_ids = {
        segment.id
        for segment in segments
        if is_low_information_analysis_segment(segment, groups)
    }
    if not noise_ids:
        return list(segments)
    meaningful = [segment for segment in segments if segment.id not in noise_ids]
    if not meaningful:
        return []
    context_ms = max(0, context_seconds) * 1000
    kept_ids = {segment.id for segment in meaningful}
    signal_ids = {match.segment_id for match in find_all_trigger_matches(segments)}
    if groups is not None:
        signal_ids.update(match.segment_id for match in find_all_trigger_matches(segments, groups))
    anchors = [
        segment
        for segment in meaningful
        if segment.id in signal_ids
        or any(term in normalize_text(segment.text) for term in _FORMULA_CONTEXT_TERMS)
    ]
    intervals = sorted(
        (segment.start_ms - context_ms, segment.end_ms + context_ms) for segment in anchors
    )
    starts = [start for start, _end in intervals]
    furthest_ends: list[int] = []
    for _start, end in intervals:
        furthest_ends.append(max(end, furthest_ends[-1]) if furthest_ends else end)
    for noisy in segments:
        if noisy.id not in noise_ids:
            continue
        if not noisy.text.strip():
            continue
        index = bisect_right(starts, noisy.end_ms) - 1
        if index >= 0 and furthest_ends[index] >= noisy.start_ms:
            kept_ids.add(noisy.id)
    return [segment for segment in segments if segment.id in kept_ids]


def _find_phrase_rows(
    segments: Iterable[TranscriptSegment],
    phrase_rows: list[tuple[str, str, str, str]],
) -> list[TriggerMatch]:
    rows: list[TriggerMatch] = []
    for segment in segments:
        text = normalize_text(segment.text)
        segment_matches: list[tuple[str, str, str, str]] = []
        for phrase, strength, category, display_phrase in phrase_rows:
            if phrase in text:
                # Prefer a specific phrase such as "高频考点" to its overlapping
                # generic alias "考点", while retaining genuinely separate signals.
                if any(phrase in existing[0] for existing in segment_matches):
                    continue
                segment_matches = [existing for existing in segment_matches if existing[0] not in phrase]
                segment_matches.append((phrase, strength, category, display_phrase))
        rows.extend(
            TriggerMatch(segment.id, display_phrase, strength, category, segment.start_ms, segment.end_ms)
            for _phrase, strength, category, display_phrase in segment_matches
        )
    strength_rank = {"strong": 0, "medium": 1, "weak": 2, "query": 0}
    return sorted(rows, key=lambda item: (strength_rank.get(item.strength, 3), item.start_ms))


def build_candidate_window(
    segments: list[TranscriptSegment],
    anchor_id: int,
    max_seconds: int = 90,
    before_seconds: int = 45,
    after_seconds: int = 45,
) -> list[TranscriptSegment]:
    """Expand around a trigger, stopping at likely topic boundaries and max duration."""
    by_id = {segment.id: index for index, segment in enumerate(segments)}
    if anchor_id not in by_id:
        return []
    anchor_index = by_id[anchor_id]
    chosen = [segments[anchor_index]]
    before_limit = segments[anchor_index].start_ms - before_seconds * 1000
    after_limit = segments[anchor_index].end_ms + after_seconds * 1000

    index = anchor_index - 1
    while index >= 0 and segments[index].start_ms >= before_limit:
        current = segments[index]
        next_segment = segments[index + 1]
        if current.end_ms < next_segment.start_ms - 2500 or EXAM_BOUNDARY_RE.match(current.text.strip()):
            break
        chosen.insert(0, current)
        index -= 1

    index = anchor_index + 1
    while index < len(segments) and segments[index].end_ms <= after_limit:
        previous = segments[index - 1]
        current = segments[index]
        if current.start_ms > previous.end_ms + 2500 or EXAM_BOUNDARY_RE.match(current.text.strip()):
            break
        if current.end_ms - chosen[0].start_ms > max_seconds * 1000:
            break
        chosen.append(current)
        index += 1

    return chosen


def build_semantic_chunks(
    segments: list[TranscriptSegment],
    max_seconds: int = 300,
    overlap_seconds: int = 30,
) -> list[tuple[TranscriptSegment, ...]]:
    """Cover an entire transcript with bounded, overlapping timestamped chunks."""
    if not segments:
        return []
    chunks: list[tuple[TranscriptSegment, ...]] = []
    start_index = 0
    max_ms = max_seconds * 1000
    overlap_ms = overlap_seconds * 1000
    while start_index < len(segments):
        end_index = start_index
        while end_index + 1 < len(segments) and segments[end_index + 1].end_ms - segments[start_index].start_ms <= max_ms:
            end_index += 1
        chunks.append(tuple(segments[start_index : end_index + 1]))
        if end_index == len(segments) - 1:
            break
        next_start = end_index + 1
        overlap_from = segments[end_index].end_ms - overlap_ms
        for index in range(start_index + 1, end_index + 1):
            if segments[index].end_ms >= overlap_from:
                next_start = index
                break
        start_index = next_start
    return chunks


def merge_candidate_windows(
    segments: list[TranscriptSegment],
    matches: list[TriggerMatch],
    max_merged_seconds: int = 180,
) -> list[CandidateWindow]:
    """Merge overlapping high-recall windows while retaining every source trigger."""
    segment_positions = {segment.id: index for index, segment in enumerate(segments)}
    raw_windows: list[tuple[int, int, TriggerMatch]] = []
    for match in matches:
        window = build_candidate_window(segments, match.segment_id)
        if not window:
            continue
        raw_windows.append((segment_positions[window[0].id], segment_positions[window[-1].id], match))
    raw_windows.sort(key=lambda item: (segments[item[0]].start_ms, segments[item[1]].end_ms))

    merged: list[CandidateWindow] = []
    current_start: int | None = None
    current_end: int | None = None
    current_triggers: list[TriggerMatch] = []
    for start, end, match in raw_windows:
        if current_start is None:
            current_start, current_end, current_triggers = start, end, [match]
            continue
        would_end = max(current_end, end)
        close_enough = segments[start].start_ms <= segments[current_end].end_ms + 5000
        within_limit = segments[would_end].end_ms - segments[current_start].start_ms <= max_merged_seconds * 1000
        if close_enough and within_limit:
            current_end = would_end
            current_triggers.append(match)
            continue
        merged.append(CandidateWindow(tuple(segments[current_start : current_end + 1]), tuple(current_triggers)))
        current_start, current_end, current_triggers = start, end, [match]
    if current_start is not None and current_end is not None:
        merged.append(CandidateWindow(tuple(segments[current_start : current_end + 1]), tuple(current_triggers)))
    return merged


def validate_decision(decision: CandidateDecision, window: list[TranscriptSegment]) -> CandidateDecision:
    """Reject unsafe model ranges and coerce them to a valid continuous window."""
    valid_ids = [segment.id for segment in window]
    if not decision.is_exam_point:
        return decision
    if not valid_ids or decision.start_sentence_id not in valid_ids or decision.end_sentence_id not in valid_ids:
        return decision.model_copy(update={"is_exam_point": False, "confidence": 0.0, "reason": "模型选择了候选区间之外的句子"})
    start_index = valid_ids.index(decision.start_sentence_id)
    end_index = valid_ids.index(decision.end_sentence_id)
    if start_index > end_index:
        return decision.model_copy(update={"is_exam_point": False, "confidence": 0.0, "reason": "模型返回了反向区间"})
    selected_ids = set(valid_ids[start_index : end_index + 1])
    if decision.trigger_sentence_ids and not set(decision.trigger_sentence_ids).issubset(set(valid_ids)):
        return decision.model_copy(update={"is_exam_point": False, "confidence": 0.0, "reason": "模型返回了不存在的触发句"})
    return decision.model_copy(update={"trigger_sentence_ids": decision.trigger_sentence_ids or [valid_ids[valid_ids.index(decision.start_sentence_id)]]})


def parse_model_json(raw: str) -> CandidateDecision:
    """Parse JSON even when a local instruct model wraps it in a code fence."""
    cleaned = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    try:
        return CandidateDecision.model_validate(json.loads(cleaned[start : end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"模型 JSON 无法校验: {exc}") from exc


def parse_semantic_json(raw: str) -> SemanticSelection:
    cleaned = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON 对象")
    try:
        return SemanticSelection.model_validate(json.loads(cleaned[start : end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"模型 JSON 无法校验: {exc}") from exc


def decision_prompt(window: list[TranscriptSegment]) -> str:
    lines = "\n".join(segment.as_prompt_line() for segment in window)
    return f"""你是课堂考点标注器。只依据下方带编号的教师转录内容，判断是否明确提示某个具体知识点会被考试或考核。\n\n要求：\n- 只选择列表中存在的连续句子，不能编造时间或句子。\n- “重点”但没有具体知识对象时，is_exam_point=false。\n- 保留触发考试信号的原话。\n- 只输出 JSON，不要 Markdown。\n\n输出字段：is_exam_point, trigger_sentence_ids, start_sentence_id, end_sentence_id, knowledge_point, summary, exam_signal_quote, reason, confidence（0 到 1）。\n\n转录：\n{lines}"""


def analysis_prompt_lines(chunk: tuple[TranscriptSegment, ...]) -> str:
    lines: list[str] = []
    previous: TranscriptSegment | None = None
    for segment in chunk:
        if previous is not None and segment.id > previous.id + 1:
            lines.append(
                f"（{format_time(previous.end_ms)}-{format_time(segment.start_ms)} "
                "之间部分字幕未提供，不得推断缺失内容或跨缺口拼接为同一考点。）"
            )
        lines.append(segment.as_prompt_line())
        previous = segment
    return "\n".join(lines)


def semantic_selection_prompt(chunk: tuple[TranscriptSegment, ...]) -> str:
    lines = analysis_prompt_lines(chunk)
    return f"""你是课堂视频考点筛选器。请检查下面这一段带时间戳的课堂转录，找出所有可能需要学生复习的具体知识点或关键步骤。\n\n目标是高召回：宁可标记疑似重点，也不要漏掉教师在强调掌握、理解、易错、得分、步骤、结论、典型题或考试相关性的内容。不能只因为没有出现“必考”而忽略一个候选。\n\n规则：\n- 每个候选必须对应具体知识对象或步骤；纯寒暄、过渡、重复口头语不能标记。\n- 有明确考试提示时 level 使用 confirmed；其他可能有学习价值的内容使用 possible。\n- 允许返回空 candidates；不要为了凑结果而编造考点。\n- 只能选择下方实际提供、按时间顺序排列的字幕段编号；不能编造时间、句子或教师未说过的结论。\n- 返回一个 JSON 对象，不要 Markdown。\n\nJSON 格式：\n{{\n  "candidates": [\n    {{\n      "level": "confirmed 或 possible",\n      "start_sentence_id": 1,\n      "end_sentence_id": 3,\n      "title": "简短考点标题",\n      "reason": "为何值得复习",\n      "evidence_quote": "教师原话或最关键的转录句",\n      "confidence": 0.0\n    }}\n  ]\n}}\n\n转录：\n{lines}"""


def strict_exam_intent_prompt(chunk: tuple[TranscriptSegment, ...]) -> str:
    """Require evidence that the teacher deliberately signals exam relevance."""
    lines = analysis_prompt_lines(chunk)
    return f"""你是期末考试考点的严格审核器。请只依据下方教师课堂转录，找出老师明确表示或无歧义暗示“该具体内容会在期末考试、试卷、考核中被考查”的内容。

这是低误报模式，目标是少而准确。普通讲解、定义说明、公式推导、例题演示、步骤讲授、常规提醒，即使本身有学习价值，也一律不要标记，除非老师同时明确表达了考试意向或刻意强调必须掌握且语境清楚地指向考查。

严格规则：
- 只接受老师原话中存在的明确证据，例如“期末会考”“考试重点”“必考”“卷子会出”“一定要会”“这里容易丢分”等，或同等明确的考查暗示。
- 不能根据学科常识、知识难度、AI 推断或“这内容很重要”自行推定考试意向。
- 每个候选必须是具体知识对象或步骤，且 evidence_quote 必须保留体现考试意向的教师原话。
- 只允许返回 level 为 confirmed 的候选；证据不足时返回空 candidates，绝不能返回 possible。
- 只能选择下方实际提供、按时间顺序排列的字幕段编号，不能编造时间、句子或教师未说过的结论。
- 返回一个 JSON 对象，不要 Markdown。

JSON 格式：
{{
  "candidates": [
    {{
      "level": "confirmed",
      "start_sentence_id": 1,
      "end_sentence_id": 3,
      "title": "简短考点标题",
      "reason": "教师如何明确表达考查意向",
      "evidence_quote": "包含考试意向的教师原话",
      "confidence": 0.0
    }}
  ]
}}

转录：
{lines}"""


def semantic_json_repair_prompt(raw_output: str, strict_mode: bool = False) -> str:
    """Use a short second-pass request to repair a malformed semantic response."""
    level = "confirmed" if strict_mode else "confirmed 或 possible"
    return f"""只修复下方内容为合法 JSON；不要添加解释、Markdown、时间或教师未说过的信息。信息不足时返回 {{"candidates":[]}}。

JSON 格式：{{"candidates":[{{"level":"{level}","start_sentence_id":1,"end_sentence_id":1,"title":"...","reason":"...","evidence_quote":"...","confidence":0.0}}]}}

待修复内容：
{raw_output[:8000]}"""


def json_repair_prompt(raw_output: str) -> str:
    return f"""下面的模型输出本应是 JSON，但格式无效。请只返回修复后的合法 JSON，不得添加解释、Markdown 或新内容。\n\n必须保持格式：\n{{"candidates":[{{"level":"possible","start_sentence_id":1,"end_sentence_id":2,"title":"...","reason":"...","evidence_quote":"...","confidence":0.5}}]}}\n\n待修复输出：\n{raw_output}"""


def dataclass_to_dict(value: Any) -> dict[str, Any]:
    return asdict(value)
