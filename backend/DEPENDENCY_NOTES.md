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
