# ASR 模型准备进度与预计等待时间

实施日期：2026-10-16，香港时间。

## 已接入范围

| 模型 | 权重下载范围 | 首次部署的环境反馈 |
| --- | --- | --- |
| Whisper turbo | 识别权重与配置 | 检查已有基础运行环境 |
| Whisper large-v3 | 识别权重与配置 | 检查已有基础运行环境 |
| Qwen3-ASR 1.7B | 识别权重各分片、ForcedAligner 权重与配置 | 独立 Python、PyTorch、推理依赖与导入检查 |
| FireRedASR2S | 官方 AED 和 VAD 的全部必需文件 | 独立 Python、PyTorch、推理依赖与导入检查 |
| Paraformer-large | 固定 v2.0.4 的 BiCIF 与 FSMN-VAD | CPU / GPU 对应独立环境与设备检查 |
| Dolphin 中文 small | 官方 small.cn 权重与 FSMN-VAD | 固定版本源码、独立环境、依赖与设备检查 |

保留已下架 Nano 的底层兼容路径；它也使用共享进度与下载逻辑，没有重新开放前端入口。

模型首次准备、缓存删除后重下载、部分下载失败后重试使用相同流程。已经完整校验的文件可复用，未完成文件可续传；Dolphin / Nano 可复用经过 SHA-256 校验的同仓库 Paraformer VAD。

## 用户看到的反馈

1. 检查已有环境与模型。
2. 安装运行环境：显示具体步骤、步骤序号、可识别的依赖包名和本步骤耗时。
3. 获取文件清单：显示当前仓库；总大小尚未确定时使用不定进度。
4. 下载模型权重：显示组件、当前文件、整体字节进度、下载速度和数字化预计剩余时间。
5. 校验模型文件：显示当前文件，以及可取得的校验字节进度。
6. 完成模型准备：所有必需文件及模型结构验证成功后才发布就绪清单。

示例：

```text
下载模型权重                                      65%
语音识别权重 · ModelScope
model-00001-of-00002.safetensors
650.0 MB / 1000.0 MB                         5.0 MB/s
权重下载预计还需约 1 分 10 秒，随后进行校验。
```

统一组件用于设置页模型卡片、任务抽屉，以及处理视频过程中自动准备模型的单视频和批量处理页。顶栏使用当前准备阶段及该阶段的百分比；模型任务不再显示固定的“准备中 5%”。

进度按阶段解释：下载百分比代表所需权重文件的字节完成比例，安装步骤百分比代表已完成的步骤比例，不代表已消耗的时间比例。

## 预计时间的含义

- 下载估时覆盖本模型剩余的权重传输，不包含随后的校验或尚未执行的其他阶段。
- 使用最近约 12 秒的实际接收字节量估算速度，取得至少 2 秒样本后给出 ETA；续传缓存不计入新增下载速度。
- 切换文件、服务器忽略 Range 后重新开始时重置速度样本。
- aria2 使用本机鉴权 RPC 的 `completedLength`，不以可能预分配或稀疏的 `.part` 文件长度推算下载量；下载结束后关闭 RPC 下载进程。
- 安装依赖、创建环境、结构验证等阶段没有可靠的时间分母，显示实际步骤和“暂时无法可靠估算”。不会把步骤比例换算成时间倒计时。
- 10 秒没有接收新字节时，标记等待网络并撤下速度与 ETA。前端超过 8 秒未取得新事件，或轮询失败时，也撤下估时并说明连接状态。
- 安装、独立验证子进程每秒上报存活状态；超时、取消和失败会结束所属子进程，保留可重试的缓存。

## 实现与兼容

- `preparation_progress.py` 提供结构化阶段事件、下载速度估计、安装命令监控和验证子进程事件转发。
- `scripts/prepare_asr_modelscope.py` 统一各引擎的 ModelScope 文件清单、固定版本、大小和 SHA-256 校验、分块传输与 aria2 进度。
- `scripts/prepare_asr_huggingface.py` 在备用源逐文件下载时使用同一套进度；Hub 工作线程回调保留任务事件上下文。
- 下载网络依赖在应用主环境执行，独立推理环境继续承担原有模型验证；不要求用户为已有推理环境额外安装 HTTP 客户端。
- 保留原有模型仓库、设备选择、权重校验和源切换策略；取消操作直接传播，不触发备用源下载。
- `processing_jobs` 增量添加可空的 `preparation_json`，旧任务通过 API 返回 `preparation: null`。已有课程、字幕和模型选择不变。
- 准备和清理任务的请求记录包含模型 ID，模型状态提供 `active_job_id` / `active_job_kind`；前端刷新后恢复轮询，完成后清除关联。Whisper 任务恢复时会展开所在区域。
- `preparation.active` 在模型准备结束后关闭，避免转写和 AI 筛选阶段继续显示旧下载进度。
- 保留原有任务 `progress` 字段，新增 `preparation` 包含阶段、文件、组件、字节量、速度、ETA 和更新时间。
- 减少动态效果模式下停用不定进度条的位移动画及宽度过渡；阶段文字和数值照常更新。

## 验证记录

- 前端全量：408 项通过；生产构建通过，仍有原有的 bundle 大小提示。
- 后端相关回归：212 项通过，1 项因本机符号链接权限跳过。
- 全部后端 `tests/`：1335 项通过、1 项跳过、8 项失败。之后新增的生命周期及取消测试也已纳入上述相关回归。
- 8 项失败使用独立进程加载本次修改前的 HEAD 模块复测，全部复现：5 项已有视频库查询次数断言，3 项已有 Qwen / FireRed 批量恢复断言。没有为此覆盖工作区文件。
- 原有 `tests/frontend_workbench.cjs` 已执行，在“更多 → 打开演示”的旧断言处停止。当前 HEAD 已移除该入口，此处未改动产品或放回演示入口来迎合旧测试。
- 新增专项浏览器脚本 `tests/frontend_preparation_progress.cjs`：使用隔离 API 验证六个模型的各阶段、数字估时、实时数值变化、刷新恢复、连接中断与重连、网络停滞、完成后的按钮恢复、顶栏与任务抽屉；电脑窗口覆盖 856 / 1316 / 1540 / 1920px。
- 真实本地限速 HTTP + 随应用提供的 aria2 测试：验证中间字节进度、传输完成后退出及最终文件内容，防止仅按文件长度或文件个数计算进度。
- 逐模型模拟官方元数据与真实文件哈希，验证冷缓存、完整文件复用、清理后重下载，以及两个下载源的文件级事件。
- 2026-10-16 对六个在用模型的 10 个组件做真实联网只读元数据核验：所需文件齐全，文件大小、SHA-256 和具体提交均可取得。该检查不等同于将六个模型的 GB 级权重全部重新下载。
- 没有删除用户模型或发起付费模型调用；服务更新后六个模型仍为已下载，旧准备任务可以正常读取。
- 原始 `frontend/src/theme.css` 的 SHA-256 保持为 `95E0A7971C3E3BB11C327E674B3667387B537915CCB2AADB1802A1B9C811D12B`。

日志和证据：

```text
.runtime/preparation-final-backend.log
.runtime/preparation-frontend-final.log
.runtime/preparation-build.log
.runtime/preparation-backend-all.log
.runtime/preparation-baseline.log
.runtime/preparation-browser.log
.runtime/preparation-modelscope-metadata.json
.runtime/preparation-progress-qa-*/checks.json
.runtime/preparation-progress-qa-*/downloading-{856,1316,1540,1920}.png
.runtime/preparation-progress-qa-*/task-drawer.png
```
