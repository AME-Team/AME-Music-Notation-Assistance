"""Stage 3: ボーカルAMT(#55, §6 Stage 3, §15 R-5, §17 Q-2)。

ボーカルステム(`vocals.wav`)から TorchCREPE (F0追跡) + ビブラート平滑化 +
ヒステリシス付きノート分割を用いてノートイベント列を抽出する。

設計書の要件:
- TorchCREPE (F0) による高精度ピッチ・周期性抽出
- ビブラート平滑化・ポルタメント除去(メディアンフィルタ)
- ヒステリシス付きノート分割によるピッチ境界でのチャタリング・細切れ化防止(R-5)
- オンセット検出と組み合わせた同一音高連打の分割
- モノフォニック指定パートでの重複ノート解消
- 共通後処理: 最小デュレーション未満(60ms)・ベロシティ下限未満(25)のノートを
  `ghost_candidate` フラグ付きで保持(削除はしない、§6 Stage 3)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np
import scipy.ndimage

from app.pipeline.transcribe.common import (
    MIN_DURATION_FLOOR_SEC,
    NoteEvent,
    TranscriptionResult,
    is_ghost_candidate,
)

VOCALS_ALGO_VERSION = "1.0.0"

INPUT_SAMPLE_RATE = 16000
HOP_LENGTH = 160  # 10ms フレーム周期 (16000Hz / 160 = 100fps)
FMIN_HZ = 50.0  # 約 G1
FMAX_HZ = 1100.0  # 約 C6 (一般的なソプラノ上限までカバー)
VOICING_THRESHOLD = 0.25  # periodicity の有声判定閾値
MEDIAN_FILTER_KERNEL = 19  # ~190ms (ビブラート周期 4〜7Hz の揺れを吸収)
HYSTERESIS_SEMITONES = 0.6  # ピッチ遷移判定のヒステリシス閾値(半音)

# テスト用に関数シグネチャを抽象化
VocalsTranscriber = Callable[[np.ndarray, int], TranscriptionResult]


def apply_vibrato_smoothing(
    f0: np.ndarray,
    kernel_size: int = MEDIAN_FILTER_KERNEL,
) -> np.ndarray:
    """F0 系列にメディアンフィルタを適用し、ビブラート・微小変動を平滑化する(§15 R-5)。

    NaN(無声フレーム)区間は除外して連続有声区間ごとにフィルタを適用する。
    境界効果によるピッチ歪みを防ぐため、端点拡張(mode='nearest')メディアンフィルタを使用する。
    """
    if len(f0) == 0:
        return f0.copy()

    # kernel_size は奇数である必要がある
    k = kernel_size if kernel_size % 2 == 1 else kernel_size + 1
    smoothed = f0.copy()

    # 有声区間ごとにスライスしてメディアンフィルタを適用
    voiced_indices = np.where(~np.isnan(f0) & (f0 > 0))[0]
    if len(voiced_indices) == 0:
        return smoothed

    # 連続区間の検出
    splits = np.where(np.diff(voiced_indices) > 1)[0]
    segments = np.split(voiced_indices, splits + 1)

    for seg in segments:
        if len(seg) >= k:
            smoothed[seg] = scipy.ndimage.median_filter(f0[seg], size=k, mode="nearest")
        elif len(seg) > 2:
            # 区間長がカーネル長未満の場合は区間長以下の最大の奇数カーネルを適用
            sub_k = len(seg) if len(seg) % 2 == 1 else len(seg) - 1
            if sub_k >= 3:
                smoothed[seg] = scipy.ndimage.median_filter(f0[seg], size=sub_k, mode="nearest")

    return smoothed


def _resolve_monophonic_overlaps(notes: list[NoteEvent]) -> list[NoteEvent]:
    """モノフォニック制約: 時間的に重複するノートを解消する(§6 Stage 3)。"""
    if not notes:
        return []

    sorted_notes = sorted(notes, key=lambda n: n.onset_sec)
    resolved: list[NoteEvent] = []

    for i in range(len(sorted_notes)):
        cur = sorted_notes[i]
        onset = cur.onset_sec
        offset = onset + cur.duration_sec

        if i + 1 < len(sorted_notes):
            next_onset = sorted_notes[i + 1].onset_sec
            if offset > next_onset:
                offset = next_onset

        raw_duration = offset - onset
        if raw_duration <= 0.0:
            continue

        ghost = is_ghost_candidate(raw_duration, cur.velocity)
        resolved.append(
            NoteEvent(
                onset_sec=onset,
                duration_sec=raw_duration,
                midi=cur.midi,
                velocity=cur.velocity,
                ghost_candidate=ghost,
            )
        )

    return resolved


def segment_vocal_notes_from_f0(
    f0: np.ndarray,
    audio: np.ndarray,
    sr: int,
    *,
    hop_length: int = HOP_LENGTH,
    hysteresis_semitones: float = HYSTERESIS_SEMITONES,
) -> list[NoteEvent]:
    """平滑化された F0 系列およびオンセット検出からヒステリシス付きノート分割を行う(R-5)。"""
    import librosa

    onset_frames = set(
        librosa.onset.onset_detect(y=audio, sr=sr, hop_length=hop_length, units="frames")
    )

    notes: list[NoteEvent] = []
    n_frames = len(f0)
    frame_dur = hop_length / sr

    current_start: int | None = None
    current_center_midi: float | None = None
    current_int_midi: int | None = None

    def _flush_note(start_frame: int, end_frame: int, midi_pitch: int) -> None:
        start_sec = start_frame * frame_dur
        end_sec = (end_frame + 1) * frame_dur
        raw_duration = max(0.0, end_sec - start_sec)
        duration_sec = max(raw_duration, MIN_DURATION_FLOOR_SEC)

        start_sample = int(start_frame * hop_length)
        end_sample = min(int((end_frame + 1) * hop_length), len(audio))
        segment = audio[start_sample:end_sample]
        if len(segment) > 0:
            rms = float(np.sqrt(np.mean(segment**2)))
            db = 20.0 * np.log10(rms + 1e-9)
            norm = float(np.clip((db + 50.0) / 50.0, 0.0, 1.0))
            velocity = int(np.clip(1 + 126 * norm, 1, 127))
        else:
            velocity = 64

        # ghost判定は後続の _resolve_monophonic_overlaps で一元評価されるため仮値 False
        notes.append(
            NoteEvent(
                onset_sec=start_sec,
                duration_sec=duration_sec,
                midi=midi_pitch,
                velocity=velocity,
                ghost_candidate=False,
            )
        )

    for t in range(n_frames):
        pitch = f0[t]
        is_voiced = not np.isnan(pitch) and pitch > 0
        if is_voiced:
            exact_midi = float(librosa.hz_to_midi(pitch))
            is_onset = t in onset_frames

            if current_start is None:
                current_start = t
                current_center_midi = exact_midi
                current_int_midi = int(np.round(exact_midi))
            else:
                assert current_center_midi is not None
                assert current_int_midi is not None
                # ヒステリシス判定: 現在の中心音高から hysteresis_semitones 以上乖離した場合に遷移
                diff = abs(exact_midi - current_center_midi)
                target_int_midi = int(np.round(exact_midi))
                pitch_shifted = diff >= hysteresis_semitones and target_int_midi != current_int_midi
                if is_onset or pitch_shifted:
                    # 前ノート終了・新ノート開始
                    _flush_note(current_start, t - 1, current_int_midi)
                    current_start = t
                    current_center_midi = exact_midi
                    current_int_midi = target_int_midi
                else:
                    # 同一ノート内: 中心音高を緩やかに追従(EMA)
                    current_center_midi = 0.9 * current_center_midi + 0.1 * exact_midi
        else:
            if current_start is not None and current_int_midi is not None:
                _flush_note(current_start, t - 1, current_int_midi)
                current_start = None
                current_center_midi = None
                current_int_midi = None

    if current_start is not None and current_int_midi is not None:
        _flush_note(current_start, n_frames - 1, current_int_midi)

    return _resolve_monophonic_overlaps(notes)


def run_vocals_transcription(
    audio_path: Path,
    *,
    transcriber: VocalsTranscriber | None = None,
    model: str = "full",
) -> TranscriptionResult:
    """ボーカルステムからノートイベント列を得る(#55, §6 Stage 3)。

    `transcriber` を渡すことでテストでのモックが可能。
    """
    import librosa

    if transcriber is not None:
        audio, sr = librosa.load(str(audio_path), sr=INPUT_SAMPLE_RATE, mono=True)
        return transcriber(audio, sr)

    import torch
    import torchcrepe

    audio, sr = librosa.load(str(audio_path), sr=INPUT_SAMPLE_RATE, mono=True)
    if len(audio) == 0:
        return TranscriptionResult(notes=[], pedals=[])

    # torchcrepe の推論
    audio_tensor = torch.from_numpy(audio.astype(np.float32)).unsqueeze(0)
    device = "cpu"

    pitch_tensor, periodicity_tensor = torchcrepe.predict(
        audio_tensor,
        sr,
        hop_length=HOP_LENGTH,
        fmin=FMIN_HZ,
        fmax=FMAX_HZ,
        model=model,
        batch_size=2048,
        device=device,
        return_periodicity=True,
    )

    pitch = pitch_tensor.squeeze(0).cpu().numpy()
    periodicity = periodicity_tensor.squeeze(0).cpu().numpy()

    # 無声区間を NaN に設定
    f0 = np.where(periodicity >= VOICING_THRESHOLD, pitch, np.nan)

    # ビブラート平滑化
    smoothed_f0 = apply_vibrato_smoothing(f0, kernel_size=MEDIAN_FILTER_KERNEL)

    # ヒステリシス付きノート分割
    notes = segment_vocal_notes_from_f0(
        smoothed_f0,
        audio,
        sr,
        hop_length=HOP_LENGTH,
        hysteresis_semitones=HYSTERESIS_SEMITONES,
    )

    return TranscriptionResult(notes=notes, pedals=[])
