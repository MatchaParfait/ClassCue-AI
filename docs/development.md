# 开发与技术说明

本文保留开发环境、启动方式、接口、模型适配和测试等技术资料。面向用户的软件介绍与使用入口见 [README](../README.md)；发行签名、自动更新及公告维护见 [发行维护指南](releases.md)。

一个 Windows 本地运行的桌面应用，同时保留网页开发入口：使用可切换的本地 ASR 模型转写课堂视频，通过统一的 OpenAI 兼容接口筛选期末考点，并生成可反复使用的重点导航。默认使用 DeepSeek 官方 API，也可以改成第三方中转站。复习时可以快速跳转、标记已掌握，或顺手移除误判。

## 一键开始

推荐使用 GitHub Releases 的 `ClassroomVideoStudy-版本-windows-x64.zip`，解压到可写目录后双击 `启动课堂重点复习.cmd`。正式便携包内置前端，无需 Node.js；首次仍需联网准备 Python 和桌面依赖，需要 Windows WebView2 Runtime。

维护者配置签名并发布 tag 后才会出现正式发行包。也可以使用源码入口（首次构建需要 Node.js）：

1. 克隆仓库，或使用 GitHub 的 `Code -> Download ZIP`，完整解压到可写目录。
2. 双击 `启动课堂重点复习.cmd`。
3. 首次启动等待准备窗口完成便携 Python、桌面依赖和页面构建，随后打开独立应用窗口。需要 Windows WebView2 Runtime。
4. 在“AI 服务”中先填写或获取模型名称，再填写 API Key 和 Base URL；默认 Base URL 是 DeepSeek 官方 API，也可以改成第三方中转站。

前后端使用同一套控制入口，不需要分别启动或查找进程：

- `启动课堂重点复习.cmd`：打开桌面窗口，已有实例时唤起原窗口。首次成功启动自动建立桌面和开始菜单快捷方式。
- `停止课堂重点复习.cmd`：向桌面实例请求退出；有任务时先确认“停止任务并退出”，安全停止后再关闭所属服务。
- `重启课堂重点复习.cmd`：重启所属服务，保留桌面窗口；有任务时提示先停止。应用数据迁移继续使用校验、立即重启和自动重连流程。

有任务时关闭窗口会提示“后台继续／取消关闭”，托盘提供“打开主窗口”和“退出”。无任务时关闭窗口会退出并停止所属服务。窗口左下角“桌面”中可设置启动/唤起快捷键，留空关闭。完整说明见 [桌面生命周期](desktop-lifecycle.md)。

桌面正常运行由 API 直接提供构建后的页面，无需启动 Node/Vite。`启动本地前端.cmd` 保留浏览器与 Vite 开发入口，首次需在 `frontend` 执行 `npm install`。端口默认 API `8765`、开发前端 `5173`，已有运行记录时沿用记录。旧版 Streamlit 仍可通过 `scripts/bootstrap.ps1` / `scripts/start_app.py` 启动。

首次准备需要联网，通常会下载数百 MB 的 Python 和依赖。模型选择本身不会下载；点击“下载 / 准备所选模型”或第一次使用该模型转写时，才下载所选模型。默认使用 Whisper `turbo`。自动运行环境保存在项目内的 `.runtime`，不会修改系统 Python、PyCharm 解释器或开发用 `.venv`。

## 更新与公告

“设置 → 关于与更新”支持每日后台版本检查、可选自动下载和确认后安装重启；顶栏“公告”显示项目通知和未读数量。检查仅访问公开 GitHub 签名源，不上传视频、字幕或密钥，离线不影响本地使用。

正式便携包支持同依赖、同数据格式版本的原地升级，备份程序和数据库并保留用户数据。源码目录不自动覆盖，不兼容更新需要手动升级。维护者配置 GitHub Secret、发布版本及发送公告的步骤见 [发行与公告维护指南](releases.md)。本项目采用 [MIT License](../LICENSE)。

## 功能

