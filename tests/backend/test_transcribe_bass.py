"""Stage 3: ベースAMT の単体テスト(#54, §6 Stage 3, §15 R-6)。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from app.pipeline.transcribe.bass import (
    LPF_CUTOFF_HZ,
    _resolve_monophonic_overlaps,
    _segment_notes_from_f0,
    apply_lpf,
    check_and_correct_octave_error,
    run_bass_transcription,
)
from app.pipeline.transcribe.piano import (
    NoteEvent,
    TranscriptionResult,
)


def _write_sine_wav(
    path: Path, freqs: list[tuple[float, float]], duration: float = 1.0, sr: int = 22050
) -> None:
    """テスト用 WAV を生成する。freqs は [(frequency_hz, amplitude), ...]"""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    signal = np.zeros_like(t)
    for freq, amp in freqs:
        signal += amp * np.sin(2 * np.pi * freq * t)
    # クリップ
    signal = np.clip(signal, -1.0, 1.0)
    sf.write(str(path), signal, sr, format="WAV")


def test_apply_lpf_attenuates_high_frequencies() -> None:
    sr = 22050
    duration = 0.5
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    # 低域 100Hz (通過) + 高域 2000Hz (減衰)
    low_freq = 0.5 * np.sin(2 * np.pi * 100 * t)
    high_freq = 0.5 * np.sin(2 * np.pi * 2000 * t)
    composite = low_freq + high_freq

    filtered = apply_lpf(composite, sr, cutoff_hz=LPF_CUTOFF_HZ)

    # 低域の振幅はおよそ維持される
    rms_low_orig = np.sqrt(np.mean(low_freq**2))
    rms_filtered = np.sqrt(np.mean(filtered**2))
    rms_composite = np.sqrt(np.mean(composite**2))

    assert rms_filtered < rms_composite
    assert abs(rms_filtered - rms_low_orig) < 0.05


def test_apply_lpf_handles_very_short_signals() -> None:
    """#124 レビュー指摘: padlen 未満の極めて短い信号でも例外を出さずにフィルタ処理できること。"""
    sr = 22050
    # わずか 10 サンプルの短信号 (padlen ~ 27 未満)
    short_signal = np.sin(np.linspace(0, 1, 10))
    filtered = apply_lpf(short_signal, sr, cutoff_hz=LPF_CUTOFF_HZ)
    assert len(filtered) == 10
    # 空信号
    assert len(apply_lpf(np.array([]), sr)) == 0


def test_octave_error_correction_detects_subharmonic() -> None:
    """第2倍音が支配的で基本波が微弱な場合、1オクターブ下に補正されること(R-6)。"""
    sr = 22050
    duration = 0.5
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)

    f0_true = 82.41  # E2
    # 基本波が微弱 (0.05) で第2倍音 (164.82Hz) が強い (0.8)
    signal = 0.05 * np.sin(2 * np.pi * f0_true * t) + 0.8 * np.sin(
        2 * np.pi * 2 * f0_true * t
    )

    # F0 推定器が誤って第2倍音 (164.82Hz) を推定したとする
    n_frames = 20
    f0_erroneous = np.full(n_frames, 2 * f0_true)

    corrected = check_and_correct_octave_error(f0_erroneous, signal, sr)

    # 1オクターブ下の基本波 (~82.41Hz) に補正されること
    assert np.allclose(corrected, f0_true, atol=2.0)


def test_octave_error_correction_preserves_pure_tone() -> None:
    """純音(倍音なし)の場合に誤補正(オクターブ下降)されないこと。"""
    sr = 22050
    duration = 0.5
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)

    f_pure = 164.81  # E3 単音
    signal = 0.8 * np.sin(2 * np.pi * f_pure * t)

    n_frames = 20
    f0_series = np.full(n_frames, f_pure)

    corrected = check_and_correct_octave_error(f0_series, signal, sr)

    # E3 のまま維持されること
    assert np.allclose(corrected, f_pure, atol=1.0)


def test_resolve_monophonic_overlaps_trims_preceding_notes() -> None:
    """先行ノートが後続ノートの onset を越えている場合、重複が解消されること。"""
    notes = [
        NoteEvent(
            onset_sec=0.0, duration_sec=1.5, midi=40, velocity=80, ghost_candidate=False
        ),
        NoteEvent(
            onset_sec=1.0, duration_sec=1.0, midi=43, velocity=85, ghost_candidate=False
        ),
        NoteEvent(
            onset_sec=2.5, duration_sec=0.5, midi=45, velocity=70, ghost_candidate=False
        ),
    ]

    resolved = _resolve_monophonic_overlaps(notes)

    assert len(resolved) == 3
    # 最初のノートは 1.0秒でトリムされる
    assert abs(resolved[0].onset_sec - 0.0) < 1e-6
    assert abs(resolved[0].duration_sec - 1.0) < 1e-6
    # 2番目のノートは重複なし
    assert abs(resolved[1].onset_sec - 1.0) < 1e-6
    assert abs(resolved[1].duration_sec - 1.0) < 1e-6
    # 3番目のノート
    assert abs(resolved[2].onset_sec - 2.5) < 1e-6
    assert abs(resolved[2].duration_sec - 0.5) < 1e-6


def test_segment_notes_flags_short_and_quiet_notes_as_ghost() -> None:
    sr = 22050
    hop = 512

    # 短いノート(2フレーム ~ 46ms < 60ms)
    f0 = np.array([np.nan, 82.4, 82.4, np.nan, 110.0, 110.0, 110.0, 110.0, np.nan])
    # 小さな振幅のダミー波形
    audio = np.full(len(f0) * hop, 0.001)

    notes = _segment_notes_from_f0(f0, audio, sr, hop_length=hop)

    assert len(notes) == 2
    # 1つ目は短く、かつ振幅が小さいため ghost_candidate
    assert notes[0].ghost_candidate is True
    # 2つ目も振幅が小さいため ghost_candidate
    assert notes[1].ghost_candidate is True


def test_run_bass_transcription_with_mock_transcriber(tmp_path: Path) -> None:
    audio_path = tmp_path / "dummy_bass.wav"
    _write_sine_wav(audio_path, [(82.41, 0.5)], duration=0.2)

    fake_notes = [
        NoteEvent(
            onset_sec=0.0, duration_sec=0.2, midi=40, velocity=80, ghost_candidate=False
        )
    ]

    def _mock_transcriber(audio: np.ndarray, sr: int) -> TranscriptionResult:
        return TranscriptionResult(notes=fake_notes, pedals=[])

    result = run_bass_transcription(audio_path, transcriber=_mock_transcriber)
    assert len(result.notes) == 1
    assert result.notes[0].midi == 40
    assert result.pedals == []


def test_run_bass_transcription_with_real_pipeline(tmp_path: Path) -> None:
    """合成ベーストーン(E2: 82.41Hz, MIDI 40)の実パイプライン推論テスト。"""
    audio_path = tmp_path / "real_bass.wav"
    # 0.5秒の E2 音 (MIDI 40)
    _write_sine_wav(audio_path, [(82.41, 0.6)], duration=0.5)

    result = run_bass_transcription(audio_path)

    assert len(result.notes) >= 1
    # 検出されたノートの主要な MIDI 番号が 40 (E2) 付近であること
    midis = [n.midi for n in result.notes]
    assert 40 in midis or 39 in midis or 41 in midis
    assert result.pedals == []
