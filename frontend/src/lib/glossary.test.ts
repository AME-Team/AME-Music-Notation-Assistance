import { describe, expect, it } from "vitest";
import { GLOSSARY } from "./glossary";

describe("GLOSSARY", () => {
  it("すべてのキーに空でない説明文が設定されている", () => {
    for (const [key, value] of Object.entries(GLOSSARY)) {
      expect(value.length, `${key} の説明が空`).toBeGreaterThan(0);
    }
  });

  it("説明文は簡潔さを保つため長すぎない(目安: 120文字以内)", () => {
    for (const [key, value] of Object.entries(GLOSSARY)) {
      expect(value.length, `${key} の説明が長すぎる`).toBeLessThanOrEqual(120);
    }
  });
});
