import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite is kept intentionally small. This app is standalone and does not depend on
// the Python dashboard/export pipeline.
const repoRoot = decodeURIComponent(new URL("..", import.meta.url).pathname).replace(/^\/([A-Za-z]:)/, "$1");

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    open: false,
    fs: {
      allow: [repoRoot],
    },
  },
});
