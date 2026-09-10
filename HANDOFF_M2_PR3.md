# M2 (Issue #3) 引き継ぎメモ — PR3残タスク

作成日: 2026-09-10。作業を別のAIエージェントに引き継ぐために作成。

## プロジェクト概要

AME Music Notation Assistance。MP3等の音源からMusicXML(Dorico向け)を
生成するデスクトップアプリ(Windows専用、Electron+TypeScriptフロントエンド、
FastAPIバックエンド)。設計書はGitHub Issue #72〜#76、#82(最優先の追補)に
全文転記されている。ロードマップはM0〜M5。

**現在のマイルストーン: M2「採譜と出力の縦貫通」(Issue #3)** — MP3から
MusicXMLまでをAI(L1/L2)を一切使わずL0(決定論的整音)のみで一度通すことが
目的。ここが通れば以降は品質改善、通らなければ設計見直しが必要という
プロジェクト最重要マイルストーン。

## 完了済み(mainにマージ済み)

- **PR1** (#86, merged): #22 (`piano_transcription_inference`実行時検証) +
  #23 (Score IR定義、`backend/app/domain/score.py`等)
- **PR2** (#87, merged): #24 (Stage 3 ピアノAMT配線) + #25 (Stage 4決定論的
  クオンタイズ、`pipeline/quantize.py`) + #26 (L0決定論的整音、
  `pipeline/refine/baseline.py`)
- **PR3-1** (#88, merged): #25追加分(`quantize_pedal_ticks`) + #27
  (Stage 6 MusicXML/MIDIエクスポート、`pipeline/export/{score_builder,
  musicxml,midi}.py`)

現在 `main` はこれらをすべて含む。バックエンドの高速テストは
`uv run --project backend pytest -q` で271件パス。

## 現在の作業ブランチ: `feature/m2-invalidation-api-frontend`

`main`から分岐(`d357167`)。**GitHubにpush済み**
(`origin/feature/m2-invalidation-api-frontend`)。まだPRは作成していない。
1コミットのみ、ruff/mypy/ruff-format・Gate 1(pre-commit AIレビュー)を
通過済み。内容:

- `backend/app/domain/stages.py` (新規) — ステージ依存グラフ
  (`downstream_of(stage) -> tuple[str,...]`)。`separate→transcribe→quantize`
  / `beat→quantize` の推移的閉包を返す。
- `backend/app/services/stage_invalidation.py` (新規) —
  `invalidate_downstream(workspace_dir, project_id, stage) -> list[str]`。
  `domain.stages.downstream_of`を使い、下流ステージの
  `analysis/{stage}.meta.json`を削除する(成果物ファイル自体は残す)。
- `tests/backend/test_stage_invalidation.py` (新規) — 上記2つのテスト、
  10件すべてパス済み(依存グラフ5件+無効化5件、Windowsの
  `PermissionError`リトライ成功/リトライ上限超過の2件を含む)。
- 本ファイル(`HANDOFF_M2_PR3.md`、リポジトリルート)も同じコミットに含めた。

**これらはまだ`worker/dsp_main.py`等から一切呼ばれていない**(依存グラフと
無効化関数を用意しただけの土台)。下記「残っている作業」の1番目
(`worker/dsp_main.py`への配線)から続けること。

## 残っている作業(Issue #27の親Issue #3、および#29)

### 1. #29 ステージ独立再実行と下流無効化(FR-14) — 実装途中

計画済みだが未実装の部分:

- **`worker/dsp_main.py`への配線**:
  - `run_separate_stage`/`run_beat_stage`/`run_transcribe_stage`が
    実際に(スキップされずに)新しい成果物を書いた場合、
    `stage_invalidation.invalidate_downstream(workspace_dir, project_id,
    "separate"|"beat"|"transcribe")`を呼ぶ。
  - **呼び出し順序が重要(Gate1レビューで指摘され`stage_invalidation.py`の
    docstringに明記済み)**: 必ず**自ステージの`storage.write_stage_metadata`
    (自分自身のmeta.json書き込み)より前**に`invalidate_downstream`を
    呼ぶこと。逆順にすると、`invalidate_downstream`が(Windowsの
    `PermissionError`リトライ上限超過等で)例外送出しても、次回実行時は
    `should_skip_stage`が自ステージを既にスキップしてしまい、
    無効化が二度と行われず下流の無効化漏れが恒久的に残る。
  - `run_beat_stage`は現状`params`引数を受け取っていない
    (`def run_beat_stage(job_id, project_id, workspace_dir)`)。`force`
    フラグ対応のため`params: dict`引数を追加し、`main()`の呼び出し箇所
    (`elif stage == "beat": run_beat_stage(...)`)も修正すること。
  - 各ステージの`should_skip_stage`呼び出しを`force`フラグでバイパスする:
    `force = params.get("force", False)`、
    `if not force and storage.should_skip_stage(...)`のように変更。
- **`api/schemas.py`**:
  - `RunStageRequest`に`force: bool = False`を追加。
  - `StageStatus`に`stale: bool = False`を追加。
- **`api/jobs.py`の`run_stage`**: `body.force`を`params`へ混ぜて
  `manager.create_job(project_id=..., stage=..., params={**body.params,
  "force": body.force})`のように`JobManager.create_job`へ渡す
  (`JobManager`/`create_job`自体のシグネチャ変更は不要、`params`は
  既存のfree-form dict)。
- **`api/media.py`の`patch_beatmap`**: `applied_any`がTrueの場合、
  書き込み後に`stage_invalidation.invalidate_downstream(workspace_dir,
  project_id, "beat")`を呼ぶ(#29の完了条件そのもの:
  「ビートを補正すると量子化以降が無効化され、再実行すると反映される」)。
  `settings.workspace_dir`は既に引数で取得済み。
- **`services/project_service.py`の`_with_stage_summary()`**: 各ステージの
  最新jobが`status == "succeeded"`なのに`analysis/{stage}.meta.json`が
  存在しない場合、そのステージに`stale: True`を追加する。
  `storage.stage_metadata_path(workspace_dir, project_id, stage)`を使う。
  `ProjectService`は現状`workspace_dir`をdataclassフィールドとして
  持っているのでそのまま使える。
- **テスト**: `worker/dsp_main.py`への配線・`api/media.py`のPATCH連動・
  `project_service`の`stale`判定、それぞれに回帰テストを追加すること
  (`tests/backend/test_dsp_main.py`, 新規`tests/backend/test_media_api.py`
  相当の既存ファイル、`tests/backend/test_project_service.py`相当)。
  既存のテストファイル名は`grep -rl "ProjectService\|patch_beatmap"
  tests/backend/`等で確認すること。

### 2. `api/export.py` — Stage 6のHTTPエンドポイント(#27の一部、未実装)

- `POST /api/projects/{id}/export` — body: `{format: "musicxml"|"midi",
  parts?, options?}`。`services/score_service.py`の`ScoreService.read_score()`
  でScore IRを取得し、`.model_dump(mode="json")`で辞書化してから
  `pipeline/export/musicxml.render_musicxml()` または
  `pipeline/export/midi.render_midi()`を呼ぶ。
  `pipeline.export.score_builder.ExportError`を捕捉して422に変換すること
  (`musicxml.py`/`midi.py`のdocstringに明記されている契約)。
  生成物は`storage.musicxml_export_path()`/`storage.midi_export_path()`
  (`infra/storage.py`に既存)にも保存する(設計書§11.6)。
  同期エンドポイント(ジョブ化しない、モデル推論を伴わないため高速)。
- レスポンスは生成したファイルのダウンロード(`FileResponse`、
  `api/media.py`の`get_original_audio`と同じパターン)、または生成
  バイト列を直接返すか — 設計書§11.6の記述と`docs/dorico-import.md`
  (下記)の使い勝手を踏まえて判断すること。

### 3. `api/score.py` — Score IR取得エンドポイント(未実装)

- `GET /api/projects/{id}/score` — Score IR全体を返す
  (`ScoreService.read_score()`)。未実行(#23未到達)なら404。
- `GET /api/projects/{id}/score/preview.musicxml?bars=1-16` — 部分出力
  (設計書§11.5)。範囲外のパラメータ処理・全曲プレビューとの違いを
  設計すること。M2完了条件としては必須ではない(#27の完了条件は
  「実曲からMusicXMLが出力され、スキーマ的に妥当である」のみ)ため、
  時間が無ければ`GET /score`のみ先に実装し、previewは後回しでも良い
  (ユーザーに確認すること)。

### 4. `main.py`への登録

新しいrouter(`api/export.py`, `api/score.py`)を`app.include_router(...)`
に追加する。既存の`api/jobs.py`/`api/media.py`の登録パターンを踏襲。

### 5. フロントエンド最小限の変更(ユーザー決定済み: 最小限のみ)

`ProjectWorkspace.tsx`(または相当のコンポーネント、`grep -rl
"ProjectWorkspace" frontend/src`で確認)に:

- 「採譜を実行」ボタン(`transcribe`ステージの`POST .../stages/transcribe/run`)
- 「量子化を実行」ボタン(`quantize`ステージ)
- 「MusicXMLをエクスポート」ボタン(ダウンロード)
- ステージの`stale`状態表示(上記#29で追加した`StageStatus.stale`)

`api/client.ts`(または相当)にラッパー追加。**重要**: バックエンドの
`api/schemas.py`変更後は必ず`npm run typegen`(`frontend/package.json`の
スクリプト名を確認)を実行し、`backend/openapi.json`と
`frontend/src/api/generated.ts`を再生成してコミットに含めること
(CIの`typegen-check`ジョブがこの差分をチェックしている、実際に
`gh pr checks`で`typegen-check`ジョブが存在することを確認済み)。
ピアノロール編集UIやOSMDプレビューはM3スコープなので**実装しないこと**
(ユーザーが明示的に決定済み)。

### 6. `docs/dorico-import.md`(#28関連、未実装)

Doricoインポート手順・推奨設定・検証チェックリストの雛形。**Doricoは
Windows専用の商用ソフトでこのサンドボックスには無い**ため、実機検証は
ユーザーが行う旨を明記すること。チェックリスト項目(設計書#28より):
大譜表/左右手割り当て、声部分離、異名同音表記(調号との整合)、タイと
休符のグルーピング、ペダル記号、拍子変化とテンポ変化。

### 7. README更新

「DSPパイプライン」表と「既知の制約」節をM2の内容に更新。

## 開発プロセス上の重要な注意点

- **`review-round` Skill**に従うこと: featureブランチ→毎コミットGate 1
  (pre-commit AIレビュー)→PR→`/request-review`でGate 2→未解決スレッド
  ゼロまでループ。`--no-verify`や`SKIP=`は絶対に使わない
  (`.claude/skills/review-round/SKILL.md`参照)。
- **コミット前に必ず実行**: `pre-commit run ruff/ruff-format/mypy
  --files <対象>`、`uv run --project backend pytest -q`。
- **git操作は必ずリポジトリルート(`/home/ai-developer/projects/AME-Team/
  AME-Music-Notation-Assistance`)から実行すること**。`cd backend`した
  シェルの状態が後続コマンドに残り、`git add`等が別ディレクトリ基準の
  相対パスで失敗する事故が過去に発生した。
- **既知の環境問題**: 過去に1度、`git commit`が`git-write-tree`エラーで
  失敗し、`.git/objects/`内の特定blobオブジェクトファイルが物理的に
  存在しない(が索引のハッシュ自体は正しい)という破損に遭遇した。
  作業ツリーのファイル内容自体は無事だったため、
  `git hash-object -w <file>`で該当ファイルを個別に再書き込みして復旧した
  (`git add`だけでは復旧しなかった)。再発した場合は同じ手順を試すこと。
  `git fsck --full`で`dangling tree`系の警告は無害(正常なゴミオブジェクト)
  だが、`missing blob`/`invalid object`は要対応。
- **Gate 1のAIレビューエンジンが`timeout after 600s`で失敗することが
  複数回あった**(コード起因ではない一時的なエンジン障害)。単純に同じ
  コミットコマンドをリトライすれば通ることが多い(3回連続失敗すると
  fail-open的にescapeする仕組みがある)。
- **AME AI Review Systemはv0.2.11に更新済み**(このセッション内で実施)。
  `.pre-commit-config.yaml`・`.github/workflows/review_{command,reply}.yml`・
  `.claude/skills/review-round/SKILL.md`はすべて最新化済み。
- **Issue起票済み**: Gate1(pre-commit)にIssue #129の「重大度によらない
  総レビュー回数上限」機構が未実装という不整合を
  `AME-Team/AME-AI-Review-System#134`として起票済み。**この修正は別の
  AIエージェントが対応する予定なので、`AME-AI-Review-System`リポジトリ
  自体には手を出さないこと**(ユーザーからの明示的な指示)。

## マージ済みPRのユーザー決定事項(再掲、変更しないこと)

1. Score IRは`domain/`にPydanticで定義(設計書§5.2「domain/は外部依存ゼロ」
   との緊張は意図的に受容)。
2. M2のフロントエンド範囲は最小限(採譜/量子化実行ボタン+MusicXML
   エクスポートボタンのみ)。
3. #29は M2内で完全実装する(スコープを削らない)。

## このセッションで確立した技術的パターン(踏襲すること)

- ステージ関数の型: `run_xxx_stage(job_id, project_id, workspace_dir,
  params)` → `should_skip_stage`でスキップ判定 → 楽観的並行性制御
  (書き込み直前に再読込し比較、`raw_before`は検証用の読み取りと同一の
  読み取りを使い回すこと。分離すると並行書き込みを検出できないバグに
  なる、実際にレビューで指摘され修正した実例あり)。
- `pipeline/`はdomain非依存(dictやタプルで入出力)。例外は
  `pipeline/refine/baseline.py`(`domain.pitch`を直接呼ぶ、ロジック二重化
  回避のため意図的な例外として文書化済み)。
- 拍子の順方向補完ロジックは`pipeline/time_signature.py`の
  `time_signature_at_bar()`に集約済み(以前`quantize.py`と
  `export/score_builder.py`に重複していたのをレビュー指摘で統合した)。
- Gate 1レビューは複数ラウンド(3〜4回)かかることが多い。LOW指摘でも
  安易に無視せず、既知の制約としてdocstringに明記するか実際に直すかを
  都度判断すること(false positiveだと判断した場合はdocstringで
  明示的に反証し、無闇に無駄なコードを足さない — 実例:
  `quantize_pedal_ticks`のdocstringに`_seconds_to_raw_tick`が空anchorsを
  既に処理済みである旨を明記した)。

## 次にやること(推奨順序)

`git checkout feature/m2-invalidation-api-frontend`
(`git fetch`してから`git checkout -t origin/feature/m2-invalidation-api-frontend`)
で続きから始めること。土台(`domain/stages.py`等)は既にコミット・push済み。

1. `worker/dsp_main.py`への`invalidate_downstream`配線 + `force`フラグ対応。
2. `api/schemas.py`(`force`/`stale`追加)→ `api/jobs.py`/`api/media.py`
   配線 → `project_service.py`の`stale`判定。ここまでで#29完了。
3. `api/export.py`/`api/score.py`実装 + `main.py`登録。
4. フロントエンド最小限の変更 + typegen再生成。
5. `docs/dorico-import.md` + README更新。
6. PRを作成し、Gate 2レビュー → ユーザー確認 → マージ。

各ステップごとに`uv run --project backend pytest -q`とmypy/ruffを通し、
コミットすること(大きな一括コミットにしない、これまでのPRと同じ粒度)。
