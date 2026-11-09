# Codex 可选配套：安装与更新

ClassCue APP 和 Codex 插件在同一仓库维护，插件位于
`integrations/codex-desktop/`，当前版本为 `0.8.0`。
插件不是 APP 的运行依赖。普通用户直接使用 APP 即可，无需安装 Codex、Node.js 或插件。

插件包含 19 个课程工具、4 个讨论与界面工具，以及两个学习 Skill。
能力与调用示例见[课程工具](codex-course-tools.md)；
原题和课堂片段的持久讨论见[桌面讨论](codex-desktop-bridge.md)。

## 安装仓库插件

需要 Windows、Codex Desktop，以及可在 `PATH` 中找到的 Node.js 24 或更高版本。
CLI 命令需要 `codex` 可执行文件。插件已附带预构建的 MCP 服务、讨论界面与第三方许可证，
安装和首次调用均不执行 npm 安装，也不自动下载模型。

通过 GitHub 注册本仓库的 marketplace，再选择安装：

```powershell
codex plugin marketplace add MatchaParfait/ClassCue-AI --sparse .agents/plugins --sparse integrations/codex-desktop
codex plugin add classcue@classcue
```

如果已下载包含本插件的仓库源码，也可以在仓库根目录执行：

```powershell
codex plugin marketplace add .
codex plugin add classcue@classcue
```

两种来源使用相同的 marketplace 名称 `classcue`，选一种即可。
仓库中的 `.agents/plugins/marketplace.json` 仅提供可安装条目，
不会随 APP 启动自动安装或启用插件。安装后重新打开 Codex，在插件列表中确认 ClassCue 已启用。
如果新装 Node.js 后找不到 `node`，完全退出并重新启动 Codex，让它重新读取 `PATH`。

这是仓库 marketplace 分发，不等同于已上架 OpenAI 官方公共插件目录。
GitHub 安装读取已推送的仓库版本；本地尚未提交或推送的修改只适合通过本地来源验收。

## 第一次连接 APP

1. 先启动 ClassCue，完成首次运行环境准备。
2. 在 APP 的“设置 → 讨论工具”中选择 Codex，再选择已存在的学习工作区并保存。
3. 在 Codex 新聊天中请求：“查看 ClassCue 当前处理设置，再列出可检索的课程。”

保存设置会在当前 Codex 数据目录的 `classcue/connection.json` 创建连接，
登记 APP 位置、Python 环境和允许读取的学习工作区，不安装插件或发送模型请求。
插件每次调用重新读取连接；首次保存后可直接重试工具，不需要重新安装。
切换工作区时保留旧工作区的讨论绑定。

默认数据目录为 `~/.codex`。自定义 `CODEX_HOME` 时，APP 和目标 Codex 必须使用同一个目录。
当前宿主不会默认把 `CODEX_HOME` 传给 MCP；仓库插件根据宿主提供的安装缓存位置识别所属目录。
未知缓存布局会明确报错，避免读取其他配置；这时可以使用下方的本机安装器。
APP 被移动或重新安装后，重新保存连接以更新路径。

未完成连接时，工具仍可被发现，但调用会明确提示保存 APP 设置；
不会猜测视频库，也不会先启动一个未登记的应用。
连接完成后的课程工具可以按需启动 ClassCue 本地服务；只有打开视频等操作才需要唤起窗口。

## 已使用个人来源时

旧的 `classcue@personal` 可继续使用本机安装器更新，无需迁移讨论或数据。
在 APP 或便携包的根目录运行：

```powershell
pwsh -File integrations/codex-desktop/install.ps1 -WorkspaceRoot '<学习工作区>'
codex plugin marketplace add '<个人 marketplace 根目录>'
codex plugin add classcue@personal
```

