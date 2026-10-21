"""Batch processing and study-library management for local classroom videos."""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from asr_models import get_model
from asr_ui import render_asr_settings, render_retranscription
from ai_provider import create_judge
from ai_ui import render_ai_settings
from core import DEFAULT_TRIGGER_GROUPS, format_time, validate_trigger_groups
from batch_processing import format_eta, run_batch
from app_storage import TRIGGER_LIBRARY_PATH, active_storage_paths
from openai_judge import OpenAICompatibleJudge
from path_picker import read_path_picker_result, start_path_picker
from runtime import whisper_backend
from video_pipeline import discover_videos, process_video_once, register_local_video
from video_store import VideoStore
from qwen_resilience import read_json, atomic_json


LIBRARY_ROOT, DATABASE_PATH, AUDIO_DIR = active_storage_paths()
store = VideoStore(DATABASE_PATH)

st.set_page_config(page_title="课堂视频重点复习", page_icon="Video", layout="wide")
st.title("课堂视频 · 重点复习")
st.caption("本机转写课堂视频，生成期末重点导航，处理一次即可反复复习。")


def poll_path_picker() -> None:
    state = st.session_state.get("_path_picker")
    if not state:
        return
    finished, selected = read_path_picker_result(state)
    if not finished:
        return
    target_key = state["target_key"]
    st.session_state.pop("_path_picker", None)
    if selected:
        st.session_state[target_key] = selected
        st.session_state["_path_picker_message"] = f"已选择：{selected}"
    else:
        st.session_state["_path_picker_message"] = "已取消选择。"


def start_path_picker_for(target_key: str, directory: bool, button_key: str) -> None:
    state = st.session_state.get("_path_picker")
    if state:
        st.warning("已有一个文件选择窗口正在打开，请先关闭它。")
        return
    try:
        picker = start_path_picker("directory" if directory else "file")
        picker["target_key"] = target_key
        st.session_state["_path_picker"] = picker
        st.session_state["_path_picker_button_key"] = button_key
    except Exception as exc:
        st.error(f"无法打开文件选择器：{exc}")


def render_path_picker_status() -> None:
    poll_path_picker()
    message = st.session_state.pop("_path_picker_message", None)
    if message:
        st.caption(message)
    state = st.session_state.get("_path_picker")
    if not state:
        return
    st.info("Windows 选择窗口已打开。关闭窗口后，点击“读取选择结果”；页面其余部分仍可继续使用。")
    if st.button("读取选择结果", key="read_path_picker"):
        poll_path_picker()
        st.rerun()


poll_path_picker()


def open_review(video_id: int) -> None:
    video = store.get_video(video_id)
    if not video:
        st.error("视频记录不存在。")
        return
    if video["status"] != "review_ready":
        st.warning("该视频尚未处理完成，暂时不能进入重点复习页面。")
        return
    st.session_state.review_video_id = video_id
    st.session_state.video_start = 0.0
    st.switch_page("pages/review.py")


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


with st.sidebar:
    st.caption(f"数据库：{DATABASE_PATH}")
    asr_model_id = render_asr_settings()
    st.divider()
    st.subheader("运行状态")
    if get_model(asr_model_id).engine == "whisper":
        whisper = whisper_backend()
        st.caption(f"Whisper：{whisper.device.upper()} / {whisper.compute_type}")
        st.caption(whisper.detail)
    else:
        st.caption("独立模型进程；Qwen 使用分阶段 GPU 推理，不自动转 CPU")
    ai_provider, ai_profile, api_key = render_ai_settings()
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
        help="仅把命中“会考、重点、必考、易错”等词附近的字幕交给 AI，可减少 API 调用，但可能漏掉没有提示词的考点。",
    )
    filter_formula_noise = st.checkbox(
        "过滤公式和低信息字幕",
        value=True,
        key="filter_formula_noise",
        help="移除孤立的纯公式口述和重复口头语；保留考试/教学提示及前后 20 秒字幕。不修改原始字幕，不自动覆盖已完成结果。",
    )
    with st.expander("编辑关键词预筛选词库"):
        reset_col, save_col = st.columns(2)
        if reset_col.button("恢复默认词库"):
            st.session_state.video_trigger_library_text = default_trigger_library_text()
            TRIGGER_LIBRARY_PATH.write_text(st.session_state.video_trigger_library_text, encoding="utf-8")
            st.rerun()
        if save_col.button("保存词库"):
            try:
                active_trigger_groups = validate_trigger_groups(json.loads(st.session_state.video_trigger_library_text))
                TRIGGER_LIBRARY_PATH.write_text(json.dumps(active_trigger_groups, ensure_ascii=False, indent=2), encoding="utf-8")
                st.success("词库已保存。")
            except (ValueError, json.JSONDecodeError) as exc:
                st.error(f"词库格式错误：{exc}")
        st.text_area(
            "词库 JSON",
            key="video_trigger_library_text",
            height=360,
            help="结构：信号强度 -> 分类 -> 短语数组。自定义短语仅在开启关键词提前过滤时生效。",
        )

