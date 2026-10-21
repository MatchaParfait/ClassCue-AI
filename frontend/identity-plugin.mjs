import { createHash } from "node:crypto";
import { realpathSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = realpathSync.native(resolve(fileURLToPath(new URL("..", import.meta.url))));
const canonicalRoot = process.platform === "win32" ? repositoryRoot.toLowerCase() : repositoryRoot;
const rootId = createHash("sha256").update(canonicalRoot).digest("hex").slice(0, 12);

export default function frontendIdentity() {
  return {
    name: "classroom-frontend-identity",
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        if (request.method !== "GET" || request.url?.split("?")[0] !== "/__classroom/frontend-identity") {
          next();
          return;
        }
        response.setHeader("Content-Type", "application/json");
        response.setHeader("Cache-Control", "no-store");
        response.end(JSON.stringify({ service: "classroom-video-study-frontend", root_id: rootId }));
      });
    },
  };
}
