"""Disposable AI connection API with real persistence and local encrypted keys."""
from pathlib import Path
import json
import os
import sys

sys.path.insert(0, str(Path.cwd()))
root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
os.environ["CLASSROOM_DATA_ROOT"] = str(root / "workspace")

import ai_provider
import api.main as api_main
import app_storage
from local_credentials import save_api_key
from video_store import VideoStore
import uvicorn

for variable in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
    os.environ.pop(variable, None)
app_storage.DATA_DIR = root
ai_provider.SETTINGS_PATH = root / "ai.json"
profile = ai_provider.ProviderProfile(base_url="https://fixture.invalid/v1", model="fixture-fast")
if not ai_provider.SETTINGS_PATH.exists():
    ai_provider.SETTINGS_PATH.write_text(json.dumps({"profile": profile.model_dump()}), encoding="utf-8")
    save_api_key(ai_provider.interface_key_path(profile), "fixture-primary-key")


class Judge:
    def test_api_connection(self):
        return "隔离测试接口已连接"

    def list_remote_models(self):
        return ["fixture-fast", "fixture-accurate"]

    def close(self):
        pass


def create_judge(profile, key, *_args, **_kwargs):
    profile.require_ready(key)
    return Judge()


api_main.create_judge = create_judge
application = api_main.create_app(VideoStore(root / "workspace" / "project.db"))
uvicorn.run(application, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
