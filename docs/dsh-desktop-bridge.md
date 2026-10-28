# 单题 DSH 桌面桥接

## 本次边界

本模块只负责把用户保存的一道完整题目放入 DeepSeek Harness 原生草稿，
保存工作区与会话身份，再从题目返回原讨论。文字与多张裁片按来源顺序传入；
同一情景的多个小问留在同一个讨论中。模型选择、发送、解题、工具、历史和上下文
仍由 Harness 负责。本次不调用模型，不实现总结按钮、自动拆题、分类或检索。

目标版本是 Windows 桌面 `0.2.1-rc.3`。插件清单中的 `platform: web` 是
DSH 桌面共享渲染器的加载约定；Host 同时校验 Windows、desktop profile 与
Session Controller 精确版本，不能作为独立网页插件运行。

## 结构与持久化

- `integrations/dsh-desktop/` 是一个含 Host、原生界面客户端与 bundle patch 的插件。
  使用桌面自带的 Cordis 和 Remote 服务，不打包第二套运行时，不修改安装包。
- Python 通过有长度限制、凭据校验与超时的 Windows 命名管道请求插件。
  不增加独立 HTTP 监听器，不接管 Harness 启停。
- Host 在所选 Harness home 的 `classcue-bridge/` 保存连接文件及题目快照。
  Windows ACL 限制该目录；连接文件含本地配对凭据，禁止提交、分享或打印。
- ClassCue 在当前视频库数据库中独立创建 `dsh_bridge_connection`、
  `dsh_bridge_requests`。保存连接文件路径、不可变传输快照、请求编号和会话回执，
  不向 `study_questions` 添加字段。数据库迁移时这些表一并保留。
- 每题每个内容版本有默认讨论，可明确新建另一讨论；旧讨论可继续打开。
  文件首次选择的工作区由 Host 记忆，之后同文件的新题复用该工作区。

程序先持久保存请求与预留 Session ID，再调用原生创建接口，完成 Session flush
后才发布绑定，再由客户端准备草稿。断线重试复用请求和 Session ID。
对话不存在、已归档、工作区移除或实例身份变化会报错，不伪装成恢复而新建对话。

图片字节随题目传输，不依赖 ClassCue 临时 URL。原题快照保存后不变，
未发送草稿另存备份；客户端监听编辑并定期补存文字与图片，Host 重启时核对原生日志
是否已有用户消息。已有用户草稿发生冲突时不覆盖；用户原生发送后再次打开只导航。
客户端突然崩溃可能丢失尚未完成备份的最后一次编辑，原始题目快照仍保留。
带文件引用或斜杠命令等复杂草稿不保证完整语义恢复，首版覆盖文字和题图。

## 安装与连接

先关闭准备安装插件的那个 DSH 桌面 profile，再执行：

```powershell
.\integrations\dsh-desktop\install.ps1 `
  -DesktopExecutable '<DSH安装目录>\DeepSeek Harness.exe' `
  -HarnessHome '<用户选择的Harness home>'
```

安装器调用该桌面安装附带的 `dsh plugin --profile desktop add file:...`，
不使用系统中可能过旧的全局 CLI。只操作显式选定的 Harness home，
不终止任何已有进程；运行中的 profile 会被拒绝。插件更新也需重启该 profile。

开启对应 DSH 桌面版并完成其首次使用引导后，在 ClassCue 题目控件的“DSH 桌面连接”选择
`classcue-bridge` 文件夹或填写其中的 `connection.json` 路径，点击连接。
前端不读取或保存该文件中的凭据。随后点击“在 DSH 中讨论”，首次在 DSH
原生弹窗选择工作区，也可使用原生目录选择器登记其他文件夹。

普通安装配对到默认 home 时，讨论按钮通过 `dsh://open` 唤起桌面；
该链接仅负责唤起窗口，插件负责选择具体会话。自定义或隔离 profile 需要用户
显式开启对应桌面实例，不用全局协议误唤起另一实例。ClassCue 退出不关闭 DSH。

## 资料阅读模块的接入点

已接入主开发目录完成的手动资料阅读模块。该模块目前尚未提交到 `main`；
本 worktree 使用其相关源码快照，不修改主开发目录。后续合并应保留资料模块已有实现，
只叠加桥接注册、发行文件和以下题目侧栏接入：

```tsx
import { DshQuestionActions } from "../components/DshQuestionActions";

<DshQuestionActions questionId={question.id} contentVersion={question.version}
  scope={scope} active={active} />
```

组件使用现有桌面桥接检测，在普通浏览器中隐藏且不发请求。使用页面现有的
React Query provider；内部包含连接、开始讨论、明确新建、旧版本历史入口与状态。
所有缓存按视频库作用域隔离。隐藏工作台或收起侧栏时不挂载查询与回执轮询。
入口位于题目侧栏底部，长题干独立滚动；仅对未移除的已保存题目显示，
选题、编辑、导入或保存期间不显示，避免把当前草稿误当作旧版本发送。
不需要改题目存储格式。

