import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Axon web — observability-style service & API catalog (dashboard copied
// from Arc's UI/platform). It composes ../base + ../components, exactly
// like Arc: app/component code uses the @base / @components aliases;
// src/lib/*.ts stays relative-only (node --test loads those directly).
//
// Demo is a separate build, brought up only with `--mode demo`
// (npm run dev:demo / build:demo). In demo mode the @client alias resolves
// to src/lib/demo-client.ts (fixture dataset); in every other mode it
// resolves to src/lib/live-client.ts and demo code/data is not part of
// the module graph at all. __AXON_DEMO__ gates the few demo-only labels.
export default defineConfig(({ mode }) => {
  const demo = mode === "demo";
  return {
    plugins: [react()],
    define: { __AXON_DEMO__: JSON.stringify(demo) },
    resolve: { alias: {
      "@client": path.resolve(__dirname, demo ? "src/lib/demo-client.ts" : "src/lib/live-client.ts"),
      "@base": path.resolve(__dirname, "base"),
      "@components": path.resolve(__dirname, "components"),
    } },
    build: {
      rollupOptions: { output: { manualChunks: (id: string) => {
        if (id.includes("node_modules/lucide-react")) return "icons";
        if (id.includes("node_modules/react") || id.includes("node_modules/scheduler")) return "vendor";
        return undefined;
      } } },
    },
    server: {
      port: 5174,
      proxy: {
        "/api": { target: "http://127.0.0.1:8080", changeOrigin: true },
      },
    },
  };
});