try:
    trigger_groups = validate_trigger_groups(json.loads(st.session_state.video_trigger_library_text))
except (ValueError, json.JSONDecodeError):
    trigger_groups = None


def get_api_judge(mode: str | None = None) -> OpenAICompatibleJudge:
    return create_judge(ai_profile, api_key, ai_provider, mode=mode or ai_judgment_mode)


STATUS_LABELS = {
    "uploaded": "待处理",
    "processing": "处理中",
    "transcribed": "已转写",
    "review_ready": "可复习",
    "failed": "处理失败",
    "resource_paused": "资源暂停",
}


def source_folder_label(folder: str) -> str:
    path = Path(folder)
    return path.name or str(path)


def source_folder_option_labels(folders: list[str]) -> dict[str, str]:
    """Keep the selector compact, but disambiguate folders with the same final name."""
    labels = {folder: source_folder_label(folder) for folder in folders}
    for folder, label in labels.items():
        if sum(other == label for other in labels.values()) > 1:
            labels[folder] = folder
    return labels


def video_review_label(video: object) -> str:
    row = video
    display_name = (row["display_name"] or Path(row["filename"]).stem).strip()
    status = STATUS_LABELS.get(row["status"], row["status"])
    return f"{display_name} · {status} · {source_folder_label(row['source_folder'])} · #{row['id']}"


st.subheader("批量处理")
overview = store.list_videos()
overview_counts = {
    "total": len(overview),
    "ready": sum(row["status"] == "review_ready" for row in overview),
    "processing": sum(row["status"] == "processing" for row in overview),
    "failed": sum(row["status"] == "failed" for row in overview),
}
stat_columns = st.columns(4)
stat_columns[0].metric("视频总数", overview_counts["total"])
stat_columns[1].metric("可复习", overview_counts["ready"])
stat_columns[2].metric("处理中", overview_counts["processing"])
stat_columns[3].metric("失败待重试", overview_counts["failed"])
metrics_rows = [row for row in store.list_processing_metrics() if row["transcription_seconds"] > 0]
if metrics_rows:
    total_media = sum(max(1, int(row["duration_ms"])) / 1000 for row in metrics_rows)
    total_transcription = sum(float(row["transcription_seconds"]) for row in metrics_rows)
    st.caption(
        f"已记录平均转写速度：{total_media / max(total_transcription, 0.1):.1f} 倍实时速度；"
        "批量运行时会根据最近视频动态估算剩余时间。"
    )
folder_cols = st.columns([1, 4, 1])
if folder_cols[0].button("选择视频文件夹"):
    start_path_picker_for("video_folder", directory=True, button_key="choose_video_folder")
folder = folder_cols[1].text_input("本机视频文件夹", key="video_folder", placeholder=r"E:\course-videos")
if folder_cols[2].button("扫描"):
    try:
        st.session_state.batch_paths = [str(path) for path in discover_videos(folder)]
        st.session_state.selected_batch_paths = st.session_state.batch_paths.copy()
    except Exception as exc:
        st.error(str(exc))

batch_paths: list[str] = st.session_state.get("batch_paths", [])
paused_queue = read_json(AUDIO_DIR / "batch_queue.json", {})
if not batch_paths and paused_queue.get("status") == "resource_paused":
    batch_paths = paused_queue["paths"]
    st.session_state.batch_paths = batch_paths
