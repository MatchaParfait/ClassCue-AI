# 开源分发、自动更新与公告

## 源码布局兼容性

Python 应用源码已收进 `classcue/` 包，发行清单显式列出包文件。
打包与更新助手都保留同一目录布局，不把包内模块展平复制。
旧版便携包若使用不允许 `classcue/` 路径的更新校验器，不能直接自动升级到此布局：
请解压完整新发行包并沿用既有数据位置，不要绕过签名或路径校验。
若需要支持旧版原地升级，须先单独发布允许新路径的过渡版本。
源码 checkout 的一键启动入口不变，且仍不允许自动覆盖源码。

## 用户端

- 支持 Windows 10/11 x64。GitHub Releases 的 `ClassroomVideoStudy-版本-windows-x64.zip` 是便携发行包，不是 EXE 安装器。
- 解压到可写的独立目录，双击 `启动课堂重点复习.cmd`。已包含前端，不要求 Node.js；首次仍需联网下载便携 Python 和锁定依赖，需要 WebView2。
- 设置 → 关于与更新：默认每天后台检查一次；启动后延迟 12 秒检查，不阻塞首屏。自动下载默认关闭；不会静默安装或强制重启。
- 更新检查只下载公开的签名 JSON，不发送视频、字幕、API Key、设备标识或统计数据。GitHub/CDN 仍能看到正常网络请求的 IP。
- 自动检查可关闭；仍可手动检查。离线失败不会阻止启动、播放或本地工作。未过期的已验证公告继续可读；到期的源/公告不展示。
- 顶栏“公告”查看公告；展开标记已读，支持全部已读。关闭“显示公告未读提醒”仅隐藏未读徽标，公告入口仍保留。
- 源码克隆/Code ZIP 没有签名安装清单，或存在 `.git` 时，永不原地覆盖。请自行拉取代码并同步依赖/构建，或换用正式发行包。

## 维护者首次配置

发行公钥在 `release_config.json`。此源码仓库不包含私钥；维护者须从安全备份取得与公钥匹配的私钥，存放在 Git 忽略的 `.runtime/release-signing/private-key.txt`，或使用受保护的环境变量。**不要提交、发送或公开私钥。**

1. 离线安全备份私钥。限制本地文件访问权限；Windows 上 `chmod` 不等于完整的 ACL 隔离。
2. GitHub 仓库 Settings → Secrets and variables → Actions → New repository secret，名称为 `RELEASE_SIGNING_KEY`，值为上述文件里的单行内容。
3. 提交代码、公钥、公告文件及工作流。确认 Actions 已启用，仓库允许工作流写 Releases；仓库须公开，客户端不携带访问令牌。
4. 在仓库 Actions Variables 中设置 `ENABLE_SIGNED_RELEASES=true`。新仓库默认不启用签名发布，避免尚未配置密钥就触发发布任务。
5. 运行一次 `Release And Announcements` 的手动工作流，建立 `update-feed` 预发行源；尚无正式包时也可以仅发公告。

新克隆通常不带私钥。已有发行身份时只能从安全备份恢复到上述私有路径或使用环境变量，不能重新 keygen 覆盖现有公钥。
全新分叉项目请修改仓库名和工作流仓库限制，清空公钥后自行运行 `python scripts/release.py keygen`，建立自己的身份。

## 发布新版本

1. 修改 `pyproject.toml` 的稳定版本号，例如 `0.2.1`，运行 `uv lock`，更新 `release/notes.md`。
2. 提交后创建与版本完全一致的 `v0.2.1` tag 并推送。
3. 工作流运行 Python/前端测试，构建前端，生成 ZIP 与签名 `app-release.json`，先上传草稿，完成后发布正式版本，最后刷新签名公告源。
4. 用户下次联网检查后看到新版本。紧急发布后可让用户手动检查；这不是实时推送，也不需要自建服务器。

发布失败请先检查 Actions 日志。不会覆盖既有正式版本附件；草稿中断时，检查/删除未完成草稿后重新运行，不要修改已经分发版本的 ZIP。
若本地只想打包验收（不会上传）：

```powershell
cd frontend
npm ci
npm run build
cd ..
python scripts/release.py build --notes release/notes.md
```

输出位于 `.runtime/dist`。源文件必须逐项列入根目录 `release_manifest.toml`，并已加入 Git 索引；另外加入 `frontend/dist` 的生产构建。不会自动收录整个 `scripts` 或 `docs`，也不包含内部指令、实验工具、测试、维护文档及截图。必要的上游许可证仍随包保留。

新增运行模块、worker 或资源时，须同步修改清单并运行 `tests/test_release_manifest.py`。清单缺项、重复、非法路径、未暂存或缺失文件都会阻止打包。`--include-untracked` 仅供本地验收清单中尚未暂存的文件，不会自动收录其他未跟踪内容，发布工作流不使用此选项。更新客户端的路径校验继续兼容旧清单；发布源文件选择独立于该校验。
可使用 `python scripts/release.py verify <ZIP文件路径>` 离线验证包内签名和所有程序文件。
开源源码及测试继续由 GitHub 仓库/Source code 附件提供。MIT 许可证、字体/模型的原始许可证随程序保留。

