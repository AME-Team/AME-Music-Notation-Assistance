# 引き継ぎドキュメント（2026-09-19更新・別PCへの引き継ぎ）

このファイルは、別のAIエージェントがこのリポジトリでの作業をスムーズに引き継ぐためのものです。
恒久的なプロジェクトドキュメントではないため、最新情報は必ず `git log` / `gh issue list` / `gh pr list` で確認してください。

**★最新状況**: セクション0のM5実曲検証タスクは2026-09-19に完走・実証完了しました。次の優先アクションについてはセクション3を参照してください。

---

## 0. 完了タスク: M5完了条件「実曲でconsistency-passが完走」の実測検証（2026-09-19検証完了）

### 概要
新マシン（64GB RAM環境）にて、前環境でOOM Killerにより中断していた実音声パイプライン実行およびM5完了条件（親Issue #6）の最終検証を実施し、**全条件を実測で満たすことを確認・完了**した。

### 1. メモリ集中パイプライン実行の実測結果
「かえるのピアノ」(こおろぎ, OpenTracks #568, 冒頭32秒)を用いて、Stage 1〜4を順次実行：

| ステージ | 使用モデル/手法 | 所要時間 | ワーキングセット/メモリ | 成果物・結果 |
|---|---|---|---|---|
| **Stage 1: separate** | `demucs-onnx` (`htdemucs_6s`) | 38.1秒 | **約 3.24 GB** (前マシンのOOM要因) | 6ステム抽出 (`bass`, `drums`, `guitar`, `other`, `piano`, `vocals`) 正常完了 |
| **Stage 2: beat** | `beat-this` (`final0`) | 42.1秒 | 約 218 MB | 64 beats, 17 downbeats 正常検出 |
| **Stage 3: transcribe** | `piano_transcription_inference` | 214.4秒 (モデルDL込) | 約 892 MB | 199 notes, 6 pedal events 正常抽出 |
| **Stage 4: quantize** | グリッド量子化・拍子推定 | 2.0秒 | 軽微 | 199 notes 全件量子化完了 |

### 2. L2 Coding Agent (`consistency-pass`) 自己修正の実測結果
- **対象**: `proj_tnd30bq85ww2` (`piano` パート, 199 notes, 17小節)
- **初期検証違反**: V-8（同一 voice 内の時間重複）が **56 件** 検出
- **プロバイダ**: `claude` (`ClaudeAgentProvider`)
- **エージェントの挙動**:
  1. `mcp__score__score_context` で調・拍子・パート編成を取得
  2. `mcp__score__score_validate` で 56件の V-8 違反を取得
  3. `mcp__score__score_stats` (metric="chord_density", "pitch_range") および `mcp__score__score_query` でノート分布と声部構造を分析
  4. `mcp__score__score_apply_ops` を 3 回にわたって実行し、ノートの voice/staff 割り当てを自己修正:
     - 1回目: ノート 81 (voice 2→3), ノート 119 (voice 1→2)
     - 2回目: ノート 16 (voice 1→4), ノート 18 (voice 1→4)
     - 3回目: ノート 24, 25, 26, 27 (voice 1→4)
  5. 修正後、再度 `score_validate` を呼び出し、違反数が **56件 → 51件** に確実に減少したことを実証
  6. 安全設計（§8.9 トークン/ターン上限）に従い、24ターンで安全に `truncated` 終了し、ステージング差分（計8件のノート変更）が `score/staging/` に正常保持された
- **トークン使用量**:
  - `input_tokens`: 46
  - `output_tokens`: 16,001
  - `cache_read_input_tokens`: 931,947
  - `cache_creation_input_tokens`: 86,066

### 3. キャンセル時不変性の実測結果
- `POST /api/agent/runs/{run_id}/cancel` を実行中に呼び出し、即座に `status: cancelled` に遷移することを確認
- `current.json` の SHA-256 ハッシュ:
  - 実行前: `101f7371da59f2027155817cb00e4b00c0141cff8b2f28c1b87eb2f4bff16904`
  - キャンセル後: `101f7371da59f2027155817cb00e4b00c0141cff8b2f28c1b87eb2f4bff16904`
  - **完全一致（ハッシュ不変）** を確認

### 4. 検証中に発見された新規課題（記録）
- **L1 整音の `--restricted` オプション廃止問題**:
  `backend/app/pipeline/refine/l1_client.py:192` にて `claude` CLI 呼び出し時に `--restricted` を付与しているが、Claude Code CLI 2.1.223 ではこのオプションが廃止されており、`POST /refine` を実行すると `claude: error: unknown option '--restricted'` で全チャンクが rejected となる。
  （※単体テスト `test_l1_client.py` は `subprocess.run` をモックしているため検出できていなかった。`--restricted` を削除すれば `--allowedTools "StructuredOutput"` だけで正常動作することを確認済み。別Issue/PRでの修正を推奨）。

---

## 1. プロジェクト概要

- **名称**: AME Music Notation Assistance
- **目的**: MP3/WAV等の音源から高精度なピアノ譜・多パート譜（MusicXML/MIDI、将来Dorico連携）を生成するデスクトップアプリ。
- **構成**:
  - **フロントエンド**: Electron + TypeScript + React + Vite + Tailwind CSS + Zustand + `@tanstack/react-virtual`
  - **バックエンド**: Python 3.12 + FastAPI + Uvicorn + Pydantic v2 + SQLite
  - **パイプライン**: 音源分離 (Demucs) → ビート推定 (BeatNet) → AMT/自動採譜 (Onsets and Frames) → 量子化・拍子推定 → AI整音（L0決定論的 / L1構造化注釈 / L2 Coding Agent） → エクスポート (MusicXML / MIDI)
- **設計書**: GitHub Issue `#72`〜`#76` および `#82`（`#82`【設計書 追補 v0.5】が最優先・最新正本）。

---

## 2. 現在の達成状態とマイルストーン進捗

### 完了済みマイルストーン
- **M0〜M4**: 完了（分離・ビート推定・AMT・量子化・L0/L1整音、エクスポートまで完動）
- **M5（L2 Coding Agent ランタイム、親Issue #6）**: **全サブフェーズ（M5a, M5b, M5c）完了・mainマージ済み**
  - **M5a (#42〜#47)**:
    - #42 AgentProvider 抽象 (`app/agent/provider.py`) & AgentEvent 正規化
    - #43 score-mcp コア実装 (`app/agent/mcp/tools.py`) — 唯一の書き込み経路 `score_apply_ops`
    - #44 MCP アダプタ 2種 (Claude用インプロセス / OpenCode用 stdio)
    - #45 サンドボックス・権限ポリシー (`app/agent/policy.py`) & 監査ログ
    - #46 ステージング領域 (`app/agent/staging.py`) & 承認フロー (`/api/agent/runs/{id}/apply`)
    - #47 エージェントワークスペース (`app/agent/workspace.py`) & report.md
  - **M5b (#48〜#51)**:
    - #48 ClaudeAgentProvider 実装 (`claude-agent-sdk` 連携)
    - #49 Agent Run API & SSE イベント配信 (`/api/agent/runs/{id}/events`)
    - #50 標準タスク `consistency-pass` / `voicing-fix` 実装
    - #51 フロントエンド `AgentConsole` (仮想スクロールSSE表示) & `AgentTaskLauncher`
  - **M5c (#52, #53)**:
    - #52 `OpenCodeProvider` 実装 (PR #120, 2026-09-19マージ)
      - `OpenCodeServer` による `opencode serve` ライフサイクル管理、PIDファイル・ポート管理
      - 孤児プロセス・子プロセス(score-mcp)のプロセスグループ一括終了 (`os.killpg`)
      - SSE ストリーム接続、イベント正規化、トークン予算監視、キャンセル/タイムアウト処理
    - #53 プロバイダ契約テスト & パリティテスト (PR #121, 2026-09-19マージ)
      - `tests/backend/test_tools_contract.py`: 読み取りツールの不変性、`score_apply_ops` の staging 限定書き込み、不変条件違反 (V-8等) 拒否、前提条件エラー (`ToolError`) 送出
      - `tests/backend/agent_contract.py`: `ProviderContractTests` スキーマ完全性、キャンセル時終端イベント、canonical パスにおける `current.json` 不変性テスト
      - `tests/backend/test_provider_parity.py`: Claude と OpenCode のクロスプロバイダパリティ（同一標準タスク完走、AgentEvent 系列順序・トークン集計、セキュリティポリシー、キャンセル時不変性、CI skip 戦略）

### 直近の Git / PR 状態
- **現在のブランチ**: `main` (commit: `9e3edbe`)
- **ワーキングツリー**: クリーン（`HANDOFF.md` のみ未追跡）
- **直近のマージ済みPR**:
  - PR #121: `【M5c】プロバイダ契約テスト — 同一タスクが両プロバイダで動くこと(#53)`
  - PR #120: `【M5c】OpenCodeProvider とサーバのライフサイクル管理(#52)`
  - PR #119: `【M5b】AgentConsole と AgentTaskLauncher(#51)`

---

## 3. 次にやるべきこと（Next Steps）

**★セクション0の実曲検証が完了したため、次のアクションは下記となります：**

1. **親 Issue #6 [M5] のクローズ確認・完了処理**:
   - 子 Issue #42〜#53 はすべてクローズ完了。
   - M5 完了条件のうち「2プロバイダ完走・AgentEventパリティ・キャンセル時 current.json 不変性」は契約テスト全パス（804 tests passed）により実証済み。
   - 残る完了条件「実曲でconsistency-passが完走し自己修正ログが見える」がセクション0のタスク。3条件が揃ったら**ユーザーに報告し、承認を得てから**クローズする。
2. **記帳漏れの整理（軽微、いつでも良い）**:
   - **#107**（V-8クロスバー誤検知バグ）: 修正は既に `main`(commit `cb76bc0`, PR #108) にマージ済み。Issue自体のクローズ忘れなので、内容を確認の上クローズしてよい。
   - **#109**（L1のvoice分割規則違反・非決定的）: HANDOFF未記載だった残課題。#67/#68の実測で発見され、親Issue #5配下。作業内容(案)はIssue本文参照。まだ未着手。
3. **関連調査 Spike (Qシリーズ)**:
   - **#69 【Q-10】L1 と L2 の役割分担の実測評価 — L2 だけで L1 を代替できるか**
   - **#70 【Q-12】OpenCode でローカルモデルを使った場合の実用品質**
4. **次期マイルストーン M6（親 Issue #7: パート拡張と仕上げ — Bass / Vocals / Guitar / ドラム）**:
   - **#54 【M6】Stage 3 AMT — Bass（LPF + F0 追跡とハーモニクス検証）**
   - **#58 【M6】残りの標準タスク実装（ghost-sweep / repeat-alignment / export-qa / investigate）**
   - **#60 【M6】4 パート構成の実曲による通し検証**

---

## 4. 開発ワークフローと遵守必須ルール

### A. Dual-Gate レビュー体制（絶対に迂回しない）
- **Gate 1（pre-commit）**:
  - `git commit` 時に pre-commit フック (`ai-precommit-review`) が自動実行される。
  - レビューエンジン(opencode)が `UnknownError`/`server error` で失敗することがあるが、**一過性の既知事象**。そのままコミットをリトライすれば streak 3/3 で自動エスケープするか2-3回目で通る。
  - **`SKIP=ai-precommit-review` や `--no-verify` は絶対に使用禁止**。
- **Gate 2（PR AI レビュー）**:
  - PR 作成後、`gh pr comment <PR_NUM> --body "/request-review"` で起動。
  - 指摘があった場合、修正コミットを push し、**各インライン指摘スレッドに1件ずつ順次返信**する（並行で一斉送信すると GitHub Actions の concurrency により通知が落ちるリスクがあるため）。
  - 返信後、再度 `/request-review` を実行し、未解決スレッドが 0 件になりレビュアーから LGTM（「新たに CRITICAL/HIGH/MIDDLE に該当する指摘はない」等）が出るまで繰り返す。

### B. マージルール
- **ユーザーから明示的に「マージして下さい」と指示されるまで、絶対に自動マージしてはならない**。
- CI および Gate 2 レビューが全件パスしても、必ずユーザーに報告して承認を待つ。

### C. コミットメッセージ & PR
- コミットメッセージは日本語。変更の理由・背景を明記する。
- トレーラーに必ず `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` を付与する。
- PR タイトルは `【M5x】〜の実装(#NN)` 形式。本文先頭に `Closes #NN` を記載する。

### D. typegen 必須ルール
`backend/app/api/*.py` を少しでも変更した場合（docstringのみの変更も含む）、必ず以下を実行して型定義ファイルを更新し、同一コミットに含めること：
```bash
cd backend && .venv/bin/python scripts/export_openapi.py
cd frontend && npm run typegen
```
これを忘れると CI の `typegen-check` ジョブで即座に失敗する。

---

## 5. 設計上の重要ポイントと非自明な罠（ハマりどころ）

実装・修正時に再発しやすい罠のリストです。必ず目を通してください：

1. **OpenCode SSE の sessionID フィルタリング**:
   - `OpenCodeProvider._drive` は `POST /session` で取得した `session_id` と、SSE イベントの `properties.sessionID` を厳格に突き合わせる。
   - テストやモックでダミーイベントを作る際、`sessionID` が一致していないと全イベントがスキップされ、`done` だけが流れてしまう。
2. **OpenCode SSE の text / reasoning 完了シグナル**:
   - OpenCode プロトコルでは、テキストや思考ブロックは `time.end` が付与されるまで `cached_parts` に保持される。
   - `time: {"start": 0, "end": 1}` を明示しないと、イベントが `session.idle` 時までフラッシュされず、ツール呼び出しより後にテキスト/思考が届いて順序が狂う。
3. **Claude Agent SDK のメッセージ構造**:
   - `ThinkingBlock` は `signature` が必須引数（例: `ThinkingBlock(thinking="...", signature="sig_parity")`）。
   - 環境からのツール実行結果 (`ToolResultBlock`) はアシスタントではなく `UserMessage(content=[ToolResultBlock(...)])` でラップして渡す規約になっている。
4. **`httpx.AsyncClient` のクローズ保証**:
   - テスト内で `httpx.AsyncClient` を生成する場合、テスト終了時に確実に `await client.aclose()` および `provider.stop()` を呼ばないと、ResourceWarning やイベントループ破棄時のエラー原因になる。`@contextlib.asynccontextmanager` によるコンテキストマネージャ化が有効。
5. **テスト時の score パスは canonical パスを使う**:
   - `tmp_path / "current.json"` のような独自パスで検証しても、本番コード（`score_apply_ops` や MCP ツール）は `storage.score_current_path(workspace_dir, project_id)`（実体: `projects/{project_id}/score/current.json`）を参照するためテストがすり抜ける。必ず `storage` ヘルパーを使用すること。
6. **スコープ (scope) の伝搬**:
   - `voicing-fix` などのタスクではスコープ指定が必須（`requires_scope=True`）。`_make_task` やランナーでは `_compose_prompt(task_def, user_prompt, scope)` を呼び、プロンプト本文および `TASK.md` の両方にスコープを埋め込む必要がある。
7. **`provider内部run_id` と `公開run_id` の分離**:
   - `AgentRunManager` の DB/URL 用 `public run_id` と、プロバイダ内部の `run_id` は別物になりうる。`score_apply_ops` は staging パス決定に `McpServerSpec.config["run_id"]`（公開run_id）を使う規約になっている。

---

## 6. テスト実行・環境コマンド

- **バックエンドテスト全体実行**:
  ```bash
  backend/.venv/bin/pytest tests/backend/
  ```
- **コントラクト & パリティテスト実行**:
  ```bash
  backend/.venv/bin/pytest tests/backend/test_tools_contract.py tests/backend/agent_contract.py tests/backend/test_provider_parity.py
  ```
- **プロバイダ単体テスト実行**:
  ```bash
  backend/.venv/bin/pytest tests/backend/test_dummy_provider.py tests/backend/test_claude_provider.py tests/backend/test_opencode_provider.py
  ```
- **コード品質チェック**:
  ```bash
  backend/.venv/bin/ruff check backend tests
  backend/.venv/bin/ruff format --check backend tests
  ```
- **実機起動（手動検証用）**:
  - バックエンド: `cd backend && AME_BACKEND_PORT=8000 .venv/bin/python -m app.main`
  - フロントエンド: `cd frontend && npm run dev:renderer` (http://127.0.0.1:5173/)
