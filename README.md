# AME Music Notation Assistance

MP3 → MIDI/MusicXML の自動採譜・AI整音アシスタント。**Windows 11 x64 専用**(NFR-08′)。
Electron(TypeScript) + Python(FastAPI) 構成。設計の全文は GitHub Issue に転記されている
(#72〜#76 が v0.4、#82 が Windows専用化+Electron採用のv0.5追補で最優先)。

このドキュメントは M0(#1)時点の状態を記す。

## ディレクトリ構成

```
backend/     FastAPI サーバ・DSP Worker(Python 3.12, uv 管理)
frontend/    Electron + Vite/React(TypeScript)
tests/backend/  pytest によるバックエンドのテスト
workspace/   実行時生成データ(.gitignore対象)。SQLite・プロジェクトごとの音声/成果物
```

`backend/app/domain/` は外部ライブラリに一切依存しない(§5.2)。Score IR・編集オペレーション・
検証ロジックは将来ここに実装し、単体テストを厚くする方針を維持すること。

## セットアップ

### バックエンド

```bash
uv python install 3.12
cd backend
uv sync
uv run pytest ../tests/backend   # または repo 直下から: uv run --project backend pytest
uv run ruff check . ../tests/backend
```

依存は §13(設計書)の確定版スタックを固定しているが、実インストール検証の結果 2 点だけ
バージョンを変更している。理由は [`backend/DEPENDENCY_NOTES.md`](backend/DEPENDENCY_NOTES.md)
を参照(`torch`/`torchaudio` を 2.13.0→2.10.0 に、`piano_transcription_inference` に
`audioread` を追加)。

### フロントエンド

```bash
cd frontend
npm install
npm run typegen     # backend/openapi.json → src/api/generated.ts (要: 事前に export_openapi.py 実行)
npm run dev          # Vite dev server + Electron を同時起動
npm run build        # renderer(Vite) + electron(tsc) をビルド
npm start             # ビルド後に Electron を起動
```

**パッケージマネージャは npm を採用**(pnpm は未導入。corepack を追加する複雑さを避けた)。
**Lint/Format は Biome を採用**(eslint+prettier の二重設定を避けるため単一ツールに統一)。

## OpenAPI → TypeScript 型生成(NFR-09)

手書きの型二重管理を禁止する。`backend/openapi.json` と `frontend/src/api/generated.ts` は
生成物としてコミットする。Pydantic モデルを変更したら:

```bash
cd backend && uv run python scripts/export_openapi.py
cd ../frontend && npm run typegen
```

CI(`typegen-check` job)がこの2ファイルの最新性を `git diff --exit-code` で検証する。

## アーキテクチャ上の主要な決定

- **Renderer とバックエンドの通信は REST + SSE のみ**(Electron の IPC には楽譜/ジョブデータを乗せない、#82 §2)
- **SSE はライブラリを使わず FastAPI の `StreamingResponse` で自前実装**(確定版スタックに専用ライブラリが無いため)
- **DSP Worker は API サーバと別プロセス**(`asyncio.create_subprocess_exec` で起動し、stdout の JSON Lines を進捗として読む。NFR-04)
- **ローカル認証**: Electron main が起動時にトークンを生成し環境変数でバックエンドへ渡す。全API(`/health`除く)で `X-AME-Token` を検証する(NFR-10′)
- **ブラウザ標準の `EventSource` は認証ヘッダを送れない**ため、フロントの SSE 購読は `fetch` + `ReadableStream` を自前実装している(`frontend/src/lib/sse.ts`)

## 既知の制約(Known Limitations)

このリポジトリの自動テストは Linux 環境で実行されているため、以下は **Windows 11 実機での
最終確認が別途必要**:

- `python.exe` がアプリ終了後に残らないことの確認(Linux では同等のプロセス残留確認で代替検証)
- Windows 資格情報マネージャー経由の API キー解決(`keyring` 経由。Linux では環境変数のみで
  フォールバックすることを確認済み)
- DirectML Execution Provider(Radeon 780M での実測ベンチマークは M1 で実施)
- Windows Defender / SmartScreen の未署名バイナリ警告(署名方針は M6 の Q-17 で決定)
- `frontend/e2e/app.spec.ts`(Playwright, Electron 実起動スモークテスト)は Chromium を
  ヘッドで動かすため `libgtk-3` 等のシステムライブラリが必要。この開発サンドボックスには
  未導入のため、CI(windows-latest)または実機での実行を前提とする

`ffmpeg` の同梱方式(`beat-this` が非WAV入力に要求する)は未解決のまま(#79 参照、M6のQ-17と関連)。

## テスト

| 対象 | コマンド |
| :--- | :--- |
| バックエンド全体 | `uv run --project backend pytest`(repo直下) |
| フロントLint/Format | `cd frontend && npm run lint` |
| フロント型チェック | `cd frontend && npm run typecheck` |
| フロント単体テスト | `cd frontend && npm test` |
| Electron E2E | `cd frontend && npm run build && npm run test:e2e` |
