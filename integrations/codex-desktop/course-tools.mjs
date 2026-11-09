import { readFile, stat } from "node:fs/promises";
import { isAbsolute, join } from "node:path";
import { spawn } from "node:child_process";

const string = { type: "string", maxLength: 120 };
const id = { type: "integer", minimum: 1, maximum: Number.MAX_SAFE_INTEGER };
const seconds = { type: "number", minimum: 0 };
const pagination = { limit: { type: "integer", minimum: 1, maximum: 50, default: 20 },
  offset: { type: "integer", minimum: 0, default: 0 } };
const course = { course_id: { type: "string", maxLength: 100,
  description: "list_courses 返回的课程 ID；省略则为全库。" } };
const resultSchema = { type: "object", properties: { library_id: { type: "string" } },
  required: ["library_id"], additionalProperties: true };
const coverage = { type: "object", properties: {
  video_count: { type: "integer", minimum: 0 }, with_subtitles: { type: "integer", minimum: 0 }, missing_subtitles: { type: "integer", minimum: 0 },
}, required: ["video_count", "with_subtitles", "missing_subtitles"] };
const nullableOffset = { type: ["integer", "null"], minimum: 0 };
function pageSchema(properties, required, extra = {}, extraRequired = []) {
  return { ...resultSchema, properties: { ...resultSchema.properties,
    total: { type: "integer", minimum: 0 }, items: { type: "array", maxItems: 50,
      items: { type: "object", properties, required } },
    has_more: { type: "boolean" }, next_offset: nullableOffset, ...extra,
  }, required: ["library_id", "total", "items", "has_more", "next_offset", ...extraRequired] };
}
const timeRange = { type: "object", properties: { start_seconds: seconds, end_seconds: seconds },
  required: ["start_seconds", "end_seconds"] };
