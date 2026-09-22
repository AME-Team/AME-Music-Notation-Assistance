import { describe, expect, it, vi } from "vitest";
import { getSheetCursor, setSheetCursorVisible } from "./osmdCursor";

function fakeCursor() {
  return {
    show: vi.fn(),
    hide: vi.fn(),
    nextMeasure: vi.fn(),
    previousMeasure: vi.fn(),
    reset: vi.fn(),
  };
}

describe("getSheetCursor / setSheetCursorVisible (#160)", () => {
  it("描画済みのカーソルを返し、表示/非表示を委譲する", () => {
    const cursor = fakeCursor();
    expect(getSheetCursor({ cursor })).toBe(cursor);

    expect(setSheetCursorVisible({ cursor }, true)).toBe(true);
    expect(cursor.show).toHaveBeenCalledTimes(1);

    expect(setSheetCursorVisible({ cursor }, false)).toBe(true);
    expect(cursor.hide).toHaveBeenCalledTimes(1);
  });

  it("cursorが無い(未描画・描画失敗)場合は何もせずfalseを返す", () => {
    for (const sheet of [{}, { cursor: undefined }, undefined, null]) {
      expect(getSheetCursor(sheet)).toBeNull();
      expect(() => setSheetCursorVisible(sheet, false)).not.toThrow();
      expect(setSheetCursorVisible(sheet, false)).toBe(false);
      expect(setSheetCursorVisible(sheet, true)).toBe(false);
    }
  });

  it("cursorが操作を持たない(別物)場合もnullとして扱う", () => {
    expect(getSheetCursor({ cursor: {} as never })).toBeNull();
  });

  it("ヘルパ無しでcursorを触ると実機と同じ例外になる(修正の必要性の記録)", () => {
    // Windows実機で発生した失敗そのもの: 描画が失敗した状態で`hide`を呼ぶ。
    const unrendered: { cursor?: { hide(): void } } = {};
    expect(() => unrendered.cursor?.hide()).not.toThrow(); // optional chainingなら落ちない
    expect(() => {
      const cursor = unrendered.cursor as unknown as { hide(): void };
      cursor.hide();
    }).toThrow(TypeError);
  });
});