- 本机读取 `mp4`、`mov`、`mkv`、`avi` 视频，不上传或复制原视频。
- 可选择 Whisper `turbo`、Whisper `large-v3`、Qwen3-ASR `1.7B` 或 FireRedASR2-AED；仅下载使用的模型。
- Qwen 配套 ForcedAligner `0.6B`，提供带原视频时间偏移的字词及字幕时间戳。
- 模型缓存可查看占用和按模型清除，清除不影响原视频、字幕或复习结果。
- 模型列表先显示配置及上次检查结果；普通处理页面只后台检查当前模型，设置页点击“修改模型”后才检查全部模型，并逐项更新状态。尚未检查不会显示成“未准备”，检查失败保留已有结果并可单独重试。
- 完整状态检查缓存五分钟，轻量检查缓存一分钟；模型和运行环境的目录大小持久缓存，准备、清理、就绪标记变化或主动刷新后重新统计。重复请求复用同一轮检查；开始转写和选择 GPU 时仍执行必要的真实校验。
- 同一次运行复用所选模型，启用另一模型转写时释放旧模型；Whisper 直接解码失败时才生成临时 WAV，转写后自动删除。
- 批量处理时，本地 ASR 转写与上一个视频的 AI 筛选重叠执行。
- 支持 DeepSeek 官方 API 和自定义中转站共用一套 OpenAI-compatible Chat Completions 配置。
- 约五分钟一段调用所选 AI 服务，最多四路并发；成功字幕块会缓存，中断后不重复请求。
- 默认在发送前过滤孤立的纯公式口述和重复口头语，保留考试/教学提示及前后 20 秒上下文；原始字幕不变。该规则是保守启发式，不保证识别全部 ASR 错误。
- “期末考点”与“扩展重点”独立保存，默认以更精简的期末考点为主。
- 重点复习页提供字幕、时间点跳转、上一个/下一个重点、播放进度记忆和独立滚动导航。
- 可标记“已掌握”或“移除”；移除不会物理删除，支持 `Ctrl+Z` 和后续恢复。
- 管理页显示视频数量、完成数、失败数和当前处理状态；批处理过程中按本机历史速度估算剩余时间。
- 全应用使用一个视频库，按文件夹管理不同课程，搜索覆盖全部视频。

## 系统要求

当前项目面向 64 位 Windows 10/11。一键启动会自行准备 Python 3.12。GPU 模式需要兼容的 NVIDIA 驱动；GPU 不可用时自动使用 CPU。

## 开发环境

下面的命令只供修改代码和运行测试使用，普通用户不需要执行：

推荐使用 Python 3.12、Node.js 24。测试与发行工作流通过 `uv sync --locked --group dev` 安装提交的锁定依赖，不直接安装全部依赖的最新版本。本机可使用同样的方式：

```powershell
python -m pip install uv
uv sync --locked --group dev
uv run --locked --group dev python -m pytest tests -q
```

下面是保留的手动环境准备入口，排查 GPU 环境时也可以使用：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python setup_environment.py
```

开发安装脚本不会安装 PyTorch、torchvision 或本地大语言模型；视频转写由 faster-whisper/CTranslate2 独立使用 GPU。脚本结束时会显示实际转写后端。

使用 Qwen 时才额外创建 `.runtime/qwen-asr` 独立 Python 3.12 环境并安装 PyTorch、`qwen-asr` 等依赖，不会改变 Whisper 环境的依赖版本。

如果必须确认 GPU 可用，可以运行：

```powershell
python setup_environment.py --require-gpu
```

检测失败时程序仍可以使用 CPU；`--require-gpu` 仅让安装检查返回失败，便于排查 NVIDIA 驱动、CUDA/cuDNN 运行库或老显卡兼容问题。

## 配置 AI 服务

管理页左侧和复习页“复习设置”使用同一套 AI 服务设置：

- **模型名称**：可以直接填写，也可以先点击列表图标从当前 Base URL 获取远端模型；部分站点不提供模型列表，此时手动输入即可。
- **Base URL**：默认是 `https://api.deepseek.com`，也可以填写中转站给出的 API 根地址。根地址通常是 `https://站点/v1`，也可以包含中转站要求的路径前缀；不要填写 `/chat/completions`、`/responses` 或 `/models` 端点。
- **API Key**：填写与当前 Base URL 和模型对应的密钥。
- 更换 Base URL 或 API Key 会清除旧模型列表。
- 网络检查图标会使用当前模型发送一个很小的普通文本请求，验证 API Key、Base URL、模型和 Chat Completions 是否可用；不会发送课堂视频或字幕，可能产生少量 API 费用。
- 有效的 Base URL、模型和接口参数会自动保存，下次打开时恢复上次使用的配置；保存图标仍可用于立即保存。普通配置不包含密钥，密钥会按 Base URL 分开记忆，切换中转站地址时不会混用。
- “接口参数”可设置输出上限、请求超时、JSON 模式及 `max_tokens` / `max_completion_tokens` 字段。仅发送选择的长度字段，不同时发送两个。