## 发公告

编辑 `release/announcements.json`，它是一个 JSON 数组。示例（请替换成真实日期及内容，不要直接把过期示例当公告发布）：

```json
[
  {
    "id": "notice-20261020-01",
    "title": "项目维护通知",
    "body": "这里是纯文本内容。\n支持换行，不支持 HTML 或脚本。",
    "level": "important",
    "published_at": "2026-10-20T09:00:00+08:00",
    "expires_at": "2026-11-01T00:00:00+08:00",
    "url": "https://github.com/MatchaParfait/ClassCue-AI"
  }
]
```

- `id` 使用字母、数字、`_`、`-`，每条唯一。发布新消息使用新 ID；复用 ID 修改内容不会重置已读状态。
- 必填 `id/title/body/published_at`；时间必须带时区。`level` 可为 `info` 或 `important`，`url` 仅允许 HTTPS。
- 可选 `min_version/max_version` 限定稳定版本范围；可选 `expires_at` 使公告自动下线。
- 最多 100 条、标题 120 字、正文 12000 字。清空为 `[]` 可撤下全部公告；移除单条会在下次成功检查后生效。
- 提交到 `main` 自动重新签名并上传 `update-feed/feed.json`。不需要发新 APP，也不需要给客户端 GitHub Token。
- 工作流每周刷新签名源有效期（30 天）；可手动运行。停用 Actions 超过有效期时，旧源会被客户端拒绝，需恢复发布后重新检查。

## 安装安全与恢复

客户端固定仓库、公钥和 HTTPS GitHub/CDN 域名；Ed25519 验证源及包内文件清单，SHA-256 验证下载。拒绝过期/倒退源、路径穿越、大小/数量超限、重复大小写文件、符号链接和目录联接。公告作为纯文本渲染。

安装需要桌面原生确认，网页不能直接执行；未保存设置、运行/排队/暂停任务及目录迁移均阻止安装。准备阶段阻止新任务/目录变更。只等待已核验所属服务退出，不按进程名称强杀其他程序。

原地升级仅用于依赖锁定结果和 `data_schema` 一致的兼容版本。比较依赖时忽略项目自身版本字段，不会因为正常版本递增误判；真正依赖变化或公钥变化需手动升级。数据结构有不兼容变化时必须递增 `data_schema`。

依赖变化的手动升级：先通过应用正常退出并确认后端停止，备份完整安装目录和外置数据目录；验证新 ZIP 后，将其中的程序文件解压覆盖到原安装目录（不要删除 `data`、`.runtime`、模型或外置库），再运行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap.ps1 -Desktop -PrepareOnly` 同步锁定依赖，最后正常启动。保留旧备份；不要同时运行两份程序。若数据格式变化，必须先阅读该版本迁移说明，不能假定覆盖即兼容。源码 `.venv` 由开发者自行同步，不使用便携环境的这条流程。

安装先验证全部旧文件未被修改，完整解包验证，再备份所有旧程序文件及 SQLite 一致性快照。仅替换清单内程序，不替换视频、配置、模型、`.venv` 或 `.runtime`。
文件替换失败自动恢复旧程序；替换中断后下次正常桌面启动使用保留的旧辅助程序恢复。新 API 成功启动后记为完成。
**这不是完整系统回滚**：新版本启动后故障不自动回退，数据库不会被自动还原，也不掩盖启动错误。新旧数据库格式必须兼容。

备份位于 `.runtime/updates/backups/<安装ID>/`，SQLite 快照为 `library-backup.sqlite3`；签名清单、暂存包、日志也在 `.runtime/updates`。
若新程序无法启动，完全退出桌面和后端后，从该目录取回旧程序文件（不要覆盖现有用户数据）。复杂情况请先备份完整安装目录和实际数据目录，再联系维护者。不要直接覆盖还在运行的文件。
备份暂不自动删除，确认稳定且自行另有数据备份后再清理旧备份，预留至少两倍程序包展开大小及数据库快照空间。

## 本地验收

运行 `python -m pytest tests/test_updates.py -q`、`cd frontend; npm test` 和 `npm run build`。
Vite 启动后运行 `node tests/frontend_updates.cjs`，使用模拟公告和原生桥验证 856/1316/1540/1920px 桌面宽度、取消焦点、安装错误、源码模式及暂停下载；不会真实安装更新或访问远程公告源。

私钥丢失不能伪造旧身份的更新。密钥泄漏时立即停用工作流、撤下公告源，安全生成新身份并要求用户手动升级至新公钥版本；当前实现不通过远程内容自动信任新公钥。
