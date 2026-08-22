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
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
});
