"""Stage 3: ピアノAMT(#24, §6 Stage 3)。

ByteDance Piano Transcription(`piano_transcription_inference`)を使用する。#22の
実行時検証でライブラリのソースを直接確認済みの契約: 入力は16kHz mono、
`PianoTranscription(device="cpu").transcribe(audio, midi_path=None)` が
`{'est_note_events': [...], 'est_pedal_events': [...]}` を返す。ノートは
`{'onset_time','offset_time','midi_note','velocity'}`、ペダルは
`{'onset_time','offset_time'}`(いずれも `backend/scripts/verify_piano_transcription.py`
で実推論して確認済み)。

**この段階では一切ノートを捨てない**(§6 Stage 3)。削除は可逆でなければならないため、
極端に短い/弱いノートは `ghost_candidate` フラグを立てるだけで保持する。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

# ライブラリの要求仕様(#22で確認)。44.1kHzのステムはここでリサンプルする。
INPUT_SAMPLE_RATE = 16000

# 共通後処理のしきい値(#24)。design §7.4 のL0 ghost判定
# (confidence<0.35 かつ duration<60ms かつ velocity<25、3条件すべてのAND)とは
# 別に、Stage 3自身はduration/velocityそれぞれ単独の条件(いずれか一方を
# 満たせばフラグを立てる、より広く網をかける粗いフィルタ)として位置づける。
# `confidence` はpiano_transcription_inferenceがノート単位で公開していない
# (ライブラリのpost-processorが内部の閾値判定で既にest_note_eventsへ採用する
# か否かを決めており、その確信度はAPIとして出てこない)ため、AMT由来のノートは
# 既定 `confidence=1.0` とする(既知の制約: L0のghost判定式のconfidence項は
# 現状AMT由来ノートに対しては事実上働かない。#26実装時にも同じ前提を引き継ぐ)。
GHOST_MIN_DURATION_SEC = 0.06
GHOST_MIN_VELOCITY = 25

# 生成物にゼロ長ノートを書き込むと `domain.score.Note`(duration_sec>0)の
# 不変条件に違反するため、モデルが極端な(onset==offset等の)出力をした場合の
# 最終防波堤として使う最小値。
_MIN_DURATION_FLOOR_SEC = 0.001

# `PianoTranscription.transcribe(audio, midi_path)` の呼び出し面。テストで
# モックできるよう抽象化する(`pipeline/beat.py` の `BeatTracker` と同形)。
PianoTranscriber = Callable[["np.ndarray"], dict[str, Any]]


@dataclass(frozen=True)
class NoteEvent:
    onset_sec: float
    duration_sec: float
    midi: int
    velocity: int
    ghost_candidate: bool


@dataclass(frozen=True)
class PedalEvent:
    start_sec: float
    stop_sec: float


@dataclass(frozen=True)
class TranscriptionResult:
    notes: list[NoteEvent]
    pedals: list[PedalEvent]


def _load_audio_16k_mono(audio_path: Path) -> np.ndarray:
    import librosa

    audio, _ = librosa.load(str(audio_path), sr=INPUT_SAMPLE_RATE, mono=True)
    return audio


def _to_note_event(raw_event: dict[str, Any]) -> NoteEvent:
    onset = float(raw_event["onset_time"])
    offset = float(raw_event["offset_time"])
    duration = max(offset - onset, _MIN_DURATION_FLOOR_SEC)
    velocity = int(raw_event["velocity"])
    ghost = duration < GHOST_MIN_DURATION_SEC or velocity < GHOST_MIN_VELOCITY
    return NoteEvent(
        onset_sec=onset,
        duration_sec=duration,
        midi=int(raw_event["midi_note"]),
        velocity=velocity,
        ghost_candidate=ghost,
    )


def _build_result(raw: dict[str, Any]) -> TranscriptionResult:
    notes = [_to_note_event(e) for e in raw["est_note_events"]]
    pedals = [
        PedalEvent(start_sec=float(p["onset_time"]), stop_sec=float(p["offset_time"]))
        for p in raw["est_pedal_events"]
    ]
    return TranscriptionResult(notes=notes, pedals=pedals)


def run_piano_transcription(
    audio_path: Path, *, transcriber: PianoTranscriber | None = None
) -> TranscriptionResult:
    """ピアノステムからノート/ペダルイベント列を得る(#24)。

    `transcriber` を渡すとテストでモックできる(実モデル推論を待たずに配管を検証する)。
    """
    audio = _load_audio_16k_mono(audio_path)

    if transcriber is None:
        from piano_transcription_inference import PianoTranscription

        model = PianoTranscription(device="cpu")

        def _transcribe(loaded_audio: np.ndarray) -> dict[str, Any]:
            return model.transcribe(loaded_audio, midi_path=None)

        transcriber = _transcribe

    raw = transcriber(audio)
    return _build_result(raw)
