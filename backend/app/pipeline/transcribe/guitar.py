"""Stage 3: ギター/その他パートAMT(#56, §6 Stage 3, Q-15)。

Basic Pitch(Spotify, Apache-2.0)が配布する学習済みONNXモデルを直接onnxruntimeで
推論し、ノートイベントを抽出する。`basic-pitch` パッケージそのものは
TensorFlowを強制依存させ、かつ同梱numpyの上限バージョンがPython 3.12環境と
非互換のため採用しない(#56 Q-15、設計書§1.2)。影響を受けるのはこの
Guitar/Otherのポリフォニック採譜のみで、主対象のピアノ(ByteDanceモデル)は
無傷(§6 Stage 3 表)。

モデルファイル(`nmp.onnx`, Apache-2.0, ~230KB)は `spotify/basic-pitch` の
`v0.4.0` タグから初回実行時に取得しSHA256で検証する(ピアノの
`transcribe.piano.ensure_checkpoint` と同じhttpx+filelock方式)。モデル出力
(note/onset のposteriorgram)からノートイベントへ変換する後処理は、
`basic_pitch/note_creation.py`(Apache-2.0)のアルゴリズムを、重い依存
(`mir_eval`/`pretty_midi`/TensorFlow)を排してnumpy/scipyのみで移植したもの。
本プロジェクトはpitch bendを扱わないため(`common.NoteEvent`にフィールドが
無い)、pitch bend推定(`get_pitch_bends`)は移植しない。

対象ステム: guitar / other(htdemucs_6sの6ステムのうち、ピアノ以外の残り2つの
ポリフォニック楽器)。設計書は「精度は最も低い。校正前提」と位置づけている。

設計書の要件:
- Basic Pitch ONNXモデルの直接利用(onnxruntime、TensorFlow非依存)
- posteriorgram(note/onset)からのノートイベント抽出後処理を自前実装
- 共通後処理: 最小デュレーション未満(60ms)・ベロシティ下限未満(25)のノートを
  `ghost_candidate` フラグ付きで保持(削除はしない、§6 Stage 3)
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import scipy.signal

from app.pipeline.transcribe.common import (
    MIN_DURATION_FLOOR_SEC,
    NoteEvent,
    TranscriptionResult,
    is_ghost_candidate,
)

if TYPE_CHECKING:
    import onnxruntime as ort

# ギター/その他AMTのアルゴリズム版数。パラメータやロジック更新時にインクリメントする
# (bass.pyの`BASS_ALGO_VERSION`と同じ運用、#124レビュー指摘を踏襲)。guitar/otherは
# 同一の`run_guitar_transcription`を共有するが、将来どちらか一方だけロジックが
# 分岐した場合にメタデータが誤誘導にならないよう、別定数として分けておく
# (#56レビュー指摘: 当面は同値)。
GUITAR_ALGO_VERSION = "1.0.0"
OTHER_ALGO_VERSION = "1.0.0"

# --- Basic Pitch (Spotify, Apache-2.0) 由来のモデル定数 ---
# https://github.com/spotify/basic-pitch/blob/v0.4.0/basic_pitch/constants.py 準拠。
# `basic-pitch`パッケージ自体は使わず後処理アルゴリズムのみを移植するため、
# 依存パッケージのバージョン変動で値がずれないようここに直接定義する。
AUDIO_SAMPLE_RATE = 22050
FFT_HOP = 256
AUDIO_WINDOW_LENGTH_SEC = 2
ANNOTATIONS_FPS = AUDIO_SAMPLE_RATE // FFT_HOP  # == 86
ANNOT_N_FRAMES = ANNOTATIONS_FPS * AUDIO_WINDOW_LENGTH_SEC  # == 172 (1推論窓あたりの出力フレーム数)
AUDIO_N_SAMPLES = AUDIO_SAMPLE_RATE * AUDIO_WINDOW_LENGTH_SEC - FFT_HOP  # == 43844
MIDI_OFFSET = 21  # frames/onsets列インデックス0はMIDI21(A0、ピアノ最低音)に対応
N_OVERLAPPING_FRAMES = 30  # 隣接窓間でオーバーラップさせるフレーム数
ONSET_THRESHOLD = 0.5
FRAME_THRESHOLD = 0.3
MINIMUM_NOTE_LENGTH_MS = 127.7
ENERGY_TOLERANCE = 11  # ノート終端判定でのエネルギー途切れ許容フレーム数
# モデル出力を時刻へ変換する際の窓オフセット補正定数。basic-pitch本家が実測で
# 定めた値で、アルゴリズム上の理論的導出根拠はない(basic-pitch由来のまま移植)。
MAGIC_ALIGNMENT_OFFSET = 0.0018

# DEFAULT_MINIMUM_NOTE_LENGTH_MS(127.7ms)をフレーム数に換算した値(basic-pitch
# 本家の`DEFAULT_MIN_NOTE_LEN = 11`と一致する)。
MIN_NOTE_LEN_FRAMES = int(round(MINIMUM_NOTE_LENGTH_MS / 1000 * (AUDIO_SAMPLE_RATE / FFT_HOP)))

# モデル配布元をv0.4.0タグ(不変のgit参照)に固定し、内容が変わらないことを
# SHA256で検証する(#56, NFR-11のトレーサビリティ方針)。
_MODEL_URL = (
    "https://raw.githubusercontent.com/spotify/basic-pitch/v0.4.0/"
    "basic_pitch/saved_models/icassp_2022/nmp.onnx"
)
_MODEL_SHA256 = "2c3c1d144bfa61ad236e92e169c13535c880469a12a047d4e73451f2c059a0ec"
DEFAULT_MODEL_PATH = Path.home() / "basic_pitch_data" / "nmp.onnx"

# ONNXモデルの入出力テンソル名(実ファイルを`onnxruntime`でロードして実測確認済み、
# basic_pitch/inference.pyのハードコード値とも一致する、#56)。
ONNX_INPUT_NAME = "serving_default_input_2:0"
ONNX_OUTPUT_NOTE = "StatefulPartitionedCall:1"
ONNX_OUTPUT_ONSET = "StatefulPartitionedCall:2"

# テスト用に関数シグネチャを抽象化(piano/bass/vocalsの各Transcriber型と同形)。
GuitarTranscriber = Callable[[np.ndarray, int], TranscriptionResult]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_onnx_model(path: Path = DEFAULT_MODEL_PATH) -> Path:
    """Basic PitchのONNXモデル(~230KB)を取得する(#56)。

    ダウンロード基盤(httpx取得+FileLockによるプロセス間直列化+アトミック配置)は
    `piano.ensure_checkpoint`と共通のため`_model_fetch.fetch_verified_model`に
    集約されている(#56レビュー指摘)。ファイルサイズが小さいため、pianoの
    サイズ閾値方式より厳密な全体SHA256検証を注入する。
    """
    from app.pipeline.transcribe._model_fetch import fetch_verified_model

    def _is_valid(p: Path) -> bool:
        return _sha256_file(p) == _MODEL_SHA256

    def _describe_failure(p: Path) -> str:
        # 診断の断片(実測ハッシュ値・期待値)のみを返す。URLや定型の前置き/末尾は
        # `fetch_verified_model`側が一元管理する(#56レビュー3巡目の指摘)。
        return f"sha256={_sha256_file(p)}, expected={_MODEL_SHA256}"

    return fetch_verified_model(
        _MODEL_URL, path, is_valid=_is_valid, describe_failure=_describe_failure, timeout=60.0
    )


def _window_audio(audio: np.ndarray, hop_size: int) -> list[np.ndarray]:
    """`audio`を長さ`AUDIO_N_SAMPLES`の窓に分割する。末尾窓はゼロ埋めする。"""
    windows = []
    for i in range(0, len(audio), hop_size):
        window = audio[i : i + AUDIO_N_SAMPLES]
        if len(window) < AUDIO_N_SAMPLES:
            window = np.pad(window, (0, AUDIO_N_SAMPLES - len(window)))
        windows.append(window)
    return windows


def _unwrap_output(
    batched: np.ndarray, original_length: int, n_overlapping_frames: int
) -> np.ndarray:
    """窓ごとの推論出力 `(n_windows, ANNOT_N_FRAMES, n_freqs)` を1つの行列へ結合する。

    窓の前後半分のオーバーラップフレームは(隣接窓と重複するため)捨ててから
    連結し、元音源の長さから決まる有効フレーム数へトリムする。
    """
    n_olap = n_overlapping_frames // 2
    trimmed = batched[:, n_olap:-n_olap, :] if n_olap > 0 else batched
    n_windows, n_frames_per_window, n_freqs = trimmed.shape
    unwrapped = trimmed.reshape(n_windows * n_frames_per_window, n_freqs)
    n_output_frames = int(np.floor(original_length * (ANNOTATIONS_FPS / AUDIO_SAMPLE_RATE)))
    return unwrapped[:n_output_frames, :]


def _run_onnx_inference(
    audio: np.ndarray, session: ort.InferenceSession
) -> tuple[np.ndarray, np.ndarray]:
    """音源全体を窓分割し、1回のバッチ推論でnote/onsetのposteriorgramを得る。"""
    overlap_len = N_OVERLAPPING_FRAMES * FFT_HOP
    hop_size = AUDIO_N_SAMPLES - overlap_len
    original_length = len(audio)
    padded = np.concatenate(
        [np.zeros(overlap_len // 2, dtype=np.float32), audio.astype(np.float32)]
    )
    windows = np.stack(_window_audio(padded, hop_size))[:, :, np.newaxis].astype(np.float32)

    frame_batch, onset_batch = session.run(
        [ONNX_OUTPUT_NOTE, ONNX_OUTPUT_ONSET], {ONNX_INPUT_NAME: windows}
    )

    frames = _unwrap_output(frame_batch, original_length, N_OVERLAPPING_FRAMES)
    onsets = _unwrap_output(onset_batch, original_length, N_OVERLAPPING_FRAMES)
    return frames, onsets


def _infer_onsets_from_frames(
    onsets: np.ndarray, frames: np.ndarray, n_diff: int = 2
) -> np.ndarray:
    """フレーム強度の急激な変化からonsetを補完する(basic-pitch由来)。

    上流実装は`frame_diff`が全て0の場合に0除算(NaN)が起こりうるが、本プロジェクトの
    方針(想定される縮退入力でも例外を出さない)に合わせ、0除算のみガードする
    (#56、bass.pyの短信号ガードと同種の防御)。
    """
    diffs = []
    for n in range(1, n_diff + 1):
        frames_appended = np.concatenate([np.zeros((n, frames.shape[1])), frames])
        diffs.append(frames_appended[n:, :] - frames_appended[:-n, :])
    frame_diff = np.min(diffs, axis=0)
    frame_diff[frame_diff < 0] = 0
    frame_diff[:n_diff, :] = 0
    max_diff = np.max(frame_diff)
    if max_diff > 0:
        frame_diff = np.max(onsets) * frame_diff / max_diff
    return np.max([onsets, frame_diff], axis=0)


def _output_to_notes_polyphonic(
    frames: np.ndarray,
    onsets: np.ndarray,
    *,
    onset_thresh: float,
    frame_thresh: float,
    min_note_len: int,
    energy_tol: int = ENERGY_TOLERANCE,
) -> list[tuple[int, int, int, float]]:
    """posteriorgram(frame/onset)からポリフォニックなノートイベント候補を抽出する。

    Spotify Basic Pitchの`output_to_notes_polyphonic`(`note_creation.py`,
    Apache-2.0)をmir_eval/pretty_midiに依存しないnumpy/scipyのみへ移植したもの
    (#56)。onsetのピーク検出を起点にエネルギー追跡でノート終端を決める本体処理に
    加え、「melodia trick」(明確なonsetを取り逃した残余エネルギーの塊からも
    追加でノートを拾う後処理、basic-pitch本家は常時有効)を含む。

    戻り値は `(start_frame, end_frame, midi, amplitude)` のタプル列
    (amplitudeは0〜1)。
    """
    n_frames = frames.shape[0]
    if n_frames == 0:
        return []

    # 実配列の列数から求める(モデル出力は88鍵=A0〜C8分の列を持つが、テスト等で
    # より小さい配列を渡した場合でも近傍列アクセスが範囲外にならないよう、
    # 固定定数ではなく実際のshapeを使う、#56レビュー指摘)。
    max_freq_idx = frames.shape[1] - 1

    onsets = _infer_onsets_from_frames(onsets, frames)

    peak_thresh_mat = np.zeros_like(onsets)
    peaks = scipy.signal.argrelmax(onsets, axis=0)
    peak_thresh_mat[peaks] = onsets[peaks]

    onset_idx = np.where(peak_thresh_mat >= onset_thresh)
    # 後方の時刻から走査する(basic-pitch由来: 後続のonsetから確定させていくことで
    # 前方のonsetがエネルギー追跡の対象範囲を正しく引き継げる)。
    onset_time_idx = onset_idx[0][::-1]
    onset_freq_idx = onset_idx[1][::-1]

    remaining_energy = frames.copy()

    note_events: list[tuple[int, int, int, float]] = []
    for note_start_idx, freq_idx in zip(onset_time_idx, onset_freq_idx, strict=True):
        if note_start_idx >= n_frames - 1:
            continue

        i = note_start_idx + 1
        k = 0
        while i < n_frames - 1 and k < energy_tol:
            if remaining_energy[i, freq_idx] < frame_thresh:
                k += 1
            else:
                k = 0
            i += 1
        i -= k

        if i - note_start_idx <= min_note_len:
            continue

        remaining_energy[note_start_idx:i, freq_idx] = 0
        if freq_idx < max_freq_idx:
            remaining_energy[note_start_idx:i, freq_idx + 1] = 0
        if freq_idx > 0:
            remaining_energy[note_start_idx:i, freq_idx - 1] = 0

        amplitude = float(np.mean(frames[note_start_idx:i, freq_idx]))
        note_events.append((int(note_start_idx), int(i), int(freq_idx) + MIDI_OFFSET, amplitude))

    energy_shape = remaining_energy.shape
    while np.max(remaining_energy) > frame_thresh:
        i_mid, freq_idx = np.unravel_index(np.argmax(remaining_energy), energy_shape)
        remaining_energy[i_mid, freq_idx] = 0

        i = i_mid + 1
        k = 0
        while i < n_frames - 1 and k < energy_tol:
            if remaining_energy[i, freq_idx] < frame_thresh:
                k += 1
            else:
                k = 0
            remaining_energy[i, freq_idx] = 0
            if freq_idx < max_freq_idx:
                remaining_energy[i, freq_idx + 1] = 0
            if freq_idx > 0:
                remaining_energy[i, freq_idx - 1] = 0
            i += 1
        i_end = i - 1 - k

        i = i_mid - 1
        k = 0
        while i > 0 and k < energy_tol:
            if remaining_energy[i, freq_idx] < frame_thresh:
                k += 1
            else:
                k = 0
            remaining_energy[i, freq_idx] = 0
            if freq_idx < max_freq_idx:
                remaining_energy[i, freq_idx + 1] = 0
            if freq_idx > 0:
                remaining_energy[i, freq_idx - 1] = 0
            i -= 1
        i_start = i + 1 + k

        if i_end - i_start <= min_note_len:
            continue

        amplitude = float(np.mean(frames[i_start:i_end, freq_idx]))
        note_events.append((int(i_start), int(i_end), int(freq_idx) + MIDI_OFFSET, amplitude))

    return note_events


def _model_frames_to_time(n_frames: int) -> np.ndarray:
    """モデル出力のフレームインデックス列を秒に変換する(basic-pitch由来)。

    ウィンドウ処理(推論を`AUDIO_N_SAMPLES`単位の窓に区切って行う)により、
    素朴な `frame_index * hop / sr` では窓境界ごとにズレが蓄積する。
    `MAGIC_ALIGNMENT_OFFSET`はbasic-pitch本家が実測で定めた補正定数。
    """
    import librosa

    original_times = librosa.frames_to_time(
        np.arange(n_frames), sr=AUDIO_SAMPLE_RATE, hop_length=FFT_HOP
    )
    window_numbers = np.floor(np.arange(n_frames) / ANNOT_N_FRAMES)
    window_offset = (FFT_HOP / AUDIO_SAMPLE_RATE) * (
        ANNOT_N_FRAMES - (AUDIO_N_SAMPLES / FFT_HOP)
    ) + MAGIC_ALIGNMENT_OFFSET
    return original_times - (window_offset * window_numbers)


def run_guitar_transcription(
    audio_path: Path, *, transcriber: GuitarTranscriber | None = None
) -> TranscriptionResult:
    """ギター/その他ステムからノートイベント列を得る(#56, §6 Stage 3)。

    `transcriber` を渡すことでテストでのモックが可能(piano/bass/vocalsの
    各`run_*_transcription`と同じ契約)。
    """
    import librosa

    audio, sr = librosa.load(str(audio_path), sr=AUDIO_SAMPLE_RATE, mono=True)

    if transcriber is not None:
        return transcriber(audio, sr)

    if len(audio) == 0:
        return TranscriptionResult(notes=[], pedals=[])

    import onnxruntime as ort

    model_path = ensure_onnx_model()
    session = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])

    frames, onsets = _run_onnx_inference(audio, session)
    note_events = _output_to_notes_polyphonic(
        frames,
        onsets,
        onset_thresh=ONSET_THRESHOLD,
        frame_thresh=FRAME_THRESHOLD,
        min_note_len=MIN_NOTE_LEN_FRAMES,
    )

    times_s = _model_frames_to_time(frames.shape[0])
    notes: list[NoteEvent] = []
    for start_idx, end_idx, midi_pitch, amplitude in note_events:
        onset_sec = float(times_s[start_idx])
        offset_sec = float(times_s[end_idx])
        duration_sec = max(offset_sec - onset_sec, MIN_DURATION_FLOOR_SEC)
        velocity = int(np.clip(round(127 * amplitude), 1, 127))
        ghost = is_ghost_candidate(duration_sec, velocity)
        notes.append(
            NoteEvent(
                onset_sec=onset_sec,
                duration_sec=duration_sec,
                midi=midi_pitch,
                velocity=velocity,
                ghost_candidate=ghost,
            )
        )

    # 決定論的な出力順にする(basic-pitch本家はonset走査順→melodia trick順の
    # まま返すため実行順に依存した並びになる、#56)。
    notes.sort(key=lambda n: (n.onset_sec, n.midi))
    return TranscriptionResult(notes=notes, pedals=[])
