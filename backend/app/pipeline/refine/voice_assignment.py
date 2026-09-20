"""発音区間ベースのvoice割当(#130/#109)。

「同一voice内で時間的に重なるノートを作らない」という記譜上の制約(MusicXMLの
voiceは常に単旋律)を満たすため、L0(`baseline.py`、量子化済みtick軸)とL1の
検証層(`l1_runner.py`の機械的修復、小節内beat軸)が同一のアルゴリズムを必要とする。
片方だけ修正されて両者が乖離するのを避けるため(#128レビュー指摘と同じ理由)、
時間単位に依存しない形でここへ一元化する。

`pipeline/`は外部依存(domain含む)を持たない方針(`beat.py`/`quantize.py`と
同様)であり、本モジュールも標準ライブラリのみに依存する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

# 声部数上限(設計書§7.4「パートあたり最大4声部」、検証層V-7と一致)。
MAX_VOICES: Final = 4


@dataclass(frozen=True)
class VoiceCandidate:
    """voice割当の対象となるノート1件。

    `onset`/`end`は「呼び出し側と一致していれば何でもよい」時間単位(ticks/beats)。
    同一呼び出し内で単位を混ぜてはならない。

    `end`が`None`の場合は音価不明を表し、時間方向の重なりは判定せず、`onset`が
    一致する同時発音のみを衝突として扱う。
    """

    key: int
    midi: int
    onset: float
    end: float | None


def _conflicts(last_onset: float, last_end: float | None, candidate: VoiceCandidate) -> bool:
    """同一voice内で、直前に割り当てたノートと`candidate`が記譜上衝突するか。

    - `onset`が一致する(和音・同時発音)。音価に関係なく別voiceが必要。
    - `candidate.onset`が直前ノートの発音区間内(開始より後、終了より前)に入る。

    直前ノートの音価が不明(`last_end is None`)な場合は時間方向の重なりを
    判定できないため、同時発音のみを衝突とする。
    """
    if last_onset == candidate.onset:
        return True
    if last_end is None:
        return False
    return candidate.onset < last_end


def assign_voices(
    candidates: Sequence[VoiceCandidate],
    *,
    preferred: Mapping[int, int] | None = None,
    max_voices: int = MAX_VOICES,
) -> tuple[dict[int, int], frozenset[int]]:
    """ノート群へvoice 1..`max_voices`を割り当てる(区間スケジューリングの貪欲解)。

    開始位置順(同一開始位置では高音順、同音高では`key`順)にノートを見て、
    **直前ノートと衝突しない最小番号のvoice**へ割り当てる。これにより、開始
    タイミングが異なっても持続時間が重なる音(アルペジオ、サステインしたまま
    次の音が鳴るケース等)は別voiceへ回され、同一voice内の時間重複が生じない。

    同一開始位置の同時発音が高音から順にvoice 1, 2, 3, 4へ割り当てられる従来の
    挙動(#26/#56)は、同時発音同士が必ず衝突するため自然に保たれる。

    `preferred`(#109): ノートごとの「望ましいvoice」。指定されたvoiceが衝突
    しない限りそのvoiceを維持し、衝突する場合のみ別voiceへ回す(L1の検証層が
    AIのvoice選択を最小限だけ修正するために使う)。`None`(L0)の場合は常に
    最小番号の空きvoiceを使う。

    Returns:
        `(key -> voice)`のマッピングと、**飽和したノートキー**の集合。飽和とは
        `max_voices`すべてが衝突していて、どのvoiceへ回しても同一voice内の
        重複が残る状態を指す。設計書§7.4の4声上限と検証層V-7(`1 <= voice <= 4`)
        を守る限り、5音以上の同時発音に対して重複の無い解は存在しない
        (鳩の巣原理)ため、この場合は**最も早く終わるvoice**へ回して重なり幅を
        最小化し、飽和したことを呼び出し元へ返す(呼び出し元が可視化・記録する)。
    """
    ordered = sorted(candidates, key=lambda c: (c.onset, -c.midi, c.key))
    last_onset: dict[int, float] = {}
    voice_end: dict[int, float | None] = {}
    result: dict[int, int] = {}
    saturated: set[int] = set()
    for candidate in ordered:
        preferred_voice = None if preferred is None else preferred.get(candidate.key)
        preferred_is_usable = (
            preferred_voice is not None
            and 1 <= preferred_voice <= max_voices
            and (
                preferred_voice not in last_onset
                or not _conflicts(
                    last_onset[preferred_voice], voice_end[preferred_voice], candidate
                )
            )
        )
        if preferred_is_usable:
            voice = preferred_voice
        else:
            free_voices = [
                voice
                for voice in range(1, max_voices + 1)
                if voice not in last_onset
                or not _conflicts(last_onset[voice], voice_end[voice], candidate)
            ]
            if free_voices:
                voice = free_voices[0]
            else:
                voice = min(
                    range(1, max_voices + 1),
                    key=lambda v: (
                        voice_end[v] if voice_end[v] is not None else candidate.onset,
                        v,
                    ),
                )
                saturated.add(candidate.key)
        assert voice is not None  # preferred_is_usable が真なら値が入っている
        last_onset[voice] = candidate.onset
        previous_end = voice_end.get(voice)
        # 音価不明のノートは「開始位置で終わる」ものとして扱い、後続ノートとの
        # 時間重複判定に持ち越さない(同時発音の判定は開始位置の一致で別途行う)。
        new_end = candidate.end if candidate.end is not None else candidate.onset
        voice_end[voice] = new_end if previous_end is None else max(previous_end, new_end)
        result[candidate.key] = voice
    return result, frozenset(saturated)
