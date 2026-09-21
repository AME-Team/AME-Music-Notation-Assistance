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

/** 有効なログレベル(IPCで受け取った値の検証に使う)。 */
const VALID_LEVELS: readonly LogLevel[] = ["DEBUG", "INFO", "WARN", "ERROR"];

/**
 * IPC等の外部入力から来たログレベルを安全に正規化する(#148レビュー指摘)。
 *
 * `logger[level.toLowerCase()]`のように未検証のキーで引くと、想定外の文字列で
 * `undefined`を呼び出して**ログ自体が落ちる**(＝肝心のエラーが残らない)。
 * 不正な値は「エラー転送経路から来たもの」とみなしてERRORに寄せる。
 */
export function normalizeLogLevel(value: unknown): LogLevel {
  const upper = String(value ?? "").toUpperCase();
  return VALID_LEVELS.includes(upper as LogLevel) ? (upper as LogLevel) : "ERROR";
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
