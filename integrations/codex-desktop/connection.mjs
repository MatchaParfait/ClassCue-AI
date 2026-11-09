import { readFile, realpath, stat } from "node:fs/promises";
import { basename, dirname, isAbsolute, join } from "node:path";
import { homedir } from "node:os";
import { openDraftStore } from "./drafts.mjs";

export async function readConnection(env = process.env) {
  const reconnect = "请在 ClassCue 的“设置 → 讨论工具”中选择 Codex，重新保存工作区后重试。";
  let home = env.CODEX_HOME;
  if (!env.CLASSCUE_CODEX_CONNECTION_FILE && !home && env.PLUGIN_ROOT) {
    // Codex omits CODEX_HOME from the MCP environment but supplies the installed cache root.
    const plugin = dirname(env.PLUGIN_ROOT), cache = dirname(dirname(plugin)), plugins = dirname(cache);
    if (!isAbsolute(env.PLUGIN_ROOT) || basename(plugin) !== "classcue"
        || basename(cache) !== "cache" || basename(plugins) !== "plugins")
      throw new Error("无法确定插件所属的 Codex 数据目录，请使用 ClassCue 随附的 install.ps1 安装。");
    home = dirname(plugins);
  }
  home ||= join(homedir(), ".codex");
  const file = env.CLASSCUE_CODEX_CONNECTION_FILE || (isAbsolute(home) ? join(home, "classcue", "connection.json") : "");
  if (!file || !isAbsolute(file)) throw new Error(`插件连接设置无效。${reconnect}`);
  let value;
  try {
    if ((await stat(file)).size > 128 * 1024) throw new Error("连接文件过大");
    value = JSON.parse((await readFile(file, "utf8")).replace(/^\uFEFF/, ""));
  } catch (error) {
    throw new Error(`${error.code === "ENOENT" ? "尚未连接 ClassCue" : "无法读取插件连接设置"}。${reconnect}`);
  }
  if (!value || value.format !== "classcue-codex-connection" || value.version !== 1
      || !Array.isArray(value.roots) || value.roots.length === 0 || value.roots.length > 32
      || value.roots.some(root => typeof root !== "string" || !isAbsolute(root)))
    throw new Error(`插件连接设置无效。${reconnect}`);
  return value;
}

export async function configuredRoots(env = process.env) {
  if (!env.CLASSCUE_CODEX_CONNECTION_FILE && env.CLASSCUE_CODEX_ROOT) {
    if (!isAbsolute(env.CLASSCUE_CODEX_ROOT)) throw new Error("插件工作区必须使用绝对路径。");
    return [env.CLASSCUE_CODEX_ROOT];
  }
  const value = await readConnection(env);
  return [...new Set(value.roots)];
}

export async function discussionStore(threadId, env = process.env) {
  let match;
  try {
    for (const root of await configuredRoots(env)) {
      let store;
      try {
        await stat(join(root, ".classcue-codex", "drafts.sqlite"));
        store = await openDraftStore(root);
      } catch (error) {
        if (error.code === "ENOENT") continue;
        throw error;
      }
      if (!store.db.prepare("SELECT 1 FROM drafts WHERE thread_id=?").get(threadId)) {
        store.close();
      } else if (match && await realpath(root) !== match.root) {
        store.close();
        throw new Error("当前对话在多个工作区重复绑定，已停止加入题目。");
      } else if (match) {
        store.close();
      } else match = store;
    }
    if (!match) throw new Error("当前对话没有可读取的 ClassCue 绑定，请从原题打开并检查插件连接。");
    return match;
  } catch (error) { match?.close(); throw error; }
}
