import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import frontendIdentity from "./identity-plugin.mjs";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  return {
    plugins: [react(), frontendIdentity()],
    resolve: { dedupe: ["three"] },
    server: {
      fs: { allow: [".", "../experiments/subtitle-cyberspace/src"] },
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": {
          target: env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8765",
          changeOrigin: false,
          ws: true,
        },
      },
    },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/routerRequest.js"],
    },
  };
});
