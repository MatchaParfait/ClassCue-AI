# 贡献指南

## 开发环境

应用的 Python 模块位于 `classcue/`；`api/` 存放 HTTP 路由；`scripts/` 存放
启动、准备和发行工具。请使用类似
`from classcue.video_store import VideoStore` 的包导入方式，并将新的应用模块
放在对应的包目录中，不要放在仓库根目录。入口和相对于安装目录的资源位置请
参阅开发指南中的目录说明。

本地环境、启动命令、API、模型实现和测试说明见[开发指南](docs/development.md)。
维护者还应阅读[发行与公告维护指南](docs/releases.md)。

请使用与 CI 一致的 Windows、Python 3.12 和 Node.js 24 环境。先安装 `uv`，
然后运行：

```powershell
uv sync --locked --group dev
python scripts/check_repository_hygiene.py
Push-Location frontend
npm ci
Pop-Location
uv run --locked --group dev python -m pytest tests -q
Push-Location frontend
npm test -- --maxWorkers=2 --minWorkers=1
npm run build
Pop-Location
```

运行时依赖在 `pyproject.toml` 中声明。依赖发生变化时，请运行 `uv lock` 并提交
更新后的 `uv.lock`；一键启动器使用该锁定文件和隔离的 `.runtime` 环境。
不要提交 `.runtime`。

## 拉取请求

- 保持改动聚焦，并为变更后的持久化逻辑或处理流水线补充测试。
- 不要提交 `data/`、`projects/`、`.env`、提取出的音频、视频、模型或 API Key。
- 问题报告、测试、截图和文档只能使用匿名化或合成的媒体与字幕。
- 生成图片、恢复的资源、ASR 运行结果、日志和任务缓存保留在本机；
  只有经过审阅且确实需要的产品资源，才能复制到指定源码目录。
- 暂存明确的文件路径，或使用 `git add -p`；提交前检查
  `git diff --cached` 和 `git diff --cached --check`。未经审阅不要暂存整个工作区。
- 暂存新增或修改文件后运行 `python scripts/check_repository_hygiene.py`。
  该检查读取 Git 索引，因此每次暂存变化后都要重新运行。
- 仓库清理、CI 改动和无关的产品改动应拆分到不同提交中。
- 内部 Agent 指引和独立实验流程保持私有。`experiments/subtitle-cyberspace/src/`
  下被应用使用的共享场景模块仍属于应用源码。
- 新增运行时文件时更新 `release_manifest.toml`。公开测试、文档和构建辅助工具
  不会自动包含在便携应用中。

公共与私有备份边界，以及已发布敏感材料的处理方式，见[仓库维护指南](docs/repository-maintenance.md)。

## 可选的浏览器回归测试

在 `frontend` 中安装 Playwright：

```powershell
npm install --no-save --package-lock=false playwright
```

然后针对隔离的测试服务运行 `tests/` 中的浏览器脚本。也可以将
`CLASSROOM_PLAYWRIGHT_MODULE` 设置为已安装的 Playwright 模块路径。
回归测试不得使用真实视频、凭据或付费服务。

## 安全与隐私

发现凭据泄露时，请私下报告给仓库维护者。不要在公开问题中发布真实的
DeepSeek Key 或课堂录音。