当前适配 **非流式 Chat Completions**，不包含 Responses、Anthropic 原生接口或跨协议网关。AI 提示词、字幕分块、最多四路并发及两种考点判定模式保持不变。更换接口地址或模型仅影响后续请求，不自动覆盖已完成的重点或复习状态。

默认请求 JSON 模式；只有服务明确返回“不支持 `response_format` / `json_object`”的参数错误时，才移除该参数重试，之后仍校验 JSON。认证、限流或其他请求错误不会伪装成 JSON 兼容问题。输出达到长度上限时，该块不保存为成功缓存；提高上限后重试。

非敏感配置保存在本机 `data/ai_providers.json`，两个页面和 CLI 都使用它。第一次运行且没有保存配置时，使用 `.env` 的配置默认值。

可以在页面中输入 API Key，也可以复制 `.env.example` 为 `.env` 后填写：

```text
DEEPSEEK_API_KEY=your-deepseek-api-key
DEEPSEEK_MODEL=deepseek-v4-flash
```

如果要使用中转站，可以使用同一套环境变量：

```text
OPENAI_BASE_URL=https://your-relay.example/v1
OPENAI_API_KEY=your-relay-api-key
OPENAI_MODEL=your-model-name
```

页面中的“记住密钥”会使用 Windows DPAPI 加密保存给当前 Windows 用户；密钥文件按 Base URL 哈希区分，旧版 DeepSeek 和中转站密钥文件仍可兼容读取。密钥不会写入供应商配置或课程数据库。密钥来源依次为显式命令行参数、本机记忆、当前接口对应的环境变量。请勿提交 `.env`、`data/`、`projects/` 或任何 API Key。

## 开发启动

```powershell
python -m streamlit run video_app.py --server.port 8503
```

在浏览器打开 `http://localhost:8503`。扫描本机视频文件夹，勾选需要的视频开始批量处理。完成后从“复习视频库”进入重点复习页。

### React 重点复习页

本地浏览器复习页与旧版 Streamlit 共存。通常直接使用根目录的三个启停入口；也可在终端统一控制：

```powershell
.\.venv\Scripts\python.exe scripts/start_frontend.py start
.\.venv\Scripts\python.exe scripts/start_frontend.py stop
.\.venv\Scripts\python.exe scripts/start_frontend.py restart
```

需要分别调试服务时才使用：

```powershell
python -m api.main
cd frontend
npm install
npm run dev
```

API 默认只监听 `127.0.0.1:8765`，前端默认地址为 `http://127.0.0.1:5173`。打开前端根路径进入视频库；`http://127.0.0.1:5173/demo` 使用显式 mock 数据验证复习交互。API 与旧版 Streamlit 共用应用的唯一视频库；`app_storage.py` 统一管理数据路径。

Windows 也可以先执行一次 `cd frontend; npm install`，之后直接双击仓库根目录的 `启动本地前端.cmd`，它会同时启动 API 和 Vite。

如果要关闭浏览器，让任务在终端中连续运行，可以使用独立批处理入口：

```powershell
python batch_process.py --videos "D:\课堂视频"
```

命令行默认使用页面保存的统一接口配置；尚未保存时使用 DeepSeek 官方 API 默认值。可以直接覆盖中转站地址和模型：

```powershell
python batch_process.py --videos "D:\课堂视频" --base-url "https://your-relay.example/v1" --model "your-model-name"
```

该命令使用中转站的本机密钥记忆或 `OPENAI_API_KEY`。还可用 `--max-tokens 4096`、`--token-parameter max_completion_tokens`、`--api-timeout 120`、`--no-json-mode` 覆盖参数；CLI 覆盖不会修改已保存配置。可用 `--mode high_recall` 选择扩展重点，默认是 `strict_exam_intent`。同一时间只运行一个批处理任务，避免多个任务同时争用 GPU 和 API 限额。

## 播放器降噪

播放器已支持可选的 GTCRN CPU 实时降噪，通过视频工具栏的波形按钮开启/关闭，不改原视频或转写结果。约 535 KB 的模型与原始 MIT 许可证内置于 `assets/gtcrn/`，随源码分发，无需额外下载模型。网页与桌面启动准备阶段会校验资源；首次开启按需加载，切换不暂停视频，异常时回到原声。可运行 `python scripts/prepare_gtcrn.py` 离线检查资源。部署与音频格式、缓冲延迟说明见 [播放器降噪](gtcrn-playback.md)。

