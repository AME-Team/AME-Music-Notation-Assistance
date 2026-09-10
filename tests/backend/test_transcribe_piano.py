"""#24: Stage 3 ピアノAMTパイプラインのテスト。

`run_beat_estimation` の `tracker` 注入パターン(pipeline/beat.py)と同様、
`transcriber` を注入したモックでの高速テストと、実モデルでの `@pytest.mark.slow`
テストの両方を用意する。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app.pipeline.transcribe.piano import (
    GHOST_MIN_DURATION_SEC,
    GHOST_MIN_VELOCITY,
    run_piano_transcription,
)


def _fake_transcriber(raw: dict):
    def _transcribe(_audio: np.ndarray) -> dict:
        return raw

    return _transcribe


def test_maps_note_events_to_note_event_dataclass(tmp_path: Path) -> None:
    audio_path = tmp_path / "piano.wav"
    sf.write(audio_path, np.zeros(1600, dtype=np.float32), 16000)

    raw = {
        "est_note_events": [
            {"onset_time": 1.0, "offset_time": 1.5, "midi_note": 60, "velocity": 90},
        ],
        "est_pedal_events": [],
    }
    result = run_piano_transcription(audio_path, transcriber=_fake_transcriber(raw))

    assert len(result.notes) == 1
    note = result.notes[0]
    assert note.onset_sec == 1.0
    assert note.duration_sec == pytest.approx(0.5)
    assert note.midi == 60
    assert note.velocity == 90
    assert note.ghost_candidate is False


def test_pedal_events_are_mapped(tmp_path: Path) -> None:
    audio_path = tmp_path / "piano.wav"
    sf.write(audio_path, np.zeros(1600, dtype=np.float32), 16000)

    raw = {
        "est_note_events": [],
        "est_pedal_events": [{"onset_time": 2.0, "offset_time": 3.5}],
    }
    result = run_piano_transcription(audio_path, transcriber=_fake_transcriber(raw))

    assert len(result.pedals) == 1
    assert result.pedals[0].start_sec == 2.0
    assert result.pedals[0].stop_sec == 3.5


def test_short_duration_note_is_flagged_ghost_candidate_but_kept(
    tmp_path: Path,
) -> None:
    """回帰(#24): 一切ノートを捨てない。短い/弱いノートはフラグのみ付けて保持する。"""
    audio_path = tmp_path / "piano.wav"
    sf.write(audio_path, np.zeros(1600, dtype=np.float32), 16000)

    onset = 0.0
    raw = {
        "est_note_events": [
            {
                "onset_time": onset,
                "offset_time": onset + GHOST_MIN_DURATION_SEC / 2,
                "midi_note": 60,
                "velocity": 90,
            },
        ],
        "est_pedal_events": [],
    }
    result = run_piano_transcription(audio_path, transcriber=_fake_transcriber(raw))

    assert len(result.notes) == 1  # 削除されず保持される
    assert result.notes[0].ghost_candidate is True


def test_low_velocity_note_is_flagged_ghost_candidate_but_kept(tmp_path: Path) -> None:
    audio_path = tmp_path / "piano.wav"
    sf.write(audio_path, np.zeros(1600, dtype=np.float32), 16000)

    raw = {
        "est_note_events": [
            {
                "onset_time": 0.0,
                "offset_time": 0.5,
                "midi_note": 60,
                "velocity": GHOST_MIN_VELOCITY - 1,
            },
        ],
        "est_pedal_events": [],
    }
    result = run_piano_transcription(audio_path, transcriber=_fake_transcriber(raw))

    assert len(result.notes) == 1
    assert result.notes[0].ghost_candidate is True


def test_normal_note_is_not_flagged_ghost_candidate(tmp_path: Path) -> None:
    audio_path = tmp_path / "piano.wav"
    sf.write(audio_path, np.zeros(1600, dtype=np.float32), 16000)

    raw = {
        "est_note_events": [
            {"onset_time": 0.0, "offset_time": 0.5, "midi_note": 60, "velocity": 90},
        ],
        "est_pedal_events": [],
    }
    result = run_piano_transcription(audio_path, transcriber=_fake_transcriber(raw))

    assert result.notes[0].ghost_candidate is False


def test_zero_duration_note_gets_floor_duration(tmp_path: Path) -> None:
    """回帰(#24): モデルがonset==offsetの縮退ノートを出しても、

    `domain.score.Note` の `duration_sec>0` 不変条件に違反しないよう、
    ゼロではない最小デュレーションへ切り上げる。
    """
    audio_path = tmp_path / "piano.wav"
    sf.write(audio_path, np.zeros(1600, dtype=np.float32), 16000)

    raw = {
        "est_note_events": [
            {"onset_time": 1.0, "offset_time": 1.0, "midi_note": 60, "velocity": 90},
        ],
        "est_pedal_events": [],
    }
    result = run_piano_transcription(audio_path, transcriber=_fake_transcriber(raw))

    assert result.notes[0].duration_sec > 0.0


@pytest.mark.slow
def test_run_piano_transcription_with_real_model(tmp_path: Path) -> None:
    """実モデルで合成ピアノ音声を推論する(#22/#24)。既知のノート列に対する

    見逃し率まではここでは問わない(スモークテスト。詳細な精度確認は
    `backend/scripts/verify_piano_transcription.py` で行う)。
    """
    sample_rate = 44100
    duration_sec = 2.0
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    # 440Hz(A4=MIDI69相当)のトーンをピアノステムのつもりで合成する。
    audio = 0.5 * np.sin(2 * np.pi * 440.0 * t)
    audio_path = tmp_path / "piano.wav"
    sf.write(audio_path, audio.astype(np.float32), sample_rate)

    result = run_piano_transcription(audio_path)

    assert isinstance(result.notes, list)
    assert isinstance(result.pedals, list)