const transcriptItem = { segment_id: id, start_seconds: seconds, end_seconds: seconds, text: { type: "string" } };
const retrievalSchemas = {
  list_courses: pageSchema({ course_id: { type: "string" }, name: { type: "string" }, path: { type: "string" },
    ...coverage.properties }, ["course_id", "name", "path", ...coverage.required]),
  list_videos: pageSchema({ video_id: id, name: { type: "string" }, duration_seconds: seconds,
    has_subtitles: { type: "boolean" }, slide_count: { type: "integer", minimum: 0 },
    play_url: { type: "string" } }, ["video_id", "name", "has_subtitles", "slide_count", "play_url"],
  { coverage }, ["coverage"]),
  search_subtitles: pageSchema({ video_id: id, video_name: { type: "string" }, ...transcriptItem,
    play_url: { type: "string" }, match_count: { type: "integer", minimum: 1 },
    context_range: timeRange, context: { type: "array", maxItems: 5, items: {
      type: "object", properties: transcriptItem, required: Object.keys(transcriptItem) } },
    context_truncated: { type: "boolean" } },
  ["video_id", "video_name", ...Object.keys(transcriptItem), "play_url", "match_count", "context_range",
    "context", "context_truncated"],
  { coverage, total_videos: { type: "integer", minimum: 0 }, total_matches: { type: "integer", minimum: 0 },
    queries: { type: "array", items: { type: "string" } }, match_type: { const: "literal" },
    match_mode: { enum: ["any", "all"] }, empty_reason: { type: ["string", "null"] } },
  ["coverage", "total_videos", "total_matches", "queries", "match_type", "match_mode", "empty_reason"]),
  read_transcript: { ...resultSchema, properties: { ...resultSchema.properties,
    video_id: id, video_name: { type: "string" }, has_subtitles: { type: "boolean" },
    transcript_version: { type: "string" }, items: { type: "array", maxItems: 100, items: {
      type: "object", properties: transcriptItem, required: Object.keys(transcriptItem) } },
    has_more: { type: "boolean" }, next_offset: nullableOffset, play_url: { type: "string" } },
  required: ["library_id", "video_id", "video_name", "has_subtitles", "transcript_version",
    "items", "has_more", "next_offset", "play_url"] },
  list_slides: pageSchema({ slide_id: id, start_seconds: seconds, end_seconds: seconds,
    visible_from_seconds: seconds, visible_until_seconds: seconds, representative_seconds: seconds,
    is_preferred: { type: "boolean" }, image_hash: { type: "string" } },
  ["slide_id", "start_seconds", "end_seconds", "visible_from_seconds", "visible_until_seconds",
    "representative_seconds", "is_preferred", "image_hash"],
  { video_id: id, extracted_slide_count: { type: "integer", minimum: 0 } }, ["video_id", "extracted_slide_count"]),
  read_analysis: pageSchema({ marker_id: id, analysis_mode: { type: "string" }, start_seconds: seconds,
    end_seconds: seconds, title: { type: "string" }, note: { type: "string" }, source_quote: { type: "string" },
    source: { enum: ["manual", "analysis"] }, text_truncated: { type: "boolean" }, jump_seconds: seconds,
    importance: { enum: ["standard", "important", "critical"] },
    review_status: { type: "string" }, play_url: { type: "string" } },
  ["marker_id", "analysis_mode", "start_seconds", "end_seconds", "title", "note", "source_quote", "source",
    "text_truncated", "jump_seconds", "importance", "review_status", "play_url"],
  { video_id: id, analysis_runs: { type: "array", items: { type: "object", properties: {
    analysis_mode: { type: "string" }, status: { type: "string" }, candidate_count: { type: "integer", minimum: 0 },
    updated_at: { type: "string" },
  }, required: ["analysis_mode", "status", "candidate_count", "updated_at"] } },
    evidence_type: { const: "saved_analysis" } }, ["video_id", "analysis_runs", "evidence_type"]),
  list_jobs: pageSchema({ job_id: { type: "string" }, job_type: { type: "string" }, status: { type: "string" },
    progress: { type: "number" }, message: { type: "string" }, created_at: { type: "string" } },
  ["job_id", "job_type", "status", "progress", "message", "created_at"],
  { scope: { const: "course_and_batch_jobs" } }, ["scope"]),
  get_processing_status: { ...resultSchema, properties: { ...resultSchema.properties,
    service_status: { const: "running" }, asr_selection: { type: "object" }, asr_observation: { type: "object" },
    recall_selection: { type: "object" }, ai_connection: { type: "object" } },
  required: ["library_id", "service_status", "asr_selection", "asr_observation", "recall_selection", "ai_connection"] },
};
const imageSchema = { ...resultSchema, properties: { ...resultSchema.properties,
  video_id: id, evidence_type: { enum: ["extracted_slide", "video_frame"] },
  image: { type: "object", properties: { mime_type: { const: "image/jpeg" },
    width: { type: "integer", minimum: 1 }, height: { type: "integer", minimum: 1 } },
  required: ["mime_type", "width", "height"], additionalProperties: false } },
required: ["library_id", "video_id", "evidence_type", "image"] };
retrievalSchemas.read_slide = { ...imageSchema, properties: { ...imageSchema.properties, slide_id: id },
  required: [...imageSchema.required, "slide_id"] };
retrievalSchemas.read_video_frame = { ...imageSchema, properties: { ...imageSchema.properties,
  time_seconds: seconds, play_url: { type: "string" } },
required: [...imageSchema.required, "time_seconds", "play_url"] };
const batchJobSchema = { ...resultSchema, properties: {
  ...resultSchema.properties, job_id: { type: "string" }, job_type: { type: "string" },
  status: { type: "string" }, summary: { type: "object", additionalProperties: { type: "integer", minimum: 0 } },
  entries: { type: "array", maxItems: 50, items: { type: "object" } },
  reused: { type: "boolean" }, next_offset: { type: ["integer", "null"], minimum: 0 },
  has_more: { type: "boolean" },
}, required: ["library_id", "job_id", "job_type", "status", "summary"] };
const batchPreviewSchema = { ...resultSchema, properties: {
  ...resultSchema.properties, valid: { type: "boolean" }, total: { type: "integer", minimum: 0 },
  options: { type: "object" }, effects: { type: "object", properties: {
    calls_ai_service: { type: "boolean" }, moves_imported_sources: { type: "boolean" },
    replaces_existing_transcripts: { type: "boolean" }, may_prepare_asr: { type: "boolean" },
  }, required: ["calls_ai_service", "moves_imported_sources", "replaces_existing_transcripts", "may_prepare_asr"] },
}, required: ["library_id", "valid", "total", "options", "effects"] };
const requestId = { type: "string", minLength: 8, maxLength: 100, pattern: "^[a-zA-Z0-9_-]+$",
  description: "本次动作的唯一请求标识。超时重试使用相同标识和参数；新操作使用新标识。" };
