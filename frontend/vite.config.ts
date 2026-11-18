import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API runs on the FastAPI server (default 12000). In dev we proxy so the
// browser talks to one origin and no CORS config is needed.
const apiTarget = process.env.QUICKREEL_API ?? "http://127.0.0.1:12000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
      "/healthz": { target: apiTarget, changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
