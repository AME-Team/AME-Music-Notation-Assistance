# 引き継ぎドキュメント（2026-09-19更新・別PCへの引き継ぎ）

このファイルは、別のAIエージェントがこのリポジトリでの作業をスムーズに引き継ぐためのものです。
恒久的なプロジェクトドキュメントではないため、最新情報は必ず `git log` / `gh issue list` / `gh pr list` で確認してください。

**★最優先で読むこと**: セクション0に、今まさに進行中で中断しているタスク（M5実曲検証）の詳細と再開手順がある。作業マシンを乗り換えたため、この中断タスクから再開してほしい。

---

## 0. 進行中タスク: M5完了条件「実曲でconsistency-passが完走」の検証（中断中）

### 背景・経緯
M5（親Issue #6、L2 Coding Agentランタイム）の子Issue #42〜#53は全てクローズ済みだが、
親Issue #6自体の完了条件のうち以下が未達だった（過去のM2〜M4でも同型の「実曲/実音声検証未実施」パターンが繰り返し起きている）:

> `consistency-pass` が実曲で完走し、**検証違反を自己修正するログが Agent Console に見える**

ユーザーと相談の上、これを実際に実測で満たす作業に着手した。他の選択肢（#109調査、M6着手）は保留にしてこのタスクを優先している。

### 音源についてのライセンス確認（重要・ユーザー承認済み）
実曲入力として `OpenTracks`（旧DOVA-SYNDROME, https://opentracks.com/）の音源を使う方針。
このサイトのライセンスページ要約を確認したところ「AIのトレーニングへの使用」を禁止する記載があり、
音源分析（AMT等）がグレーゾーンに当たる可能性を**ユーザーに明示的に提示済み**。
その上でユーザーは「OpenTracksをそのまま使う（前例踏襲）」を選択している。
これはM4のspike実験（#67/#68/#107）で既に同じ運営元の音源を使った前例があるための判断。
**別のAIエージェントがこの判断を勝手に覆して別音源に差し替えるべきではない**（ユーザー了承済みの方針のため）。

### 使用する音源（M4と同一トラック）
- 曲名: 「かえるのピアノ」(作曲: こおろぎ)
- OpenTracks詳細ページ: `https://opentracks.com/bgm/detail/568`
- 楽器タグ: `ﾋﾟｱﾉ`（ピアノ単独 — 現状AMTはピアノ専用実装のため、この単一楽器構成が必須条件）
- 元の長さ: 1:36（96秒）
- **今回はM4と同様、冒頭32秒を切り出したクリップを使う**（L1のチャンク課金がチャンクあたり実測$0.5前後かかるため、コスト抑制のため）

#### 音源の再取得手順（ダウンロードURLは署名付きで数時間で失効するため、都度この手順で取り直す）
```bash
# 1. 詳細ページを取得し、audio(mp3)の署名付きURLを抽出する
curl -sL --max-time 15 "https://opentracks.com/bgm/detail/568" -o /tmp/pc_568.html
grep -oE 'https://dova-worker\.tracks-cid\.workers\.dev\?filepath=bgm%2Faudio%2F[^"]*' /tmp/pc_568.html | head -1
# → 出力されたURL(&amp;は&に読み替える)をcurlでダウンロードする
curl -sL --max-time 30 "<上記URL>" -o /tmp/ame_realsong/kaeru_no_piano_full.mp3

# 2. 冒頭32秒をwavに切り出す(mp3書き出しはlibsndfileが非対応なのでwavにする。
#    ALLOWED_FORMATS = {"mp3","wav","flac","m4a"} なのでwavはそのままプロジェクト作成に使える)
cd backend && .venv/bin/python -c "
import soundfile as sf
import librosa
y, sr = librosa.load('/tmp/ame_realsong/kaeru_no_piano_full.mp3', sr=None, mono=True)
sf.write('/tmp/ame_realsong/kaeru_no_piano_clip32s.wav', y[: int(32*sr)], sr)
"
```
※ 前回(このセッション)ダウンロード済みのファイルは `/tmp/ame_realsong/` 配下に置いたが、
**別マシンでは`/tmp`は共有されないため上記手順で必ず再取得すること**。

### ここまでで完了した作業
1. 上記手順で音源を取得・32秒クリップ化(このマシンの`/tmp/ame_realsong/kaeru_no_piano_clip32s.wav`で確認済み、正常に読み込めた)。
2. バックエンドを起動: `cd backend && AME_BACKEND_PORT=8123 .venv/bin/python -m app.main`
3. プロジェクト作成に成功:
   ```bash
   curl -s -X POST http://127.0.0.1:8123/api/projects -F "file=@kaeru_no_piano_clip32s.wav;type=audio/wav"
   ```
   → `proj_rqpxtqahkk78` が作成された(ただしこのproject_idは**このマシンの`backend/workspace/`にしか存在しない**。gitignore対象で他マシンには無いので、新マシンでは新規にプロジェクト作成からやり直すこと)。
