import { readFile, realpath, stat } from "node:fs/promises";
import { isAbsolute, relative, sep } from "node:path";
import { createHash } from "node:crypto";

export async function readContext(contextFile, root) {
  if (!root || !isAbsolute(root) || !isAbsolute(contextFile))
    throw new Error("请先安装插件并设置 ClassCue Codex 工作区。");
  const [canonicalRoot, file] = await Promise.all([realpath(root), realpath(contextFile)]);
  const child = relative(canonicalRoot, file);
  if (!child || child === ".." || child.startsWith(`..${sep}`) || isAbsolute(child))
    throw new Error("上下文文件不在已配置的 ClassCue 工作区内。");
  if (!/^\.classcue\/discussions\/[a-f0-9]{32}\/context\.json$/.test(child.split(sep).slice(-4).join("/")))
    throw new Error("请选择 ClassCue 生成的讨论上下文文件。");
  if ((await stat(file)).size > 30 * 1024 * 1024)
    throw new Error("题目上下文超过大小限制。");
  const raw = await readFile(file);
  const context = JSON.parse(raw.toString("utf8"));
  if (context.format !== "classcue-codex-context" || context.version !== 1
      || !Array.isArray(context.content) || context.content.length === 0 || context.content.length > 24)
    throw new Error("题目上下文格式不兼容。");
  const content = [];
  let bytes = 0;
  for (const part of context.content) {
    if (part.type === "text" && typeof part.text === "string" && part.text.trim()) {
      bytes += Buffer.byteLength(part.text);
      content.push({ type: "text", text: part.text });
    } else if (part.type === "image" && ["image/png", "image/jpeg", "image/webp"].includes(part.media_type)
        && typeof part.data === "string") {
      const image = Buffer.from(part.data, "base64");
      const valid = part.media_type === "image/png" ? image.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex"))
        : part.media_type === "image/jpeg" ? image.subarray(0, 3).equals(Buffer.from("ffd8ff", "hex"))
          : image.subarray(0, 4).toString() === "RIFF" && image.subarray(8, 12).toString() === "WEBP";
      if (!valid || image.toString("base64") !== part.data) throw new Error("题图格式无效。");
      bytes += image.length;
      content.push({ type: "image", mimeType: part.media_type, data: part.data });
    } else throw new Error("题目包含不支持的内容。");
  }
  if (bytes > 20 * 1024 * 1024) throw new Error("题目超过 20 MB。");
  return { content, digest: createHash("sha256").update(raw).digest("hex"), metadata: {
    request_id: context.request_id, question_id: context.question_id,
    content_version: context.content_version, course_id: context.course_id,
    document_id: context.document_id,
    source_kind: context.source_kind, video_id: context.video_id,
    start_ms: context.start_ms, end_ms: context.end_ms,
  } };
}
