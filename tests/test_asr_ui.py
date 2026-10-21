from streamlit.testing.v1 import AppTest
import pytest

import classcue.asr_models as asr_models
import classcue.asr_ui as asr_ui
from classcue.core import TranscriptSegment
import classcue.app_storage as app_storage
from classcue.video_store import VideoStore


def test_switching_model_only_persists_selection_without_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "asr.json")
    monkeypatch.setattr(asr_ui, "runtime_size", lambda: 0)
    monkeypatch.setattr(asr_ui, "runtime_is_ready", lambda: False)
    calls = []
    monkeypatch.setattr(asr_ui, "prepare_asr_model", lambda *args: calls.append(args[0]))
    app = AppTest.from_string("from classcue.asr_ui import render_asr_settings\nrender_asr_settings()").run()
    assert not app.exception
    app.selectbox(key="asr_model_id").select("qwen3-asr-1.7b").run()
    assert not app.exception
    assert asr_models.selected_model_id() == "qwen3-asr-1.7b"
    assert calls == []
    assert not asr_models.MODEL_ROOT.exists()
    app.button(key="prepare_asr").click().run()
    assert not app.exception
    assert calls == ["qwen3-asr-1.7b"]


def test_rendering_model_settings_does_not_scan_model_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "asr.json")
    monkeypatch.setattr(
        asr_ui,
        "model_is_downloaded",
        lambda _model_id: (_ for _ in ()).throw(AssertionError("页面重跑不应检查模型")),
    )
    monkeypatch.setattr(
        asr_ui,
        "cache_size",
        lambda _model_id: (_ for _ in ()).throw(AssertionError("页面重跑不应扫描缓存")),
    )
    app = AppTest.from_string("from classcue.asr_ui import render_asr_settings\nrender_asr_settings()").run()
    assert not app.exception


def test_firered_ui_disables_cpu_and_explicitly_migrates_legacy_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "asr.json")
    monkeypatch.setattr(asr_ui, "prepare_asr_model", lambda *_: pytest.fail("No model preparation"))
    asr_models.SETTINGS_PATH.write_text(
        '{"model_id": "fireredasr2-aed", "firered_device": "cpu"}', encoding="utf-8",
    )
    app = AppTest.from_string("from classcue.asr_ui import render_asr_settings\nrender_asr_settings()").run()
    assert not app.exception
    assert not any(item.key == "firered_device" for item in app.radio)
    assert asr_models.selected_firered_device() == "cpu"
    assert any("CPU 处理已禁用" in item.value for item in app.warning)
    app.button(key="firered_gpu_save").click().run()
    assert not app.exception
    assert asr_models.selected_firered_device() == "gpu"
    assert any("CPU 处理已禁用" in item.value for item in app.warning)


def test_retired_model_in_existing_session_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "asr.json")
    asr_models.select_model("qwen3-asr-1.7b")
    app = AppTest.from_string(
        "import streamlit as st\n"
        "st.session_state.setdefault('asr_model_id', 'paraformer-large-vad-punc')\n"
        "st.session_state.setdefault('clear_asr_target', 'paraformer-large-vad-punc')\n"
        "st.session_state.setdefault('confirm_clear_asr', True)\n"
        "from classcue.asr_ui import render_asr_settings\n"
        "render_asr_settings()"
    ).run()
    assert not app.exception
    assert app.session_state.asr_model_id == "qwen3-asr-1.7b"
    assert app.session_state.clear_asr_target == "qwen3-asr-1.7b"
    assert app.session_state.confirm_clear_asr is False
    assert app.button(key="clear_asr").disabled


