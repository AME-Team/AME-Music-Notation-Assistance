# Dorico インポート手順・推奨設定・検証チェックリスト(#28, R-7)

> [!IMPORTANT]
> Dorico は Windows/macOS 専用の商用ソフトであり、このリポジトリの開発・CI サンドボックス
> (Linux)には存在しない。そのため本ドキュメントに記載する検証チェックリストの実施(実際に
> Dorico へインポートして見た目を確認する作業)は、**Windows 実機を持つユーザーが行う**必要が
> ある。本ドキュメントはその手順の雛形であり、実施結果そのものではない。

## 背景

設計書 §15 R-7: 「Dorico の MusicXML インポート挙動が想定と異なると、手直しコストが減らない
= アプリの存在価値が失われる」ため、実曲で早期に往復テストを行うことが M2(#3)の完了条件に
含まれている。

## 生成物の場所

`POST /api/projects/{id}/export`(`{"format": "musicxml"}`)を実行すると、ダウンロードされる
ファイルと同じ内容が `workspace/{id}/export/score.musicxml` にも保存される
(`backend/app/api/export.py`)。Dorico へはこの `.musicxml` ファイル(非圧縮の生XML、`.mxl`
圧縮形式ではない)をそのままインポートする。

## インポート手順

1. Dorico を起動し、メニューから **ファイル → 開く**(File → Open)を選択する。
2. ファイル種別のフィルタで **MusicXML ファイル**(`*.musicxml`, `*.xml`)を選択できることを
   確認し、`score.musicxml` を選ぶ。
3. Dorico の「MusicXML 読み込みオプション」ダイアログが表示された場合、既定値のまま
   読み込んで問題ないはずである(本システムの出力は特別なインポートオプションを前提と
   しない、後述の「推奨設定」参照)。
4. 読み込み後、下記の「検証チェックリスト」に沿って表示内容を確認する。

## 推奨設定(生成側の設計、設計書§6 Stage 6より)

本システムの MusicXML 出力(`backend/app/pipeline/export/musicxml.py` +
`score_builder.py`)は、以下の方針で Dorico への忠実なインポートを狙っている。インポート時に
Dorico 側で特別な設定変更は基本的に不要なはずだが、想定と異なる場合はこれらの前提を疑うこと。

- `<divisions>` は 480 固定(1/32 三連まで整数表現可能)。
- パートごとに `<part-name>` / `<score-instrument>` / `<midi-instrument>` を明示する。
- ピアノは 2 段譜(`<staves>2</staves>`)、`<clef number="1">G</clef>` /
  `<clef number="2">F</clef>`(`app/domain/score.py` の `Clef`)。
- 異名同音表記(`Spelling.step`/`alter`/`octave`)は Stage 4 末尾の L0 決定論的整音
  (`pipeline/refine/baseline.py`)が調号との整合を考慮して決定済みの値をそのまま出力する
  (Dorico 側での再解決を前提としない)。
- ペダル記号は量子化ステージ(#25)で計算した `start_tick`/`stop_tick` を使う。

## 検証チェックリスト(#28)

実曲(MP3)を M2 パイプライン(分離→ビート推定→採譜→量子化→エクスポート)に通して生成した
`score.musicxml` を Dorico で開き、以下が意図通りに再現されるか確認する。想定と異なる項目が
あれば、生成側(`pipeline/export/`)で対処すべきか、Dorico 側の表示設定で対処すべきかを判断し、
Issue化する。

- [ ] 大譜表と左右手の割り当て(ピアノパートが2段譜として表示され、`voice`/`staff`
      (`app/domain/score.py` の `Note.voice`/`Note.staff`)に沿って左右手に振り分けられているか)
- [ ] 声部の分離(同一段内で複数声部が意図通りに分かれているか、`Note.voice` の値が
      Dorico 上の声部番号と対応しているか)
- [ ] 異名同音表記(調号との整合)(L0整音が選んだ `Spelling` が、実際の調号のもとで
      不自然な表記(例: 調号上ありえない臨時記号の多用)になっていないか)
- [ ] タイと休符のグルーピング(`quantize_note_onsets` が計算した `duration_tick` から、
      Dorico が小節をまたぐ音符を適切にタイ表記へ分割しているか。休符のグルーピングが
      不自然に細分化されていないか)
- [ ] ペダル記号(サスティンペダルの `<pedal>` が意図した位置(`start_tick`/`stop_tick`)に
      表示されるか)
- [ ] 拍子変化とテンポ変化(`beatmap.json` の `time_signatures`/`tempo_map` から
      `score_builder.py` が構築した拍子・テンポ変化が、対応する小節位置で正しく反映されるか)

## 既知の制約(M2時点)

- ピアノ1パートのみが対象(#3の完了条件はピアノパートの往復検証)。ドラム譜
  (`<percussion>` + Doricoドラムマップ)やギター等の複数パート対応は将来のマイルストーン。
- ユーザーによる手動編集(`provenance == "user"` のノート)はまだ編集APIが無く、M2時点では
  発生しない。M3で編集APIが入った際は、その結果も含めた往復検証が別途必要になる。
- 本チェックリストの実施(Dorico実機での見た目確認)自体は、このリポジトリの自動テスト
  (`uv run --project backend pytest`, `frontend/e2e/`)には含まれない。README「既知の制約」
  にも記載の通り、Windows実機作業として別途トラッキングすること。
