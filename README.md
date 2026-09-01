# AME Music Notation Assistance

MP3 → MIDI/MusicXML の自動採譜・AI整音アシスタント。**Windows 11 x64 専用**(NFR-08′)。
Electron(TypeScript) + Python(FastAPI) 構成。設計の全文は GitHub Issue に転記されている
(#72〜#76 が v0.4、#82 が Windows専用化+Electron採用のv0.5追補で最優先)。

このドキュメントは M1(#2)着手時点の状態を記す。

## ディレクトリ構成

```
backend/     FastAPI サーバ・DSP Worker(Python 3.12, uv 管理)
frontend/    Electron + Vite/React(TypeScript)
tests/backend/  pytest によるバックエンドのテスト
workspace/   実行時生成データ(.gitignore対象)。SQLite・プロジェクトごとの音声/成果物
```

`backend/app/domain/` は外部ライブラリに一切依存しない(§5.2)。Score IR・編集オペレーション・
検証ロジックは将来ここに実装し、単体テストを厚くする方針を維持すること。

## クイックスタート(Windows)

初回セットアップ(`uv sync` / `npm install`)からアプリ起動まで、リポジトリ直下の
[`start.bat`](start.bat) をダブルクリックする(またはコマンドプロンプトで実行する)だけで
1コマンドで完了する。事前に [`uv`](https://docs.astral.sh/uv/) と Node.js は導入しておくこと。

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

## DSP パイプライン(M1)

`backend/app/pipeline/` にステージ実装、`backend/app/api/media.py` に配信APIがある。

| ステージ / API | 実装 | 説明 |
| :--- | :--- | :--- |
| Stage 1 分離 | `pipeline/separate.py` | `demucs-onnx`。プリセット `fast`/`standard`/`high_quality` → `htdemucs`/`htdemucs_6s`/`htdemucs_ft`。出力は32bit float WAV(`workspace/{id}/stems/{name}.wav`) |
| Stage 2 ビート推定 | `pipeline/beat.py` | `beat-this`。`pipeline/time_signature.py` で拍子を自前導出(beat-this は拍子を出力しない)。出力は `workspace/{id}/analysis/beatmap.json` |
| BeatGridEditor 補正 | `pipeline/beatmap_edit.py` | オフセット/固定BPM上書き/ダウンビート回転/小節ごとの拍子上書きを純粋関数として実装 |
| 波形ピーク | `pipeline/peaks.py` | 初回リクエスト時に計算し `analysis/peaks/{name}.json` にキャッシュ |

ジョブとして実行するステージは `POST /api/projects/{id}/stages/{stage}/run` の `stage` に
`"separate"` または `"beat"` を指定する(`params: {preset, execution_provider}` は separate のみ)。

メディア・解析API(`api/media.py`):

| Method | Path | 説明 |
| :--- | :--- | :--- |
| GET | `/api/projects/{id}/audio/original` | 原曲配信(Range対応) |
| GET | `/api/projects/{id}/audio/stems/{name}` | ステム配信(Range対応) |
| GET | `/api/projects/{id}/analysis/peaks/{name}` | 波形ピーク(`name="original"` またはステム名) |
| GET | `/api/projects/{id}/analysis/beatmap` | `beatmap.json` |
| PATCH | `/api/projects/{id}/analysis/beatmap` | BeatGridEditor の手動補正を反映(`source: "manual"` になる) |

DirectML Execution Provider の実測ベンチマーク(Q-13, #17)は別途対応予定(詳細は次節)。

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

**Stage 1分離が途中で失敗した場合のファイル一貫性**: `run_separate_stage` はステムを1つずつ
アトミックに書き込むが、途中で例外が発生すると新旧のステムファイルが混在した状態で残る
(波形ピークキャッシュと分離ステージのメタデータ(`params_hash`)はどちらも無効化されるため、
少なくとも矛盾した波形データが表示され続けたり、混在ステムが誤ってスキップ判定でそのまま
使い回されたりすることは無い)。完全な一貫性(全ステムをステージング先に書いてから
ディレクトリごと入れ替える等)は未実装。

**`os.replace` によるアトミック置換とWindowsのファイルロック**: `storage.write_json` /
`_write_wav_atomically` は一時ファイル→`os.replace` でアトミックに書き込むが、Windowsでは
置換先を他プロセス(例: `/audio/stems/{name}` を配信中の `FileResponse`)が開いたままだと
`PermissionError` になりうる(POSIXでは同時オープン中でも置換できるため、この開発環境の
Linux上のテストでは検出されない)。ジョブ失敗時にはリトライ(再実行)で解消できるが、
配信中の置換が頻発する場合はリトライ付きの置換に変更する必要があり、Windows実機での
挙動確認が必要。

## テスト

| 対象 | コマンド |
| :--- | :--- |
| バックエンド(高速、既定) | `uv run --project backend pytest`(repo直下) |
| バックエンド(実モデル検証、`slow`) | `uv run --project backend pytest -m slow` |
| フロントLint/Format | `cd frontend && npm run lint` |
| フロント型チェック | `cd frontend && npm run typecheck` |
| フロント単体テスト | `cd frontend && npm test` |
| Electron E2E | `cd frontend && npm run build && npm run test:e2e` |

`slow` マーカーが付いたテストは `demucs-onnx`(htdemucs_6s, 約258MB)や `beat-this` の
チェックポイント(約77MB)を実際にダウンロード・推論するため、既定の `pytest` 実行からは
除外される(`pytest.ini` の `addopts`)。CI はこの2系統を別ステップとして両方実行する。
