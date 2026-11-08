import { App, applyDocumentTheme, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps";

const app = new App({ name: "ClassCue", version: "0.3.0" }, {}, { autoResize: true });
const title = document.querySelector("h1");
const state = document.querySelector("#status");
const preview = document.querySelector("#preview");
const summary = document.querySelector("#summary");
const refresh = document.querySelector("#refresh");
let initialized = false, loading = false;
const labels = {
  pending: "原题已保存。请从 ClassCue 打开讨论，首次自动附上原题。",
  inserting: "上次加入操作尚无回执，请先检查输入框。",
  attached: "已加入过原题。此面板仅供查看，重复打开不会附图。",
  pasted: "已向原生输入框粘贴过原题，请检查后发送。此面板仅供查看。",
  unknown: "未收到草稿回执，请先检查输入框。",
  blocked: "Codex 未接受草稿，请检查后重试。",
};
function theme(context) {
  if (context?.theme) applyDocumentTheme(context.theme);
  if (context?.styles?.variables) applyHostStyleVariables(context.styles.variables);
}
function showError(error) { state.textContent = error.message; state.setAttribute("role", "alert"); }
function render(value) {
  title.textContent = value.title;
  state.setAttribute("role", "status");
  state.textContent = labels[value.status] || "正在准备…";
  preview.replaceChildren();
  const count = value.content.filter(part => part.type === "image").length;
  const clip = value.source_kind === "video_clip";
  summary.textContent = clip ? `课堂字幕 · ${count} 张精选 PPT`
    : count ? `${count} 张题图 · 按原题顺序` : "纯文字题 · 没有图片附件";
  let imageIndex = 0;
  for (const part of value.content) {
    if (part.type === "text") {
      const paragraph = document.createElement("p");
      paragraph.textContent = part.text;
      preview.append(paragraph);
    } else {
      const figure = document.createElement("figure");
      const image = document.createElement("img");
      image.src = `data:${part.mimeType};base64,${part.data}`;
      image.alt = `${clip ? "精选 PPT" : "题图"} ${++imageIndex}`;
      const caption = document.createElement("figcaption");
      caption.textContent = image.alt;
      figure.append(image, caption);
      preview.append(figure);
    }
  }
}
async function load() {
  if (!initialized || loading) return;
  loading = true;
  try {
    const result = await app.callServerTool({ name: "classcue_discussion", arguments: {} });
    if (result.isError) throw new Error(result.content?.[0]?.text || "无法接收题目。");
    render(result._meta.classcue);
  } catch (error) { showError(error); }
  finally { loading = false; }
}
app.ontoolresult = result => {
  if (result._meta?.classcue && !loading) {
    render(result._meta.classcue);
    if (initialized) void load();
  }
};
app.onhostcontextchanged = theme;
app.onteardown = async () => ({});
refresh.addEventListener("click", () => void load());
try {
  await app.connect();
  theme(app.getHostContext());
  initialized = true;
  await load();
} catch (error) { showError(error); }
