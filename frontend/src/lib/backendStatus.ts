import type { BackendStatus } from "./electron-api";

/**
 * #146: バックエンド状態の購読ロジック。
 *
 * mainプロセスは状態遷移時に**一度だけ** `backend:status` を送るため、レンダラーが
 * そのイベントを購読する前に `ready` が送られると、UIは初期値の
 * 「バックエンドを起動しています...」のまま**永久に固まる**(開発時はVite経由で
 * レンダラーの起動が遅く、実際に発生した)。`getBackendStatus()`で現在値を引いて
 * 取り戻せるようにする。
 *
 * 状態は `starting` → (`ready` | `error`) の一方向にしか進まないため、
 * 購読イベントと現在値取得の**順序が入れ替わっても巻き戻さない**(下の
 * `mergeBackendStatus`)。DOMに依存しない純粋なロジックにしてテスト可能にしている。
 */

export interface BackendStatusSnapshot {
  status: BackendStatus;
  detail?: string;
}

export interface BackendStatusApi {
  /**
   * 現在値の取得。**旧preload(このAPIを持たないバージョン)では未定義**になり得る
   * ため optional にしてある。レンダラー(Vite/バンドル)とpreload(dist-electron)は
   * 別々にビルドされるので、開発中はバージョンがずれる(実際にWindows実機の開発モードで
   * `api.getBackendStatus is not a function` でアプリ全体が落ちた)。
   */
  getBackendStatus?(): Promise<BackendStatusSnapshot>;
  onBackendStatus(cb: (status: BackendStatus, detail?: string) => void): () => void;
}

/**
 * 状態の巻き戻しを防ぐ。`starting` は初期値でしか意味を持たないため、
 * 既に確定状態(`ready`/`error`)を得ている場合は `starting` を無視する。
 */
export function mergeBackendStatus(
  current: BackendStatusSnapshot,
  next: BackendStatusSnapshot,
): BackendStatusSnapshot {
  if (current.status !== "starting" && next.status === "starting") return current;
  return next;
}

/**
 * 状態変化を購読し、購読前に送られた分を現在値の取得で取り戻す。
 *
 * 戻り値は購読解除の関数。`getBackendStatus()`が失敗しても購読側で更新され得るため
 * 例外は握りつぶす(起動失敗の表示はmainからの`error`イベントが担う)。
 */
export function watchBackendStatus(
  api: BackendStatusApi,
  onChange: (snapshot: BackendStatusSnapshot) => void,
): () => void {
  let latest: BackendStatusSnapshot | null = null;
  const apply = (next: BackendStatusSnapshot): void => {
    latest = latest ? mergeBackendStatus(latest, next) : next;
    onChange(latest);
  };

  // 購読を先に張ってから現在値を引く(この間に届いたイベントを取りこぼさない)。
  const unsubscribe = api.onBackendStatus((status, detail) => apply({ status, detail }));
  // 旧preloadには無いので、無ければ購読のみで動く(落とさない)。
  const pull = api.getBackendStatus?.();
  if (pull) void pull.then(apply, () => undefined);
  return unsubscribe;
}
