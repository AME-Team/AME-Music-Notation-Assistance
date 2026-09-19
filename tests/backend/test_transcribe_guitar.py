"""Stage 3: ギター/その他AMT(Basic Pitch ONNX)の単体テスト(#56, §6 Stage 3, Q-15)。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from app.pipeline.transcribe.common import NoteEvent, TranscriptionResult
from app.pipeline.transcribe.guitar import (
    ANNOT_N_FRAMES,
    AUDIO_N_SAMPLES,
    AUDIO_SAMPLE_RATE,
    GUITAR_ALGO_VERSION,
    MIN_NOTE_LEN_FRAMES,
    OTHER_ALGO_VERSION,
    _infer_onsets_from_frames,
    _model_frames_to_time,
    _output_to_notes_polyphonic,
    _sha256_file,
    _unwrap_output,
    _window_audio,
    ensure_onnx_model,
    run_guitar_transcription,
)


def _write_sine_wav(
    path: Path,
    freqs: list[tuple[float, float]],
    duration: float = 1.0,
    sr: int = AUDIO_SAMPLE_RATE,
) -> None:
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    signal = np.zeros_like(t)
    for freq, amp in freqs:
        signal += amp * np.sin(2 * np.pi * freq * t)
    signal = np.clip(signal, -1.0, 1.0)
    sf.write(str(path), signal, sr, format="WAV")


def test_guitar_algo_version_is_defined() -> None:
    assert isinstance(GUITAR_ALGO_VERSION, str)
    assert len(GUITAR_ALGO_VERSION.split(".")) >= 2


def test_guitar_and_other_algo_versions_are_kept_in_sync() -> None:
    """guitar/otherは同一の`run_guitar_transcription`を共有するため、

    アルゴリズム更新時は両定数を同時に上げる必要がある。片方だけ更新すると
    もう片方のステムが古いキャッシュのままスキップされ続けるため、この
    テストで同期漏れを検知する(#56レビュー指摘)。意図的に分岐させる場合は
    このテスト自体を更新すること。
    """
    assert GUITAR_ALGO_VERSION == OTHER_ALGO_VERSION


def test_min_note_len_frames_matches_basic_pitch_reference() -> None:
    """basic-pitch本家の`DEFAULT_MIN_NOTE_LEN = 11`と一致すること(#56)。"""
    assert MIN_NOTE_LEN_FRAMES == 11


def test_window_audio_pads_final_window() -> None:
    hop = 100
    audio = np.arange(250, dtype=np.float32)
    windows = _window_audio(audio, hop)
    assert all(len(w) == AUDIO_N_SAMPLES for w in windows)
    # 最初の要素は元の音声と一致(パディングなし部分)
    assert windows[0][0] == 0.0
    assert windows[0][249] == 249.0
    assert windows[0][250] == 0.0


def test_unwrap_output_trims_overlap_and_length() -> None:
    n_windows = 3
    n_freqs = 4
    batched = np.ones((n_windows, ANNOT_N_FRAMES, n_freqs), dtype=np.float32)
    # 各ウィンドウを識別できるよう値を変える
    for i in range(n_windows):
        batched[i, :, :] = i

    unwrapped = _unwrap_output(
        batched, original_length=AUDIO_SAMPLE_RATE * 4, n_overlapping_frames=30
    )
    # オーバーラップ(前後15フレームずつ)が除去され、ウィンドウごとに142フレーム残る
    assert unwrapped.shape[1] == n_freqs
    assert unwrapped.shape[0] <= n_windows * (ANNOT_N_FRAMES - 30)


def test_infer_onsets_from_frames_handles_all_zero_frames_without_nan() -> None:
    """フレーム強度が完全に一定(差分ゼロ)でも0除算によるNaNが出ないこと(#56)。"""
    frames = np.zeros((10, 3), dtype=np.float64)
    onsets = np.zeros((10, 3), dtype=np.float64)
    result = _infer_onsets_from_frames(onsets, frames)
    assert not np.any(np.isnan(result))
    assert np.all(result == 0)


def test_output_to_notes_polyphonic_detects_note_from_onset_peak() -> None:
    """明確なonsetピーク+持続エネルギーから1音のノートが抽出されること。"""
    n_frames = 40
    n_freqs = 5
    frames = np.zeros((n_frames, n_freqs))
    onsets = np.zeros((n_frames, n_freqs))

    freq_idx = 2
    onsets[5, freq_idx] = 0.9
    frames[5:30, freq_idx] = 0.8

    notes = _output_to_notes_polyphonic(
        frames,
        onsets,
        onset_thresh=0.5,
        frame_thresh=0.3,
        min_note_len=5,
    )

    assert len(notes) == 1
    start_idx, end_idx, midi_pitch, amplitude = notes[0]
    assert start_idx == 5
    assert end_idx > start_idx
    assert midi_pitch == freq_idx + 21
    assert 0.0 < amplitude <= 1.0


def test_output_to_notes_polyphonic_rejects_notes_shorter_than_min_len() -> None:
    n_frames = 40
    n_freqs = 5
    frames = np.zeros((n_frames, n_freqs))
    onsets = np.zeros((n_frames, n_freqs))

    onsets[5, 2] = 0.9
    frames[5:8, 2] = 0.8  # 3フレームのみ(min_note_len=10未満)

    notes = _output_to_notes_polyphonic(
        frames, onsets, onset_thresh=0.5, frame_thresh=0.3, min_note_len=10
    )
    assert notes == []


def test_output_to_notes_polyphonic_melodia_trick_recovers_missed_onset() -> None:
    """明確なonsetピークが無くても、持続的な残余エネルギーからノートが拾われること(melodia trick)。"""
    n_frames = 40
    n_freqs = 5
    frames = np.zeros((n_frames, n_freqs))
    onsets = np.zeros((n_frames, n_freqs))  # onsetピークなし

    frames[10:30, 3] = 0.8

    notes = _output_to_notes_polyphonic(
        frames, onsets, onset_thresh=0.5, frame_thresh=0.3, min_note_len=5
    )
    assert len(notes) == 1
    assert notes[0][2] == 3 + 21


def test_output_to_notes_polyphonic_handles_zero_frames() -> None:
    frames = np.zeros((0, 5))
    onsets = np.zeros((0, 5))
    assert (
        _output_to_notes_polyphonic(
            frames, onsets, onset_thresh=0.5, frame_thresh=0.3, min_note_len=5
        )
        == []
    )


def test_model_frames_to_time_is_monotonic_and_starts_near_zero() -> None:
    times = _model_frames_to_time(200)
    assert times[0] == pytest.approx(0.0, abs=1e-3)
    assert np.all(np.diff(times) > 0)


def test_ensure_onnx_model_reuses_valid_cached_file(
    tmp_path: Path, monkeypatch
) -> None:
    """既にSHA256が一致する有効なファイルがあれば再ダウンロードしないこと(#56)。"""
    import app.pipeline.transcribe.guitar as guitar_module

    fake_model_path = tmp_path / "nmp.onnx"
    fake_content = b"fake-onnx-model-bytes"
    fake_model_path.write_bytes(fake_content)
    fake_hash = _sha256_file(fake_model_path)
    monkeypatch.setattr(guitar_module, "_MODEL_SHA256", fake_hash)

    result = ensure_onnx_model(fake_model_path)
    assert result == fake_model_path
    assert fake_model_path.read_bytes() == fake_content


def test_run_guitar_transcription_with_mock_transcriber(tmp_path: Path) -> None:
    audio_path = tmp_path / "dummy_guitar.wav"
    _write_sine_wav(audio_path, [(164.81, 0.5)], duration=0.2)

    fake_notes = [
        NoteEvent(
            onset_sec=0.0, duration_sec=0.2, midi=52, velocity=80, ghost_candidate=False
        )
    ]

    def _mock_transcriber(audio: np.ndarray, sr: int) -> TranscriptionResult:
        return TranscriptionResult(notes=fake_notes, pedals=[])

    result = run_guitar_transcription(audio_path, transcriber=_mock_transcriber)
    assert len(result.notes) == 1
    assert result.notes[0].midi == 52
    assert result.pedals == []


def test_run_guitar_transcription_returns_empty_for_empty_audio(tmp_path: Path) -> None:
    audio_path = tmp_path / "empty.wav"
    sf.write(str(audio_path), np.zeros(0, dtype=np.float32), AUDIO_SAMPLE_RATE)

    result = run_guitar_transcription(audio_path)
    assert result.notes == []
    assert result.pedals == []


@pytest.mark.slow
def test_run_guitar_transcription_with_real_model(tmp_path: Path) -> None:
    """実ONNXモデルで合成和音(E3+A3+C4)を推論する(#56)。ModelはGitHubから

    初回のみダウンロードされるため、ネットワーク不通環境ではスキップする
    (`test_run_piano_transcription_with_real_model`と同じ方針)。
    """
    audio_path = tmp_path / "real_guitar.wav"
    _write_sine_wav(
        audio_path,
        [(164.81, 0.3), (220.00, 0.3), (261.63, 0.3)],  # E3, A3, C4
        duration=2.0,
    )

    try:
        result = run_guitar_transcription(audio_path)
    except Exception as exc:
        import httpx

        if isinstance(exc, (httpx.HTTPError, OSError)):
            pytest.skip(
                f"ONNX model download failed due to network/server outage: {exc}"
            )
        raise

    assert isinstance(result.notes, list)
    midis = {n.midi for n in result.notes}
    assert {52, 57, 60} & midis
