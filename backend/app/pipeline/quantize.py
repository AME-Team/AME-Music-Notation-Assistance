"""Stage 4: 決定論的クオンタイズ(#25, §6 Stage 4, §7.1)。

拍グリッドへのスナップは数値処理であり、AIではなくここで確定させる。AIは
ここで生成された候補(`snap_candidates`)から選ぶだけであり、タイミング数値を
生成することはない。

`pipeline/` は外部依存(domain含む)を持たない方針(`beat.py`/`separate.py`と同様)
のため、入力はdomainモデルではなく `beatmap.json` の生の辞書形状(`beats`/
`time_signatures`)と、ノートを表す単純なタプル列で受け取る。呼び出し元
(`worker/dsp_main.py`)がScore IRのフィールドへ結果をマッピングする。
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass

from app.pipeline.time_signature import time_signature_at_bar

# MusicXMLの `<divisions>`(四分音符あたりのtick数)の既定値(#27)。
DEFAULT_DIVISIONS = 480

# 生成するスナップ候補の分解能。キーは四分音符を何等分するか
# (design doc「1/4, 1/8, 1/8T, 1/16, 1/16T, 1/32」に対応)。
_GRID_DIVISOR_BY_RESOLUTION: dict[str, int] = {
    "1/4": 1,
    "1/8": 2,
    "1/8T": 3,
    "1/16": 4,
    "1/16T": 6,
    "1/32": 8,
}

# 拍上の重み(§7.1「強拍ほど良い」)の判定に使う、四分音符の細分レベル。
# 昇順(粗い=強い拍から)に並べ、tickがそのレベルの格子に厳密に乗っていれば
# そのレベルの重みを採用する(最初に一致したレベル、すなわち最も粗い=最も強い
# 拍の重みを使う)。
_METRICAL_LEVELS = (1, 2, 3, 4, 6, 8, 12, 16, 24, 32)
_METRICAL_LEVEL_TOLERANCE = 1e-6

DEFAULT_TOP_N = 3

# Swing検出(§6 Stage4): オフビート8分音符の位置([0,1)で0.5相当)からの
# ずれを見る。ウィンドウ外(裏拍らしくない位置)のオンセットは対象外にする。
_OFFBEAT_WINDOW = (0.3, 0.7)
_MIN_SAMPLES_FOR_SWING_DETECTION = 6
_SWING_DETECTION_THRESHOLD = 0.05


@dataclass(frozen=True)
class SnapCandidateResult:
    id: str
    resolution: str
    tick: int
    score: float


@dataclass(frozen=True)
class QuantizedNote:
    note_id: int
    onset_tick: int
    duration_tick: int
    snap_candidates: list[SnapCandidateResult]
    selected_snap: str


def _denominator_at_bar(time_signatures: list[dict], bar: int) -> int:
    """`bar`時点で有効な拍子の分母(順方向に補完)。指定が無ければ4/4相当。

    `pipeline/time_signature.py`の`time_signature_at_bar`の薄いラッパ
    (#27-M2レビュー指摘: `pipeline/export/score_builder.py`と同じ補完
    ロジックが重複していたため、共有ヘルパーへ集約した)。
    """
    return time_signature_at_bar(time_signatures, bar)[1]


def _beat_tick_anchors(
    beats: list[dict], time_signatures: list[dict], divisions: int
) -> list[tuple[float, float]]:
    """`(time_sec, tick)` のペア列を、小節構造に属するビート(bar>0)に対して作る。

    先頭の(bar>0の)ビートをtick=0とし、各ビートの拍子分母から求めた
    「そのビート1つ分のtick数」を順に積算していく。分母はビート単位で変わり
    うる(拍子変化)ため、ビートごとに`_denominator_at_bar`で都度引く。

    **契約(呼び出し元 `pipeline/beat.py`/`pipeline/time_signature.py` との暗黙の
    取り決め、#25-M2レビュー指摘)**: `beats` の各エントリは「その小節の拍子の
    分母が表す音符1個分」(4/4なら四分音符、6/8なら8分音符)に対応する、という
    前提で tick を積算する。実測ビート時刻はテンポ変動により厳密な等間隔には
    ならない(線形補間はそのためにある)が、「1エントリ=1分母音符」という
    *拍単位の粒度*自体はビート推定・拍子推定側(`time_signature.py` の
    `_BEATS_PER_BAR_TO_SIGNATURE` 等)が保証するものとし、本関数ではそれを
    検証しない。将来ビート推定の出力粒度(例: 指揮者レベルの拍のみ返す等)が
    変わる場合は、この関数ではなく上流かこの契約自体を見直すこと。
    """
    anchors: list[tuple[float, float]] = []
    tick = 0.0
    for beat in beats:
        if beat["bar"] <= 0:
            continue
        anchors.append((beat["time_sec"], tick))
        denominator = _denominator_at_bar(time_signatures, beat["bar"])
        tick += divisions * 4 / denominator
    return anchors


def _seconds_to_raw_tick(onset_sec: float, anchors: list[tuple[float, float]]) -> float:
    """ビートアンカー列からの線形補間(区間外は端の区間の傾きで外挿)で、

    秒をtick(丸める前の連続値)に変換する。
    """
    if not anchors:
        return 0.0
    if len(anchors) == 1:
        return anchors[0][1]

    times = [a[0] for a in anchors]
    if onset_sec <= times[0]:
        (t0, k0), (t1, k1) = anchors[0], anchors[1]
    elif onset_sec >= times[-1]:
        (t0, k0), (t1, k1) = anchors[-2], anchors[-1]
    else:
        idx = bisect.bisect_right(times, onset_sec) - 1
        idx = max(0, min(idx, len(anchors) - 2))
        (t0, k0), (t1, k1) = anchors[idx], anchors[idx + 1]

    if t1 == t0:
        return k0
    fraction = (onset_sec - t0) / (t1 - t0)
    return k0 + fraction * (k1 - k0)


def _metrical_weight(tick: float, divisions: int) -> float:
    """tickが乗っている最も粗い(=最も強い)拍格子の重み(1/level)を返す。

    どの格子にも(許容誤差内で)乗っていなければ0.0。
    """
    for level in _METRICAL_LEVELS:
        unit = divisions / level
        remainder = tick % unit
        if remainder < _METRICAL_LEVEL_TOLERANCE or (unit - remainder) < _METRICAL_LEVEL_TOLERANCE:
            return 1.0 / level
    return 0.0


# 拍上の重み(タイブレークにのみ使う)が誤差の大小関係を逆転させないよう、
# 誤差(tick単位、丸め後)の桁と比べて無視できるほど小さいスケールに留める
# (#25-M2レビューで、weightが支配的になり細かい格子(1/8/1/8T/1/16…)が
# 誤差0でも粗い1/4格子に常に負ける採点式バグを修正)。
_WEIGHT_TIEBREAK_SCALE = 1e-6
_ERROR_ROUNDING_NDIGITS = 6


def _snap_candidates_for_tick(
    raw_tick: float,
    divisions: int,
    *,
    swing_ratio: float | None,
    top_n: int,
) -> list[SnapCandidateResult]:
    """1ノート分のスナップ候補を生成・採点し、上位`top_n`件を返す(#25)。

    最優先は「元位置からの誤差の小ささ」であり、拍上の重み(強拍ほど高い)は
    誤差が(丸め誤差の範囲で)実質同点の候補どうしを比べるときのタイブレーク
    としてのみ働く(§7.1)。誤差そのものを主軸にしないと、粗い格子(1/4等)ほど
    重みが大きいため、誤差ゼロで正確に一致する細かい格子(1/8/1/8T/1/16等)より
    誤差のある粗い格子が常に勝ってしまい、8分音符やスウィングのリズムが
    量子化で潰れてしまう。
    """
    candidates: list[tuple[str, int, float]] = []
    for resolution, divisor in _GRID_DIVISOR_BY_RESOLUTION.items():
        grid = divisions / divisor
        snapped = round(raw_tick / grid) * grid
        error = round(abs(snapped - raw_tick), _ERROR_ROUNDING_NDIGITS)
        weight = _metrical_weight(snapped, divisions)
        score = weight * _WEIGHT_TIEBREAK_SCALE - error
        candidates.append((resolution, round(snapped), score))

    if swing_ratio is not None:
        # スウィングしたオフビート位置にも候補を追加する(#25 Swing検出)。
        # 直前の四分音符境界を基準に、スウィング比の位置を候補として提示する。
        quarter_grid = divisions
        quarter_boundary = (raw_tick // quarter_grid) * quarter_grid
        swung_tick = quarter_boundary + swing_ratio * quarter_grid
        error = round(abs(swung_tick - raw_tick), _ERROR_ROUNDING_NDIGITS)
        # スウィング候補自体は「オフビート8分音符相当」の強さとして扱う。
        weight = _metrical_weight(round(quarter_boundary + 0.5 * quarter_grid), divisions)
        score = weight * _WEIGHT_TIEBREAK_SCALE - error
        candidates.append(("1/8-swing", round(swung_tick), score))

    candidates.sort(key=lambda c: c[2], reverse=True)
    top = candidates[:top_n]
    letters = "abcdefghij"
    return [
        SnapCandidateResult(id=letters[i], resolution=res, tick=tick, score=round(score, 6))
        for i, (res, tick, score) in enumerate(top)
    ]


def _offset_grid_containing_tick(tick: int, divisions: int) -> float:
    """`tick` がちょうど乗っている格子の中で最も粗い(=最大の)格子幅を返す。

    終端(オフセット)のスナップに使う格子を、分解能ラベル(例: `"1/8-swing"`)
    からではなく選択済みの `onset_tick` 自身から逆算するために使う(#25-M2
    レビュー指摘: `"1/8-swing"` は `_GRID_DIVISOR_BY_RESOLUTION` に無いため
    固定の1/8格子へフォールバックしていたが、swingしたonset(例: quarter+320)は
    一般に1/8格子(240の倍数)には乗らず、オンセットを含まない格子で終端を
    スナップしてしまっていた)。どの標準格子にも乗っていなければ最も細かい
    1/32格子にフォールバックする。
    """
    for divisor in sorted(_GRID_DIVISOR_BY_RESOLUTION.values()):
        grid = divisions / divisor
        remainder = tick % grid
        if remainder < _METRICAL_LEVEL_TOLERANCE or (grid - remainder) < _METRICAL_LEVEL_TOLERANCE:
            return grid
    finest_divisor = max(_GRID_DIVISOR_BY_RESOLUTION.values())
    return divisions / finest_divisor


def detect_swing_ratio(raw_ticks: list[float], divisions: int) -> float | None:
    """8分音符オフビート位置のオンセット分布から、スウィング比を推定する(#25)。

    各onsetの「四分音符内での位置」を`[0, 1)`へ正規化し、オフビート8分音符が
    来るはずの中間付近(`_OFFBEAT_WINDOW`)に分布するものだけを対象に、その
    中央値を「実際のオフビート位置」として推定する。中央値が0.5(ストレートな
    8分音符)から`_SWING_DETECTION_THRESHOLD`以上ずれていればスウィング比として
    返す。サンプル数が少なすぎる、またはストレートに近い場合は`None`
    (=スウィング候補を追加しない)。

    **既知の制約(#25-M2レビュー指摘)**: 楽曲全体で単一の比率を推定し、
    拍子(分母)ごとの区別をしない。6/8等の複合拍子では四分音符単位の
    オフビート窓(`_OFFBEAT_WINDOW`)が実際の拍構造と噛み合わない、また
    ストレートな区間とスウィングした区間が混在する楽曲では中央値が
    両者の中間に寄り、ストレートな小節のノートにも無関係な `"1/8-swing"`
    候補が付与されうる。区間ごと/拍子ごとの推定は将来の改善課題とする。
    """
    positions = []
    for tick in raw_ticks:
        pos = (tick % divisions) / divisions
        if _OFFBEAT_WINDOW[0] <= pos < _OFFBEAT_WINDOW[1]:
            positions.append(pos)

    if len(positions) < _MIN_SAMPLES_FOR_SWING_DETECTION:
        return None

    median_pos = statistics.median(positions)
    if abs(median_pos - 0.5) < _SWING_DETECTION_THRESHOLD:
        return None
    return median_pos


def quantize_note_onsets(
    notes: list[tuple[int, float, float]],
    beats: list[dict],
    time_signatures: list[dict],
    *,
    divisions: int = DEFAULT_DIVISIONS,
    top_n: int = DEFAULT_TOP_N,
) -> dict[int, QuantizedNote]:
    """ノート列を量子化する(#25)。

    `notes` は `(note_id, onset_sec, duration_sec)` のタプル列、`beats`/
    `time_signatures` は `beatmap.json` の同名フィールドの生の辞書形状を
    そのまま渡す。戻り値は `note_id -> QuantizedNote` のマッピング。

    `onset_sec`/`duration_sec` 自体は一切変更しない(呼び出し元のScore IRの
    生データは不変のまま)。beatmap.json の再編集後にこの関数を呼び直せば、
    常に最新のテンポマップに基づいて再量子化できる。

    `duration_tick`(音価)は、**選択された `selected_snap` と同じ分解能の
    格子へ終端(オフセット)もスナップした上で** `onset_tick` との差から求める
    (#25-M2レビュー指摘: オンセットだけスナップし終端を生値のままにすると、
    オンセットが前方へスナップされた分だけ実演奏より長くなり、後続ノートの
    `onset_tick` と重なりうる/選択分解能と無関係な半端な長さになる)。それでも
    実測ノート長を丸めるだけなので、隣接ノートとの重なりを完全には排除しない
    (完全な音価表記への変換はMusicXMLエクスポート側/L0の責務)。
    """
    anchors = _beat_tick_anchors(beats, time_signatures, divisions)
    raw_ticks = {
        note_id: _seconds_to_raw_tick(onset_sec, anchors) for note_id, onset_sec, _ in notes
    }
    swing_ratio = detect_swing_ratio(list(raw_ticks.values()), divisions)

    result: dict[int, QuantizedNote] = {}
    for note_id, onset_sec, duration_sec in notes:
        raw_onset_tick = raw_ticks[note_id]
        raw_offset_tick = _seconds_to_raw_tick(onset_sec + duration_sec, anchors)

        candidates = _snap_candidates_for_tick(
            raw_onset_tick, divisions, swing_ratio=swing_ratio, top_n=top_n
        )
        best = candidates[0]
        if best.resolution in _GRID_DIVISOR_BY_RESOLUTION:
            offset_grid = divisions / _GRID_DIVISOR_BY_RESOLUTION[best.resolution]
        else:
            # "1/8-swing" 等、ラベルから直接格子幅を引けない候補は、選択済みの
            # onset_tick自身が乗っている格子を逆算する(#25-M2レビュー指摘)。
            offset_grid = _offset_grid_containing_tick(best.tick, divisions)
        quantized_offset_tick = round(raw_offset_tick / offset_grid) * offset_grid
        duration_tick = max(round(quantized_offset_tick - best.tick), 1)

        result[note_id] = QuantizedNote(
            note_id=note_id,
            onset_tick=best.tick,
            duration_tick=duration_tick,
            snap_candidates=candidates,
            selected_snap=best.id,
        )
    return result


def quantize_pedal_ticks(
    pedals: list[tuple[float, float]],
    beats: list[dict],
    time_signatures: list[dict],
    *,
    divisions: int = DEFAULT_DIVISIONS,
) -> list[tuple[int, int]]:
    """ペダルイベント`(start_sec, stop_sec)`をtickへ変換する(#25/#27)。

    ノートのスナップ候補生成(`quantize_note_onsets`)とは異なり、ペダルは
    離散的な音価を持つ記譜対象ではなく継続的な操作(MusicXMLの`<pedal>`)
    なので、拍グリッドへのスナップは行わない。`_beat_tick_anchors`による
    線形補間で得られる生tick位置を丸めるだけで十分(#27のMusicXML書き出しが
    tick位置を必要とするための変換)。

    `[0, 最終アンカーのtick]` の範囲へクランプする(#27-M2レビュー指摘):
    先頭ビートより前に始まる、または最終ビートより後まで続くペダルは
    `_seconds_to_raw_tick` の外挿により範囲外(負値やスコア末尾超過)の
    tickになりうる。MusicXMLへ不正なtick位置を書き出さないよう、ここで
    有効範囲に収める。

    `beats`が空(`anchors`も空)の場合、`max_tick`は0にフォールバックする。
    このとき`_seconds_to_raw_tick`自身も空`anchors`を明示的に処理して
    `0.0`を返す(例外は発生しない)ため、後続のクランプ処理はそのまま
    安全に機能する(#27-M2レビュー3巡目で指摘された懸念の確認結果)。
    """
    anchors = _beat_tick_anchors(beats, time_signatures, divisions)
    max_tick = round(anchors[-1][1]) if anchors else 0
    result: list[tuple[int, int]] = []
    for start_sec, stop_sec in pedals:
        start_tick = max(0, min(round(_seconds_to_raw_tick(start_sec, anchors)), max_tick))
        stop_tick = max(0, min(round(_seconds_to_raw_tick(stop_sec, anchors)), max_tick))
        if start_tick >= max_tick:
            # スコア末尾ちょうどにクランプされた場合、+1する余地が無い
            # (#27-M2レビュー2巡目の指摘: この場合に限り開始側を1tick
            # 手前へ寄せ、非ゼロ長を確保する。全体が0tickの退化楽曲の
            # 場合のみ、これでもなお長さ0のまま)。
            start_tick = max(0, max_tick - 1)
        stop_tick = max(stop_tick, min(start_tick + 1, max_tick))
        result.append((start_tick, stop_tick))
    return result
