import { build } from "esbuild";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

export const resourceUri = "ui://classcue/discussion.html";
let cached;
export function discussionResource() {
  return cached ??= (async () => {
    const [bundle, template, icon] = await Promise.all([
      build({ entryPoints: [fileURLToPath(new URL("./app.mjs", import.meta.url))],
        bundle: true, write: false, format: "esm", minify: true, target: "es2022",
        legalComments: "inline", logLevel: "silent" }),
      readFile(new URL("./view.html", import.meta.url), "utf8"),
      readFile(new URL("./icon.png", import.meta.url)),
    ]);
    return template.replace("CLASSCUE_ICON", `data:image/png;base64,${icon.toString("base64")}`)
      .replace("CLASSCUE_SCRIPT", () => bundle.outputFiles[0].text.replace(/<\/script/gi, "<\\/script"));
  })();
}