本机安装器需要 PowerShell 7、Node.js 24 及以上，以及已准备好的 APP Python 环境；
不再需要 npm。它只合并个人 marketplace 中的 ClassCue 条目，保留其他插件。
可以用 `-NodeExecutable` 指定 Node.js 路径，用 `-CodexHome` 指定 Codex 数据目录。
安装产生的绝对路径仅保留在本机，不写回公开源码。

如果改用仓库来源，先移除个人来源中的旧插件，再安装仓库版本，
避免同时启用两份同名课程工具：

```powershell
codex plugin remove classcue@personal
codex plugin add classcue@classcue
```

移除插件不迁移或删除 APP 的视频库和讨论快照。原连接文件位于插件缓存之外，
新来源继续使用同一份连接。更换来源不需要删除整个个人 marketplace。

## 更新

APP 更新与插件更新分别进行，已有 APP 自动更新不会静默安装或更新 Codex 插件。

首次升级到包含 `0.8.0` 配套文件的 APP 包时，旧版更新器的精确路径允许表
可能拒绝新增文件。此类旧安装需要手动下载并按 APP 升级说明安装新版，
不要绕过更新校验。新版已登记这些路径，后续相同路径的更新仍按正常兼容规则判断。
仅更新 Codex 插件缓存不受这项 APP 更新限制。

GitHub marketplace 来源：

```powershell
codex plugin marketplace upgrade classcue
codex plugin add classcue@classcue
```

本地仓库来源先更新本地源码，再执行 `codex plugin add classcue@classcue`；
`marketplace upgrade` 只用于 Git 来源，不适用于本地目录。
个人来源则先重新运行本机安装器，再执行 `codex plugin add classcue@personal`。
完成后重新打开 Codex，确认实际工具列表；仅修改源码不会更新正在运行的 MCP 进程。

## 使用场景

“帮我找数据库课里讲事务隔离的地方，读前后两分钟字幕，并看看第 18 分钟的 PPT。”

Agent 先查课程和视频，在指定范围内搜索字幕，再读取命中位置的上下文。
查看第 18 分钟时，先列出该时段已有 PPT 状态，再读取对应图片；
需要确认那一刻实际画面时，使用 `read_video_frame`。
整个过程读取已有资料，不重新运行 ASR，也不调用 ClassCue 配置的 AI 服务。
返回给 Agent 的字幕和图片会进入当前 Codex 对话。

## 开发与打包

日常修改可继续编辑插件的 `.mjs` 源码和 Skill。发布插件改动时同步更新
`package.json`、`package-lock.json`、两份插件清单与服务声明中的版本，再运行：

```powershell
npm ci --prefix integrations/codex-desktop --ignore-scripts --no-audit --no-fund
npm run build --prefix integrations/codex-desktop
npm run build:check --prefix integrations/codex-desktop
npm run check --prefix integrations/codex-desktop
npm test --prefix integrations/codex-desktop
```

`build.mjs` 从锁定依赖生成 `server.bundle.mjs`、`discussion.html` 和
`LICENSE.txt`、`THIRD_PARTY_NOTICES.txt`。这些文件随源码分发，保持干净安装无需 npm；
CI 重新构建并检查产物是否与源码一致，避免漏发旧服务或旧界面。
构建不嵌入本机路径、用户配置、视频或凭据。

仓库分发采用标准根目录 `plugin.json` / `mcp.json`，由宿主展开 `${PLUGIN_ROOT}`。
旧本机安装器保留 `.codex-plugin/plugin.json` / `.mcp.json` 兼容方式，
在复制目的地生成明确的 Node、服务和连接路径。
两条安装路径均应使用隔离 Codex 数据目录和合成资料验收，不调用付费模型。

插件运行文件同时列入 APP 的发行清单及精确更新路径允许表，
让便携包保留手动安装可选配套的能力。

打包格式与 marketplace 依据
[OpenAI 官方插件打包文档](https://developers.openai.com/plugins/build/plugins)和
[本地 stdio 打包说明](https://developers.openai.com/plugins/deploy/desktop-only)。
