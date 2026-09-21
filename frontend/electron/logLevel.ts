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
