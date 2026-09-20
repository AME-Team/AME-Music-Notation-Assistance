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
# 4声上限(設計書§7.4/検証層V-7)に達し、同一voice内の時間重複を避けられなかった
# ノートに付与するフラグ(#130/#134)。重複が構造上不可避であることを無言にせず、
# 下流(L1のチャンク入力、L2の`score_query`の`flags_contains`、将来のUI表示)が
# 判別できるようにする。
VOICE_SATURATION_FLAG = "voice_saturated"


@dataclass(frozen=True)
class RefineNoteInput:
    """L0への入力ノート1件分(#26)。`is_user=True` のノートは、L0が返す

    差分に一切含まれない(呼び出し元は変更しない)。

    `duration_tick`(#130): `onset_tick`と同じ量子化格子単位の音価。区間ベースの
    voice割当(`_assign_voices_in_interval_order`)が「時間的に重なるか」を判定する
    ために使う。`duration_sec`(生の秒音価)では tick 軸上の重なりを判定できない
    (tick と秒の相互変換にはテンポマップが必要で、この純粋関数モジュールは
    それを知らない)ため、量子化済みの音価を別途受け取る。`None`(未量子化)の
    場合は音価不明として扱い、時間方向の重なりは判定せず同一`onset_tick`のみを
    衝突とみなす(量子化前の呼び出しでも決定的に動作させるため)。
    """

    id: int
    midi: int
    onset_tick: int
    duration_sec: float
    velocity: int
    confidence: float
    duration_tick: int | None = None
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


def _note_end_tick(note: RefineNoteInput) -> int | None:
    """ノートの発音終了tick(排他的終端)。音価不明(`duration_tick is None`)ならNone。

    `duration_tick=0`(ゼロ長)は「開始と同じ位置で終わる」= 時間方向の重なりを
    持たない音として扱う(同時発音の判定は`_conflicts_in_voice`が`onset_tick`の
    一致で別途行う)。
    """
    if note.duration_tick is None:
        return None
    return note.onset_tick + max(note.duration_tick, 0)


def _conflicts_in_voice(last_onset: int, last_end: int | None, note: RefineNoteInput) -> bool:
    """同一voice内で、直前に割り当てたノートと`note`が記譜上衝突するか(#130)。

    MusicXMLのvoiceは「常に単旋律」であるべき(同一voice内で時間的に重なる
    ノートは不正)ため、以下のいずれかで衝突とみなす:

    - `onset_tick`が一致する(和音・同時発音)。音価に関係なく別voiceが必要。
    - `note.onset_tick`が直前ノートの発音区間内(開始より後、終了より前)に入る。

    直前ノートの音価が不明(`last_end is None`)な場合は時間方向の重なりを
    判定できないため、同時発音のみを衝突とする。
    """
    if last_onset == note.onset_tick:
        return True
    if last_end is None:
        return False
    return note.onset_tick < last_end


