from __future__ import annotations

from classcue.paths import PROJECT_ROOT

import json
from pathlib import Path

import streamlit as st

from classcue.core import CandidateDecision, DEFAULT_TRIGGER_GROUPS, TranscriptSegment, build_candidate_window, find_trigger_matches, format_time
from classcue.db import Database
from classcue.judge import LocalJudge
from classcue.runtime import qwen_backend, whisper_backend


APP_ROOT = PROJECT_ROOT
DATA_DIR = APP_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "audio"
TRIGGER_CONFIG_PATH = DATA_DIR / "trigger_terms.json"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
db = Database(DATA_DIR / "classroom.db")

st.set_page_config(page_title="课堂考点定位", page_icon="🎧", layout="wide")
st.title("课堂回放 · 考点定位")
st.caption("本地转写、扩展关键词检索与可核验的考点记录")


def save_upload(uploaded_file) -> Path:
    target = UPLOAD_DIR / uploaded_file.name
    target.write_bytes(uploaded_file.getbuffer())
    return target


def render_segments(segments: list[TranscriptSegment], highlighted: set[int] | None = None) -> None:
    highlighted = highlighted or set()
    for segment in segments:
        marker = "🔎 " if segment.id in highlighted else ""
        st.markdown(f"`{format_time(segment.start_ms)}` **{marker}[{segment.id}]** {segment.text}")


