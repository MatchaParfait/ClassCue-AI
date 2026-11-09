import { readFile } from "node:fs/promises";

export const resourceUri = "ui://classcue/discussion.html";
let cached;
export function discussionResource() {
  return cached ??= readFile(new URL("./discussion.html", import.meta.url), "utf8");
}
