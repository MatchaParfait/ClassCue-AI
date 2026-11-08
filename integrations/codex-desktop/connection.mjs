import { readFile, realpath, stat } from "node:fs/promises";
import { isAbsolute, join } from "node:path";
import { openDraftStore } from "./drafts.mjs";

export async function configuredRoots(env = process.env) {
  if (!env.CLASSCUE_CODEX_CONNECTION_FILE) return [env.CLASSCUE_CODEX_ROOT].filter(Boolean);
  const file = env.CLASSCUE_CODEX_CONNECTION_FILE;
  if (!isAbsolute(file) || (await stat(file)).size > 128 * 1024)
    throw new Error("插件连接设置无效，请重新安装 ClassCue 插件。");
  const value = JSON.parse((await readFile(file, "utf8")).replace(/^\uFEFF/, ""));
  if (value.format !== "classcue-codex-connection" || value.version !== 1
      || !Array.isArray(value.roots) || value.roots.length === 0 || value.roots.length > 32
      || value.roots.some(root => typeof root !== "string" || !isAbsolute(root)))
    throw new Error("插件连接设置无效，请重新安装 ClassCue 插件。");
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
