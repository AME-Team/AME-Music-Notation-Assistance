# AME Music Notation Assistance

MP3 → MIDI/MusicXML の自動採譜・AI整音アシスタント。**Windows 11 x64 専用**(NFR-08′)。
Electron(TypeScript) + Python(FastAPI) 構成。設計の全文は GitHub Issue に転記されている
(#72〜#76 が v0.4、#82 が Windows専用化+Electron採用のv0.5追補で最優先)。

このドキュメントは M2(#3、MP3→MusicXMLの縦貫通)時点の状態を記す。

## ディレクトリ構成

```
backend/     FastAPI サーバ・DSP Worker(Python 3.12, uv 管理)
frontend/    Electron + Vite/React(TypeScript)
tests/backend/  pytest によるバックエンドのテスト
workspace/   実行時生成データ(.gitignore対象)。SQLite・プロジェクトごとの音声/成果物
```

`backend/app/domain/` は外部ライブラリに一切依存しない(§5.2)。Score IR・編集オペレーション・
検証ロジックは将来ここに実装し、単体テストを厚くする方針を維持すること。

## アプリの起動手順 (Windows)

本アプリは **Electron が起動時に Python バックエンドを自動的に子プロセスとして起動・管理する設計** になっています。そのため、**バックエンドを手動で起動する必要はありません**。

### 1. 最も簡単な起動方法 (ワンクリック / 推奨)

リポジトリ直下の [`start.bat`](start.bat) をダブルクリックする（またはコマンドプロンプト / PowerShell から実行する）だけで、初回セットアップからアプリ起動までが自動で行われます。

- 事前準備:
  - [`uv`](https://docs.astral.sh/uv/) (Python 3.12 パッケージマネージャ)
  - [Node.js](https://nodejs.org/) (LTS)
- 起動方法:
  ```cmd
  .\start.bat
  ```
  初回実行時は自動的にバックエンドの `uv sync` とフロントエンドの `npm install` が実行され、続いて Electron アプリとバックエンドが立ち上がります。

---

### 2. コマンドラインからの起動手順 (日常の開発・実行)

#### 初回セットアップ (初回のみ)

```cmd
:: 1. バックエンドの依存関係インストール
cd backend
uv sync

:: 2. フロントエンドの依存関係インストール
cd ..\frontend
npm install
```

#### アプリ起動コマンド

セットアップ後は、**以下のコマンドだけでアプリ全体（フロントエンド + バックエンド）が起動します**:

```cmd
cd frontend
npm run dev
```

> **内部で何が起きるか**:
> 1. Vite 開発サーバー (`localhost:5173`) が起動します。
> 2. Electron メインプロセスが起動します。
> 3. Electron がバックグラウンドで Python バックエンド (`uv run --project backend python -m app.main`) を空きポートで自動 spawn します。
> 4. ヘルスチェック疎通確認後、アプリウィンドウが表示されます。
> 5. アプリウィンドウを閉じると、Python プロセスも自動的に終了・クリーンアップされます。

---

### 3. 本番ビルド版での起動確認

```cmd
cd frontend
npm run build
npm start
```

---

### 4. 開発者向け個別コマンド (単体テスト・API単体起動など)

バックエンド単体でのテストや API 動作確認を行いたい場合のみ、以下を使用します:

- **バックエンドの単体テスト**:
  ```bash
  cd backend
  uv run pytest ../tests/backend
  ```
- **バックエンドの静的検査**:
  ```bash
  cd backend
  uv run ruff check . ../tests/backend
  ```
- **バックエンドの単体起動 (API のみ確認・Swagger UI 閲覧時など)**:
  ```bash
  cd backend
  uv run python -m app.main --port 8000
  ```
  起動後、ブラウザで `http://127.0.0.1:8000/docs` を開くと Swagger UI を確認できます。

**備考**:
- パッケージマネージャは npm を採用 (pnpm は未導入)。
- フロントエンドの Lint/Format は Biome を採用 (`npm run lint` / `npm run format`)。
- バックエンドの依存関係の詳細・注意点は [`backend/DEPENDENCY_NOTES.md`](backend/DEPENDENCY_NOTES.md) を参照。

## ログ出力と保存期間

Electron アプリ実行時のログ (メインプロセス、画面コンソール、Python バックエンドの stdout/stderr) は日付別ログファイルに自動出力されます。

- **保存場所**: `%APPDATA%\ame-frontend\logs\` (`app.getPath("userData")/logs/`)
- **ファイル名形式**: `app-YYYY-MM-DD.log`
- **保存期間**: **1 週間 (7 日間)**。ログ肥大化を防止するため、7 日を超過した古いログファイルは起動時に自動削除されます。
- **ログフォルダの確認方法**: アプリメニューの **[ヘルプ] → [ログフォルダを開く]** からエクスプローラーで直接開くことができます。

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

## DSP パイプライン(M1/M2)

`backend/app/pipeline/` にステージ実装、`backend/app/api/media.py`/`api/score.py`/`api/export.py`
に配信・取得・書き出しAPIがある。

| ステージ / API | 実装 | 説明 |
| :--- | :--- | :--- |
| Stage 1 分離 | `pipeline/separate.py` | `demucs-onnx`。プリセット `fast`/`standard`/`high_quality` → `htdemucs`/`htdemucs_6s`/`htdemucs_ft`。出力は32bit float WAV(`workspace/{id}/stems/{name}.wav`)。ピアノステムは`standard`(htdemucs_6s)でのみ生成される |
| Stage 2 ビート推定 | `pipeline/beat.py` | `beat-this`。`pipeline/time_signature.py` で拍子を自前導出(beat-this は拍子を出力しない)。出力は `workspace/{id}/analysis/beatmap.json` |
| BeatGridEditor 補正 | `pipeline/beatmap_edit.py` | オフセット/固定BPM上書き/ダウンビート回転/小節ごとの拍子上書きを純粋関数として実装 |
| 波形ピーク | `pipeline/peaks.py` | 初回リクエスト時に計算し `analysis/peaks/{name}.json` にキャッシュ |
| Stage 3 AMT(採譜) | `pipeline/transcribe/{piano,bass,vocals,guitar}.py` | パートごとに方式が異なる: **piano** = `piano_transcription_inference`(ポリフォニック+ペダル)。**bass/vocals** = LPF+F0追跡/TorchCREPE(モノフォニック、`voice=1`/`staff=1`固定で書き込む)。**guitar/other** = Basic Pitch(Spotify, Apache-2.0)の公開ONNXモデルを`onnxruntime`で直接推論(ポリフォニックだが現状は同じく`voice=1`/`staff=1`固定、#56)。ノートは一切削除せず、ゴースト候補は`Note.flags`へ引き継ぐ。出力は`workspace/{id}/score/current.json`(Score IR、`app/domain/score.py`) |
| Stage 4 量子化+L0整音 | `pipeline/quantize.py` + `pipeline/refine/baseline.py` | 決定論的クオンタイズ(拍子・テンポマップに沿ってスナップ)の直後に、AI(L1/L2)を使わない決定論的な整音(異名同音・声部・段割り当て)をステージ末尾で実行する。**既知の制約**: 本ステージは現状pianoパートのみを処理する。bass/vocals/guitar/otherのノートは`onset_tick`/`duration_tick`/`spelling`が設定されないため、Stage 6書き出しの対象にならない(#60の実曲検証で対応予定) |
| Stage 6 書き出し | `pipeline/export/{score_builder,musicxml,midi}.py` | partitura経由でScore IR→MusicXML/Standard MIDI Fileへ変換。量子化+L0実行済み(`onset_tick`/`spelling`設定済み)が前提で、未実行は`ExportError`(API層で422)。Doricoへのインポート手順・推奨設定・検証チェックリストは [`docs/dorico-import.md`](docs/dorico-import.md) 参照(#28) |
| ステージ独立再実行/無効化(#29) | `domain/stages.py` + `services/stage_invalidation.py` | `separate→transcribe→quantize`/`beat→quantize`の依存グラフ。上流が実際に再実行(または手動編集)されると下流の`analysis/{stage}.meta.json`を削除し、`stale`として要再実行を示す |

ジョブとして実行するステージは `POST /api/projects/{id}/stages/{stage}/run` の `stage` に
`"separate"`/`"beat"`/`"transcribe"`/`"quantize"` を指定する(`params: {preset,
execution_provider}` は separate のみ)。`force: true` を指定すると入力/パラメータ不変でも
再実行できる(#29、上流無効化と組み合わせて使う)。

メディア・解析API(`api/media.py`):

| Method | Path | 説明 |
| :--- | :--- | :--- |
| GET | `/api/projects/{id}/audio/original` | 原曲配信(Range対応) |
| GET | `/api/projects/{id}/audio/stems/{name}` | ステム配信(Range対応) |
| GET | `/api/projects/{id}/analysis/peaks/{name}` | 波形ピーク(`name="original"` またはステム名) |
| GET | `/api/projects/{id}/analysis/beatmap` | `beatmap.json` |
| PATCH | `/api/projects/{id}/analysis/beatmap` | BeatGridEditor の手動補正を反映(`source: "manual"` になる、実際に補正が適用されると量子化(#29)を無効化する) |

Score IR・エクスポートAPI(`api/score.py`/`api/export.py`):

| Method | Path | 説明 |
| :--- | :--- | :--- |
| GET | `/api/projects/{id}/score` | Score IR全体(採譜=transcribe未実行なら404)。`analysis/quantize.meta.json`が無効化されて`stale`(#29)な状態でも、`score/current.json`自体は削除されないため200で(古い可能性のある)Score IRを返す。呼び出し元は`GET /api/projects/{id}`が返す`stages.quantize.stale`で要再実行かどうかを判別すること。部分小節プレビュー(`/score/preview.musicxml`)はM2スコープ外、将来PRで対応予定 |
| POST | `/api/projects/{id}/export` | `{format: "musicxml"\|"midi"}`。同期処理(モデル推論を伴わないためジョブ化しない)。生成物は`workspace/{id}/export/score.{musicxml,mid}`にも保存する |

リビジョン管理API(`api/revisions.py`, #59, FR-15):

| Method | Path | 説明 |
| :--- | :--- | :--- |
| POST | `/api/projects/{id}/revisions` | 現在のScore IRスナップショットから名前付きリビジョンを作成(`{name, description?}`) |
| GET | `/api/projects/{id}/revisions` | リビジョン一覧取得(新しい順) |
| GET | `/api/projects/{id}/revisions/{rev_id}` | リビジョン詳細取得 |
| GET | `/api/projects/{id}/revisions/{rev_id}/diff` | 指定リビジョンと現在スコア(またはbase_revision_id)とのパート別ノート統計差分 |
| POST | `/api/projects/{id}/revisions/{rev_id}/restore` | 指定リビジョンへスコアを復元(Undo/Redoスタックをクリアし、`score/revisions.jsonl`に監査ログ追記) |
| DELETE | `/api/projects/{id}/revisions/{rev_id}` | リビジョンメタデータの削除 |

コード進行自動推定(`pipeline/harmony.py`, #57, FR-16):
- Stage 4(quantize)完了時に全パートのノート発音区間・持続音からピッチクラス分布(クロマベクトル)を集計し、ベース音優先重み付けと調号に沿ったテンプレートマッチングでコード進行を自動推定。
- `ScoreIR.chords` に格納され、L1 チャンク文脈(`chord_hints`)、L1 楽曲全体プロンプト、および L2 MCP ツール `score_context` に自動注入される。


DirectML Execution Provider の実測ベンチマーク(Q-13, #17)は別途対応予定(詳細は次節)。

## 既知の制約(Known Limitations)

このリポジトリの自動テストは Linux 環境で実行されているため、以下は **Windows 11 実機での
最終確認が別途必要**:

- `python.exe` がアプリ終了後に残らないことの確認(Linux では同等のプロセス残留確認で代替検証)
- Windows 資格情報マネージャー経由の API キー解決(`keyring` 経由。Linux では環境変数のみで
  フォールバックすることを確認済み)
- DirectML Execution Provider(Radeon 780M での実測ベンチマークは M1 で実施。
  `uv run --project backend python backend/scripts/benchmark_separation.py <音声ファイル>
  --preset standard --out report.md` で計測する。この開発サンドボックス(Linux)の
  `onnxruntime` は DirectML を含まないため、`DmlExecutionProvider` が検出できず自動的に
  CPU計測のみになる。NFR-01(実性能に基づく最終的な推奨設定)の確定にはWindows実機での
  再実行結果が必要)
- Windows Defender / SmartScreen の未署名バイナリ警告(署名方針は M6 の Q-17 で決定)
- `frontend/e2e/app.spec.ts`(Playwright, Electron 実起動スモークテスト)は Chromium を
  ヘッドで動かすため `libgtk-3` 等のシステムライブラリが必要。この開発サンドボックスには
  未導入のため、CI(windows-latest)または実機での実行を前提とする

`ffmpeg` の同梱方式(`beat-this` が非WAV入力に要求する)は未解決のまま(#79 参照、M6のQ-17と関連)。

**MIDI書き出しの複数パート時のチャンネル衝突(#27)**: `partitura.save_score_midi`は単一声部の
パートを既定でMIDIチャンネル0に割り当てる。M2はピアノ1パートのみのため顕在化しないが、
将来複数パート(ギター/ボーカル等)に対応する際は、`pipeline/export/midi.py`の
`_apply_midi_programs`と合わせてチャンネル割り当ての見直しが必要。

**Score IR取得APIのpreviewは未実装(#23)**: `GET /score/preview.musicxml?bars=1-16`
(部分小節プレビュー、設計書§11.5)はM2完了条件に含まれないため見送った(ユーザー確認済み)。
`GET /score`(全体取得)のみ実装済み。

**Stage 1分離が途中で失敗した場合のファイル一貫性**: `run_separate_stage` はステムを1つずつ
アトミックに書き込むが、途中で例外が発生すると新旧のステムファイルが混在した状態で残る
(波形ピークキャッシュと分離ステージのメタデータ(`params_hash`)はどちらも無効化されるため、
少なくとも矛盾した波形データが表示され続けたり、混在ステムが誤ってスキップ判定でそのまま
使い回されたりすることは無い)。加えて `should_skip_stage` はハッシュ一致に加え、記録した
期待ステム名がディスク上に(部分集合として)全て揃っているかも確認するため、ステムの手動
削除やジョブの強制終了(Python例外を経由しないkillで上のクリーンアップが走らないケース)
でメタデータだけが残っても、スキップ判定は成立しない。ただしこれはファイル名の存在確認に
留まり、内容までは検証しない: 個々のファイル自体は一時ファイル→`os.replace`でアトミックに
書かれるため中途半端な内容になることは無いが、**同名の別プリセット出力で完全に上書きされて
いた場合**(強制終了のタイミング次第であり得る)は検出できない。完全な一貫性(全ステムを
ステージング先に書いてからディレクトリごと入れ替える等)は未実装。

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