## 本地 ASR 模型

管理页左侧和复习页“复习设置”中可选择模型，选择会保存到 `data/asr_settings.json`：

| 模型 | 运行方式 | 时间戳 |
| --- | --- | --- |
| Whisper turbo | faster-whisper / CTranslate2 | 原生字词时间戳 |
| Whisper large-v3 | faster-whisper / CTranslate2 | 原生字词时间戳 |
| Qwen3-ASR 1.7B | 独立 Transformers 工作进程 | Qwen3-ForcedAligner 0.6B 对齐 |
| FireRedASR2S（VAD + AED） | 独立 FireRedASR2S 工作进程 | AED 原生字词时间戳 |
| Paraformer-large（BiCIF） | 独立 FunASR / PyTorch CPU 或 CUDA 工作进程 | BiCIF 原生字词时间戳 |
| Dolphin 中文 small（CTC） | 独立官方 Dolphin / PyTorch CPU 或 CUDA 工作进程 | 官方已训练 CTC 字词对齐 |

- 不预先下载全部模型；下载中断可以重新准备，只有完整下载才标记为“已下载”。
- 所有可选 ASR 模型默认从 ModelScope 下载，不需要先连接 Hugging Face。Whisper / Qwen 使用同名仓库并逐文件校验大小和 SHA-256，同时检查 Qwen 分片索引；FireRed 使用官方 ModelScope 仓库。全部组件成功后才标记就绪。Whisper / Qwen / FireRed 仅在 ModelScope 失败时尝试 Hugging Face 备用源；两个源均不可达时显示失败原因，不自动配置代理或使用第三方 HF 镜像。
- 下载失败留下的部分缓存也可以在设置页清理。模型任务期间禁用重复提交，任务消息显示当前来源与文件；aria2 下载时准备百分比按已校验文件更新，大文件传输期间可能暂时不变。原始实机实验记录不随公开源码分发。
- Paraformer CPU 使用官方 BiCIF 时间戳增强权重和 FSMN-VAD，独立环境只安装 CPU 版 PyTorch，并屏蔽 CUDA。音频分块解码到临时 WAV，VAD 使用官方离线文件调用，只接收完整语音区间；结束静音阈值为 800 毫秒，最长语音段 30 秒在模型构建时设置。ASR 按片段长度稳定排序组批，不合并或重切原片段，每批实际处理最多 4 个语音片段，默认使用 16 个 CPU 计算线程；不加载标点、说话人或语言模型。识别后按原片段索引恢复字幕顺序和连续编号，字词时间戳映射回原视频时间轴，缺失或无效时拒绝生成字幕。基准脚本可用 `--batch-order time` 对照原时间顺序组批。模型 ID 为 `paraformer-large-cpu`，不是 Hugging Face 普通版 `paraformer-zh`。
- Paraformer 现在可以在设置页选择 CPU / GPU，默认仍为 CPU，原模型 ID 和权重缓存保持兼容。GPU 使用单独的 `paraformer-gpu` 环境（PyTorch 2.7.1 + CUDA 12.8），不覆盖 `paraformer-cpu` 环境。CUDA 未通过 GPU 环境中的实际张量分配与同步检查时，页面禁用 GPU，后端也拒绝保存 GPU 选择；GPU ASR 推理失败不会静默降级到 CPU。首次点击“准备 GPU 环境”只准备依赖，不更改已保存的设备。设备切换后工作进程重建，ASR 加载到所选设备，FSMN-VAD 固定在 CPU，以避免逐帧 CUDA 标量读取；权重、切分阈值和原生 BiCIF 时间戳保持不变。
- Paraformer GPU 默认参考 Qwen 按原生总显存选档，不使用当前空闲显存或共享显存：至少 3 GiB 使用 BS16，2 GiB 至不足 3 GiB 使用 BS8，低于 2 GiB 或容量检测失败时禁止 GPU 选择，CPU 默认 BS4。分档沿用 64 MiB 容量预留容差；2/3 GiB 显卡尚未实机验证。每个视频任务重新选档；仅真实 CUDA OOM 才按 BS16 → 8 → 4 → 2 → 1 有限降级，重试失败批次并保留当前任务的低档。BS1 仍失败时进入资源暂停，不切换设备或精度。模型加载 OOM 只清理并重试一次；显式传入的 BS 保留作为手动测试参数。暂停后继续会重新处理当前未完成的视频，尚未新增视频内持久化检查点。
- 接口：`PUT /api/asr/selection` 接受 `{"model_id":"paraformer-large-cpu","paraformer_device":"cpu"}` 或 `"gpu"`；模型状态返回 `device`、`gpu_available`、`gpu_runtime_ready`、`gpu_prepare_available` 和 `gpu_name`。可通过 `POST /api/asr/models/paraformer-large-cpu/prepare?device=gpu` 单独准备 GPU 环境。基准脚本 `scripts/benchmark_paraformer_cpu.py` 兼容 CPU 默认模式，也支持 `--device gpu --batch-size 8`；默认 VAD 使用 CPU，`--vad-device gpu` 仅用于 GPU ASR 下的性能对照。GPU 模式记录设备级利用率、设备级显存和本进程 PyTorch 分配器峰值，计时边界显式同步 CUDA，并分别审计 ASR/VAD 参数设备和实际编码器批次。单文件 VAD 的实际批次仍为 1，不随 ASR 的 BS 增大而并行处理内部计算块。
- 权重统一保存在 `models/asr/<模型标识>/`，Qwen 识别模型和时间对齐模型一起准备、一起清除。
- 磁盘空间不足时可在本机 `data/asr_storage.json` 配置 `{"storage_root": "D:\\codex-models\\classroom-agent"}`，将权重和 Qwen/FireRed 环境及依赖缓存放到该专用目录下。此配置不提交到 Git，不会移动已有文件；使用过其他位置的模型需回到原位置清除。目录必须是绝对路径且不能经过链接或联接。
- “模型缓存”中可选择清除对象并确认；正在下载或转写的模型不能清除。
- Qwen 可勾选“同时清除 Qwen 独立运行环境”，进一步释放依赖占用。共享 `.runtime/cache` 下载缓存和旧版 Hugging Face 全局缓存不在模型清除范围内。
- FireRed 使用独立 `.runtime/firered-asr2` 环境，直接调用官方 `FireRedAsr2System.process()`：FireRedVAD 检测语音、原生切段、AED 转写并合并回原视频时间轴，不再固定 55 秒切段。进入 FireRed 前，视频音频由 PyAV 分块解码并直接写入 16 kHz 单声道 PCM16 临时 WAV，不再把完整音频保留在 float32 数组中；输出仍保留原始 PTS 空洞，和旧版音频内容一致。沿用官方离线 VAD 默认参数（单个语音片段最长约 20 秒，VAD 推理按最多约 300 秒分块），ASR 单批一个片段，支持约 60 分钟及更长音频；LID 和标点模块不启用。
- FireRed 模型准备包含独立的 AED 和离线 `FireRedVAD/VAD` 权重，已有完整 AED 缓存会复用，只补下载 VAD（约 2.3 MB）；缺少任一组件都不会标记为已下载。进度显示音频读取、VAD 检测和逐段转写，原有停止按钮仍会终止独立 worker 并释放资源。
- FireRed 运行环境固定官方源码版本，GPU 使用 PyTorch 2.10/CUDA 12.8，需兼容的 NVIDIA 驱动。准备时同时检查特征提取和应用 worker 依赖；旧运行环境标记会在下次准备时更新。
- FireRed 在 GPU 上启用官方半精度模式（支持时 BF16，否则 FP16），CPU 保持 FP32；AED 字词时间戳仍开启。
- FireRed 默认选择 GPU；可在 React 或 Streamlit 的 ASR 设置中手动选择 CPU，设备选择会保存到本机配置。GPU 不可用时不会静默回退，需手动选择 CPU。CPU 使用 BS=3、同一份 AED/VAD 权重和字词时间戳，并禁用 CUDA；启动及逐批处理检查系统内存余量。选择 CPU 时会提示：60 分钟视频预计需要约 50 分钟、占用约 10 GB 内存，建议离线处理；这是估计值，实际结果随硬件和音频变化。
- FireRed 加载 ZIP 格式 PyTorch 权重时使用 `mmap=True`，AED 使用官方模型类创建空参数后以 `assign=True` 严格装载检查点，避免随机权重和检查点双份分配，降低 Windows 加载峰值内存；官方推理逻辑不变。
- AED 的 CTC 对齐输入单独转为 FP32，兼容 torchaudio 不接受 BF16 的限制；强制对齐失败时禁止官方平均时间戳回退，任务报错并保留原结果。
- Hugging Face 无法连接时，模型准备自动回退到 FireRed 官方 ModelScope 仓库；也可运行 `python scripts/prepare_firered_modelscope.py`。脚本复用经过 SHA-256 校验的已有 AED/VAD 文件，并使用项目附带 aria2 断点下载（无 aria2 时使用流式 HTTP）；只有全部必需文件校验通过才写入就绪标记。运行环境仍通过页面“准备模型”安装。
- FireRed 字幕保留原视频音轨延迟、时间空洞及文本空格/标点。有文本却缺少时间戳、时间戳无效或文本不匹配时会报错，不会生成虚假的字幕时间。
- Qwen 使用官方长音频 `transcribe()` 流程：先用 PyAV 解码原视频，再由 Qwen 内部按官方策略切分、批量识别、强制对齐并合并回原视频时间轴。显存不足 12GB 时识别模型用 GPU、对齐模型用 CPU，并保持单项批处理以控制峰值显存；至少 12GB 时允许两项批处理。没有可用 CUDA 时使用 CPU，CPU 运行较慢。
- Qwen 返回官方合并后的字词时间戳，应用层只负责保留原文标点、校验时间范围并分组为字幕段。
- 切换模型不会自动替换已有字幕。需要重新识别的视频可在复习设置或登记后的单视频面板中选择“重新转写”并确认。成功后原字幕和 AI/复习状态被替换，需重新筛选；失败时保留原结果。
- React 单视频页勾选“使用当前模型重新转写”后再开始或重试处理，会使用当前 ASR 模型重新识别并继续 AI 筛选。普通重试只复用字幕；AI 筛选失败时新字幕仍保留，再次重试无需重复转写。
- AI 失败但已有字幕的视频可在 React 视频库或单视频页打开“查看字幕”；页面明确显示 AI 重点尚未完成，不会修改失败状态或生成假重点。
- 模型加载只使用已准备的本地目录，不会在推理期间另外下载隐藏的模型。Qwen 工作进程启用 Hugging Face 离线模式。