def test_nano_removed_from_streamlit_without_retirement_notice_or_file_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "asr.json")
    original = '{"model_id": "fun-asr-nano-2512", "nano_device": "gpu"}'
    asr_models.SETTINGS_PATH.write_text(original, encoding="utf-8")
    app = AppTest.from_string(
        "import streamlit as st\n"
        "st.session_state.setdefault('asr_model_id', 'fun-asr-nano-2512')\n"
        "st.session_state.setdefault('clear_asr_target', 'fun-asr-nano-2512')\n"
        "st.session_state.setdefault('_asr_nano_runtime_size', 12345)\n"
        "from classcue.asr_ui import render_asr_settings\n"
        "render_asr_settings()"
    ).run()
    assert not app.exception
    assert app.session_state.asr_model_id == asr_models.DEFAULT_MODEL_ID
    assert all("Nano" not in option for option in app.selectbox(key="asr_model_id").options)
    assert all("Nano" not in option for option in app.selectbox(key="clear_asr_target").options)
    assert not any("Nano" in item.value for item in app.caption)
    assert not any("已下架" in item.value for item in app.warning)
    assert not any(item.key == "nano_device" for item in app.radio)
    assert asr_models.SETTINGS_PATH.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("available", [False, True])
def test_paraformer_streamlit_devices_follow_cuda_availability(tmp_path, monkeypatch, available):
    import classcue.paraformer_runtime as paraformer_runtime

    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "asr.json")
    monkeypatch.setattr(asr_ui, "paraformer_cuda_status", lambda: {
        "gpu_available": available, "gpu_prepare_available": available,
    })
    monkeypatch.setattr(paraformer_runtime, "require_cuda", lambda: None)
    monkeypatch.setattr(asr_ui, "prepare_asr_model", lambda *_a, **_kw: pytest.fail("No preparation"))
    asr_models.select_model("paraformer-large-cpu", paraformer_device="cpu")
    app = AppTest.from_string("from classcue.asr_ui import render_asr_settings\nrender_asr_settings()").run()
    assert not app.exception
    assert app.radio(key="paraformer_device").options == (["CPU", "GPU"] if available else ["CPU"])
    if available:
        app.radio(key="paraformer_device").set_value("gpu").run()
        assert not app.exception
        assert asr_models.selected_paraformer_device() == "gpu"
    else:
        assert app.button(key="prepare_paraformer_gpu").disabled
        assert asr_models.selected_paraformer_device() == "cpu"


def test_management_and_review_pages_expose_same_model_settings(tmp_path, monkeypatch):
    workspace = tmp_path / "course"
    monkeypatch.setenv("CLASSROOM_DATA_ROOT", str(workspace))
    monkeypatch.setattr(app_storage, "DEEPSEEK_API_KEY_PATH", tmp_path / "key.dpapi")
    monkeypatch.setattr(app_storage, "TRIGGER_LIBRARY_PATH", tmp_path / "triggers.json")
    monkeypatch.setattr(asr_models, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(asr_models, "SETTINGS_PATH", tmp_path / "asr.json")
    monkeypatch.setattr(asr_ui, "runtime_size", lambda: 0)
    store = VideoStore(workspace / "project.db")
    path = tmp_path / "video.mp4"
    path.write_bytes(b"local video fixture")
    video_id = store.create_video("hash", path.name, str(path), str(tmp_path / "audio.wav"), 1000)
    store.mark_transcribed(video_id, [TranscriptSegment(1, 0, 1000, "单纯形法。")], "whisper-turbo")
    store.set_status(video_id, "review_ready")
    app = AppTest.from_file(str(asr_models.ROOT / "classcue" / "video_app.py")).run(timeout=30)
    assert not app.exception
    expected_models = [model.label for model in asr_models.MODELS.values()]
    assert app.selectbox(key="asr_model_id").options == expected_models
    app.session_state.review_video_id = video_id
    app.switch_page("pages/review.py").run(timeout=30)
    assert not app.exception
    assert app.selectbox(key="asr_model_id").options == expected_models
    assert app.button(key=f"retranscribe_{video_id}").disabled
    app.selectbox(key="asr_model_id").select("qwen3-asr-1.7b").run()
    assert not app.exception
    assert not asr_models.MODEL_ROOT.exists()
    assert store.get_segments(video_id)[0].text == "单纯形法。"
