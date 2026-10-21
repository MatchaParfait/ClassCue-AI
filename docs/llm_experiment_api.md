# LLM 考点提取实验接口

> 本文为第一版历史说明。实验请求参数及执行流程已更新，请使用
> 运行本地 API 后查看 `/docs` 中的当前接口；自定义提示词见
> [高级召回提示词](advanced-recall-prompts.md)。先预览并人工审查，再传确认快照执行。
> 下文旧版 strategy 参数不再用于新实验；正式视频处理接口没有改动。

本实验接口面向“先 ASR、后反复比较 LLM 方案”的调试流程。ASR 字幕保存在项目
SQLite 数据库中；实验只读取已保存的字幕，不会重新转写视频，也不会删除其他实验的
考点结果。

## 1. 先完成一次 ASR

启动本地 API：

```powershell
python -m api.main
```

注册视频：

```powershell
curl.exe -X POST http://127.0.0.1:8765/api/videos/register `
  -H "Content-Type: application/json" `
  -d '{"path":"D:/videos/lesson-01.mp4"}'
```

返回的 `video_id` 用于纯 ASR 转写。该接口不会调用 LLM：

```powershell
curl.exe -X POST http://127.0.0.1:8765/api/videos/1/transcribe `
  -H "Content-Type: application/json" `
  -H "Content-Type: application/json" `
  -d '{}'
```

任务结束后可确认字幕已经保存：

```powershell
curl.exe http://127.0.0.1:8765/api/videos/1/subtitles
```

## 2. 创建一个独立实验

实验只接受已有 ASR 的视频：

```powershell
curl.exe -X POST http://127.0.0.1:8765/api/videos/1/experiments `
  -H "Content-Type: application/json" `
  -d '{
    "name":"严格考点-无关键词",
    "strategy":"strict_exam_intent",
    "filter_formula_noise":true
  }'
```

可用的实验参数：

- `strategy`: `strict_exam_intent`（少而准）或 `high_recall`（高召回）。
- `keywords`: 自定义词列表；填写后自动启用关键词预筛选。
- `use_keyword_prefilter`: 是否只把关键词附近的片段交给 LLM。
- `filter_formula_noise`: 是否过滤公式/低信息片段。
- `filter_formula`: 额外排除词，多个词可用逗号、分号或换行分隔。

每次提交都会生成新的 `experiment_id`，即使参数相同也不会覆盖旧实验。
返回的 `job_id` 可通过原有任务接口查看进度：

```powershell
curl.exe http://127.0.0.1:8765/api/jobs/JOB_ID
```

## 3. 比较实验

列出某个视频的全部实验：

```powershell
curl.exe http://127.0.0.1:8765/api/videos/1/experiments
```

读取单个实验及其结果：

```powershell
curl.exe http://127.0.0.1:8765/api/experiments/EXPERIMENT_ID
curl.exe http://127.0.0.1:8765/api/experiments/EXPERIMENT_ID/markers
```

每个实验的结果使用独立的内部分析模式保存，因此可以同时保留和比较：

1. 严格考点；
2. 高召回重点；
3. 高召回 + 关键词预筛选；
4. 不同降噪规则。

## 4. 建议的实验矩阵

先选 2～3 个代表性视频完成一次 ASR，再固定字幕版本，只改变一个变量：

| 实验 | strategy | 关键词预筛选 | 降噪 | 目的 |
|---|---|---:|---:|---|
| A | `strict_exam_intent` | 否 | 是 | 低误报基线 |
| B | `high_recall` | 否 | 是 | 高召回基线 |
| C | `high_recall` | 是 | 是 | 测试关键词是否减少噪声 |
| D | `strict_exam_intent` | 否 | 否 | 测试公式/低信息过滤影响 |

每个实验完成后，按“准确率、漏检率、重复/噪声数量、单视频耗时”进行人工抽样比较。
选定方案后，再把该方案接回正式项目流程。