React 单视频处理页同时勾选“期末考点”和“扩展重点”时采用高召回模式；只勾选“期末考点”时采用严格模式。关键词按逗号、分号或换行分隔并发送至后端。“排除的公式文本”按相同分隔符解析，忽略大小写与空白，以字面子串匹配排除对应 AI 输入字幕段，不执行正则表达式、不修改已保存的原始字幕。

命令行也支持选择、准备和清除模型，无需先打开页面：

```powershell
python batch_process.py --asr-model qwen3-asr-1.7b --download-asr-model
python batch_process.py --videos "D:\课堂视频" --asr-model qwen3-asr-1.7b
python batch_process.py --asr-model qwen3-asr-1.7b --clear-asr-model --clear-qwen-runtime
```

准备 Qwen 时，NVIDIA 机器默认安装 PyTorch 2.7.1 CUDA 12.8 运行包，需要兼容的驱动；其他机器使用 CPU 运行包。安装失败可查看 `.runtime/logs/qwen-install.log`，推理失败查看 `.runtime/logs/qwen-worker.log`。

Windows 加载 Qwen 权重时使用 safetensors 0.8.0 的 `pread` 读取方式，避免本机测试中出现的内存映射读取原生崩溃。此适配仅作用于独立 Qwen 进程加载权重期间。

