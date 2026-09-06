# Dependency deviations from 設計書 §13 (実インストール検証で判明)

Issue #9 の完了条件どおり、`uv pip compile` による解決検証(設計書v0.4)止まりだった依存を実際に
`uv add` してインストール・import 検証した。以下の2点で設計書の指定と異なる結果になったため、
黙って変更せず記録する。

## 1. `torch` を `2.13.0` ではなく `2.10.0` に固定

**症状:** `torch==2.13.0` (CPU) 自体は正常にインストール・import できたが、`beat-this` /
`torchcrepe` が依存する `torchaudio` の PyPI デフォルトホイール(2.11.0)が **CUDA ランタイム
(`libcudart.so.13`, `libtorch_cuda.so`, `libc10_cuda.so`) に無条件でリンクされており**、
CPU-only 環境では `import torchaudio`(→ `torchcrepe` 経由)が `OSError` で失敗する。

`download.pytorch.org/whl/cpu`(PyTorch公式のCPU専用ホイール索引)を確認したところ、
`torchaudio` の CPU 専用ビルドは **`2.10.0` が最後** で、それ以降(2.11.0〜)は同索引に
存在しない。かつ `torchaudio==X.Y.Z` は対応する `torch==X.Y.Z` を厳密に要求するため、
`torch` 側も `2.10.0` に合わせる必要がある。

**採用した対応:** `torch==2.10.0` / `torchaudio==2.10.0` を `download.pytorch.org/whl/cpu`
から取得するよう `pyproject.toml` の `[tool.uv.sources]` / `[[tool.uv.index]]` で固定。
`beat-this`(`torch>=2`)・`torchcrepe`(バージョン制約なし)ともに `torch>=2` を満たすため、
機能要件を損なわない。

**確認事項:** `torch.cuda.is_available()` は `False`(意図通り CPU-only)。CUDA 系
`nvidia-*` パッケージは一切インストールされない。

**残作業:** M1(Q-13: DirectML EP実測)着手時に `torchaudio` を実際に使うコードパス
(`torchcrepe` の F0 抽出, §6 Stage 3)が `torch==2.10.0` で問題なく動作するか再確認する。
問題があれば「`torchaudio` を経由しない F0 抽出への差し替え」も選択肢に入れる。

## 2. `piano_transcription_inference` に `audioread` を明示的に追加

**症状:** `piano_transcription_inference`(0.0.6)は `utilities.py` で `import audioread`
しているが、パッケージのメタデータに `audioread` が依存として宣言されておらず、
素の `uv add piano_transcription_inference` では `ModuleNotFoundError: No module named
'audioread'` で import が失敗する(上流のパッケージング漏れ)。

**採用した対応:** `audioread` を `backend/pyproject.toml` の直接依存として追加。

**R-16 との関係:** 設計書 R-16「`piano_transcription_inference` が Python 3.12 / 最新
numpy で動かない」への回答として、**Python 3.12 / numpy 2.5.2 環境でも import 自体は
`audioread` を追加するだけで通ることを確認した**。M2 冒頭で予定されている実行時(推論)検証は
別途必要(Q-14 のまま)。

## 3. `piano_transcription_inference` の実行時(推論)動作確認(#22, Q-14/R-16 クローズ)

**確認方法:** `backend/scripts/verify_piano_transcription.py` で、CPU上で実際にモデルを
ロードし、合成したピアノらしい音声(既知のノート列から倍音+ADSRエンベロープで生成)に
対して推論を実行した。

**結果: 動作する。** `PianoTranscription(device="cpu")` のロード、`.transcribe(audio,
midi_path=None)` の推論ともに例外なく完走し、`est_note_events`(`onset_time` /
`offset_time` / `midi_note` / `velocity`)と `est_pedal_events` が仕様通りの形式で
得られた。torch 2.10.0 で `torch.load` の `weights_only` 既定値変更(torch>=2.6)による
チェックポイント読込失敗という、事前に想定していた最大のリスクは**発生しなかった**。

単に「1件以上検出された」だけでは無関係な誤検出でも成功扱いになってしまうため、
既知の正解ノート列(onset_sec+midi_note)に対する見逃し率を機械的に確認したところ、
**一致率83%**(onset許容誤差0.15秒以内・同一midi_noteで判定)だった。合成音声は
学習データ(実ピアノ録音)と音色が異なるため完全な一致は期待していなかったが、
ノイズではなく実質的に妥当な検出をしていることが確認できた。

**チェックポイント取得の注意点:** ライブラリ自身は `os.system("wget -O ... <URL>")` で
チェックポイント(~165MB, Zenodo)を取得するが、**Windows に `wget` は無い**
(NFR-08′ 違反になる)。`verify_piano_transcription.py` の `ensure_checkpoint()` は
`httpx` で同じURL・同じ既定パス(`~/piano_transcription_inference_data/`)へ事前
ダウンロードすることで、ライブラリ内部の `wget` 呼び出し自体を(サイズ条件チェックにより)
スキップさせている。M2 本実装(#24, Stage 3 AMT)でも同じ方式を踏襲する。

**NFR-01 への示唆:** このサンドボックス(Linux, 実機不明のCPU)で、3.5秒の合成音声の
推論に約4.07秒(モデルロード除く)を要した(RTF ≈ 1.16)。単純に5分曲へ外挿すると
約5.8分となり、設計書の NFR-01(AMT ≤ 3分/5分曲)を超過しうる。ただし(a) この
サンドボックスのCPU性能はユーザーの実機と異なる、(b) 合成音声は実曲より複雑でない
可能性がある、(c) セグメント処理のオーバーヘッド構成が実際の曲とは異なりうるため、
**この数値は参考値に留め、実曲・実機での計測をM2完了までにユーザーに依頼する**
(M1のNFR-01実測(#17)と同じ扱い)。
