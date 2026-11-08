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
    default: ["medium_recall"] },
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
};
const batchTargetRule = { oneOf: ["course_id", "video_ids", "folders"].map(name => ({ required: [name] })) };
function tool(name, description, properties, required = [], readOnly = true, annotations = {}, rules = {}) {
  const outputSchema = name === "preview_batch" ? batchPreviewSchema
    : ["start_batch_processing", "retry_batch", "resume_batch", "get_job", "cancel_job"].includes(name)
      ? batchJobSchema : resultSchema;
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
  tool("search_subtitles", "在指定课程或全库搜索字幕关键词。支持字面匹配；结果不足时可更换关键词。",
    { ...course, query: { ...string, minLength: 1 }, ...pagination }, ["query"]),
  tool("read_transcript", "读取视频指定时间范围的原始字幕，用于核对搜索结果和理解上下文。时间单位为秒。",
    { video_id: id, start_seconds: seconds, end_seconds: seconds,
      offset: pagination.offset, transcript_version: { type: "string",
        description: "继续读取时必须传入上一页返回的字幕版本。" } },
    ["video_id", "start_seconds", "end_seconds"]),
  tool("open_video", "用户希望观看时，打开 ClassCue 视频并定位到指定秒数；返回导航请求，不保证播放已完成定位。",
    { video_id: id, start_seconds: { ...seconds, default: 0 } }, ["video_id"], false),
  tool("start_transcription", "使用当前默认的已准备本地 ASR 为课程或指定视频生成字幕；跳过已有字幕，返回后台任务 ID。不提取考点。",
    { course_id: course.course_id, video_ids: { type: "array", items: id, minItems: 1, maxItems: 500 } },
    [], false),
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
  tool("get_job", "查询课程纯转写或完整批处理任务的进度、完成结果和失败原因。完整批次逐项结果可用 limit/offset 分页。",
    { job_id: { type: "string", maxLength: 100 }, limit: { ...pagination.limit, default: 50 },
      offset: pagination.offset }, ["job_id"]),
  tool("cancel_job", "请求停止课程纯转写或完整批处理任务，保留已保存结果。正在停止不等于已经停止；继续查询终态。",
    { job_id: { type: "string", maxLength: 100 } }, ["job_id"], false, { idempotentHint: true }),
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
      if (output.length > 256 * 1024) fail(new Error("本地工具返回内容过大。"));
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