if batch_paths:
    def display_batch_path(path: str) -> str:
        try:
            return str(Path(path).relative_to(Path(folder).expanduser().resolve()))
        except ValueError:
            return Path(path).name

    st.write(f"递归发现 {len(batch_paths)} 个视频（含子文件夹）。仅处理未完成视频；每个视频最多 60 分钟且不超过 1GB。")
    if "selected_batch_paths" not in st.session_state:
        st.session_state.selected_batch_paths = batch_paths.copy()
    else:
        valid_selected = [path for path in st.session_state.selected_batch_paths if path in batch_paths]
        if valid_selected != st.session_state.selected_batch_paths:
            st.session_state.selected_batch_paths = valid_selected
    selected_batch_paths = st.multiselect(
        "选择要处理的视频",
        batch_paths,
        format_func=display_batch_path,
        key="selected_batch_paths",
    )
    resume_paths = None
    if paused_queue.get("status") == "resource_paused":
        st.warning(paused_queue.get("message", "资源不足，队列已暂停"))
        resume_cols = st.columns(3)
        if resume_cols[0].button("继续队列"):
            resume_paths = paused_queue["paths"]
        if resume_cols[1].button("跳过当前文件"):
            resume_paths = paused_queue["paths"][1:]
        if resume_cols[2].button("停止队列"):
            atomic_json(AUDIO_DIR / "batch_queue.json", {**paused_queue, "status": "interrupted"})
            st.rerun()
        if resume_paths is not None and not resume_paths:
            atomic_json(AUDIO_DIR / "batch_queue.json", {"status": "completed"})
            st.rerun()
    start_batch = st.button(f"处理选中的 {len(selected_batch_paths)} 个视频",
                            type="primary", disabled=not selected_batch_paths)
    if start_batch or resume_paths:
        try:
            if use_keyword_prefilter and trigger_groups is None:
                raise ValueError("关键词词库 JSON 格式错误，请修正后再处理。")
            judge = get_api_judge()
        except Exception as exc:
            st.error(str(exc))
        else:
            overall = st.progress(0)
            status = st.empty()
            def update_batch_progress(event) -> None:
                done = event.completed + event.skipped + event.failed
                overall.progress(min(1.0, done / max(event.total, 1)))
                status.info(
                    f"{event.message}；完成 {done}/{event.total}，"
                    f"剩余 {event.estimate.remaining_videos} 个，预计 {format_eta(event.estimate.seconds)}"
                )

            with judge:
                summary = run_batch(
                    store,
                    resume_paths if resume_paths is not None else selected_batch_paths,
                    AUDIO_DIR,
                    judge,
                    use_keyword_prefilter=use_keyword_prefilter,
                    trigger_groups=trigger_groups,
                    analysis_mode=ai_judgment_mode,
                    on_event=update_batch_progress,
                    asr_model_id=paused_queue.get("asr_model_id", asr_model_id) if resume_paths is not None else asr_model_id,
                    filter_formula_noise=filter_formula_noise,
                    qwen_options=paused_queue.get("qwen_options") if resume_paths is not None else st.session_state.get("qwen_options"),
                )
            if summary.paused:
                st.warning("资源不足，剩余队列已暂停。关闭占用资源的软件后可继续。")
            else:
                atomic_json(AUDIO_DIR / "batch_queue.json", {"status": "completed"})
                overall.progress(1.0)
                st.success(
                    f"批量处理结束：完成 {summary.completed}，已跳过 {summary.skipped}，"
                    f"处理中 {summary.in_progress}，失败 {summary.failed}。"
                )


interrupted_videos = [row for row in store.list_videos() if row["status"] == "processing"]
if interrupted_videos:
    with st.expander(f"恢复中断任务（{len(interrupted_videos)}）"):
        st.warning("仅当应用、电脑或网络任务已经中断时使用。正在运行的批处理不应在此处重置。")
        for interrupted_video in interrupted_videos:
            recovery_cols = st.columns([5, 1.8])
            with recovery_cols[0]:
                st.write(interrupted_video["display_name"])
                st.caption(f"原始文件：{interrupted_video['filename']}")
            with recovery_cols[1]:
                if st.button("标记为失败", key=f"recover_interrupted_{interrupted_video['id']}"):
                    if store.mark_interrupted_video_failed(int(interrupted_video["id"])):
                        st.success("已标记为失败，可在“单个本地视频”或批量处理中重新处理。")
                        st.rerun()


st.divider()
st.subheader("单个本地视频")
single_cols = st.columns([1, 4, 1])
if single_cols[0].button("选择本地视频"):
    start_path_picker_for("single_video_path", directory=False, button_key="choose_single_video")
