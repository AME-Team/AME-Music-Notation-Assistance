/**
 * アプリのテーマ(ダーク/ライト)の純粋ロジック(#152)。
 *
 * **既定はダークモード**(ユーザー決定)。設定はlocalStorageに保存し、
 * `<html>`のクラス(`dark`)と`data-theme`属性へ反映する。
 *
 * 適用そのもの(DOM操作)は`stores/themeStore.ts`が担い、ここは
 * 「入力の正規化・保存値の読み書き」だけを持つ(DOM非依存でテスト可能)。
 */

export type ThemeMode = "dark" | "light";

/** 既定テーマ(ユーザー決定: ダーク)。 */
export const DEFAULT_THEME: ThemeMode = "dark";

/** 保存キー。 */
export const THEME_STORAGE_KEY = "ame.theme";

/** 表示名(設定UIに出す)。 */
export const THEME_LABEL: Record<ThemeMode, string> = {
  dark: "ダーク",
  light: "ライト",
};

/** 不明な値(未保存・壊れた値)は既定(ダーク)に寄せる。 */
export function normalizeTheme(value: unknown): ThemeMode {
  return value === "light" ? "light" : "dark";
}

/** 反対のテーマ(トグルの実装用)。 */
export function otherTheme(theme: ThemeMode): ThemeMode {
  return theme === "dark" ? "light" : "dark";
}

/** 保存値を読む(読めない場合は既定=ダーク)。 */
export function readStoredTheme(storage: Pick<Storage, "getItem">): ThemeMode {
  try {
    return normalizeTheme(storage.getItem(THEME_STORAGE_KEY));
  } catch {
    // プライベートモード等でlocalStorageが使えない場合も既定で動く。
    return DEFAULT_THEME;
  }
}

/** 保存する(保存できなくても動作は継続する)。 */
export function storeTheme(storage: Pick<Storage, "setItem">, theme: ThemeMode): void {
  try {
    storage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // 保存できない環境ではセッション内のみ反映される。
  }
}