const batchOptions = { type: "object", additionalProperties: false, properties: {
  analysis_modes: { type: "array", minItems: 1, maxItems: 8,
    items: { type: "string", pattern: "^(high_recall|medium_recall|low_recall|question_extraction|strict_exam_intent|custom_[a-f0-9]{32})$" },
    description: "省略时沿用 APP 已保存的分析方案；显式指定只覆盖本次任务。" },
  asr_model_id: { type: "string", minLength: 1, maxLength: 100, description: "省略时使用当前默认 ASR。" },
  keywords: { type: "array", maxItems: 50, items: { type: "string", minLength: 1, maxLength: 120 } },
  use_keyword_prefilter: { type: "boolean", default: false },
  filter_formula_noise: { type: "boolean", default: false },
  filter_formula: { type: "string", maxLength: 2000 },
  force_retranscribe: { type: "boolean", default: false,
    description: "仅在用户明确要求重新转写时使用；会替换已有字幕和复习结果。" },
} };
const batchTarget = {
  course_id: { ...course.course_id, description: "课程或文件夹 ID。与 video_ids、folders 三选一。" },
  video_ids: { type: "array", items: id, minItems: 1, maxItems: 500 },
  folders: { type: "array", minItems: 1, maxItems: 20, items: { type: "string", minLength: 1, maxLength: 1000 },
    description: "用户指定的本机绝对目录。递归导入，归档校验后删除来源文件；重复视频保留来源。" },
  options: batchOptions, import_only: { type: "boolean", default: false,
    description: "仅适用于 folders，只导入而不转写或调用 AI。" },
  expected_recall_revision: { type: "integer", minimum: 0,
    description: "沿用 APP 方案时传入预览返回的 recall_selection_revision，设置变化则要求重新预览。" },
};
const batchTargetRule = { oneOf: ["course_id", "video_ids", "folders"].map(name => ({ required: [name] })) };
function tool(name, description, properties, required = [], readOnly = true, annotations = {}, rules = {}) {
  const outputSchema = retrievalSchemas[name] ?? (name === "preview_batch" ? batchPreviewSchema
    : ["start_batch_processing", "retry_batch", "resume_batch", "start_processing_sequence", "get_job", "cancel_job"].includes(name)
      ? batchJobSchema : resultSchema);
  return { name, description, inputSchema: { type: "object", properties,
    required, additionalProperties: false, ...rules }, outputSchema,
  annotations: { readOnlyHint: readOnly, destructiveHint: false, openWorldHint: false, ...annotations } };
}
export const courseTools = [
  tool("list_courses", "查找 ClassCue 课程与视频文件夹，获取课程 ID 和字幕覆盖情况。",
    { query: string, ...pagination }),
  tool("list_videos", "查看课程中的视频及字幕状态；转写前用它确认目标。",
    { ...course, query: string, subtitle_status: { type: "string", enum: ["all", "available", "missing"] },
      ...pagination }),
  tool("search_subtitles", "在指定视频、课程或全库字面检索字幕。query 与 queries 二选一；候选词 any 命中任一词，all 要求同句包含全部词。默认合并间隔5秒内命中、跨视频轮流返回；附有限上下文及继续读取区间。不能跨字幕句匹配短语，不自动扩展同义词或课程。",
    { ...course, video_id: id, query: { ...string, minLength: 1 },
      queries: { type: "array", minItems: 1, maxItems: 8, items: { ...string, minLength: 1 } },
      match_mode: { type: "string", enum: ["any", "all"], default: "any" },
      merge_gap_seconds: { type: "integer", minimum: 0, maximum: 60, default: 5 },
      diverse: { type: "boolean", default: true }, context_seconds: { type: "integer", minimum: 0, maximum: 120, default: 20 },
      ...pagination }, [], true, {}, { oneOf: [{ required: ["query"] }, { required: ["queries"] }] }),
  tool("read_transcript", "读取视频指定时间范围的原始字幕，用于核对搜索结果和理解上下文。时间单位为秒。",
    { video_id: id, start_seconds: seconds, end_seconds: seconds,
      offset: pagination.offset, transcript_version: { type: "string",
        description: "继续读取时必须传入上一页返回的字幕版本。" } },
    ["video_id", "start_seconds", "end_seconds"]),
  tool("open_video", "用户希望观看时，打开 ClassCue 视频并定位到指定秒数；返回导航请求，不保证播放已完成定位。",
    { video_id: id, start_seconds: { ...seconds, default: 0 } }, ["video_id"], false),
  tool("start_transcription", "使用当前默认的已准备本地 ASR 为课程或指定视频生成字幕；跳过已有字幕，返回后台任务 ID。不提取考点。",
    { course_id: course.course_id, video_ids: { type: "array", items: id, minItems: 1, maxItems: 500 } },
    [], false, {}, { oneOf: [{ required: ["course_id"] }, { required: ["video_ids"] }] }),
  tool("start_processing_sequence", "按指定顺序在 APP 后台执行字幕转写和 PPT 提取，聊天结束不影响后续步骤。默认先转写再提取 PPT；失败或取消时停止后续步骤，保留已保存结果。不调用 AI，不下载模型；转写使用当前已准备 ASR、跳过已有字幕，PPT 复用现有提取缓存。",
    { course_id: course.course_id, video_ids: { type: "array", items: id, minItems: 1, maxItems: 500 },
      steps: { type: "array", minItems: 1, maxItems: 2, uniqueItems: true,
        items: { type: "string", enum: ["transcribe", "extract_slides"] },
        default: ["transcribe", "extract_slides"] },
      request_id: requestId }, ["request_id"], false, { idempotentHint: true },
    { oneOf: [{ required: ["course_id"] }, { required: ["video_ids"] }] }),
  tool("preview_batch", "预览已有完整批处理：课程、指定视频或本机目录三选一。返回范围、字幕覆盖、可选分析方案和操作影响；不创建任务、不导入、不调用 AI。",
    batchTarget, [], true, {}, batchTargetRule),
  tool("start_batch_processing", "启动应用已有完整批处理，按所选召回方案或题目提取进行分析。复用字幕，缺字幕时转写；会向当前 AI 服务发送字幕，可能产生 API 费用。目录导入会归档并删除来源。先用 preview_batch 核对影响；仅补字幕请用 start_transcription。",
    { ...batchTarget, request_id: requestId }, ["request_id"], false,
    { destructiveHint: true, openWorldHint: true, idempotentHint: true }, batchTargetRule),
  tool("retry_batch", "重试失败、中断或等待 AI 的完整批次，复用已完成结果；沿用原处理方案，使用当前 AI 连接，可能产生费用。不会强制重新转写。",
    { job_id: { type: "string", minLength: 1, maxLength: 100, pattern: "^[a-zA-Z0-9_-]+$" },
      request_id: requestId }, ["job_id", "request_id"], false,
    { destructiveHint: true, openWorldHint: true, idempotentHint: true }),
  tool("resume_batch", "资源已恢复时继续资源暂停的完整批次，保留已完成结果，创建续跑任务；可能继续调用 AI。恢复前应先解决资源不足。",
    { job_id: { type: "string", minLength: 1, maxLength: 100, pattern: "^[a-zA-Z0-9_-]+$" },
      request_id: requestId }, ["job_id", "request_id"], false,
    { destructiveHint: true, openWorldHint: true, idempotentHint: true }),
  tool("get_job", "查询课程纯转写、顺序处理或完整批次的进度、结果和失败原因。顺序任务与完整批次的逐项结果可用 limit/offset 分页。",
    { job_id: { type: "string", maxLength: 100 }, limit: { ...pagination.limit, default: 50 },
      offset: pagination.offset }, ["job_id"]),
  tool("cancel_job", "请求停止课程纯转写、顺序处理或完整批次，保留已保存结果。顺序任务不再启动后续步骤。正在停止不等于已经停止；继续查询终态。",
    { job_id: { type: "string", maxLength: 100 } }, ["job_id"], false, { idempotentHint: true }),
  tool("list_slides", "列出指定时间范围内已提取的 PPT 状态及其实际可见区间，时间为秒。不会启动提取；先选 slide_id 再用 read_slide 看图，不能把某页精选图说成当时画面。",
    { video_id: id, start_seconds: seconds, end_seconds: seconds, ...pagination },
    ["video_id", "start_seconds", "end_seconds"]),
  tool("read_slide", "读取指定视频的一张已提取 PPT，以图像内容返回。图片缩放至最长边1600像素；不会启动提取。",
    { video_id: id, slide_id: id }, ["video_id", "slide_id"]),
  tool("read_video_frame", "读取视频指定秒数的实际画面，以图像内容返回。适合确认当时的 PPT、板书或手写内容；不会运行 ASR 或 AI。精确时间以原视频解码为准。",
    { video_id: id, start_seconds: seconds }, ["video_id", "start_seconds"]),
  tool("read_analysis", "分页读取已保存考点、题目及分析运行状态。默认隐藏已移除条目，保留已掌握条目；区分手动题目与模型分析，不能把分析结论冒充教师原话。不启动模型。",
    { video_id: id, analysis_mode: { type: "string", pattern: "^(high_recall|medium_recall|low_recall|question_extraction|strict_exam_intent|custom_[a-f0-9]{32})$" },
      ...pagination }, ["video_id"]),
  tool("list_jobs", "查找当前库纯转写、顺序处理和完整批次，活动任务优先，便于换聊天后找回任务 ID。可按状态与包含的视频筛选，不启动或恢复任务。",
    { status: { type: "string", enum: ["all", "active", "finished"], default: "all" }, video_id: id, ...pagination }),
  tool("get_processing_status", "读取当前库保存的召回方案、ASR 选择、已有就绪检查缓存及 AI 模型配置。unknown 表示尚未检查，不等于未准备；不验证密钥、不下载模型、不发起模型请求。工具连接可自动启动本地服务。",
    {}),
];
export async function callCourseTool(name, args, env = process.env) {
  const file = env.CLASSCUE_CODEX_CONNECTION_FILE;
  if (!file || !isAbsolute(file) || (await stat(file)).size > 128 * 1024)
    throw new Error("请重新安装或连接 ClassCue 插件。");
  const connection = JSON.parse((await readFile(file, "utf8")).replace(/^\uFEFF/, ""));
  const app = connection.application;
  if (connection.format !== "classcue-codex-connection" || connection.version !== 1
      || !app || !isAbsolute(app.project_root || "") || !isAbsolute(app.python || ""))
    throw new Error("请在 ClassCue 中重新保存 Codex 连接，登记课程工具。");
  return new Promise((resolve, reject) => {
    const child = spawn(app.python, [join(app.project_root, "scripts", "codex_course_tools.py"), "--tool", name],
      { cwd: app.project_root, windowsHide: true, env: { ...env, PYTHONUTF8: "1" }, stdio: ["pipe", "pipe", "ignore"] });
    let output = "", settled = false;
    child.stdout.setEncoding("utf8");
    const fail = error => { if (!settled) { settled = true; clearTimeout(timer); child.kill(); reject(error); } };
    const timer = setTimeout(() => fail(new Error("本地工具调用超时，请查询状态后再决定是否重试。")), 110000);
    child.on("error", () => fail(new Error("无法启动 ClassCue 连接进程，请检查应用安装。")));
    child.stdin.on("error", () => fail(new Error("ClassCue 连接进程已退出。")));
    child.stdout.on("data", bytes => {
      output += bytes;
      if (output.length > 2 * 1024 * 1024) fail(new Error("本地工具返回内容过大。"));
    });
    child.on("close", code => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        if (code !== 0) throw new Error("连接进程退出，请检查 ClassCue 安装。");
        const value = JSON.parse(output);
        if (value.error) throw new Error(value.error);
        resolve(value.result);
      } catch (error) { reject(error); }
    });
    child.stdin.end(JSON.stringify(args));
  });
}
