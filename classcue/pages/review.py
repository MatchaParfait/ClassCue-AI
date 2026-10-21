"""Dedicated full-width study page for one processed classroom video."""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from classcue.asr_models import get_model
from classcue.asr_ui import render_asr_settings, render_retranscription
from classcue.ai_provider import create_judge
from classcue.ai_ui import render_ai_settings
from classcue.core import DEFAULT_TRIGGER_GROUPS, find_all_trigger_matches, format_time, transcript_to_vtt, validate_trigger_groups
from classcue.app_storage import TRIGGER_LIBRARY_PATH, active_storage_paths
from classcue.openai_judge import OpenAICompatibleJudge
from classcue.runtime import whisper_backend
from classcue.video_pipeline import process_saved_analysis_once
from classcue.video_store import VideoStore


LIBRARY_ROOT, DATABASE_PATH, _AUDIO_DIR = active_storage_paths()
store = VideoStore(DATABASE_PATH)

st.set_page_config(page_title="重点复习 · 课堂视频", page_icon="Video", layout="wide", initial_sidebar_state="collapsed")
st.markdown(
    """
    <style>
    [data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"] { display: none !important; }
    [data-testid="stMainBlockContainer"] {
        max-width: none;
        padding-top: 5rem;
        padding-left: 2rem;
        padding-right: 2rem;
    }
    [data-testid="stElementContainer"][data-stale="true"]:has([data-testid="stVideo"]),
    [data-testid="stElementContainer"][data-stale="true"]:has([data-testid="stTabs"]) {
        opacity: 1 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


def default_trigger_library_text() -> str:
    return json.dumps(DEFAULT_TRIGGER_GROUPS, ensure_ascii=False, indent=2)


def load_trigger_library_text() -> str:
    if not TRIGGER_LIBRARY_PATH.is_file():
        return default_trigger_library_text()
    try:
        return json.dumps(validate_trigger_groups(json.loads(TRIGGER_LIBRARY_PATH.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2)
    except (OSError, ValueError, json.JSONDecodeError):
        return default_trigger_library_text()


if "video_trigger_library_text" not in st.session_state:
    st.session_state.video_trigger_library_text = load_trigger_library_text()


try:
    trigger_groups = validate_trigger_groups(json.loads(st.session_state.video_trigger_library_text))
except (ValueError, json.JSONDecodeError):
    trigger_groups = None


def get_api_judge(mode: str | None = None) -> OpenAICompatibleJudge:
    return create_judge(ai_profile, api_key, ai_provider, mode=mode or ai_judgment_mode)


def remember_deleted_marker(marker: object) -> None:
    st.session_state.last_deleted_marker = {
        "database": str(DATABASE_PATH),
        "video_id": int(marker["video_id"]),
        "marker_id": int(marker["id"]),
        "previous_status": marker["review_status"],
    }


def restore_last_deleted_marker(video_id: int) -> bool:
    marker = st.session_state.get("last_deleted_marker")
    if not marker or marker["database"] != str(DATABASE_PATH) or marker["video_id"] != video_id:
        return False
    store.set_marker_review_status(marker["marker_id"], marker["previous_status"])
    st.session_state.pop("last_deleted_marker", None)
    return True


def enable_undo_shortcut() -> None:
    components.html(
        """
        <script>
        try {
          const page = window.parent.document;
          if (!page.body.dataset.markerUndoShortcut) {
            page.body.dataset.markerUndoShortcut = "true";
            page.addEventListener("keydown", (event) => {
              if (event.ctrlKey && !event.altKey && event.key.toLowerCase() === "z") {
                const focused = event.target;
                if (focused.matches("input, textarea, select") || focused.isContentEditable) return;
                const target = [...page.querySelectorAll("button")]
                  .find((button) => button.innerText.trim() === "撤回最近移除");
                if (target && !target.disabled) {
                  event.preventDefault();
                  target.click();
                }
              }
            });
          }
        } catch (_) {}
        </script>
        """,
        height=0,
    )


def jump_to_and_play(start_ms: int, marker_id: int | None = None) -> None:
    st.session_state.video_start = start_ms / 1000
    st.session_state.video_autoplay_once = True
    if marker_id is not None:
        st.session_state.study_marker_id = marker_id


def request_video_playback() -> None:
    """Ask the native player to play after the rerender caused by a time-point click."""
    components.html(
        """
        <script>
        let attempts = 0;
        const tryPlay = () => {
          try {
            const videos = window.parent.document.querySelectorAll("video");
            const video = videos[videos.length - 1];
            if (!video) return false;
            video.play().catch(() => {});
            return true;
          } catch (_) {
            return false;
          }
        };
        const timer = setInterval(() => {
          attempts += 1;
          if (tryPlay() || attempts >= 20) clearInterval(timer);
        }, 100);
        </script>
        """,
        height=0,
    )


def enable_playback_memory(video_id: int) -> None:
    """Remember playback position in this browser without rerunning Streamlit."""
    storage_key = json.dumps(f"classroom-study:{DATABASE_PATH}:{video_id}", ensure_ascii=False)
    script = """
        <script>
        const storageKey = __STORAGE_KEY__;
        let attempts = 0;
        const attach = () => {
          try {
            const videos = window.parent.document.querySelectorAll("video");
            const video = videos[videos.length - 1];
            if (!video) return false;
            if (video.dataset.studyProgressKey === storageKey) return true;
            video.dataset.studyProgressKey = storageKey;
            const restore = () => {
              const saved = Number(window.parent.localStorage.getItem(storageKey));
              if (Number.isFinite(saved) && saved > 1 && video.currentTime < 1 && saved < video.duration - 3) {
                video.currentTime = saved;
              }
            };
            video.addEventListener("loadedmetadata", restore, { once: true });
            if (video.readyState >= 1) restore();
            let lastSaved = 0;
            video.addEventListener("timeupdate", () => {
              if (Math.abs(video.currentTime - lastSaved) >= 5) {
                window.parent.localStorage.setItem(storageKey, String(video.currentTime));
                lastSaved = video.currentTime;
              }
            });
            video.addEventListener("pause", () => {
              window.parent.localStorage.setItem(storageKey, String(video.currentTime));
            });
            return true;
          } catch (_) {
            return false;
          }
        };
        const timer = setInterval(() => {
          attempts += 1;
          if (attach() || attempts >= 30) clearInterval(timer);
        }, 100);
        </script>
    """.replace("__STORAGE_KEY__", storage_key)
    components.html(script, height=0)


review_video_id = st.session_state.get("review_video_id")
if not review_video_id:
    st.title("尚未选择视频")
    st.info("请返回视频管理页面，选择一个已处理视频后点击“开始复习”。")
    st.stop()

video = store.get_video(review_video_id)
if not video:
    st.title("视频记录不存在")
    st.info("视频库中没有这条视频记录，请返回视频管理页面重新选择。")
    st.stop()
if not Path(video["video_path"]).is_file():
    st.title("找不到原视频")
    st.error("原视频已被移动或删除，请返回视频管理页面重新登记本机路径。")
    st.stop()
if video["status"] != "review_ready":
    st.title("该视频尚未完成处理")
    st.info("视频完成转写和 AI 筛选后，才会开放重点复习页面。请返回视频管理页面等待或开始处理。")
    if st.button("返回视频管理", type="primary"):
        st.switch_page("video_app.py")
    st.stop()

top_left, top_right = st.columns([4, 1])
with top_left:
    st.title(f"重点复习：{video['display_name']}")
    st.caption(f"原始文件：{video['filename']}；来源文件夹：{video['source_folder']}")
with top_right:
    if st.button("返回管理", use_container_width=True):
        st.switch_page("video_app.py")

name_cols = st.columns([4, 1])
updated_display_name = name_cols[0].text_input(
    "视频显示名称",
    value=video["display_name"],
    key=f"video_display_name_{review_video_id}",
    help="只用于本项目的复习视频库；原始视频文件名和路径不会改变。",
)
if name_cols[1].button("保存名称", key=f"save_video_display_name_{review_video_id}"):
    try:
        store.update_video_display_name(review_video_id, updated_display_name)
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))

with st.expander("复习设置", expanded=False):
    settings_left, settings_right = st.columns(2)
    with settings_left:
        selected_asr = render_asr_settings()
        saved_model = video["transcription_model"]
        try:
            saved_label = get_model(saved_model).label if saved_model else "旧版 Whisper"
        except ValueError:
            saved_label = saved_model
        st.caption(f"当前视频字幕：{saved_label}")
        if render_retranscription(store, video, selected_asr):
            st.session_state.single_registered_video_id = int(video["id"])
            st.switch_page("video_app.py")
        if get_model(selected_asr).engine == "whisper":
            whisper = whisper_backend()
            st.caption(f"Whisper：{whisper.device.upper()} / {whisper.compute_type}")
        else:
            st.caption("Qwen3-ASR · 独立进程 · 自动选择 CUDA / CPU")
        ai_provider, ai_profile, api_key = render_ai_settings()
    with settings_right:
        ai_judgment_mode = st.radio(
            "AI 判定模块",
            options=("medium_recall", "high_recall", "low_recall"),
            key="ai_judgment_mode",
            format_func=lambda mode: {"high_recall": "高召回", "medium_recall": "中召回", "low_recall": "低召回"}[mode],
        )
        use_keyword_prefilter = st.checkbox(
            "关键词提前过滤",
            value=False,
            key="keyword_prefilter",
            help="只筛选词库命中附近的字幕，速度更快但可能漏掉没有提示词的内容。",
        )
        filter_formula_noise = st.checkbox(
            "过滤公式和低信息字幕",
            value=True,
            key="filter_formula_noise",
            help="移除孤立的纯公式口述和重复口头语；保留考试/教学提示及前后 20 秒字幕。不修改原始字幕，不自动覆盖已完成结果。",
        )
        st.caption("关键词词库请在视频管理页面编辑。")

segments = store.get_segments(review_video_id)
keyword_hits = store.list_keyword_hits(review_video_id)
if not keyword_hits and segments:
    store.replace_keyword_hits(review_video_id, find_all_trigger_matches(segments, trigger_groups), segments)
    keyword_hits = store.list_keyword_hits(review_video_id)

st.caption("各召回档位分别保存；切换档位复用已有字幕。")
module_labels = {
    "high_recall": "高召回",
    "medium_recall": "中召回",
    "low_recall": "低召回",
}
module_columns = st.columns(3)
for module_mode, module_column in zip(module_labels, module_columns):
    run = store.get_analysis_run(review_video_id, module_mode)
    with module_column:
        if run and run["status"] == "completed":
            st.caption(f"{module_labels[module_mode]}：已完成，{run['candidate_count']} 个候选")
            run_requested = st.button(
                f"重新分析{module_labels[module_mode]}",
                key=f"rerun_{module_mode}_{review_video_id}",
                help="只重建当前模式的 AI 重点，不重新转写视频；已移除项目保留，已掌握状态会按新结果重新生成。",
            )
            force_analysis = True
        else:
            run_requested = st.button(
                f"运行{module_labels[module_mode]}",
                key=f"run_{module_mode}_{review_video_id}",
            )
            force_analysis = False
        if run_requested:
            progress = st.progress(0)
            status = st.empty()
            try:
                if use_keyword_prefilter and trigger_groups is None:
                    raise ValueError("关键词词库 JSON 格式错误，请回到视频管理页面修正后再处理。")
                with get_api_judge(module_mode) as judge:
                    _outcome, count = process_saved_analysis_once(
                        store,
                        review_video_id,
                        judge,
                        module_mode,
                        progress=lambda value, message: (progress.progress(value), status.info(message)),
                        use_keyword_prefilter=use_keyword_prefilter,
                        trigger_groups=trigger_groups,
                        filter_formula_noise=filter_formula_noise,
                        force=force_analysis,
                    )
                st.success(f"{module_labels[module_mode]}完成，生成 {count} 个候选。")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

view_options = st.columns([1, 1, 4])
show_subtitles = view_options[0].checkbox("显示字幕", value=True, key=f"show_subtitles_{review_video_id}")
show_mastered = view_options[1].checkbox("显示已掌握", value=False, key=f"show_mastered_{review_video_id}")
review_labels = {**module_labels, "strict_exam_intent": "旧版考点"}
markers_by_mode = {
    mode: [marker for marker in store.list_markers(review_video_id, mode)
           if show_mastered or marker["review_status"] != "mastered"]
    for mode in review_labels
}
if not markers_by_mode["strict_exam_intent"]:
    review_labels.pop("strict_exam_intent")
excluded_markers = store.list_excluded_markers(review_video_id)
autoplay_requested = st.session_state.pop("video_autoplay_once", False)
video_column, timeline_column = st.columns([7, 3], gap="large")
with video_column:
    st.video(
        video["video_path"],
        start_time=st.session_state.get("video_start", 0.0),
        subtitles=transcript_to_vtt(segments) if show_subtitles else None,
        autoplay=autoplay_requested,
    )
    if autoplay_requested:
        request_video_playback()
    enable_playback_memory(review_video_id)
with timeline_column:
    st.subheader("重点导航")
    navigation_markers = sorted(
        [marker for mode in review_labels for marker in markers_by_mode[mode]],
        key=lambda marker: (marker["start_ms"], marker["id"]),
    )
    current_marker_id = st.session_state.get("study_marker_id")
    current_index = next(
        (index for index, marker in enumerate(navigation_markers) if marker["id"] == current_marker_id),
        -1,
    )
    previous_col, next_col = st.columns(2)
    if previous_col.button(
        "上一个重点",
        use_container_width=True,
        disabled=current_index <= 0,
        key=f"previous_marker_{review_video_id}",
    ):
        previous = navigation_markers[current_index - 1]
        jump_to_and_play(previous["start_ms"], previous["id"])
        st.rerun()
    next_index = current_index + 1
    if next_col.button(
        "下一个重点",
        use_container_width=True,
        disabled=next_index >= len(navigation_markers),
        key=f"next_marker_{review_video_id}",
    ):
        next_marker = navigation_markers[next_index]
        jump_to_and_play(next_marker["start_ms"], next_marker["id"])
        st.rerun()
    deleted_marker = st.session_state.get("last_deleted_marker")
    can_undo = bool(
        deleted_marker
        and deleted_marker["database"] == str(DATABASE_PATH)
        and deleted_marker["video_id"] == review_video_id
    )
    if can_undo:
        undo_col, undo_hint_col = st.columns([1, 1.4])
        if undo_col.button("撤回最近移除", key=f"undo_marker_{review_video_id}"):
            if restore_last_deleted_marker(review_video_id):
                st.rerun()
        undo_hint_col.caption("也可按 Ctrl+Z")
        enable_undo_shortcut()

    ai_tab, keyword_tab = st.tabs(
        [f"AI 重点（{len(navigation_markers)}）", f"提示词定位（{len(keyword_hits)}）"]
    )
    with ai_tab:
        recall_tabs = st.tabs([f"{label}（{len(markers_by_mode[mode])}）" for mode, label in review_labels.items()])
        for tab, (mode, label) in zip(recall_tabs, review_labels.items()):
            mode_markers = markers_by_mode[mode]
            empty_message = f"没有识别到{label}考点。"
            with tab:
                with st.container(height=660, border=True):
                    if not mode_markers:
                        st.info(empty_message)
                    for marker in mode_markers:
                        marker_cols = st.columns([1.25, 3.4, 1.15, 0.85])
                        with marker_cols[0]:
                            if st.button(f"播放 {format_time(marker['start_ms'])}", key=f"play_marker_{marker['id']}"):
                                jump_to_and_play(marker["start_ms"], marker["id"])
                                st.rerun()
                        with marker_cols[1]:
                            st.markdown(f"**{marker['title']}**")
                            st.caption(marker["note"])
                            st.write(marker["source_quote"])
                        with marker_cols[2]:
                            mastered = marker["review_status"] == "mastered"
                            if st.button("重新复习" if mastered else "已掌握", key=f"master_marker_{marker['id']}"):
                                store.set_marker_review_status(marker["id"], "active" if mastered else "mastered")
                                st.rerun()
                        with marker_cols[3]:
                            if st.button("移除", key=f"exclude_marker_{marker['id']}"):
                                remember_deleted_marker(marker)
                                store.set_marker_review_status(marker["id"], "excluded")
                                st.rerun()
    with keyword_tab:
        with st.container(height=660, border=True):
            if not keyword_hits:
                st.info("当前词库没有命中任何字幕。")
            for hit in keyword_hits:
                hit_cols = st.columns([1.25, 5])
                with hit_cols[0]:
                    if st.button(f"播放 {format_time(hit['start_ms'])}", key=f"play_keyword_{hit['id']}"):
                        jump_to_and_play(hit["start_ms"])
                        st.rerun()
                with hit_cols[1]:
                    st.markdown(f"**{hit['phrase']}** · {hit['category']}")
                    st.write(hit["source_quote"])

    if excluded_markers:
        with st.expander(f"已移除标记（{len(excluded_markers)}）"):
            for marker in excluded_markers:
                restore_cols = st.columns([4, 1])
                restore_cols[0].write(f"{format_time(marker['start_ms'])} · {marker['title']}")
                if restore_cols[1].button("恢复", key=f"restore_excluded_{marker['id']}"):
                    store.set_marker_review_status(marker["id"], "active")
                    st.rerun()
