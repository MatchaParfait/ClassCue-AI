import { isAbsolute, join } from "node:path";
import { spawn } from "node:child_process";
import { readConnection } from "./connection.mjs";

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
const searchSources = ["catalog", "bookmarks", "subtitles", "analysis"];
const searchTerm = { type: "string", minLength: 1, maxLength: 120 };
const nullableSeconds = { type: ["number", "null"], minimum: 0 };
const searchMatchType = { enum: ["literal", "phonetic", "mixed", "semantic"] };
const phoneticModes = ["auto", "off", "always"];
const phoneticMatch = { type: "object", properties: {
  query_term: searchTerm, source_text: { type: "string" },
  start_char: { type: "integer", minimum: 0 }, end_char: { type: "integer", minimum: 1 },
  syllables: { type: "string" }, requires_verification: { const: true },
}, required: ["query_term", "source_text", "start_char", "end_char", "syllables", "requires_verification"] };
const searchEvidence = { type: "object", properties: {
  source: { enum: searchSources },
  evidence_type: { enum: ["video_metadata", "user_bookmark", "asr_transcript", "manual_marker", "saved_analysis"] },
  record_id: id, text: { type: "string" }, text_truncated: { type: "boolean" },
  matched_terms: { type: "array", items: { type: "string" } },
  literal_matched_terms: { type: "array", items: { type: "string" } },
  match_type: searchMatchType,
  phonetic_matches: { type: "array", maxItems: 8, items: phoneticMatch },
  phonetic_matches_truncated: { type: "boolean" },
  start_seconds: nullableSeconds, end_seconds: nullableSeconds,
  analysis_mode: { type: "string" }, review_status: { type: "string" }, jump_seconds: seconds,
}, required: ["source", "evidence_type", "record_id", "text", "text_truncated", "matched_terms",
  "literal_matched_terms", "match_type", "phonetic_matches", "phonetic_matches_truncated",
  "start_seconds", "end_seconds"] };
