import { mkdtempSync } from "node:fs";
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
  process.env.AME_WORKSPACE_DIR = mkdtempSync(path.join(tmpdir(), "ame-e2e-workspace-"));
}

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  workers: 1,
  reporter: "list",
});
