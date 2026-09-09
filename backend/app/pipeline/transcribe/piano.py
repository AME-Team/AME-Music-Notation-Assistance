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

_CHECKPOINT_URL = (
    "https://zenodo.org/record/4034264/files/CRNN_note_F1%3D0.9677_pedal_F1%3D0.9186.pth?download=1"
)
# ライブラリの既定パス(`PianoTranscription.__init__` が `checkpoint_path` 未指定時に
# 使う既定値)に合わせる。ここへ事前配置しておくことで、ライブラリ内部の
# `os.path.getsize(checkpoint_path) >= 1.6e8` チェックが真になり、Windowsに存在しない
# `os.system("wget ...")` 呼び出し自体をスキップさせる(#22)。
DEFAULT_CHECKPOINT_PATH = (
    Path.home() / "piano_transcription_inference_data" / "note_F1=0.9677_pedal_F1=0.9186.pth"
)
# ライブラリの `os.path.getsize(checkpoint_path) < 1.6e8` と同じ閾値(#22)。
MIN_CHECKPOINT_SIZE_BYTES = int(1.6e8)


def ensure_checkpoint(path: Path = DEFAULT_CHECKPOINT_PATH) -> Path:
    """チェックポイント(~165MB)を`httpx`で取得する(#24-M2レビュー指摘)。

    `backend/scripts/verify_piano_transcription.py`(#22)ではこの事前ダウンロードを
    行っていたが、本番経路である`run_piano_transcription()`自体には組み込まれて
    いなかった。ライブラリが自前で行う`os.system("wget ...")`はWindowsに`wget`が
    無く失敗する(#22で発見、Windows CIの実行で再現・確認)ため、実推論を行う
    すべての経路でこの事前ダウンロードを通す必要がある。公開関数として置くのは、
    このステージ(`run_piano_transcription`)と検証スクリプトが共有する契約の
    中核であることを名前で明示するため(#24-M2レビュー指摘)。

    通信が途中で切れて短いファイルのまま`path`に置かれると、次回起動時に
    ライブラリ自身の`os.path.getsize(checkpoint_path) >= 1.6e8`チェックが偽になり、
    Windowsに存在しない`wget`呼び出しへ進んでしまう。ダウンロード完了後にサイズを
    検証してから`path`へ置き換える。

    プロセス間ロック(#24-M2レビュー2巡目の指摘): 複数プロセス(例: 並行ジョブ)が
    初回ダウンロードに同時に入ると、ロック無しでは~165MBを重複してダウンロード
    してしまう。`filelock`でダウンロード区間を直列化し、ロック取得後にもう一度
    存在チェックすることで、待っていた他プロセスは再ダウンロードせずそのまま
    再利用できるようにする。ロック保持中は自分以外がダウンロード中であることは
    あり得ないため、残存する`.part`(前回いずれかのプロセスがSIGKILL等で
    クラッシュした際の孤立ファイル)もここで安全に掃除できる。
    """
    if path.exists() and path.stat().st_size >= MIN_CHECKPOINT_SIZE_BYTES:
        return path

    import os

    import httpx
    from filelock import FileLock

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f"{path.name}.lock"
    with FileLock(str(lock_path)):
        # ロック取得を待っている間に、先行プロセスがダウンロードを完了させている
        # 場合がある。その場合は自分は何もダウンロードせず再利用する。
        if path.exists() and path.stat().st_size >= MIN_CHECKPOINT_SIZE_BYTES:
            return path

        for stale_part in path.parent.glob(f"{path.name}.*.part"):
            stale_part.unlink(missing_ok=True)

        tmp_path = path.parent / f"{path.name}.{os.getpid()}.part"
        try:
            with httpx.stream("GET", _CHECKPOINT_URL, follow_redirects=True, timeout=300.0) as resp:
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
    return path


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

        # チェックポイントを明示的に渡さない場合、ライブラリはこのパス文字列を既定値
        # として使う。事前にここへ配置しておくことで、Windowsに無いwgetへの
        # 再ダウンロード呼び出し自体をライブラリ内部でスキップさせる(#24-M2レビュー)。
        # `checkpoint_path`を明示的に渡す(#24-M2レビュー2巡目の指摘): 渡さないと
        # 配置先(DEFAULT_CHECKPOINT_PATH)と読込先(ライブラリ内部の既定パス文字列)が
        # 暗黙に同一であることに依存してしまい、ライブラリ更新で既定パスが変わると
        # サイレントに再びwgetフォールバックへ落ちる(Windowsでは即座に失敗する)。
        checkpoint_path = ensure_checkpoint()
        model = PianoTranscription(checkpoint_path=str(checkpoint_path), device="cpu")

        def _transcribe(loaded_audio: np.ndarray) -> dict[str, Any]:
            return model.transcribe(loaded_audio, midi_path=None)

        transcriber = _transcribe

    raw = transcriber(audio)
    return _build_result(raw)