const retrievalSchemas = {
  search_library: pageSchema({
    result_id: { type: "string" }, video_id: id, video_name: { type: "string" },
    folder_id: { type: ["integer", "null"], minimum: 1 }, folder_path: { type: "string" },
    sources: { type: "array", items: { enum: searchSources } }, score: { type: "number" },
    matched_terms: { type: "array", items: { type: "string" } },
    match_type: searchMatchType,
    position_seconds: nullableSeconds, start_seconds: nullableSeconds, end_seconds: nullableSeconds,
    context_range: { anyOf: [timeRange, { type: "null" }] }, has_subtitles: { type: "boolean" },
    evidence: { type: "array", maxItems: 6, items: searchEvidence },
    evidence_count: { type: "integer", minimum: 1 }, evidence_truncated: { type: "boolean" },
    play_url: { type: "string" },
  }, ["result_id", "video_id", "video_name", "folder_id", "folder_path", "sources", "score",
    "matched_terms", "match_type", "position_seconds", "start_seconds", "end_seconds", "context_range", "has_subtitles",
    "evidence", "evidence_count", "evidence_truncated", "play_url"],
  { coverage, total_videos: { type: "integer", minimum: 0 },
    snapshot_id: { type: "string", pattern: "^[a-f0-9]{64}$" },
    searched_sources: { type: "array", items: { enum: searchSources } },
    source_counts: { type: "object", additionalProperties: { type: "integer", minimum: 0 } },
    match_type: { enum: ["literal_passage", "phonetic_passage", "semantic_passage", "hybrid_passage"] },
    phonetic_mode: { enum: phoneticModes }, phonetic_search_used: { type: "boolean" },
    phonetic_index_pending: { type: "integer", minimum: 0 },
    phonetic_terms: { type: "array", items: searchTerm, maxItems: 32 },
    term_groups: { type: "array", items: { type: "array", items: { type: "string" } } },
    exclude_terms: { type: "array", items: { type: "string" } },
    window_seconds: { type: "integer", minimum: 1, maximum: 120 },
    search_complete: { type: "boolean" }, total_is_exact: { type: "boolean" },
    candidate_truncated_sources: { type: "array", items: { enum: searchSources } },
    candidate_limit_per_source: { type: "integer", minimum: 1 },
    empty_reason: { enum: [null, "no_videos", "no_matching_content", "incomplete_search"] },
  }, ["coverage", "total_videos", "snapshot_id", "searched_sources", "source_counts", "match_type",
    "phonetic_mode", "phonetic_search_used", "phonetic_index_pending", "phonetic_terms",
    "term_groups", "exclude_terms", "window_seconds", "search_complete", "total_is_exact",
    "candidate_truncated_sources", "candidate_limit_per_source", "empty_reason"]),
  list_courses: pageSchema({ course_id: { type: "string" }, name: { type: "string" }, path: { type: "string" },
    ...coverage.properties }, ["course_id", "name", "path", ...coverage.required]),
  list_videos: pageSchema({ video_id: id, name: { type: "string" }, duration_seconds: seconds,
    has_subtitles: { type: "boolean" }, slide_count: { type: "integer", minimum: 0 },
    play_url: { type: "string" } }, ["video_id", "name", "has_subtitles", "slide_count", "play_url"],
  { coverage }, ["coverage"]),
  search_bookmarks: pageSchema({
    bookmark_id: id, video_id: id, video_name: { type: "string" }, note: { type: "string" },
    position_seconds: { type: ["number", "null"], minimum: 0 },
    folder_id: { type: ["integer", "null"], minimum: 1 }, folder_path: { type: "string" },
    has_subtitles: { type: "boolean" }, context_range: { anyOf: [timeRange, { type: "null" }] },
    play_url: { type: "string" }, created_at: { type: "string" }, updated_at: { type: "string" },
  }, ["bookmark_id", "video_id", "video_name", "note", "position_seconds", "folder_id", "folder_path",
    "has_subtitles", "context_range", "play_url", "created_at", "updated_at"],
  { total_videos: { type: "integer", minimum: 0 }, searched_bookmarks: { type: "integer", minimum: 0 },
    queries: { type: "array", items: { type: "string" } }, match_type: { const: "literal" },
    match_mode: { enum: ["any", "all"] }, evidence_type: { const: "user_bookmark" },
    empty_reason: { enum: [null, "no_bookmarks", "no_literal_match"] } },
  ["total_videos", "searched_bookmarks", "queries", "match_type", "match_mode", "evidence_type", "empty_reason"]),
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
const batchJobSchema = { ...resultSchema, properties: {
  ...resultSchema.properties, job_id: { type: "string" }, job_type: { type: "string" },
  status: { type: "string" }, summary: { type: "object",
    properties: { elapsed_seconds: { type: "number", minimum: 0 } },
    additionalProperties: { type: "integer", minimum: 0 } },
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
  // Image tools use content only; outputSchema would require structuredContent.
  const outputSchema = ["read_slide", "read_video_frame"].includes(name) ? undefined
    : retrievalSchemas[name] ?? (name === "preview_batch" ? batchPreviewSchema
    : ["start_batch_processing", "retry_batch", "resume_batch", "start_processing_sequence",
      "start_semantic_prepare", "start_semantic_setup", "get_job", "cancel_job"].includes(name)
      ? batchJobSchema : resultSchema);
  return { name, description, inputSchema: { type: "object", properties,
    required, additionalProperties: false, ...rules }, ...(outputSchema ? { outputSchema } : {}),
  annotations: { readOnlyHint: readOnly, destructiveHint: false, openWorldHint: false, ...annotations } };
}
const allCourseTools = [
  tool("list_courses", "查找 ClassCue 课程与视频文件夹，获取课程 ID 和字幕覆盖情况。",
    { query: string, ...pagination }),
  tool("list_videos", "查看课程中的视频及字幕状态；转写前用它确认目标。",
    { ...course, query: string, subtitle_status: { type: "string", enum: ["all", "available", "missing"] },
      ...pagination }),
  tool("search_library", "统一搜索视频库名称目录、人工书签提醒、字幕、已保存考点与题目，找回哪个视频的哪段讲解。默认全部来源；sources 可限定 catalog/bookmarks/subtitles/analysis。query 为短语；queries 配合 any/all；term_groups 组内任一、组间全部（如[[范式,规范化],[冗余,分解]]）。字幕可在 window_seconds 内跨句满足组合，单词或发音序列不跨句。phonetic_mode 默认 auto：完整字面检索无结果时尝试字幕同音匹配；用户提示 ASR 同音错字或已有结果不相关时用 always，off 仅字面检索。明确错词如 B包 可与闭包作为同组字面候选。发音仅忽略声调的完整音节匹配，不是纠错或语义模型。证据保留原字幕，phonetic_matches 是待上下文核实的假设。可接 read_transcript 核对或 list_slides/read_slide 看图。名称和整段提醒不推测时间，备注和分析不是教师原话。search_complete=false 时不能断言没有内容；phonetic_index_pending>0 表示仍有待建索引，可同参数从首页重试或收紧范围。继续翻页须带 snapshot_id，不自动扩大范围。",
    { query: searchTerm, queries: { type: "array", minItems: 1, maxItems: 8, items: searchTerm },
      semantic_query: { type: "string", minLength: 1, maxLength: 1000,
        description: "按意思查找字幕的完整问题。单独提供做语义检索；与一组字面词同传做混合检索。需先准备本地 Qwen 模型并为该范围建立索引。相似度不是置信度；仍须读前后文核实。仅字幕参与，保留课程、视频、时间和排除词约束。" },
      term_groups: { type: "array", minItems: 1, maxItems: 6,
        items: { type: "array", minItems: 1, maxItems: 8, items: searchTerm },
        description: "组内任一词，组间全部满足；最多32词。词由 Agent 根据问题提出。" },
      match_mode: { enum: ["any", "all"], default: "any" },
      phonetic_mode: { enum: phoneticModes, default: "auto",
        description: "auto 无字面结果时回退；always 主动补充字幕同音候选；off 仅字面。原字幕不改写。" },
      exclude_terms: { type: "array", maxItems: 8, items: searchTerm },
      sources: { type: "array", minItems: 1, maxItems: 4, uniqueItems: true,
        items: { enum: searchSources }, default: searchSources },
      ...course, video_id: id, start_seconds: seconds, end_seconds: seconds,
      window_seconds: { type: "integer", minimum: 1, maximum: 120, default: 45 },
      context_seconds: { type: "integer", minimum: 0, maximum: 120, default: 15 },
      diverse: { type: "boolean", default: true }, ...pagination,
      snapshot_id: { type: "string", pattern: "^[a-f0-9]{64}$",
        description: "翻页时原样传入上一页快照；资料变化会要求重新搜索。" },
    }, [], true, {}, {
      anyOf: ["query", "queries", "term_groups", "semantic_query"].map(name => ({ required: [name] })),
      dependentRequired: { start_seconds: ["end_seconds"], end_seconds: ["start_seconds"] },
    }),
  tool("get_semantic_status", "读取本地 Qwen 检索模型及指定范围的字幕索引覆盖，不加载模型、不下载、不建立索引。pending_videos 表示仍未索引的字幕视频，无索引不能据此断言课堂没有内容。",
    { ...course, video_id: id }),
  tool("start_semantic_prepare", "准备或切换本地 Qwen3-Embedding-0.6B 检索模型，可下载共享权重和所选 CPU/GPU 独立环境。省略 device 沿用当前设备，首次检测到可用 GPU 时默认 GPU，否则 CPU；不建立索引、不转写、不调用外部 AI。仅用户要求准备检索模型时使用。持久 request_id 保障响应丢失重试不重复提交；用 get_job 查询、cancel_job 停止。",
    { request_id: requestId, device: { type: "string", enum: ["cpu", "cuda"] } },
    ["request_id"], false, { idempotentHint: true, openWorldHint: true }),
  tool("start_semantic_setup", "为用户明确指定的课程或视频准备语义检索：先准备或切换本地 Qwen 检索环境，再为已有字幕建立索引；可下载共享模型权重和 CPU/GPU 环境。省略 device 沿用当前设备，首次检测到可用 GPU 时默认 GPU，否则 CPU。不转写、不做 OCR、不调用外部 AI。仅用户要求开启该范围的语义检索时使用，普通查找不隐式下载或索引。持久 request_id 保障重试不重复；用 get_job 查询、cancel_job 停止，保留已完成视频索引。",
    { ...course, video_id: id, request_id: requestId,
      device: { type: "string", enum: ["cpu", "cuda"] } },
    ["request_id"], false, { idempotentHint: true, openWorldHint: true },
    { anyOf: [{ required: ["course_id"] }, { required: ["video_id"] }] }),
  tool("start_semantic_index", "为明确指定的课程或视频建立本地字幕语义索引。只使用已准备的 Qwen3-Embedding-0.6B，不转写、不调用外部 AI，不处理 PPT。后台执行并复用已有索引；返回任务 ID，用 get_job 查询或 cancel_job 停止。停止保留已完成视频。",
    { ...course, video_id: id }, [], false, {},
    { anyOf: [{ required: ["course_id"] }, { required: ["video_id"] }] }),
  tool("search_bookmarks", "检索用户在 ClassCue 自己添加的复习书签和提醒。用户说“我记得有个视频讲过自注意力”或寻找自己的标记时优先使用。只对书签备注做字面匹配；可用 query 或 queries（any/all），均省略则分页浏览全部书签。可限定课程或视频，返回备注原文、文件夹、可空时间点、播放链接和附近字幕读取区间。无时间点表示整段视频提醒，不能推测讲解位置；备注不是教师原话，不修改书签、不启动转写或模型。",
    { ...course, video_id: id, query: { ...string, minLength: 1 },
      queries: { type: "array", minItems: 1, maxItems: 8, items: { ...string, minLength: 1 } },
      match_mode: { type: "string", enum: ["any", "all"], default: "any" },
      context_seconds: { type: "integer", minimum: 1, maximum: 120, default: 30 },
      ...pagination }, [], true, {}, { not: { required: ["query", "queries"] } }),
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
  tool("open_video", "用户希望立即观看时，直接唤起 ClassCue 桌面版并定位到指定秒数；未保存修改会阻止导航。返回导航请求，不保证播放器已完成定位。结果中的 play_url 可供用户点击，优先打开桌面版，失败后提供网页备用入口；请原样引用链接。",
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
// Old callers remain supported without advertising duplicate search choices.
export const legacyCourseTools = allCourseTools.filter(tool =>
  ["search_subtitles", "search_bookmarks"].includes(tool.name));
export const courseTools = allCourseTools.filter(tool =>
  !["search_subtitles", "search_bookmarks"].includes(tool.name));
export async function callCourseTool(name, args, env = process.env) {
  const connection = await readConnection(env);
  const app = connection.application;
  if (!app || typeof app.project_root !== "string" || !isAbsolute(app.project_root)
      || typeof app.python !== "string" || !isAbsolute(app.python))
    throw new Error("请在 ClassCue 中重新保存 Codex 连接，登记课程工具。");
  return new Promise((resolve, reject) => {
    const child = spawn(app.python, [join(app.project_root, "scripts", "codex_course_tools.py"), "--tool", name],
      { cwd: app.project_root, windowsHide: true, env: { ...env, PYTHONUTF8: "1" }, stdio: ["pipe", "pipe", "ignore"] });
    let output = "", settled = false;
    child.stdout.setEncoding("utf8");
    const fail = error => { if (!settled) { settled = true; clearTimeout(timer); child.kill(); reject(error); } };
    // Semantic HTTP gets 150s after service discovery; keep the process alive beyond it.
    const timeout = name === "search_library" && args?.semantic_query ? 210000 : 110000;
    const timer = setTimeout(() => fail(new Error("本地工具调用超时，请查询状态后再决定是否重试。")), timeout);
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