def _assign_voices_in_interval_order(
    notes: list[RefineNoteInput], *, staff: int
) -> tuple[dict[int, tuple[int, int]], frozenset[int]]:
    """区間(interval)ベースのvoice割当(#130)。

    `_assign_staff_and_voice`(ピアノの大譜表、staffごとに呼ぶ)と
    `_assign_voice_single_staff`(単一譜表、staff=1固定で全体に対して呼ぶ)が
    共有する中核ロジック(#128レビュー指摘の重複実装回避を引き継ぎつつ、
    #130でグループ化の単位を「同一onset_tick」から「発音区間の重なり」へ
    一般化した)。

    各voiceについて「直前に割り当てたノートの開始tickと終了tick」を保持し、
    開始tick順(同一開始tickでは高音順)にノートを見て、**直前ノートと衝突しない
    最小番号のvoice**(1..`MAX_VOICES`)へ割り当てる。これにより、開始タイミングが
    異なっても持続時間が重なる音(アルペジオ、サステインしたまま次の音が鳴る
    ケース等)は別voiceへ回され、同一voice内の時間重複(V-8違反)が生じない。

    同一onset_tickの同時発音が高音から順にvoice 1, 2, 3, 4へ割り当てられる
    従来の挙動(#26/#56)は、同一開始tickのノートを高音順に処理し、各ノートが
    直前のノートと衝突するため自然に保たれる。

    Returns:
        `(staff, voice)`のマッピングと、**飽和したノートID**の集合。飽和とは
        4声すべてが衝突していて、どのvoiceへ回しても同一voice内の重複が残る
        状態を指す(#134で設計判断を起票済み)。飽和ノートは
        `VOICE_SATURATION_FLAG`で可視化される(`refine_baseline`参照)。

    userノートも「そのタイミングで音が鳴っている」という事実として処理に含める
    (=voice番号を1つ消費させる、#26から継続する挙動)が、userノート自身は戻り値に
    含めない(呼び出し元が変更しない)。
    """
    ordered = sorted(notes, key=lambda n: (n.onset_tick, -n.midi, n.id))
    last_onset: dict[int, int] = {}
    voice_end: dict[int, int | None] = {}
    result: dict[int, tuple[int, int]] = {}
    saturated: set[int] = set()
    for note in ordered:
        end_tick = _note_end_tick(note)
        free_voices = [
            voice
            for voice in range(1, MAX_VOICES + 1)
            if voice not in last_onset
            or not _conflicts_in_voice(last_onset[voice], voice_end[voice], note)
        ]
        if free_voices:
            voice = free_voices[0]
        else:
            # 4声すべてが衝突している(※)。設計書§7.4の「パートあたり最大4声部」と
            # 検証層V-7(`1 <= voice <= 4`)を守る限り、5音以上の同時発音を含む
            # 入力に対して重複の無い解は存在しない(鳩の巣原理)。重複を消すには
            # 声部数上限の緩和(設計変更)かノートの削除が必要で、いずれも
            # ここでは選べないため、重なり幅が最小になるvoiceへ回して
            # `voice_saturated`フラグで可視化するに留める(#134)。
            #
            # ※「すべて衝突」= 直前ノートの終了位置が本ノートの開始位置より
            #   後か、開始位置が同一(同時発音)であるvoiceしか無い状態。
            voice = min(
                range(1, MAX_VOICES + 1),
                key=lambda v: (
                    voice_end[v] if voice_end[v] is not None else note.onset_tick,
                    v,
                ),
            )
            saturated.add(note.id)
        last_onset[voice] = note.onset_tick
        previous_end = voice_end.get(voice)
        # 音価不明のノートは「開始位置で終わる」ものとして扱い、後続ノートとの
        # 時間重複判定に持ち越さない(同時発音の判定は開始tickの一致で別途行う)。
        new_end = end_tick if end_tick is not None else note.onset_tick
        voice_end[voice] = new_end if previous_end is None else max(previous_end, new_end)
        if not note.is_user:
            result[note.id] = (staff, voice)
    return result, frozenset(saturated)


def _assign_staff_and_voice(
    notes: list[RefineNoteInput],
) -> tuple[dict[int, tuple[int, int]], frozenset[int]]:
    """MIDI60でstaffを分け(§7.4)、staff内で発音区間の重なりに応じてvoiceを割る

    (#26/#130)。大譜表(ト音部/へ音部の2段)を持つピアノパート専用
    (#56レビュー指摘: 単一譜表パートには`_assign_voice_single_staff`を使う。
    MIDI60の分割をピアノ以外に適用すると、その楽器が実際には持たないstaff=2を
    参照してしまう)。

    voice番号はstaffごとに独立して1から振る(L0の契約。#56で単一譜表パートにも
    同じ規則を適用した)。戻り値の第2要素は飽和ノートIDの集合(#134)。
    """
    by_staff: dict[int, list[RefineNoteInput]] = {1: [], 2: []}
    for note in notes:
        by_staff[1 if note.midi >= MIDDLE_C else 2].append(note)

    result: dict[int, tuple[int, int]] = {}
    saturated: set[int] = set()
    for staff, staff_notes in by_staff.items():
        if staff_notes:
            staff_voices, staff_saturated = _assign_voices_in_interval_order(
                staff_notes, staff=staff
            )
            result.update(staff_voices)
            saturated |= staff_saturated
    return result, frozenset(saturated)


