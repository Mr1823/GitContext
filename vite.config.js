import { defineConfig } from "vite";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = dirname(fileURLToPath(import.meta.url));

export default defineConfig(({ command }) => ({
  root: resolve(projectRoot, "frontend"),
  base: command === "serve" ? "/" : "/static/build/",
  build: {
    outDir: resolve(projectRoot, "src/gitcontext/static/build"),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/health": "http://127.0.0.1:8765",
      "/static": "http://127.0.0.1:8765",
    },
  },
}));
