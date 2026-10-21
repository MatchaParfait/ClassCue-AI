# 播放器实时降噪

React 视频播放器工具栏的波形按钮控制 GTCRN 降噪，默认关闭。只改变当前播放音频，不改变原视频、字幕、ASR 或磁盘中的转写音轨。开启时按钮高亮，悬停显示当前状态。

## 部署

```powershell
.\.venv\Scripts\python.exe scripts/prepare_gtcrn.py
```

依赖已加入 `pyproject.toml`、`uv.lock` 与 `requirements.txt`。模型内置于 `assets/gtcrn/gtcrn_simple.onnx`，原始许可证在同目录 `LICENSE.txt`，两者均随源码分发。网页和桌面启动准备流程都会执行上述资源校验；手动启动 API 时，播放器也会在首次加载模型前校验资源。首次安装 Python 与依赖仍沿用原准备流程，降噪模型本身无需联网下载。

准备脚本只读取和校验内置资源，不下载、不复制到用户数据目录。资源缺失或损坏时，启动准备会明确报错；重新获取完整项目即可修复。旧的 `data/models/gtcrn/` 副本不再读取，不会自动删除。运行时不依赖实验目录，也不依赖 8787/8788 服务。权重固定在官方提交 `502ebfab64da7c4a9af78dcb9c6ceef1ebb01c73`，校验 Git blob `015454eef64bd4328e6ff4946c231d4a4a632e93`，535,190 字节。来源与 SHA-256 记录在 `assets/gtcrn/README.md`。上游为 https://github.com/Xiaobin-Rong/gtcrn ，采用 MIT 许可证。

## 处理路径

- 第一次使用声道或降噪功能时，共用一个 16 kHz Web Audio 上下文及一个视频媒体源。此后关闭降噪保留 16 kHz 双声道旁路，不是恢复全频原生解码路径；没有使用任何音频工具时仍是浏览器原生音频。
- 先应用声道选择，再将选中音频平均为单声道送至本机 `/api/audio/denoise` WebSocket。降噪结果复制至左右输出。音量、静音和倍速仍由原视频元素控制。
- CPU-only ONNX Runtime，每 16 ms 处理 256 个样本，FFT 512，周期性平方根 Hann 窗及带缓存 overlap-add，与已验证的实验版 GTCRN 算法相同。无需 GPU。
- 服务按需加载并共用只读模型，每个连接的网络缓存、STFT 状态独立；最多 4 个会话。关闭降噪释放连接并停止推理，共享权重保持热加载，便于再次开启。
- 切换不暂停、重载或定位视频。初始化及预缓冲期间保留原声，随后约 20 ms 交叉淡入；关闭时淡回原声。不会为了缓冲而暂停视频。
- 开启时声音有 16 ms 算法延迟、分帧等待、约 32 ms 预缓冲，以及网络/设备延迟。没有额外延迟画面；实际声画偏差未标定，不能宣称音画完全同步。
- 暂停、跳转、重播、更换视频、倍速或声道改变时重置序号和网络缓存，丢弃旧时间轴数据。暂停时不发送音频帧。
- 在途最多 24 帧，输出缓存最多 4096 样本。服务断开、初始化超时、积压、持续欠载或溢出时自动回到原声并显示可重试提示，不中断视频播放。关闭播放器时释放上下文、连接、监听器与定时器。
- 只接受来自 localhost/127.0.0.1/::1 HTTP(S) 页面的连接。没有音频上传到外网，不预解码整段视频，不设课堂时长限制。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_gtcrn_assets.py tests/test_playback_denoise.py tests/test_api.py
cd frontend
npm test
npm run build
node scripts/verify-gtcrn-playback.cjs http://127.0.0.1:5175
```

浏览器脚本需要 Playwright 与 Edge，以及实验模块的 `static/demo.mp4`。可通过 `PLAYWRIGHT_MODULE` 指定 Playwright 安装目录，`GTCRN_SOAK_SECONDS` 增加循环播放验证时长。它只在 `/demo` 页面替换示例视频，实际音频经过主项目 GTCRN 接口，不改用户数据库或视频。覆盖真实非零推理/播放、连续开关、声道、倍速、seek、暂停/恢复、服务断开回退及重试。结果和桌面截图在 `.runtime/gtcrn-playback/`。

2026-10-14 本机验证：前端全量 286 项通过，构建通过；后端降噪/API/启动与生命周期相关 102 项通过。后端全量另有 8 项既有失败，涉及视频库 SQL 查询次数与 FireRed/Qwen 批量恢复测试；在独立进程内移除本次新增的 API 降噪导入和路由后，同样的 8 项仍失败，没有改动这些业务功能。

主项目推理与实验版对同一组 200 帧随机 PCM 的输出最大绝对差为 0。最终浏览器流程另加 60 秒循环播放，共收到 4042 个推理输出帧（4021 个非零帧），浏览器错误为 0，遥测欠载为 0；声道和降噪只创建一个媒体源。桌面 1440/1100 宽度截图检查通过。

这是接入与播放验证，不是中文课堂质量评测，也不是一小时连续播放耐久测试。