## 一键启动原理

- 三个启停入口调用 `scripts/webui.ps1`，优先使用已有 `.venv` 或 `.runtime/venv`，再统一控制 API 和 Vite。
- 尚无 Python 环境时，启动入口调用 `scripts/bootstrap.ps1 -WebUI`，从 uv 官方地址下载便携 `uv`，再按照提交的 `uv.lock` 准备固定依赖。
- 默认将 Python、依赖、下载缓存和运行日志保存在 `.runtime`，该目录不会提交到 Git。配置外部 ASR 存储目录后，Qwen/FireRed 环境和其依赖缓存改用外部目录；主应用环境和日志仍保留在项目内。
- `scripts/start_frontend.py` 串行管理启动、停止和重启，验证服务及所属进程身份，并记住数据目录和端口；不关闭占用端口的无关程序。
- 网页地址只监听 `127.0.0.1`，不会向局域网公开应用。
- 删除 `.runtime` 后重新双击，可完整重建自动运行环境，不影响视频库数据。

## 视频库与存储

全应用固定使用一个视频库，课程通过视频库文件夹分类，不再提供工作区创建或切换。新安装默认在 `data/library` 保存数据库，已有安装沿用 `data/asr_storage.json` 中的 `library_data_dir`，避免丢失已有课程。