def _assign_voice_single_staff(
    notes: list[RefineNoteInput],
) -> tuple[dict[int, tuple[int, int]], frozenset[int]]:
    """単一譜表パート(bass/vocals/guitar/other)向けのvoice割当(#56/#130)。

    `_assign_staff_and_voice`のMIDDLE_C分割によるstaff振り分けは行わず、
    常に`staff=1`固定で、発音区間の重なりに応じてvoice 1..`MAX_VOICES`へ
    割り振る。bass/vocalsはモノフォニックなため通常voice=1のみを使うが、
    guitar/otherは和音を弾きうるため、これが無いと同一onset_tickの複数ノートが
    全てvoice=1へ潰れ、MusicXML上不正な重複ノートになりうる
    (#56 Gate2レビュー指摘への対応)。戻り値の第2要素は飽和ノートIDの集合(#134)。
    """
    return _assign_voices_in_interval_order(notes, staff=1)


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


def refine_baseline(
    notes: list[RefineNoteInput],
    *,
    fifths: int | None = None,
    single_staff: bool = False,
) -> dict[int, RefinedNote]:
    """L0本体(#26)。異名同音・staff/voice・ghostフラグをまとめて適用する。

    戻り値は `note_id -> RefinedNote` のマッピングで、`is_user=True` の
    ノートのIDは含まれない(呼び出し元は変更しない)。

    `fifths`(#56): 省略時は`notes`自身から調号を推定する(従来どおりの単一パート
    呼び出し)。複数パートを個別に呼び出す場合、呼び出し元(`worker/dsp_main.py`)が
    スコア全体のノートから一度だけ推定した値を全パート共通で渡すことで、
    パートごとに異なる調号推定結果になる食い違いを避ける。

    `single_staff`(#56): Trueの場合、大譜表分割(`_assign_staff_and_voice`の
    MIDDLE_C基準staff振り分け)ではなく`_assign_voice_single_staff`(常にstaff=1、
    発音区間の重なりに応じてvoiceを割当)を使う。bass/vocals/guitar/other等、
    実際に単一譜表しか持たないパートに使う(ピアノ以外でMIDDLE_C分割を使うと
    存在しないstaff=2を参照してしまうため)。

    voiceの割当はいずれの経路も発音区間ベース(`_assign_voices_in_interval_order`、
    #130)であり、`RefineNoteInput.duration_tick`を渡すことで「開始が異なるが
    持続時間が重なる音」も別voiceへ回される。渡さない場合(`None`)は音価不明として
    同一onset_tickのみを衝突とみなす従来相当の挙動になる。

    4声上限に達して重複を避けられなかったノートには`VOICE_SATURATION_FLAG`を
    付与する(#134で設計判断を起票済み)。ghostフラグと併存しうるため、付与は
    `_flag_ghost_notes`の結果に対する追加として行う。
    """
    if fifths is None:
        fifths = estimate_key_fifths([n.midi % 12 for n in notes if not n.is_user])
    spellings = _assign_spellings(notes, fifths=fifths)
    staff_voice, saturated_ids = (
        _assign_voice_single_staff(notes) if single_staff else _assign_staff_and_voice(notes)
    )
    ghost_flags = _flag_ghost_notes(notes)

    result: dict[int, RefinedNote] = {}
    for note in notes:
        if note.is_user:
            continue
        staff, voice = staff_voice[note.id]
        flags = ghost_flags[note.id]
        if note.id in saturated_ids and VOICE_SATURATION_FLAG not in flags:
            flags = (*flags, VOICE_SATURATION_FLAG)
        result[note.id] = RefinedNote(
            spelling=spellings[note.id],
            voice=voice,
            staff=staff,
            flags=flags,
        )
    return result
