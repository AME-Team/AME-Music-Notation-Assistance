import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    // Electron側の純粋ロジック(#148: ログレベル判定など)もテストする。
    include: ["src/**/*.test.ts", "electron/**/*.test.ts"],
  },
});
