/**
 * #160: OSMD(OpenSheetMusicDisplay)のカーソルを安全に扱うヘルパ。
 *
 * OSMDの`cursor`は**`render()`が成功するまで存在しない**(実測: インスタンス生成直後も
 * `render()`が例外になった後も`undefined`のまま)。譜面の描画が失敗した状態で
 * `osmd.cursor.hide()`を呼ぶと `Cannot read properties of undefined (reading 'hide')`
 * で例外になり、これがReactのeffect内で起きると**アプリ全体がエラー画面になる**
 * (Windows実機のログ: `ScorePreview.tsx:107` で `reading 'hide'`)。
 *
 * DOM/librariesに依存しない純粋なロジックにして単体テスト可能にしている。
 */

/** カーソルが持つ操作のうち、このアプリが使うもの。 */
export interface SheetCursorLike {
  show(): void;
  hide(): void;
  nextMeasure(): void;
  previousMeasure(): void;
  reset(): void;
}

export interface SheetWithCursorLike {
  cursor?: SheetCursorLike | undefined;
}

/**
 * 描画済みのカーソルを返す(未描画・描画失敗時は`null`)。
 *
 * 呼び出し側は`null`チェックしてから操作する。`render()`が例外になった状況でも
 * 例外を投げないことを保証する。
 */
export function getSheetCursor(
  sheet: SheetWithCursorLike | null | undefined,
): SheetCursorLike | null {
  const cursor = sheet?.cursor;
  if (!cursor || typeof cursor.hide !== "function" || typeof cursor.show !== "function") {
    return null;
  }
  return cursor;
}

/**
 * カーソルの表示/非表示を切り替える。適用できた場合のみ`true`を返す。
 * カーソルが無い場合は何もせず`false`(例外は投げない)。
 */
export function setSheetCursorVisible(
  sheet: SheetWithCursorLike | null | undefined,
  visible: boolean,
): boolean {
  const cursor = getSheetCursor(sheet);
  if (!cursor) return false;
  if (visible) cursor.show();
  else cursor.hide();
  return true;
}
