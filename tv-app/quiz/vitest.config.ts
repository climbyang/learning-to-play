import { defineConfig } from "vitest/config";

export default defineConfig({
  define: {
    __LTP_QUIZ_URL__: JSON.stringify("http://test.invalid:8765"),
    __LTP_QUIZ_TOKEN__: JSON.stringify("test-token-123"),
  },
  test: { environment: "jsdom", include: ["test/**/*.test.ts"] },
});