数据目录包含：

```text
project.db       # 视频元数据、字幕、提示词命中、AI 重点和复习状态
temporary_audio\ # 仅在个别视频直接解码失败时短暂存放 WAV
videos\         # WebUI 导入视频的持久视频库，不属于缓存
```

WebUI 单视频和批量导入会将视频移入视频库，复制校验成功并登记播放路径后才删除原文件。同名但内容不同的视频分别存放，避免覆盖；删除原文件失败时保留两份并提示，不影响视频库中的副本播放。正常情况下 `temporary_audio` 保持为空。

WebUI 批量导入支持包含多层子文件夹的目录，在视频库同步生成相同的文件夹层级，包括空子文件夹；视频库可逐层打开、通过路径返回上级，并在当前层新建文件夹。任意层出现非视频文件（包括隐藏文件）、符号链接或目录联接都会阻止本次所有目录的导入。整个目录树至少需要一个支持的视频；同时选择父目录及其子目录时只导入一次。选择目录只执行只读检查，点击开始后后端再次检查，再保存批次并逐个移入视频。批量导入的完全重复视频复用已有记录和字幕，保留原视频库归属、文件名和本次来源副本，并显示提示。按视频库文件夹提交的批处理也包含其全部子文件夹中的视频。

批量归档按规范化来源目录匹配：同一来源追加导入时复用原视频库文件夹，即使用户改过名称；不同来源重名时新建 `课程 (1)`、`课程 (2)`，不按名称合并。批次保存完整视频 ID 队列和逐项进度，刷新或重启后可恢复；“重试未完成视频”不再扫描原下载目录，已有字幕直接用于 AI 筛选，成功项不重复处理。

API 配置缺失或请求重试后仍出现连接、认证、限流、服务端等故障时，本批次暂缓后续 AI 请求，继续完成剩余 ASR，并显示“等待 AI 恢复”。用户修好 API 设置后手动重试未完成视频，使用最新 API 配置和原批次处理选项；不会后台探测或自动调用 API。单视频输出格式、证据校验或 ASR 失败仍单独记录，不触发全局 API 暂缓。命令行批处理调度不变。

设置中的“视频与音频存储目录”统一管理 `videos` 和 `audio` 子文件夹。检查并确认迁移后，复制校验已登记视频和音频缓存，再更新路径；已有字幕、考点和文件夹不变。旧安装在确认前继续使用原目录。详情见[目录迁移说明](storage-migration.md)。

体积较小的数据库、设置与加密凭据可通过“应用数据”统一迁入另一个父目录，使用 `library` 和 `config` 子文件夹。确认弹窗会明确提醒“修改应用数据目录后，软件会自动立即重启”；只有确认才执行，完成后页面自动重连，原数据保留为备份。视频、音频和 ASR 资源沿用各自的目录。

单视频页的“修改文件名”修改磁盘上真实的视频文件名，保留视频格式、视频 ID、字幕、考点、复习状态及用户调整的跳转时间；新视频在导入时使用该文件名。原项目内显示名称输入已隐藏。处理或暂停的任务存在时，禁止导入移动与重命名，防止任务路径失效。

## 数据与隐私

- 原视频、SQLite 数据库及 Whisper/Qwen 转写均保留在本机。
- 启用 AI 筛选时，带时间戳的转写字幕片段会发送至你选择的 DeepSeek 或中转站；中转站还可能将请求转发给其上游模型服务。
- 原视频和音频不会发送至 AI 服务。
- 使用 AI 筛选即表示带时间戳的转写字幕片段会发送至所选 AI 服务；页面不要求额外确认。请仅配置你信任的服务地址。

## 缓存与重复复习

- 已完成的字幕保存在项目数据库中，重新打开或失败重试不会再次转写。
- 视频库在前台每 15 秒同步视频列表、根目录和打开的文件夹；批处理中已完成的视频可直接复习，无需等待整批结束。重新进入视频库或切回前台会立即同步，后台和拖拽期间暂停定时同步，不刷新整页或检查 ASR 模型。
- 视频登记优先比较路径、大小和修改时间；文件未变化时不会重新读取整段视频计算哈希。
- AI 成功返回的字幕块按字幕内容、服务地址、模型、请求参数和判定模式隔离缓存，只补充未完成的请求。原默认 DeepSeek 配置仍兼容既有缓存；更换密钥不会改变缓存身份。
- 已掌握和已移除状态保存在项目数据库中。
- 播放位置保存在当前浏览器的本地存储中；下次使用同一浏览器打开同一项目视频时自动恢复。
- 修改视频内容会改变文件哈希，并按新视频处理。

