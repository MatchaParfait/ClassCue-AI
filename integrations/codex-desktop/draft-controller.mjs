export class DraftController {
  constructor(app) { this.app = app; this.active = false; }
  async insert(discussion, { retry = false } = {}) {
    if (this.active) return { status: "inserting" };
    const draft = this.app.getHostCapabilities()?.experimental?.["openai/message"]?.draft;
    if (draft !== true && (draft === null || typeof draft !== "object" || Array.isArray(draft)))
      throw new Error("当前 Codex 面板不支持原生草稿，请在对话内打开 ClassCue。");
    this.active = true;
    const call = async (name, args) => {
      const result = await this.app.callServerTool({ name, arguments: args });
      if (result.isError) throw new Error(result.content?.find(part => part.type === "text")?.text || "插件操作失败。");
      return result.structuredContent;
    };
    try {
      const claim = await call("claim_discussion_draft", {
        request_id: discussion.request_id,
        ...(retry ? { retry_attempt: discussion.attempt, confirm_unattached: true } : {}),
      });
      if (!claim.claimed) return { status: claim.status };
      let status = "attached";
      try {
        const response = await this.app.sendMessage({
          role: "user", content: discussion.content,
          _meta: { "openai/message": { target: "active", send: false } },
        }, { timeout: 15000 });
        if (response?.isError) status = "unknown";
      } catch (error) {
        status = [-32601, -32602].includes(error.code) ? "blocked" : "unknown";
      }
      // Persist only after the host reply. A lost receipt never authorizes another insertion.
      await call("acknowledge_discussion_draft", {
        request_id: discussion.request_id, attempt: claim.attempt, status,
      });
      return { status };
    } finally { this.active = false; }
  }
}
