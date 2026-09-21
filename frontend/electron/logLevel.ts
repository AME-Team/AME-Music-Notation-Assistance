/**
 * ログレベルの判定(#148)。バックエンド(Python/uvicorn)の出力は**stderrに出る**ため
 * 一律WARNにすると「エラーなのか単なるINFOなのか」がログから読み取れない。
 * 行の内容から重大度を推定して、エラー・警告が埋もれないようにする。
 *
 * Electron本体に依存しない純粋関数にしてユニットテスト可能にしている。
 */

export type LogLevel = "DEBUG" | "INFO" | "WARN" | "ERROR";

/** レベルを示す語(大文字小文字は問わない)。 */
const ERROR_MARKERS = [
  "traceback (most recent call last)",
  "critical",
  "error",
  "exception",
  "failed",
  "failure",
];
const WARN_MARKERS = ["warning", "warn", "deprecated", "retrying"];

/**
 * 出力テキスト(複数行可)からログレベルを推定する。
 *
 * エラー・警告の語が1つでも含まれればそのレベル、無ければINFO。
 * 「error」はパスやコード中の語(例: `error_response.py`)にも現れうるが、
 * 取りこぼしより誤検知を許容する方針(重大な行がINFOに埋もれる方が困る)。
 */
export function classifyLogLevel(text: string): LogLevel {
  const lower = text.toLowerCase();
  if (ERROR_MARKERS.some((marker) => lower.includes(marker))) return "ERROR";
  if (WARN_MARKERS.some((marker) => lower.includes(marker))) return "WARN";
  return "INFO";
}

/**
 * ログ1行に埋め込むテキストから改行を除去する(#149レビュー指摘)。
 *
 * レンダラー由来のテキストをそのまま書くと、改行を含む値で
 * `[ERROR] [backend] ...` のような**偽のログ行を注入**でき、原因究明に使う
 * ログの信頼性が落ちる。エスケープして1エントリ=1行を保つ。
 */
export function escapeLogNewlines(text: string): string {
  return text.replace(/\r\n|\r|\n/g, "\\n");
}

/** `ERR_ABORTED`(Chromium)。正常なリダイレクト/遷移中断でも`did-fail-load`が出る。 */
const ERR_ABORTED = -3;

/**
 * `did-fail-load`をエラーとして記録すべきか(#148レビュー指摘)。
 *
 * このイベントは**サブフレーム**の失敗や、**中断(ERR_ABORTED)**でも発火するため、
 * 無条件にERRORへ書くと**通常動作でもエラーログが出て**、本当のエラーが埋もれる
 * (ログを充実させる目的に反する)。メインレームの実失敗だけを対象にする。
 */
export function shouldLogDidFailLoad(details: {
  errorCode: number;
  isMainFrame?: boolean;
}): boolean {
  if (details.isMainFrame === false) return false;
  return details.errorCode !== ERR_ABORTED;
}
