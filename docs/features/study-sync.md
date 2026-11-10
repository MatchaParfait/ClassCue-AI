# 学习修改有线同步

Windows 在自己的视频库中保存同步身份、共同版本和事务回执，通过既有 WPD/MTP
通道与已配对平板交换纯数据。两端独立实现，不共享源码、数据库或构建目录。

## 使用

1. 先通过“连接平板”发送课程，使两端具有相同视频和字幕。
2. 在同一窗口选择已传送的视频或文件夹，点击“关联并同步所选”，在平板确认首次关联。
   考题通过新同步协议补入平板，旧课程学习包仍为 v1。
3. 平板离线新增、修改、移除或恢复考点与视频考题。
4. 再次连接并打开两端连接页面，点击“同步已关联视频”或平板的“同步学习修改”。
5. 接收端检查新增、移除和其他修改。有冲突时对照电脑、平板内容，逐条选择再确认。

电脑其他视频的修改可以在同一次连接中下发。已有视频不重传媒体。
记录包括标题、备注、原始字幕引用、定位、重要程度及可恢复移除状态。
不交换播放进度、题图、PPT、模型、密钥或磁盘路径。

已关联视频拒绝旧整包覆盖、覆盖备份恢复及字幕重置，避免丢失同步身份。
首次关联等待确认时也阻止这些操作。同一视频当前只支持关联一台电脑。
尚未关联视频的旧覆盖使用完整资料摘要检查预览是否过期，包含考题和已移除条目。
预览后本机资料变化会要求重新确认，不能把更新后的摘要静默当作用户已经批准。
普通重新筛选保留已有同步记录，后续 AI 新结果仍可追加。
视频及其学习资料移入回收站再撤销时，共同版本一并恢复。

## 数据合同

格式名 `classcue-study-sync`，版本为整数 `1`。文档严格包含
`format,version,video_sha256,duration_ms,transcript_digest,records`。
视频摘要为完整 SHA-256，时长 1 至 3,600,000 毫秒，记录最多 20,000 条，
文档最多 4 MiB，签名包装最多 8 MiB。设备通过 `study-sync-v1` 能力声明识别新版本。

记录严格包含：

```text
id,anchor,kind,origin,mode,scheme_name,scheme_revision,start_ms,end_ms,
title,note,source_quote,trigger_phrase,confidence,user_start_ms,importance,removed
```

`id` 为稳定的小写 UUID，与数据库行号分开。`kind` 为 `point/question`，
`origin` 为 `analysis/manual`。题目模式为 `question_extraction`，其他模式为
四个既有内置模式或 `custom_` 标签。时间、文字和重要程度遵守学习包边界。
自定义方案只带名称与版本，不带提示词或可执行配置。
内置方案在同步文档中统一使用固定名称和版本 0，不改变本机 AI 分析版本。
合并兼容旧文档的内置显示名称与版本差异，自定义方案继续以名称、版本区分。
同步手动考题不会将本机 AI 提取状态标为完成。

`anchor` 是 `kind,start_ms,end_ms,trigger_phrase,scheme` 的规范 JSON SHA-256。
内置 scheme 是模式，自定义 scheme 是 `custom:<名称>:<版本>`。
首次由电脑发送 bootstrap，平板只对唯一一致 anchor 建立对应关系，采用电脑同步 ID，
保留自己的数据库行号。重复 anchor 或首次存在不同内容时，不能静默猜测。
同一自定义方案只有部分条目匹配时，方案映射同时应用于未匹配的本机条目，
这些条目的身份和内容继续保留。

字幕摘要为按 `start_ms,id` 排序的 `id,start_ms,end_ms,text` 数组的规范 JSON SHA-256。
规范 JSON 按键排序，UTF-8 编码、无多余空白，十进制去除尾零，负零为零，
U+2028/U+2029 使用转义。

## 合并和 USB 确认

每设备、每视频保留上次共同文档。请求基线摘要必须吻合，首次为空。
共同版本、当前电脑版本和对端版本进行三方逐字段合并。
不同字段独立修改自动合并，同字段不同修改需选择；
移除与正文修改相遇时，按整条记录选择。缺少记录不能解释为删除，
删除必须保留 `removed: true`。

请求使用独立目录 `sync/<请求 UUID>/`，继续使用配对 P-256 签名和接收方校验：

| 文件 / 类型 | data 字段 |
| --- | --- |
| `request.json` / `sync_request` | `bootstrap,base_digest,document` |
| `receipt.json` / `sync_receipt` | `request_digest,document` |
| `ack.json` / `sync_ack` | `request_digest,document_digest` |
| `rejected.json` / `sync_rejected` | `request_digest,message` |

请求摘要针对请求 data 的规范 JSON。发出前持久保存不可变请求。
确认时重新检查预览后的本机版本，记录、共同版本及回执在同一个 SQLite 事务提交。
相同 ID、相同请求返回原回执；不同内容复用 ID 拒绝。任务日志落后于资料事务时，
下次从事务回执恢复，不重新应用旧修改。

请求方将发出时文档、现在的本机文档、接收回执再做三方合并，
保护等待期间的新编辑；有冲突继续等待选择。回执已应用后发送 ack，
接收方收到吻合 ack 才显示同步完成。断线后重发原请求或原回执。
两端同时发起同视频请求时，较小 UUID 胜出，另一请求退让；
胜出请求的完整快照仍合并双方修改，不依赖设备时钟。

## 验收

2026-11-10 在 OPPO Pad 3、Android 16 和 Windows 上，以独立 QA 应用、
隔离视频库和专用 QA USB 目录完成真实 MTP 测试：

- 两段可播放 H.264 合成视频、字幕经既有课程传输进入平板，考题经首次同步补入。
- 平板修改 AI 考点、移除考点、新增手动考点和考题、移除考题回传电脑。
- 同次交换接收电脑另一视频的移除及新增考题，逐视频比较两端完整文档一致。
- 同字段冲突在 Windows 显示后明确采用平板版本，两端一致。
- 恢复移除条目、删除共享回执文件、重建 Windows 传输管理器后继续确认，
  稳定 ID、恢复状态与条目数量保持一致。

生产通道为 WPD/MTP 与 SAF。ADB 仅安装 QA 包、运行仪器测试和设备诊断。
验收未读取个人课程库。QA 用 `WpdTransport(qa=True)` 与 QA 构建的
`ClassCue-Link-QA` 目录，正式目录仍为 `ClassCue-Link`。

复现 Windows 侧：在准备好对应 QA 仪器测试的授权设备上运行
`python -m tests.usb_study_sync --directory .runtime/qa/study-sync-<新名称>`。
`tests/frontend_device_study_sync.cjs` 使用合成 HTTP 状态验证 Windows 界面，
不作为 USB 验收替代。断线模拟是回执丢失及传输管理器重建，
未实际拔线、重启平板或进行多厂商压力测试。
