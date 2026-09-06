"""#22 (Q-14/R-16): `piano_transcription_inference` が Python 3.12 + numpy 2.5.2 で

実際に動くかの実行確認(★M2 最優先タスク)。依存解決とimportは #9 で確認済みだが、
実行時の推論動作は未検証だった。

このスクリプトは:
1. チェックポイント(~165MB)を取得する。ライブラリ自身は `os.system("wget ...")` で
   取得するが、**Windows に wget が無い**(NFR-08′ 違反)ため、`httpx` で明示的に
   事前ダウンロードし、ライブラリ側の再ダウンロードをスキップさせる
2. CPU上でモデルをロードし、合成したピアノらしい音声に対して実際に推論を実行する
3. ノート(onset/offset/pitch/velocity)とペダルイベントが取得できることを確認する
4. 所要時間を計測し、NFR-01(5分曲でAMT ≤ 3分)に対する見通しを得る

使い方:
    uv run --project backend python backend/scripts/verify_piano_transcription.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if TYPE_CHECKING:
    import numpy as np

CHECKPOINT_URL = (
    "https://zenodo.org/record/4034264/files/CRNN_note_F1%3D0.9677_pedal_F1%3D0.9186.pth?download=1"
)
# ライブラリの既定パスに合わせる(#22): 明示的に checkpoint_path を渡さない場合、
# `PianoTranscription.__init__` はこのパスを既定値として使う。ここへ事前配置して
# おくことで、`os.path.getsize(checkpoint_path) >= 1.6e8` が真になり、ライブラリ
# 内部の `os.system("wget ...")` 呼び出し自体がスキップされる(Windowsにwgetは無い)。
DEFAULT_CHECKPOINT_PATH = (
    Path.home() / "piano_transcription_inference_data" / "note_F1=0.9677_pedal_F1=0.9186.pth"
)
# ライブラリの `os.path.getsize(checkpoint_path) < 1.6e8` と同じ閾値(#22)。
MIN_CHECKPOINT_SIZE_BYTES = int(1.6e8)


def ensure_checkpoint(path: Path = DEFAULT_CHECKPOINT_PATH) -> Path:
    """チェックポイントを `httpx` で取得する(#22: Windowsにwgetが無いため自前で行う)。

    通信が途中で切れて短いファイルのまま `path` に置かれると、次回起動時に
    ライブラリ自身の `os.path.getsize(checkpoint_path) >= 1.6e8` チェックが偽になり、
    Windowsに存在しない `wget` 呼び出しへ進んでしまう(#22-M2レビュー指摘)。
    ダウンロード完了後にサイズを検証してから `path` へ置き換え、不完全な `.part`
    ファイル(例外発生時や前回の中断分)は毎回明示的に削除する。
    """
    if path.exists() and path.stat().st_size >= MIN_CHECKPOINT_SIZE_BYTES:
        print(f"[verify] checkpoint already present: {path} ({path.stat().st_size} bytes)")
        return path

    import httpx

    path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[verify] downloading checkpoint (~165MB) to {path} ...")
    tmp_path = path.with_suffix(path.suffix + ".part")
    try:
        with httpx.stream("GET", CHECKPOINT_URL, follow_redirects=True, timeout=300.0) as resp:
            resp.raise_for_status()
            with tmp_path.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    f.write(chunk)
        downloaded_size = tmp_path.stat().st_size
        if downloaded_size < MIN_CHECKPOINT_SIZE_BYTES:
            raise RuntimeError(
                f"downloaded checkpoint is too small ({downloaded_size} bytes, "
                f"expected >= {MIN_CHECKPOINT_SIZE_BYTES}); download likely truncated"
            )
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)
    print(f"[verify] downloaded {path.stat().st_size} bytes")
    return path


def synthesize_piano_like_audio(
    note_events: list[tuple[float, float, int, int]], *, sample_rate: int = 16000
) -> np.ndarray:
    """既知のノート列(onset_sec, duration_sec, midi_note, velocity)から、

    倍音+ADSRエンベロープでピアノらしい合成音声を生成する(#22/#24)。実曲は
    著作権の都合でこのサンドボックスに無いため、正解ノート列が既知の合成音声で
    代替する(検出結果と比較して機械的に妥当性を検証できる)。
    """
    import numpy as np

    total_duration = max(onset + dur for onset, dur, _, _ in note_events) + 1.0
    num_samples = int(total_duration * sample_rate)
    audio = np.zeros(num_samples, dtype=np.float64)

    for onset_sec, duration_sec, midi_note, velocity in note_events:
        freq = 440.0 * 2.0 ** ((midi_note - 69) / 12.0)
        n = int(duration_sec * sample_rate)
        t = np.arange(n) / sample_rate
        # 基音+倍音(2倍音、3倍音)を減衰させながら重ね、ピアノに近い音色にする。
        tone = (
            1.0 * np.sin(2 * np.pi * freq * t)
            + 0.5 * np.sin(2 * np.pi * freq * 2 * t)
            + 0.25 * np.sin(2 * np.pi * freq * 3 * t)
        )
        # ピアノ的な急速アタック+指数減衰のADSR。
        attack_samples = max(int(0.005 * sample_rate), 1)
        envelope = np.exp(-3.0 * t)
        envelope[:attack_samples] *= np.linspace(0, 1, attack_samples)
        tone = tone * envelope * (velocity / 127.0)

        start = int(onset_sec * sample_rate)
        end = min(start + n, num_samples)
        audio[start:end] += tone[: end - start]

    max_abs = np.abs(audio).max()
    if max_abs > 0:
        audio = audio / max_abs * 0.9
    return audio.astype(np.float32)


# ピッチ一致とみなすonset時刻の許容誤差(秒)。ピアノ推論モデルのフレーム分解能
# (piano_transcription_inference は約10msフレーム)より十分大きく取る。
ONSET_MATCH_TOLERANCE_SEC = 0.15


def match_rate(expected: list[tuple[float, float, int, int]], detected: list[dict]) -> float:
    """既知の正解ノート列(onset_sec, duration_sec, midi_note, velocity)に対し、

    検出結果(`est_note_events`)のうち「onset時刻が許容誤差内かつ同じmidi_note」の
    ものが存在する割合を返す(#22-M2レビュー指摘: 単に0件でないことだけでなく、
    実際に妥当なノートを検出しているかを機械的に確認する)。1つの正解ノートに
    複数の検出が対応してもよい(過検出はここでは問わず、見逃しのみを見る)。
    """
    if not expected:
        return 1.0
    matched = 0
    for onset_sec, _duration_sec, midi_note, _velocity in expected:
        if any(
            abs(float(d["onset_time"]) - onset_sec) <= ONSET_MATCH_TOLERANCE_SEC
            and int(d["midi_note"]) == midi_note
            for d in detected
        ):
            matched += 1
    return matched / len(expected)


def main() -> int:
    ensure_checkpoint()

    # torch.load の weights_only 既定値変更(torch>=2.6)でチェックポイント読込が
    # 失敗する可能性が#22の最大のリスクだったため、ここで実際に踏んで確認する。
    import torch

    print(f"[verify] torch version: {torch.__version__}")

    from piano_transcription_inference import PianoTranscription

    print("[verify] loading model on CPU ...")
    load_start = time.perf_counter()
    transcriptor = PianoTranscription(device="cpu")
    print(f"[verify] model loaded in {time.perf_counter() - load_start:.1f}s")

    # C4-E4-G4 (Cメジャートライアド)を1音ずつ+和音で鳴らし、ペダルも踏んだ想定にする
    # (ペダル自体はこの合成音声には反映していない。ペダル取得の可否はコードパスの
    # 疎通確認のみを目的とする)。
    note_events = [
        (0.0, 0.5, 60, 90),  # C4
        (0.5, 0.5, 64, 90),  # E4
        (1.0, 0.5, 67, 90),  # G4
        (1.5, 1.0, 60, 100),
        (1.5, 1.0, 64, 100),
        (1.5, 1.0, 67, 100),
    ]
    audio = synthesize_piano_like_audio(note_events)
    print(f"[verify] synthesized {len(audio) / 16000:.1f}s of audio ({len(note_events)} notes)")

    print("[verify] running inference ...")
    infer_start = time.perf_counter()
    result = transcriptor.transcribe(audio, midi_path=None)
    infer_elapsed = time.perf_counter() - infer_start

    note_count = len(result["est_note_events"])
    pedal_count = len(result["est_pedal_events"])
    print(f"[verify] inference took {infer_elapsed:.2f}s")
    print(f"[verify] detected {note_count} note events, {pedal_count} pedal events")
    if note_count:
        print(f"[verify] sample note event: {result['est_note_events'][0]}")

    if note_count == 0:
        print(
            "[verify] WARNING: 0 notes detected. The library ran without error but "
            "produced no output -- investigate synthetic audio quality or model config "
            "before concluding #22 is fully verified.",
            file=sys.stderr,
        )
        return 1

    # 単に「1件以上検出された」だけでは、無関係な誤検出でも exit 0 になってしまい
    # #22/#24 の結論を誤らせうる(#22-M2レビュー指摘)。既知の正解ノート列に対する
    # 見逃し率を機械的に確認する。合成音声はライブラリの学習データ(実ピアノ録音)と
    # 音色が異なるため高い一致率は期待できないが、0%(=完全に無意味な出力)ではないこと
    # を確認する目的。実曲での精度評価はユーザーに委ねる(README「既知の制約」と同じ扱い)。
    rate = match_rate(note_events, result["est_note_events"])
    print(f"[verify] onset+pitch match rate against known ground truth: {rate:.0%}")
    if rate == 0.0:
        print(
            "[verify] WARNING: 0% match against the known note list. Detected notes "
            "exist but none correspond to an expected onset+pitch -- the output may be "
            "noise rather than a meaningful transcription. Investigate before concluding "
            "#22 is verified.",
            file=sys.stderr,
        )
        return 1

    print("[verify] OK: piano_transcription_inference runs on Python 3.12 / CPU / numpy 2.5.2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