请仅处理你有权处理的课堂内容，并在涉及学生、教师或第三方信息时遵守适用的隐私与学校规定。

## 中断恢复

如果程序、电脑或网络任务中断，管理页会显示“恢复中断任务”。将卡住的视频标记为失败后重新处理，已有字幕和成功的 AI 字幕块会被复用。不要重置仍在运行的任务。

## 开发与测试

### Dolphin 中文离线转写

设置页提供 `Dolphin 中文 small（CTC）`（`dolphin-small-cn`），实载参数约 421.5M。
CPU/GPU 环境相互独立；默认 CPU、BS8，CUDA 未通过实际分配检查时禁止选择 GPU。
CPU 计算线程自动设为 `max(1, min(16, 可用逻辑处理器数 - 2))`，不提供用户调参入口。
Windows CPU ASR 子进程使用低于正常优先级，主服务和 GPU 路由不变；这不是核心隔离或占用率保证。
使用官方 `small.cn` 权重、FP32、`attention_rescoring` 和 beam10，不使用 vLLM 或流式输出。
FSMN-VAD 固定在 CPU，最长 30 秒，保留原生短段；按长度稳定排序组批后恢复原时间轴。
编码器支持实际 BS8；官方 CTC 搜索和重打分仍含逐片段循环。
适配层保留完整批量编码结果并调用官方 CTC 对齐，修正官方重打分路径只传最后一段给对齐的入参问题，
不修改官方源码、不重复编码、不用平均时间戳。
GPU OOM 将实际批次减半重试，BS1 仍失败或系统内存不足则暂停，不静默切换 CPU。
GPU 默认启用项目适配层的 `host-search`：CUDA 整批计算 top-k 后集中搬到 CPU，
仍调用原样官方 CTC 搜索；重打分输出也集中搬运，减少逐标量同步。CPU 路径不变。
beam10、FP32、官方字词对齐和默认 BS8 均不变，官方源码及安装文件不修改。
基准脚本支持 `--dolphin-decoder official` 回退对照，`--dolphin-profile` 拆分计时，
`--seed 0` 固定特征提取随机种子。原始实机优化记录不随公开源码分发。
GPU 批次前内存余量不足时仅回收一次未使用缓存，再按原阈值复查；仍不足则暂停。

`PUT /api/asr/selection` 接受 `{"model_id":"dolphin-small-cn","dolphin_device":"cpu"}` 或 `"gpu"`。
`POST /api/asr/models/dolphin-small-cn/prepare?device=gpu` 只准备环境，不改变已保存设备。
性能测试使用对应环境的 Python 运行
`scripts/benchmark_nano.py <audio-or-video> --engine dolphin --device gpu --batch-size 8 --output <new-directory>`。
请用自己有权处理的输入运行基准脚本；原始课堂字幕和历史部署记录不随公开源码分发。

### Fun-ASR-Nano 离线转写

设置页提供 `Fun-ASR-Nano 2512`，CPU/GPU 分别使用独立环境，GPU 选项需实际 CUDA 检查通过。
权重使用 ModelScope 的 `FunAudioLLM/Fun-ASR-Nano-2512`，准备时校验带 86 个已训练 CTC 张量的 checkpoint。
FSMN-VAD 在 CPU 运行，单段最长 30 秒，原生短段保留；字幕保留模型原生 CTC 时间戳和原时间轴。

当前默认输入组大小为 6。FunASR 1.4.16 带 CTC 的官方路径在组内逐条执行，实际编码器 BS1，
因此不能把 BS6 理解为 6 段并行，也不沿用 Paraformer 的显存档位。
GPU OOM 依次尝试缩小输入组 6→3→1，BS1 仍失败则暂停；不自动切换设备或伪造时间戳。
恢复暂停任务时会重新转写当前未完成视频。

部署和实测见 `docs/nano_deployment_2026-10-13.md`。基准入口：

```powershell
& D:/codex-models/classroom-agent/.runtime/fun-asr-nano-gpu/Scripts/python.exe scripts/benchmark_nano.py <audio-or-video> --device gpu --batch-size 6 --output <new-result-directory>
```

```powershell
python -m pytest -q
```

GitHub Actions 会在 Windows 上运行测试。贡献说明见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

## 许可证

本项目采用 [MIT License](../LICENSE)。
