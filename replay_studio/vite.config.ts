import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import fs from "node:fs";
import path from "node:path";

// Vite is kept intentionally small. This app is standalone and does not depend on
// the Python dashboard/export pipeline.
const repoRoot = decodeURIComponent(new URL("..", import.meta.url).pathname).replace(/^\/([A-Za-z]:)/, "$1");
const normalizedRepoRoot = path.resolve(repoRoot);

function isInsideRepo(candidate: string): boolean {
  const resolved = path.resolve(candidate);
  const relative = path.relative(normalizedRepoRoot, resolved);
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

export default defineConfig({
  plugins: [
    react(),
    {
      name: "mansim-local-file-server",
      configureServer(server) {
        server.middlewares.use("/__mansim_file", (req, res) => {
          try {
            const requestUrl = new URL(req.url || "", "http://localhost");
            const rawPath = requestUrl.searchParams.get("path") || "";
            const decodedPath = decodeURIComponent(rawPath);
            if (!decodedPath || !isInsideRepo(decodedPath) || !fs.existsSync(decodedPath)) {
              res.statusCode = 403;
              res.end("Forbidden");
              return;
            }
            const stat = fs.statSync(decodedPath);
            if (!stat.isFile()) {
              res.statusCode = 404;
              res.end("Not found");
              return;
            }
            const ext = path.extname(decodedPath).toLowerCase();
            const contentType =
              ext === ".json" ? "application/json; charset=utf-8" :
              ext === ".png" ? "image/png" :
              ext === ".html" ? "text/html; charset=utf-8" :
              "application/octet-stream";
            res.setHeader("Content-Type", contentType);
            res.setHeader("Cache-Control", "no-store");
            fs.createReadStream(decodedPath).pipe(res);
          } catch (error) {
            res.statusCode = 500;
            res.end(error instanceof Error ? error.message : String(error));
          }
        });
      },
    },
  ],
  server: {
    port: 5173,
    open: false,
    fs: {
      allow: [repoRoot],
    },
  },
});
