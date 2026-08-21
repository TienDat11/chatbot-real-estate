import path from "node:path";
import { defineConfig } from "vitest/config";

// Mirrors the packages/ui vitest setup: node environment, no DOM needed
// (logic-only tests). The "@/" alias matches apps/web/tsconfig.json paths.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
});
