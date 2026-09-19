"""Stage 3: AMT共通の定数・データ構造・ユーティリティ(#54, §6 Stage 3)。

ピアノ・ベースなど各楽器AMTパイプラインで共有される契約（ノート表現、
ゴーストノート判定閾値、最小ノート長保護など）を一元管理する。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 共通後処理のしきい値(§6 Stage 3, #24, #54)。
# design §7.4 のL0 ghost判定(confidence<0.35 かつ duration<60ms かつ velocity<25、
# 3条件すべてのAND)とは別に、Stage 3自身はduration/velocityそれぞれ単独の条件
# (いずれか一方を満たせばフラグを立てる、より広く網をかける粗いフィルタ)として位置づける。
GHOST_MIN_DURATION_SEC = 0.06
GHOST_MIN_VELOCITY = 25

# 生成物にゼロ長ノートを書き込むと `domain.score.Note`(duration_sec>0)の
# 不変条件に違反するため、モデルやオンセット検出が極端な(onset==offset等の)
# 出力をした場合の最終防波堤として使う最小値(1ms)。
MIN_DURATION_FLOOR_SEC = 0.001


def is_ghost_candidate(duration_sec: float, velocity: int) -> bool:
    """ノートがゴーストノート候補(極端に短いまたは弱い)であるかを判定する。

    デュレーションが60ms未満、またはベロシティが25未満のいずれかを満たす場合にTrue。
    """
    return duration_sec < GHOST_MIN_DURATION_SEC or velocity < GHOST_MIN_VELOCITY


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
    pedals: list[PedalEvent] = field(default_factory=list)