single_path = single_cols[1].text_input("本机视频路径", key="single_video_path", placeholder=r"E:\course-videos\lesson.mp4")
render_path_picker_status()
single_display_name = st.text_input("视频显示名称（可选）", placeholder="例如：第 03 讲 - 单纯形法")
if single_cols[2].button("登记"):
    try:
        video_id, _path, existed = register_local_video(store, single_path, AUDIO_DIR)
        if single_display_name.strip():
            store.update_video_display_name(video_id, single_display_name)
        st.session_state.single_registered_video_id = video_id
        st.success("已加载既有记录。" if existed else "已登记，请在下方开始处理。")
    except Exception as exc:
        st.error(str(exc))


single_registered_video_id = st.session_state.get("single_registered_video_id")
single_registered_video = store.get_video(single_registered_video_id) if single_registered_video_id else None
if single_registered_video:
    st.caption(
        f"已登记视频：{single_registered_video['display_name']} · "
        f"{STATUS_LABELS.get(single_registered_video['status'], single_registered_video['status'])}"
    )
    if single_registered_video["status"] == "review_ready":
        if st.button("开始复习这个视频", type="primary"):
            open_review(int(single_registered_video["id"]))
    elif single_registered_video["status"] == "processing":
        st.info("该视频正在处理中，请等待当前任务完成后再开始复习。")
    else:
        action_label = ("继续处理这个视频" if single_registered_video["status"] == "resource_paused"
                        else "重新处理这个视频" if single_registered_video["status"] == "failed"
                        else "开始处理这个视频")
        if st.button(action_label, type="primary"):
            try:
                if use_keyword_prefilter and trigger_groups is None:
                    raise ValueError("关键词词库 JSON 格式错误，请修正后再处理。")
                progress = st.progress(0)
                status = st.empty()
                with get_api_judge() as judge:
                    _outcome, count = process_video_once(
                        store,
                        int(single_registered_video["id"]),
                        judge,
                        progress=lambda value, message: (progress.progress(value), status.info(message)),
                        use_keyword_prefilter=use_keyword_prefilter,
                        trigger_groups=trigger_groups,
                        analysis_mode=ai_judgment_mode,
                        asr_model_id=asr_model_id,
                        filter_formula_noise=filter_formula_noise,
                        qwen_options=st.session_state.get("qwen_options"),
                    )
                st.success(f"自动处理完成，生成 {count} 个疑似重点。")
                st.rerun()
            except Exception as exc:
                st.error(str(exc))

    transcript = store.get_segments(int(single_registered_video["id"]))
    if transcript and single_registered_video["status"] != "processing":
        stored_model = single_registered_video["transcription_model"]
        try:
            stored_label = get_model(stored_model).label if stored_model else "旧版 Whisper"
        except ValueError:
            stored_label = stored_model
        st.caption(f"现有字幕模型：{stored_label}")
        if render_retranscription(store, single_registered_video, asr_model_id):
            st.rerun()


videos = store.list_videos()
if videos:
    st.divider()
    st.subheader("复习视频库")
    reviewable_videos = [row for row in videos if row["status"] == "review_ready"]
    st.caption(f"已登记 {len(videos)} 个视频；其中 {len(reviewable_videos)} 个已完成处理，可以开始复习。")
    source_folders = sorted({str(row["source_folder"]) for row in reviewable_videos})
    folder_labels = source_folder_option_labels(source_folders)
    folder_options = ["全部来源文件夹", *source_folders]
    selected_folder = st.selectbox(
        "来源文件夹",
        folder_options,
        format_func=lambda value: value if value == "全部来源文件夹" else folder_labels[value],
    )
    review_query = st.text_input("搜索视频名称或原始文件名", placeholder="例如：单纯形、第 03 讲")
    normalized_query = review_query.strip().casefold()
    filtered_videos = [
        row
        for row in reviewable_videos
        if (selected_folder == "全部来源文件夹" or row["source_folder"] == selected_folder)
        and (
            not normalized_query
            or normalized_query in row["display_name"].casefold()
            or normalized_query in row["filename"].casefold()
            or normalized_query in row["source_folder"].casefold()
        )
    ]
    if not filtered_videos:
        st.info("当前筛选条件下没有已完成处理、可复习的视频。")
    else:
        options = {video_review_label(row): int(row["id"]) for row in filtered_videos}
        selected_label = st.selectbox("选择视频", list(options), index=0)
        selected_video = store.get_video(options[selected_label])
        if selected_video:
            st.caption(
                f"原始文件：{selected_video['filename']}；来源：{selected_video['source_folder']}；"
                f"时长：{format_time(selected_video['duration_ms'])}"
            )
        if st.button("开始复习", type="primary"):
            open_review(options[selected_label])
