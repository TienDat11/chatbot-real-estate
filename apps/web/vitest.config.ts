import path from "node:path";
import { defineConfig } from "vitest/config";

// Logic-only tests keep the fast node default; component tests (*.test.tsx)
// opt into jsdom via a per-file `@vitest-environment jsdom` docblock. The
// "@/" alias matches apps/web/tsconfig.json paths.
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    setupFiles: ["./vitest.setup.ts"],
    // Heavy jsdom+antd component tests (ChatPage, LeadForm, TrainWorkspace,
    // CRM) blow the 5s default when 70+ files compete for CPU workers: the
    // same suite is green with --no-file-parallelism. Budget absorbs the
    // scheduling jitter without touching any assertion.
    testTimeout: 20000,
    hookTimeout: 20000,
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
});