def load_trigger_groups() -> dict:
    if TRIGGER_CONFIG_PATH.exists():
        try:
            return json.loads(TRIGGER_CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return DEFAULT_TRIGGER_GROUPS


@st.cache_resource(show_spinner=False)
def get_local_judge() -> LocalJudge:
    """Keep the loaded Qwen pipeline alive across Streamlit reruns."""
    return LocalJudge()


with st.sidebar:
    st.header("本机推理状态")
    whisper_status = whisper_backend()
    qwen_status = qwen_backend()
    st.caption(f"Whisper：{whisper_status.device.upper()} · {whisper_status.compute_type}")
    st.caption(whisper_status.detail)
    st.caption(f"Qwen：{qwen_status.device.upper()} · {qwen_status.compute_type}")
    st.caption(qwen_status.detail)
    st.divider()
    st.header("考试提示词库")
    trigger_text = st.text_area(
        "按强度编辑 JSON",
        value=json.dumps(load_trigger_groups(), ensure_ascii=False, indent=2),
        height=360,
        help="每个类别对应一组可扩展匹配的同义词。",
    )
    try:
        trigger_groups = json.loads(trigger_text)
        if st.button("保存词库"):
            TRIGGER_CONFIG_PATH.write_text(json.dumps(trigger_groups, ensure_ascii=False, indent=2), encoding="utf-8")
            st.success("已保存到本机 data/trigger_terms.json")
    except json.JSONDecodeError:
        trigger_groups = DEFAULT_TRIGGER_GROUPS
        st.error("词库 JSON 格式无效，当前使用默认词库。")


uploaded = st.file_uploader("上传课堂音频", type=["mp3", "m4a", "wav"])
if uploaded:
    if st.session_state.get("uploaded_name") != uploaded.name:
        st.session_state.uploaded_name = uploaded.name
        st.session_state.recording_id = None
        st.session_state.segments = []
        st.session_state.candidates = []
    audio_path = save_upload(uploaded)
    st.audio(uploaded)
    if st.button("开始本地转写", type="primary"):
        progress_bar = st.progress(0)
        status = st.empty()
        from classcue.transcriber import transcribe_audio

        try:
            segments, duration_ms = transcribe_audio(audio_path, progress=lambda value, message: (progress_bar.progress(value), status.info(message)))
            recording_id = db.create_recording(uploaded.name, str(audio_path), duration_ms)
            db.replace_transcript(recording_id, segments)
            st.session_state.recording_id = recording_id
            st.session_state.segments = segments
            status.success(f"转写完成：{len(segments)} 段，时长 {format_time(duration_ms)}")
        except Exception as exc:
            st.error(str(exc))

recording_id = st.session_state.get("recording_id")
segments: list[TranscriptSegment] = st.session_state.get("segments", [])
if recording_id and segments:
    st.divider()
    search_col, mode_col = st.columns([3, 1])
    with search_col:
        query = st.text_input("搜索考试提示词", value="会考", help="可搜索会考、重点、必考等词；扩展模式会使用内置同义词库。")
    with mode_col:
        expanded = st.radio("搜索范围", ["同义扩展", "精确"], horizontal=True) == "同义扩展"
    matches = find_trigger_matches(segments, query, expanded=expanded, groups=trigger_groups)
    st.write(f"找到 {len(matches)} 个命中")
    for match_index, match in enumerate(matches):
        segment = next(segment for segment in segments if segment.id == match.segment_id)
        with st.expander(f"{format_time(match.start_ms)} · {match.phrase} · {match.strength}", expanded=match_index == 0):
            st.audio(uploaded.getvalue(), format=uploaded.type, start_time=match.start_ms / 1000.0)
            st.write(segment.text)
            window = build_candidate_window(segments, match.segment_id)
            st.caption(f"候选上下文：{format_time(window[0].start_ms)} - {format_time(window[-1].end_ms)}，共 {len(window)} 段")
            render_segments(window, {match.segment_id})
            if st.button("生成考点候选", key=f"judge_{match_index}_{match.segment_id}"):
                with st.spinner("本地 Qwen 正在判断..."):
                    decision = get_local_judge().judge(window)
                if decision.is_exam_point:
                    candidate_id = db.create_candidate(recording_id, match.phrase, decision, window)
                    st.success(f"已生成候选 #{candidate_id}：{decision.knowledge_point}")
                else:
                    st.warning(decision.reason or "模型没有确认具体考点；请根据转录手动记录。")
            if st.button("手动创建考点候选", key=f"manual_{match_index}_{match.segment_id}"):
                manual = CandidateDecision(
                    is_exam_point=True,
                    trigger_sentence_ids=[match.segment_id],
                    start_sentence_id=window[0].id,
                    end_sentence_id=window[-1].id,
                    knowledge_point="待整理考点",
                    summary="请根据原始转录补充笔记。",
                    exam_signal_quote=segment.text,
                    reason="由用户从关键词命中片段创建",
                    confidence=1.0,
                )
                candidate_id = db.create_candidate(recording_id, match.phrase, manual, window)
                st.success(f"已创建待确认候选 #{candidate_id}")

    st.divider()
    left, right = st.columns([1.1, 1])
    with left:
        st.subheader("完整转录")
        render_segments(segments)
    with right:
        st.subheader("待确认考点")
        candidates = db.list_candidates(recording_id)
        if not candidates:
            st.info("从上方命中片段生成考点候选。")
        for candidate in candidates:
            with st.form(f"candidate_form_{candidate['id']}"):
                st.caption(f"候选 #{candidate['id']} · {format_time(candidate['start_ms'])} - {format_time(candidate['end_ms'])} · 置信度 {candidate['confidence']:.0%}")
                st.audio(uploaded.getvalue(), format=uploaded.type, start_time=candidate["start_ms"] / 1000.0)
                title = st.text_input("考点标题", value=candidate["title"], key=f"title_{candidate['id']}")
                note = st.text_area("我的笔记", value=candidate["summary"], key=f"note_{candidate['id']}")
                start_seconds = st.number_input("开始时间（秒）", min_value=0.0, value=candidate["start_ms"] / 1000.0, step=0.1, key=f"candidate_start_{candidate['id']}")
                end_seconds = st.number_input("结束时间（秒）", min_value=0.1, value=candidate["end_ms"] / 1000.0, step=0.1, key=f"candidate_end_{candidate['id']}")
                st.info(f"原始证据：{candidate['source_quote'] or '未提取到明确考试原话'}")
                if st.form_submit_button("确认保存"):
                    if end_seconds <= start_seconds:
                        st.error("结束时间必须晚于开始时间。")
                    else:
                        db.confirm_candidate(candidate["id"], title, note, round(start_seconds * 1000), round(end_seconds * 1000))
                        st.success("已保存，原始转录和证据已保留。")

        points = db.list_points(recording_id)
        if points:
            st.subheader("已保存考点")
            for point in points:
                with st.expander(f"{point['title']} · {format_time(point['start_ms'])}"):
                    with st.form(f"point_form_{point['id']}"):
                        point_title = st.text_input("考点标题", value=point["title"], key=f"point_title_{point['id']}")
                        point_note = st.text_area("我的笔记", value=point["note"], key=f"point_note_{point['id']}")
                        point_start = st.number_input("开始时间（秒）", min_value=0.0, value=point["start_ms"] / 1000.0, step=0.1, key=f"point_start_{point['id']}")
                        point_end = st.number_input("结束时间（秒）", min_value=0.1, value=point["end_ms"] / 1000.0, step=0.1, key=f"point_end_{point['id']}")
                        st.caption(f"不可修改的原始证据：{point['source_quote']}")
                        if st.form_submit_button("更新考点"):
                            try:
                                db.update_point(point["id"], point_title, point_note, round(point_start * 1000), round(point_end * 1000))
                                st.success("考点已更新，原始证据未变。")
                            except ValueError as exc:
                                st.error(str(exc))
