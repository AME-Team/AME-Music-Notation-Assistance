import { describe, expect, it } from "vitest";
import {
  DEFAULT_THEME,
  normalizeTheme,
  otherTheme,
  readStoredTheme,
  storeTheme,
  THEME_LABEL,
  THEME_STORAGE_KEY,
} from "./theme";

/** localStorageの代わり(読み書きの失敗も再現できる)。 */
function fakeStorage(initial: Record<string, string> = {}, fail = false) {
  const data = { ...initial };
  return {
    getItem: (key: string) => {
      if (fail) throw new Error("denied");
      return data[key] ?? null;
    },
    setItem: (key: string, value: string) => {
      if (fail) throw new Error("denied");
      data[key] = value;
    },
    data,
  };
}

describe("theme (#152)", () => {
  it("defaults to dark", () => {
    expect(DEFAULT_THEME).toBe("dark");
  });

  it("normalizes unknown or missing values to dark", () => {
    expect(normalizeTheme(null)).toBe("dark");
    expect(normalizeTheme(undefined)).toBe("dark");
    expect(normalizeTheme("")).toBe("dark");
    expect(normalizeTheme("blue")).toBe("dark");
    expect(normalizeTheme("light")).toBe("light");
    expect(normalizeTheme("dark")).toBe("dark");
  });

  it("toggles between the two modes", () => {
    expect(otherTheme("dark")).toBe("light");
    expect(otherTheme("light")).toBe("dark");
  });

  it("reads the stored value and falls back to dark", () => {
    expect(readStoredTheme(fakeStorage({ [THEME_STORAGE_KEY]: "light" }))).toBe("light");
    expect(readStoredTheme(fakeStorage({ [THEME_STORAGE_KEY]: "dark" }))).toBe("dark");
    // 保存が無い/壊れている場合は既定(ダーク)。
    expect(readStoredTheme(fakeStorage())).toBe("dark");
    expect(readStoredTheme(fakeStorage({ [THEME_STORAGE_KEY]: "sepia" }))).toBe("dark");
  });

  it("does not throw when storage is unavailable", () => {
    expect(readStoredTheme(fakeStorage({}, true))).toBe("dark");
    expect(() => storeTheme(fakeStorage({}, true), "light")).not.toThrow();
  });

  it("stores the selected value", () => {
    const storage = fakeStorage();
    storeTheme(storage, "light");
    expect(storage.data[THEME_STORAGE_KEY]).toBe("light");
  });

  it("has labels for both modes", () => {
    expect(THEME_LABEL.dark).toBe("ダーク");
    expect(THEME_LABEL.light).toBe("ライト");
  });
});
