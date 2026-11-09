import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema, ListResourcesRequestSchema,
  ReadResourceRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { readContext } from "./context.mjs";
import { threadIdentity } from "./drafts.mjs";
import { configuredRoots, discussionStore } from "./connection.mjs";
import { relative, isAbsolute } from "node:path";
import { realpath } from "node:fs/promises";
import { discussionResource, resourceUri } from "./resource.mjs";
import { courseTools, callCourseTool } from "./course-tools.mjs";

const server = new Server({ name: "classcue", version: "0.6.0" },
  { capabilities: { tools: {}, resources: {} } });
const properties = {
  request_id: { type: "string", pattern: "^[a-f0-9]{32}$" },
  attempt: { type: "string", maxLength: 100 },
};
const appOnly = { ui: { visibility: ["app"] } };
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [...courseTools, {
    name: "read_discussion_context",
    description: "读取 ClassCue 已保存讨论的原题或课堂片段字幕及有序图片。使用讨论指引提供的 context.json 绝对路径；不读取聊天记录，不启动模型。",
    inputSchema: {
      type: "object", properties: { context_file: { type: "string" } },
      required: ["context_file"], additionalProperties: false,
    },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
  }, {
    name: "classcue_discussion", title: "ClassCue 题目",
    description: "查看当前讨论绑定的 ClassCue 原题和题图。发起讨论和首次原生附图由 ClassCue 桌面按钮完成。",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true, destructiveHint: false, openWorldHint: false },
    _meta: { ui: { resourceUri, visibility: ["app"] }, "ui/resourceUri": resourceUri,
      "openai/ui": { entrypoints: [{ type: "thread" }] } },
  }, {
    name: "claim_discussion_draft", description: "领取当前对话的一次草稿插入操作。",
    inputSchema: { type: "object", properties: { request_id: properties.request_id,
      retry_attempt: properties.attempt, confirm_unattached: { type: "boolean" } },
    required: ["request_id"], additionalProperties: false },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false }, _meta: appOnly,
  }, {
    name: "acknowledge_discussion_draft", description: "保存宿主草稿回执，不发送消息。",
    inputSchema: { type: "object", properties: { ...properties,
      status: { type: "string", enum: ["attached", "blocked", "unknown"] } },
    required: ["request_id", "attempt", "status"], additionalProperties: false },
    annotations: { readOnlyHint: false, destructiveHint: false, openWorldHint: false }, _meta: appOnly,
  }],
}));
server.setRequestHandler(ListResourcesRequestSchema, async () => ({
  resources: [{ uri: resourceUri, name: "ClassCue 题目", mimeType: "text/html;profile=mcp-app" }],
}));
server.setRequestHandler(ReadResourceRequestSchema, async request => {
  if (request.params.uri !== resourceUri) throw new Error("未知界面资源。");
  return { contents: [{ uri: resourceUri, mimeType: "text/html;profile=mcp-app",
    text: await discussionResource(),
    _meta: { ui: { prefersBorder: false, csp: { connectDomains: [], resourceDomains: [] } } } }] };
});
server.setRequestHandler(CallToolRequestSchema, async request => {
  let store;
  try {
    const { name, arguments: args = {}, _meta: meta } = request.params;
    if (courseTools.some(tool => tool.name === name)) {
      const result = await callCourseTool(name, args);
      return { content: [{ type: "text", text: JSON.stringify(result) }], structuredContent: result };
    }
    if (name === "read_discussion_context") {
      if (typeof args.context_file !== "string") throw new Error("缺少上下文文件路径。");
      const file = await realpath(args.context_file);
      let allowedRoot;
      for (const root of await configuredRoots()) {
        let canonical;
        try { canonical = await realpath(root); }
        catch (error) { if (error.code === "ENOENT") continue; throw error; }
        const child = relative(canonical, file);
        if (child && child !== ".." && !child.startsWith("..\\") && !child.startsWith("../") && !isAbsolute(child)) {
          allowedRoot = canonical; break;
        }
      }
      if (!allowedRoot) throw new Error("上下文不在插件已登记的工作区内，请检查连接设置。");
      const result = await readContext(args.context_file, allowedRoot);
      return { content: result.content, _meta: result.metadata };
    }
    const threadId = threadIdentity(meta);
    store = await discussionStore(threadId);
    if (name === "classcue_discussion") {
      const discussion = await store.load(threadId);
      return { content: [{ type: "text", text: "ClassCue 原题已就绪，请在插件面板查看草稿状态。" }],
        structuredContent: { status: discussion.status },
        _meta: { classcue: discussion } };
    }
    if (typeof args.request_id !== "string" || !/^[a-f0-9]{32}$/.test(args.request_id))
      throw new Error("无效的题目身份。");
    let result;
    if (name === "claim_discussion_draft") {
      if (args.retry_attempt !== undefined && (args.confirm_unattached !== true
          || typeof args.retry_attempt !== "string")) throw new Error("请先检查输入框再重新加入。");
      result = await store.claim(threadId, args.request_id, args.retry_attempt);
    } else if (name === "acknowledge_discussion_draft") {
      result = store.acknowledge(threadId, args.request_id, args.attempt, args.status);
    } else throw new Error("未知工具。");
    return { content: [{ type: "text", text: result.status }], structuredContent: result };
  } catch (error) {
    return { content: [{ type: "text", text: error.code ? "无法读取题目上下文，请检查文件和工作区设置。" : error.message }],
      isError: true };
  } finally { store?.close(); }
});
await server.connect(new StdioServerTransport());