4. Stage 1 (`separate`、Demucs音源分離)を実行 → **OOM Killerに強制終了された**(詳細は次項)。ここで作業を中断している。

### ブロッカー: メモリ不足によるOOM Kill(未解決・要環境選定)
```
POST /api/projects/{id}/stages/separate/run
→ job status: failed, exit_code=-9
dmesg: oom-kill: ... Killed process (python) total-vm:3452388kB, anon-rss:3118672kB
```
- 原因: `demucs-onnx`(standard preset = `htdemucs_6s`、ピアノ専用ステムを得るために必須のpreset)の分離ワーカー単体で**約3GB**の常駐メモリを要求する。
- このセッションを実行していたマシンには他の長時間稼働プロセス(複数の`claude`セッション、`opencode serve`×2、hermes-agent等)が常駐しており、物理メモリ7.6GB中実質空きが2.5〜3GB、**スワップ2GBも既にほぼ枯渇(残り1.8MB)**していたため、Demucsプロセスがカーネルにkillされた。
- 対策として一時swapfile追加(`sudo fallocate -l 4G /swapfile_ame_tmp && sudo mkswap ... && sudo swapon ...`)を試みたが、**このセッションは非対話TTYのためsudoパスワード入力ができず失敗**。ユーザーはこの時点で「作業マシンを変える」ことを選択した。

### 新マシンでの再開手順
1. **着手前に必ず `free -h` で空きメモリを確認する。** 目安: 分離ワーカーに最低4GB、他プロセス分も含めるとシステム全体で6GB以上の空き(RAM+swap)が欲しい。不足していれば、新マシンでは(a) sudo swapfile追加を試す、(b) 他の重いプロセスを止められないかユーザーに確認する、のいずれかをまず行う。
2. 上記「音源の再取得手順」でクリップを用意し、プロジェクトを新規作成する。
3. パイプラインを順に実行し、各ジョブの完了を待つ(`POST /api/projects/{id}/stages/{stage}/run` → `GET /api/jobs/{job_id}` をポーリング、`status`が`succeeded`になるまで):
   - `separate` → `beat` → `transcribe` → `quantize` (有効な`VALID_STAGES`は`app/services/job_service.py`参照)
4. L0/L1整音を実行する: `POST /api/refine`(詳細は`app/api/refine.py`、`RefineRequest`スキーマは`app/api/schemas.py`)。L1は`claude` CLI経由(#104)で実行されるため、**Anthropic APIキーは不要**だが、実行マシンに認証済み`claude` CLIが必要(`shutil.which("claude")`で判定、`app/pipeline/refine/l1_client.py:is_claude_cli_available()`)。実行前に `claude --version` 等で認証状態を確認すること。
5. L2エージェントrunを起動する:
   ```
   POST /api/projects/{project_id}/agent/runs
   { "task_type": "consistency-pass", "provider": "claude" }
   ```
   `GET /api/agent/runs/{run_id}/events` (SSE)で`AgentEvent`列を観察し、**検証違反(V-1〜V-10、特に#107修正済みのV-8)を検出して自己修正するツール呼び出し列がログに現れるか**を確認する。これがM5完了条件の核心。
6. キャンセル時の不変性も合わせて実測確認する: run中に`POST /api/agent/runs/{run_id}/cancel`を叩き、`current.json`(score)のハッシュ/内容が変化していないことを確認する(設計書§8.4、`storage.score_current_path`参照)。
7. 3点(実曲完走+自己修正ログ、2プロバイダ契約テストは#53で実証済み、キャンセル時不変性)が揃ったら、**ユーザーに結果を報告し、承認を得てから**親Issue #6をクローズする(このプロジェクトでは「実測結果を提示 → ユーザーが最終判断」という運用が徹底されている。勝手にクローズしない)。
8. 記録として、実測結果(違反率・自己修正の有無・トークン/コスト)をIssue #6にコメントする(#67/#68のコメント形式を参考にすること)。

### 注意点
- L1のコストは実測で1チャンクあたり$0.5前後かかる(#67コメント参照)。L2エージェントrunも同様にトークン課金が発生するため、必要以上に何度もリトライしない。
- 32秒クリップは小節数が少ない(前回実測: 194ノート・17小節)。`consistency-pass`が「自己修正」を実演するには、意図的にV-8等の違反が起きやすい範囲(前回#107/#109の知見: 同時発音コードでvoice分割ルールを無視するパターンが頻発)を選ぶと再現しやすい。
- このタスクを完了(またはさらに別の理由で中断)した場合は、このセクション0を更新するか削除し、後続作業者が混乱しないようにすること。

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

**★最優先はセクション0（進行中・中断中のM5実曲検証）。まずそちらを再開すること。**

セクション0のタスクが完了(またはユーザー了承のもと保留)になった後の優先度順候補：

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
