import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { defineConfig } from "@playwright/test";

// e2eは**実行ごとに隔離されたワークスペース**を使う。
//
// 既定のワークスペース(リポジトリ直下の`workspace/`)をそのまま使うと、前回までの
// 実行で作られたプロジェクトが残り続ける。`app.spec.ts`は取り込んだプロジェクト名
// (`smoke-test.wav`)で要素を探すため、同名プロジェクトが複数あるとロケータがstrict
// mode違反で落ち、「アプリの不具合」に見える失敗になる(実測: ローカルの累積4件で
// `getByText`が4要素にマッチして失敗し、`AME_WORKSPACE_DIR`を空ディレクトリに向けると成功)。
//
// backendの`app/config.py`が`AME_WORKSPACE_DIR`を読むため、テストプロセス(=Electronの
// 親プロセス)で設定すれば起動するバックエンドにも伝わる。既に設定されている場合
// (CIや手動指定)はそれを尊重する。
if (!process.env.AME_WORKSPACE_DIR) {
  const e2eWorkspace = mkdtempSync(path.join(tmpdir(), "ame-e2e-workspace-"));
  process.env.AME_WORKSPACE_DIR = e2eWorkspace;
  // 自分で作った一時ワークスペースは実行終了時に片付ける(レビュー指摘:
  // 放置すると`tmpdir()`配下に実行のたび蓄積する)。`AME_WORKSPACE_DIR`を
  // 指定された場合は呼び出し側のディレクトリなので触らない。
  //
  // このモジュールはPlaywrightの各プロセスで読み込まれうるが、上のガードにより
  // 実際に作成するのは最初の1プロセスだけなので、削除も1回だけ登録される。
  const cleanupE2eWorkspace = () => rmSync(e2eWorkspace, { recursive: true, force: true });
  process.on("exit", cleanupE2eWorkspace);
  // `exit`はSIGINT/SIGTERM(Ctrl+Cでの中断)では発火しないため、シグナル側でも
  // 片付ける(レビュー指摘)。終了処理そのものはPlaywrightの既存ハンドラに
  // 任せたいので、自分のハンドラだけを外し、他にハンドラが無い場合に限り
  // 同じシグナルを送り直して既定の終了動作(終了コード付き)へ戻す。
  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    const onSignal = () => {
      cleanupE2eWorkspace();
      process.off(signal, onSignal);
      if (process.listenerCount(signal) === 0) {
        process.kill(process.pid, signal);
      }
    };
    process.on(signal, onSignal);
  }
}

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  workers: 1,
  reporter: "list",
});