`study_snapshot` 延迟读取 `StudyMaterialStore`，使用 `question`、`document`、
`region_image` 的约定：

- `question` 返回稳定 ID、document_id、version、题号与有序 regions。
- PDF/图片 region 使用保存的 bbox 获取真实裁片；Markdown 使用保存的 start/end，
  不附带课堂字幕，不让模型重写题干。
- 缺失题图且没有用户补充图时明确失败；不下载文档外部图片，不补造内容。
- 当前版本已有讨论时，直接使用保存的传输快照，不重新渲染 PDF。
- 资料模块尚未合入时返回“请先合入资料阅读模块”，其他视频功能不受影响。

桌面 API：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/dsh-bridge/connection` | 连接指定插件文件 |
| GET | `/api/dsh-bridge/status` | 实例、版本、客户端在线状态 |
| GET | `/api/dsh-bridge/workspaces` | 工作区列表 |
| POST | `/api/dsh-bridge/discussions` | 从已保存题目准备讨论 |
| GET | `/api/dsh-bridge/questions/{id}/discussions` | 含旧版本的讨论记录 |
| GET | `/api/dsh-bridge/requests/{id}` | 同步回执 |
| POST | `/api/dsh-bridge/requests/{id}/open` | 打开原讨论 |

这些路由在非桌面服务中返回 404。写接口不接受来自前端的任意题干，
只能通过保存的题目 ID 组装来源。错误响应含可读 `detail`，
`X-ClassCue-Bridge-Error` 提供稳定错误码。连接离线不会启动付费请求。

## 验证

```powershell
python -m pytest tests/test_dsh_bridge.py tests/test_release_manifest.py tests/test_api_request_boundary.py -q
npm test --prefix integrations/dsh-desktop
npm run check --prefix integrations/dsh-desktop
cd frontend
npm test -- --maxWorkers=2 --minWorkers=1
npm run build
```

自动测试只使用临时数据库、合成题目、假模型与真实本地测试管道，覆盖超时恢复、
内容版本、不同实例、重复点击、两题隔离、草稿保护、多图顺序、客户端租约和格式校验。
桌面实机调试使用独立 Harness home 和 Electron user-data-dir，不复制真实凭据。
已验证真实原生草稿显示文字及两张图片、重复传输不插入第二份、重启回到同一
Session 后恢复图片、切换第二题后可回到第一题。所有实机步骤均未发送模型请求。

资料模块接入后的自动测试进一步覆盖真实保存的 Markdown 与补图、跨页 PDF 裁片顺序、
版本变化、移除恢复、重启后旧讨论，以及实际阅读组件点击到桥接 API 的路径。
组件测试使用假桌面连接，后端测试使用假管道客户端。另通过
`tests/study_dsh_desktop.py` 完成真实 ClassCue WebView2 与 DSH Electron 桌面联合验收：
从原生资料页导入并保存跨页 PDF、将两张裁片放入同一草稿、重复打开不重复传入、
编辑时隐藏入口、Markdown 题干及小问一起传入、重开资料页、两题切换、
重启 DSH 后返回同一 Session 并恢复两张题图。检查图像和输入框可见且未被遮挡，
并人工复核截图，不能只以 DOM 中存在图片作为可见性证据。

该脚本创建一次性 ClassCue 安装及合成资料，只允许使用本 checkout `.runtime` 内的
Harness home 和 Electron user-data-dir。测试 profile 须先安装插件并完成 DSH 自身首次引导；
脚本不自动登录、充值或执行模型任务。结束后只停止本轮创建的进程。
本轮电脑操作工具不可用，使用项目已有的 CDP 方式连接真实桌面渲染器，
没有启动独立浏览器网页代替桌面。运行方式：

```powershell
python tests/study_dsh_desktop.py `
  --dsh-executable '<DSH安装目录>\DeepSeek Harness.exe' `
  --harness-home .runtime/dsh-home `
  --electron-profile .runtime/electron-profile
```

证据保存于 `.runtime/study-dsh-desktop/`，包括 `result.json`、ClassCue 资料截图、
DSH 原生草稿截图和重启后的截图。该目录不入发行包，原生日志可能含临时连接凭据，
不要提交或分享日志。验收范围止于未发送的原生草稿，不证明模型发送、解题质量或总结能力。

2026-10-28 同步主开发目录的连续跨页选区、文字边界吸附及连续阅读更新后：
后端相关测试 90 通过、1 跳过；前端完整测试 1124 通过；插件测试 9 通过；
生产构建与上述原生桌面联合验收通过。用户日常 DSH profile 和主开发目录均未修改。
