import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In dev, the console calls the gateway through this proxy so it stays same-origin and,
// like production (nginx.conf), only the console API is reachable through it.
const gateway = process.env.AGENTGUARD_API ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { "/v1/console": gateway },
  },
  build: { sourcemap: false, chunkSizeWarningLimit: 600 },
  test: { environment: "node" },
});
