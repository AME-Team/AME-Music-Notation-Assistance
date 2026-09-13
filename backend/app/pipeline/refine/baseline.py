"""L0: 決定論的整音(#26, quantizeステージ末尾で実行, §7.4)。

異名同音表記(調号 + 直前ノートからの半音進行)、五線・声部の割り当て、
ghostフラグの付与を行う純粋関数群。AIは使わない(M2の完了条件: L0のみで
完走できること)。

`pipeline/` は外部依存(domain含む)を持たない方針(`beat.py`/`quantize.py`と
同様)だが、本モジュールは意図的な例外として `domain.pitch.midi_to_spelling`
を直接呼ぶ(#26: そちらはinfra/api非依存の純粋計算関数のみを提供する
データ変換ユーティリティであり、ここで再実装するとロジックの二重管理になり
両者が食い違うリスクが生じるため、意図的な例外として許容する)。`domain.score`
の重いモデルは使わず、戻り値は平データ(タプル)に落として公開する。

同様に `estimate_key_fifths()` は `music21`(外部ライブラリ)を関数内で
遅延importして調推定に使う(#26-M2レビュー指摘: 上記の「唯一の例外」という
記述は不正確だったため訂正。music21は決定論的な統計アルゴリズムのみを提供し、
AIではないため許容する)。

`provenance == "user"`(手動編集)のノートは一切変更しない(#29「手動編集が
下流無効化で失われないこと」)。呼び出し元(`worker/dsp_main.py`)が
`refine_baseline()` の戻り値をScore IRへマッピングする。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.pitch import midi_to_spelling

MIDDLE_C = 60  # 大譜表(ト音部/へ音部)の分割基準(§7.4)
GHOST_MAX_CONFIDENCE = 0.35
GHOST_MAX_DURATION_SEC = 0.06
GHOST_MAX_VELOCITY = 25
MAX_VOICES = 4


@dataclass(frozen=True)
class RefineNoteInput:
    """L0への入力ノート1件分(#26)。`is_user=True` のノートは、L0が返す

    差分に一切含まれない(呼び出し元は変更しない)。
    """

    id: int
    midi: int
    onset_tick: int
    duration_sec: float
    velocity: int
    confidence: float
    flags: tuple[str, ...] = ()
    is_user: bool = False


@dataclass(frozen=True)
class RefinedNote:
    """L0がノート1件に対して提案する変更内容(#26)。"""

    spelling: tuple[str, int, int]  # (step, alter, octave)
    voice: int
    staff: int
    flags: tuple[str, ...]


def estimate_key_fifths(pitch_classes: list[int]) -> int:
    """ピッチクラス分布から調号(fifths, -7〜7)を推定する(#26)。

    music21のKrumhansl-Schmuckler調推定(`Stream.analyze('key')`)を使う。
    これは固定の統計アルゴリズムであり、AIではない(M2の完了条件に抵触しない)。
    ノートが1件も無ければ既定でハ長調(0)を返す。

    実体は`key_estimation.estimate_key`に委譲する(#38: L1チャンク入力の
    `key_estimate`/`key_confidence`もこれを再利用する必要が生じたため、
    music21呼び出しを共有モジュールへ集約した。L0とL1が異なる調推定結果に
    基づいてしまう食い違いを避けるための統合)。
    """
    from app.pipeline.refine.key_estimation import estimate_key

    return estimate_key(pitch_classes).fifths


def _assign_spellings(
    notes: list[RefineNoteInput], *, fifths: int
) -> dict[int, tuple[str, int, int]]:
    """onset_tick順に処理し、直前の非userノートのMIDIから半音進行を判定する(#26)。

    userノートも「直前ノート」の履歴には含める(userノートを跨いでも半音進行の
    連続性を保つため)が、userノート自身の表記は変更しない。

    **既知の制約(#26-M2レビュー指摘)**: `prev_midi`は声部・staffを区別せず
    全ノートを単一の系列として追跡する。ピアノは両手・複数声部が同時進行する
    ため、メロディの半音進行の直前に別声部(例: 同時発音コードの他の音や
    バス)のノートが挟まると、意図した上行/下行の判定が乱れ調号既定の
    シャープ/フラットへ倒れることがある。単旋律であれば問題なく機能するが、
    声部単位でこの履歴を分離する改善は将来の課題とする。
    """
    ordered = sorted(notes, key=lambda n: (n.onset_tick, n.id))
    prev_midi: int | None = None
    result: dict[int, tuple[str, int, int]] = {}
    for note in ordered:
        if note.is_user:
            prev_midi = note.midi
            continue
        spelling = midi_to_spelling(note.midi, fifths=fifths, prev_midi=prev_midi)
        result[note.id] = (spelling.step, spelling.alter, spelling.octave)
        prev_midi = note.midi
    return result


def _assign_staff_and_voice(notes: list[RefineNoteInput]) -> dict[int, tuple[int, int]]:
    """同一onset_tickのノートをグループ化し、MIDI60でstaffを分け(§7.4)、staff内で

    高音から低音の順にvoiceを割る(#26)。userノートも「そのタイミングに音がある」
    という事実としてグループには含める(=voice番号を1つ消費させる)が、userノート
    自身のstaff/voiceは変更しない。

    既知の制約: 同一staff・同一onset_tickに5音以上同時発音がある場合、5番目以降は
    全てvoice=4に潰れる(MusicXMLの慣例的な声部数上限に合わせた単純化)。
    """
    groups: dict[int, list[RefineNoteInput]] = {}
    for note in notes:
        groups.setdefault(note.onset_tick, []).append(note)

    result: dict[int, tuple[int, int]] = {}
    for group in groups.values():
        by_staff: dict[int, list[RefineNoteInput]] = {1: [], 2: []}
        for note in group:
            staff = 1 if note.midi >= MIDDLE_C else 2
            by_staff[staff].append(note)
        for staff, staff_notes in by_staff.items():
            ranked = sorted(staff_notes, key=lambda n: -n.midi)
            for voice_index, note in enumerate(ranked, start=1):
                if note.is_user:
                    continue
                result[note.id] = (staff, min(voice_index, MAX_VOICES))
    return result


def _flag_ghost_notes(notes: list[RefineNoteInput]) -> dict[int, tuple[str, ...]]:
    """confidence/duration/velocityの3条件すべてを満たすノートに

    `"ghost_candidate"` を付与する(削除はしない、§7.4)。既に付いていれば
    そのまま(重複追加しない)。
    """
    result: dict[int, tuple[str, ...]] = {}
    for note in notes:
        if note.is_user:
            continue
        if "ghost_candidate" in note.flags:
            result[note.id] = note.flags
            continue
        is_ghost = (
            note.confidence < GHOST_MAX_CONFIDENCE
            and note.duration_sec < GHOST_MAX_DURATION_SEC
            and note.velocity < GHOST_MAX_VELOCITY
        )
        result[note.id] = (*note.flags, "ghost_candidate") if is_ghost else note.flags
    return result


def refine_baseline(notes: list[RefineNoteInput]) -> dict[int, RefinedNote]:
    """L0本体(#26)。異名同音・staff/voice・ghostフラグをまとめて適用する。

    戻り値は `note_id -> RefinedNote` のマッピングで、`is_user=True` の
    ノートのIDは含まれない(呼び出し元は変更しない)。
    """
    fifths = estimate_key_fifths([n.midi % 12 for n in notes if not n.is_user])
    spellings = _assign_spellings(notes, fifths=fifths)
    staff_voice = _assign_staff_and_voice(notes)
    ghost_flags = _flag_ghost_notes(notes)

    result: dict[int, RefinedNote] = {}
    for note in notes:
        if note.is_user:
            continue
        staff, voice = staff_voice[note.id]
        result[note.id] = RefinedNote(
            spelling=spellings[note.id],
            voice=voice,
            staff=staff,
            flags=ghost_flags[note.id],
        )
    return result
