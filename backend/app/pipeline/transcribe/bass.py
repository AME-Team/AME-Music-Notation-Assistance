"""Stage 3: ベースAMT(#54, §6 Stage 3)。

ベースステム(`bass.wav`)からLPF(≤ 500Hz) + F0追跡(モノフォニック制約)および
オクターブエラー補正(R-6)を用いてノートイベント列を抽出する。

設計書の要件:
- LPF (≤ 500Hz) + F0 追跡(モノフォニック)
- オクターブエラー補正: LPF + 倍音関係チェック(R-6)
- オンセット/オフセット分割によるノート化
- モノフォニック指定パートでの重複ノート解消
- 共通後処理: 最小デュレーション未満(60ms)・ベロシティ下限未満(25)のノートを
  `ghost_candidate` フラグ付きで保持(削除はしない、§6 Stage 3)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import scipy.signal

from app.pipeline.transcribe.piano import (
    _MIN_DURATION_FLOOR_SEC,
    GHOST_MIN_DURATION_SEC,
    GHOST_MIN_VELOCITY,
    NoteEvent,
    TranscriptionResult,
)

if TYPE_CHECKING:
    pass

INPUT_SAMPLE_RATE = 22050
LPF_CUTOFF_HZ = 500.0
FMIN_HZ = 30.87  # B0 (5弦ベースの最低音域までカバー)
FMAX_HZ = 500.0  # LPF カットオフに一致

# テスト用に関数シグネチャを抽象化
BassTranscriber = Callable[[np.ndarray, int], TranscriptionResult]


def apply_lpf(audio: np.ndarray, sr: int, cutoff_hz: float = LPF_CUTOFF_HZ) -> np.ndarray:
    """4次 Butterworth ゼロ位相ローパスフィルタを適用する(§6 Stage 3)。"""
    if len(audio) == 0:
        return audio
    # ナイキスト周波数未満であることを保証
    nyquist = 0.5 * sr
    normalized_cutoff = min(cutoff_hz, nyquist * 0.95) / nyquist
    sos = scipy.signal.butter(4, normalized_cutoff, btype="lowpass", output="sos")
    padlen = 3 * (2 * sos.shape[0] + 1)
    if len(audio) <= padlen:
        # 入力が短すぎてゼロ位相パディングできない場合は因果的フィルタにフォールバック
        # (#124 レビュー指摘)
        return scipy.signal.sosfilt(sos, audio)
    return scipy.signal.sosfiltfilt(sos, audio, padlen=padlen)


def check_and_correct_octave_error(
    f0_series: np.ndarray,
    filtered_audio: np.ndarray,
    sr: int,
    *,
    n_fft: int = 2048,
    hop_length: int = 512,
    fmin_hz: float = FMIN_HZ,
) -> np.ndarray:
    """倍音関係をチェックし、オクターブ跳躍(2*f0を基本波と誤認)を補正する(R-6)。

    ベースは基本波(f0)のエネルギーが小さく第2倍音(2*f0)が支配的になりやすいため、
    F0追跡が1オクターブ上にずれるリスクがある。
    各フレーム t において推定周波数 f の半分 f/2 付近に局所ピークが存在する場合、
    基本波を f/2 に補正する(#124 レビュー指摘: 全体平均ではなくフレーム固有スペクトルを使用)。
    """
    import librosa

    corrected_f0 = np.copy(f0_series)
    valid_mask = ~np.isnan(corrected_f0) & (corrected_f0 > 0)
    if not np.any(valid_mask):
        return corrected_f0

    stft_mag = np.abs(librosa.stft(filtered_audio, n_fft=n_fft, hop_length=hop_length))
    freq_bins = librosa.fft_frequencies(sr=sr, n_fft=n_fft)

    n_frames = min(len(corrected_f0), stft_mag.shape[1])
    for t in range(n_frames):
        f = corrected_f0[t]
        if np.isnan(f) or f <= 0:
            continue
        f_sub = f / 2.0
        if f_sub < fmin_hz:
            continue

        idx_sub = int(np.argmin(np.abs(freq_bins - f_sub)))
        # フレーム t のスペクトル(端点過渡応答の影響を和らげるため前後1フレームを含めた平均)
        t_start = max(0, t - 1)
        t_end = min(stft_mag.shape[1], t + 2)
        frame_mag = np.mean(stft_mag[:, t_start:t_end], axis=1)
        start_bin = max(0, idx_sub - 4)
        end_bin = min(len(frame_mag), idx_sub + 5)
        window = frame_mag[start_bin:end_bin]
        if len(window) < 3:
            continue

        center_offset = idx_sub - start_bin
        local_max = int(np.argmax(window))
        # 局所ピークが idx_sub (またはその隣接) にあり、周囲の中央値に対して有意(1.4倍以上)か
        is_local_peak = abs(local_max - center_offset) <= 1
        noise_floor = float(np.median(window))
        if is_local_peak and frame_mag[idx_sub] > 1.4 * noise_floor and frame_mag[idx_sub] > 1e-4:
            corrected_f0[t] = f_sub

    # 孤立フレームのスパイク除去と平滑化のための中央値フィルタ
    valid_indices = np.where(~np.isnan(corrected_f0) & (corrected_f0 > 0))[0]
    if len(valid_indices) >= 3:
        med = scipy.signal.medfilt(corrected_f0[valid_indices], kernel_size=3)
        corrected_f0[valid_indices] = med

    return corrected_f0


def _resolve_monophonic_overlaps(notes: list[NoteEvent]) -> list[NoteEvent]:
    """モノフォニック制約: 時間的に重複するノートを解消する(§6 Stage 3)。

    先行ノートの終了時刻が後続ノートの開始時刻を越えている場合、
    後続ノートの開始時刻までトリミングする。
    """
    if len(notes) <= 1:
        return notes

    # 開始時刻でソート
    sorted_notes = sorted(notes, key=lambda n: n.onset_sec)
    resolved: list[NoteEvent] = []

    for i in range(len(sorted_notes)):
        cur = sorted_notes[i]
        onset = cur.onset_sec
        offset = onset + cur.duration_sec

        if i + 1 < len(sorted_notes):
            next_onset = sorted_notes[i + 1].onset_sec
            if offset > next_onset:
                offset = max(next_onset, onset + _MIN_DURATION_FLOOR_SEC)

        new_duration = max(offset - onset, _MIN_DURATION_FLOOR_SEC)
        ghost = new_duration < GHOST_MIN_DURATION_SEC or cur.velocity < GHOST_MIN_VELOCITY
        resolved.append(
            NoteEvent(
                onset_sec=onset,
                duration_sec=new_duration,
                midi=cur.midi,
                velocity=cur.velocity,
                ghost_candidate=ghost,
            )
        )

    return resolved


def _segment_notes_from_f0(
    f0: np.ndarray,
    audio: np.ndarray,
    sr: int,
    *,
    hop_length: int = 512,
) -> list[NoteEvent]:
    """F0 系列およびオンセット検出からノートイベント列を生成する。"""
    import librosa

    # オンセットフレームを検出
    onset_frames = set(
        librosa.onset.onset_detect(y=audio, sr=sr, hop_length=hop_length, units="frames")
    )

    notes: list[NoteEvent] = []
    n_frames = len(f0)
    frame_dur = hop_length / sr

    current_start: int | None = None
    current_midi: int | None = None

    def _flush_note(start_frame: int, end_frame: int, midi_pitch: int) -> None:
        start_sec = start_frame * frame_dur
        end_sec = max((end_frame + 1) * frame_dur, start_sec + _MIN_DURATION_FLOOR_SEC)
        duration_sec = end_sec - start_sec

        # 区間の RMS エネルギーを計算してベロシティ(1〜127)へマッピング
        start_sample = int(start_frame * hop_length)
        end_sample = min(int((end_frame + 1) * hop_length), len(audio))
        segment = audio[start_sample:end_sample]
        if len(segment) > 0:
            rms = float(np.sqrt(np.mean(segment**2)))
            db = 20.0 * np.log10(rms + 1e-9)
            # -50 dBFS 〜 0 dBFS を 1 〜 127 にマッピング
            norm = float(np.clip((db + 50.0) / 50.0, 0.0, 1.0))
            velocity = int(np.clip(1 + 126 * norm, 1, 127))
        else:
            velocity = 64

        ghost = duration_sec < GHOST_MIN_DURATION_SEC or velocity < GHOST_MIN_VELOCITY
        notes.append(
            NoteEvent(
                onset_sec=start_sec,
                duration_sec=duration_sec,
                midi=midi_pitch,
                velocity=velocity,
                ghost_candidate=ghost,
            )
        )

    for t in range(n_frames):
        pitch = f0[t]
        is_voiced = not np.isnan(pitch) and pitch > 0
        if is_voiced:
            midi = int(np.round(librosa.hz_to_midi(pitch)))
            is_onset = t in onset_frames

            if current_start is None:
                # ノート開始
                current_start = t
                current_midi = midi
            elif is_onset or midi != current_midi:
                # ピッチ変更または新たなオンセットで前ノート終了
                _flush_note(current_start, t - 1, current_midi)  # type: ignore[arg-type]
                current_start = t
                current_midi = midi
        else:
            if current_start is not None:
                # 無音区間でノート終了
                _flush_note(current_start, t - 1, current_midi)  # type: ignore[arg-type]
                current_start = None
                current_midi = None

    if current_start is not None and current_midi is not None:
        _flush_note(current_start, n_frames - 1, current_midi)

    return _resolve_monophonic_overlaps(notes)


def run_bass_transcription(
    audio_path: Path,
    *,
    transcriber: BassTranscriber | None = None,
) -> TranscriptionResult:
    """ベースステムからノートイベント列を得る(#54, §6 Stage 3)。

    `transcriber` を渡すことでテストでのモックが可能。
    """
    import librosa

    audio, sr = librosa.load(str(audio_path), sr=INPUT_SAMPLE_RATE, mono=True)

    if transcriber is not None:
        return transcriber(audio, sr)

    # 1. LPF (≤ 500Hz)
    filtered = apply_lpf(audio, sr, cutoff_hz=LPF_CUTOFF_HZ)

    # 2. F0 追跡 (pYIN)
    hop_length = 512
    f0, voiced_flag, _ = librosa.pyin(
        filtered,
        fmin=FMIN_HZ,
        fmax=FMAX_HZ,
        sr=sr,
        frame_length=2048,
        hop_length=hop_length,
    )
    # unvoiced を NaN に統一
    f0[~voiced_flag] = np.nan

    # 3. オクターブエラー補正 (R-6)
    corrected_f0 = check_and_correct_octave_error(
        f0, filtered, sr, n_fft=2048, hop_length=hop_length, fmin_hz=FMIN_HZ
    )

    # 4. ノートセグメンテーション & モノフォニック重複解消 & ゴースト判定
    notes = _segment_notes_from_f0(corrected_f0, filtered, sr, hop_length=hop_length)

    return TranscriptionResult(notes=notes, pedals=[])
