// Purpose: Vite build/dev config for the vmigrate web UI.
// Usage:   npm run dev    -> http://localhost:5173/ui/ (proxies API calls to VMIGRATE_API, default :8080)
//          npm run build  -> web/dist, served by the API at /ui (VMIGRATE_WEB_DIR)
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const api = process.env.VMIGRATE_API ?? "http://127.0.0.1:8080";
const paths = ["/auth", "/users", "/providers", "/endpoints", "/migrations", "/executions", "/events",
  "/workers", "/secrets", "/health"];

export default defineConfig({
  base: "/ui/",
  plugins: [react()],
  server: { proxy: Object.fromEntries(paths.map((p) => [p, { target: api, changeOrigin: false }])) },
  build: { outDir: "dist", sourcemap: true },
});
