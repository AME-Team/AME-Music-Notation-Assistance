"""Stage 3: ボーカルAMT の単体テスト(#55, §6 Stage 3, §15 R-5, §17 Q-2)。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from app.pipeline.transcribe.common import (
    NoteEvent,
    TranscriptionResult,
)
from app.pipeline.transcribe.vocals import (
    HOP_LENGTH,
    INPUT_SAMPLE_RATE,
    VOCALS_ALGO_VERSION,
    _resolve_monophonic_overlaps,
    apply_vibrato_smoothing,
    run_vocals_transcription,
    segment_vocal_notes_from_f0,
)


def _write_sine_wav(
    path: Path,
    freqs: list[tuple[float, float]],
    duration: float = 1.0,
    sr: int = INPUT_SAMPLE_RATE,
) -> None:
    """テスト用 WAV を生成する。freqs は [(frequency_hz, amplitude), ...]"""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    signal = np.zeros_like(t)
    for freq, amp in freqs:
        signal += amp * np.sin(2 * np.pi * freq * t)
    signal = np.clip(signal, -1.0, 1.0)
    sf.write(str(path), signal, sr, format="WAV")


def test_vocals_algo_version_defined() -> None:
    assert isinstance(VOCALS_ALGO_VERSION, str)
    assert len(VOCALS_ALGO_VERSION.split(".")) >= 2


def test_apply_vibrato_smoothing_reduces_variance() -> None:
    """ビブラート(周期的なピッチ揺れ 5Hz)をメディアンフィルタで吸収できること(R-5)。"""
    # 100fps (10ms刻み) で 1 秒間
    t = np.linspace(0, 1.0, 100, endpoint=False)
    center_f0 = 440.0
    # 5Hz で ±25Hz のビブラート
    vibrato_f0 = center_f0 + 25.0 * np.sin(2 * np.pi * 5.0 * t)

    smoothed = apply_vibrato_smoothing(vibrato_f0, kernel_size=19)

    var_orig = np.var(vibrato_f0)
    var_smoothed = np.var(smoothed)

    assert var_smoothed < var_orig * 0.3
    assert np.all(np.abs(smoothed - center_f0) < 15.0)


def test_apply_vibrato_smoothing_handles_unvoiced_nan() -> None:
    """NaN (無声区間) を含む系列でもクラッシュせず有声区間のみ平滑化されること。"""
    f0 = np.array([np.nan, np.nan, 440.0, 442.0, 438.0, 440.0, 441.0, np.nan, np.nan])
    smoothed = apply_vibrato_smoothing(f0, kernel_size=3)
    assert np.isnan(smoothed[0])
    assert np.isnan(smoothed[1])
    assert np.isnan(smoothed[-1])
    assert not np.isnan(smoothed[2])


def test_segment_vocal_notes_prevents_chattering_with_hysteresis() -> None:
    """音程境界付近の微小変動でノートが細切れ(チャタリング)にならないこと(R-5)。"""
    sr = INPUT_SAMPLE_RATE
    hop = HOP_LENGTH
    # MIDI 60 (261.63Hz) と 60.4 (~267.7Hz) の間を揺れる F0 系列
    # 単純な四捨五入なら同一ノートだが、境界を跨いでも hysteresis (0.6半音) により分裂しない
    t = np.linspace(0, 0.5, 50, endpoint=False)
    f0 = 261.63 + 3.0 * np.sin(2 * np.pi * 10.0 * t)
    audio = np.full(len(f0) * hop, 0.3)

    notes = segment_vocal_notes_from_f0(
        f0, audio, sr, hop_length=hop, hysteresis_semitones=0.6
    )

    # 1本のノートとしてまとまること
    assert len(notes) == 1
    assert notes[0].midi == 60


def test_segment_vocal_notes_splits_on_large_pitch_step() -> None:
    """明確な音高変化(例: C4 -> E4)で正しく別ノートに分割されること。"""
    sr = INPUT_SAMPLE_RATE
    hop = HOP_LENGTH
    # 前半 25 フレーム C4 (261.63Hz, MIDI 60)、後半 25 フレーム E4 (329.63Hz, MIDI 64)
    f0_c4 = np.full(25, 261.63)
    f0_e4 = np.full(25, 329.63)
    f0 = np.concatenate([f0_c4, f0_e4])
    audio = np.full(len(f0) * hop, 0.3)

    notes = segment_vocal_notes_from_f0(f0, audio, sr, hop_length=hop)

    assert len(notes) == 2
    assert notes[0].midi == 60
    assert notes[1].midi == 64


def test_segment_vocal_notes_evaluates_ghost_candidate() -> None:
    """短時間または低振幅のボーカル音が ghost_candidate になること。"""
    sr = INPUT_SAMPLE_RATE
    hop = HOP_LENGTH

    # 3フレーム (~30ms < 60ms) の極短有声音
    f0 = np.array([np.nan, 440.0, 440.0, 440.0, np.nan])
    audio = np.full(len(f0) * hop, 0.5)

    notes = segment_vocal_notes_from_f0(f0, audio, sr, hop_length=hop)
    assert len(notes) == 1
    assert notes[0].ghost_candidate is True


def test_resolve_monophonic_overlaps_trims_overlap() -> None:
    """重複するノートが先行ノートの短縮によって解消されること。"""
    notes = [
        NoteEvent(
            onset_sec=0.0, duration_sec=1.0, midi=60, velocity=80, ghost_candidate=False
        ),
        NoteEvent(
            onset_sec=0.8, duration_sec=1.0, midi=62, velocity=80, ghost_candidate=False
        ),
    ]
    resolved = _resolve_monophonic_overlaps(notes)
    assert len(resolved) == 2
    assert abs(resolved[0].onset_sec - 0.0) < 1e-6
    assert abs(resolved[0].duration_sec - 0.8) < 1e-6
    assert abs(resolved[1].onset_sec - 0.8) < 1e-6


def test_resolve_monophonic_overlaps_evaluates_ghost_on_raw_duration() -> None:
    """重複解消で先行ノートが短縮された際、MIN_DURATION_FLOOR_SEC クランプ前の実測デュレーションで ghost 判定されること(#55, #125 レビュー指摘)。"""
    notes = [
        NoteEvent(
            onset_sec=0.0, duration_sec=1.0, midi=60, velocity=80, ghost_candidate=False
        ),
        NoteEvent(
            onset_sec=0.02,
            duration_sec=1.0,
            midi=62,
            velocity=80,
            ghost_candidate=False,
        ),
    ]
    resolved = _resolve_monophonic_overlaps(notes)
    assert len(resolved) == 2
    # 次ノート開始が 0.02s のため raw_duration は 0.02s (< 0.06s) -> ghost_candidate=True
    assert resolved[0].ghost_candidate is True
    assert abs(resolved[0].duration_sec - 0.02) < 1e-6


def test_run_vocals_transcription_with_mock(tmp_path: Path) -> None:
    """モックトランスクライバを用いた実行フローの検証。"""
    audio_path = tmp_path / "dummy_vocals.wav"
    _write_sine_wav(audio_path, [(440.0, 0.5)], duration=0.2)

    fake_notes = [
        NoteEvent(
            onset_sec=0.0, duration_sec=0.2, midi=69, velocity=90, ghost_candidate=False
        )
    ]

    def _mock(audio: np.ndarray, sr: int) -> TranscriptionResult:
        return TranscriptionResult(notes=fake_notes, pedals=[])

    result = run_vocals_transcription(audio_path, transcriber=_mock)
    assert len(result.notes) == 1
    assert result.notes[0].midi == 69
    assert result.pedals == []


def test_run_vocals_transcription_with_real_pipeline(tmp_path: Path) -> None:
    """合成サイン波 (A4: 440Hz, MIDI 69) を用いた実パイプライン推論スモークテスト。"""
    audio_path = tmp_path / "real_vocal.wav"
    _write_sine_wav(audio_path, [(440.0, 0.6)], duration=0.4)

    # テスト高速化のため model="tiny" を使用
    result = run_vocals_transcription(audio_path, model="tiny")
    assert len(result.notes) >= 1
    midis = [n.midi for n in result.notes]
    assert 69 in midis or 68 in midis or 70 in midis
    assert result.pedals == []
