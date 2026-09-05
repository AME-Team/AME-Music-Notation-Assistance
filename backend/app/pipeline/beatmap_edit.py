"""#20: BeatGridEditor の手動補正ロジック(FR-04)。

`beatmap.json` を受け取り、要求された補正を適用した新しい辞書を返す純粋関数群。
API 層(`api/media.py`)はこれを呼んで書き戻すだけにする。
"""

from __future__ import annotations

from app.pipeline.beat import build_beatmap
from app.pipeline.time_signature import find_close_index as _find_close_index

# `_find_close_index` は `time_signature.py` と共有する(#19レビュー指摘: 独自の
# 許容誤差判定を複数モジュールに重複させると、基準がズレて小節境界が食い違いうる)。


def _rebuild_preserving_time_signatures(
    beatmap: dict, beats: list[float], downbeats: list[float]
) -> dict:
    """ビート/ダウンビートだけを敷き直し、`time_signatures` は既存のものを引き継ぐ。

    `build_beatmap` は呼ぶたびに `derive_time_signatures` で拍子を機械的に再計算する。
    オフセット/BPM上書き/ダウンビート回転はいずれも小節構造そのものを変えない操作
    なので、素朴に呼び直すと `override_time_signature` で保存した手動拍子指定が
    silently 失われる。ここで明示的に上書きして引き継ぐ。

    `rotate_downbeat` は末尾のダウンビートを脱落させて小節数を減らしうる。減った分の
    小節番号を参照する拍子指定を残すと、存在しない小節への死んだ参照になるため、
    新しい小節数の範囲内に収まるものだけを引き継ぐ。
    """
    result = build_beatmap(beats, downbeats).to_dict()
    max_bar = max((b["bar"] for b in result["beats"]), default=0)
    result["time_signatures"] = [ts for ts in beatmap["time_signatures"] if ts["bar"] <= max_bar]
    return result


def apply_offset(beatmap: dict, offset_sec: float) -> dict:
    """(a) 全体オフセットの調整。全ビート・ダウンビートを一律にずらす。

    `offset_sec == 0` は no-op として入力を無変更で返す(`apply_bpm_override`/
    `rotate_downbeat` と同じ設計)。`_rebuild_preserving_time_signatures` は
    `build_beatmap` 経由で `confidence` を毎回再計算するため、そのまま呼ぶと
    実質無変化でも別内容の dict になり、呼び出し元(api/media.py)がオブジェクト
    比較・内容比較のどちらでも「変更あり」と誤検出してしまう(#20-M1レビュー
    指摘の追加ラウンド)。
    """
    if offset_sec == 0:
        return beatmap
    beats = [round(b["time_sec"] + offset_sec, 6) for b in beatmap["beats"]]
    downbeats = [round(d + offset_sec, 6) for d in beatmap["downbeats_sec"]]
    return _rebuild_preserving_time_signatures(beatmap, beats, downbeats)


def apply_bpm_override(beatmap: dict, bpm: float) -> dict:
    """(b) 固定BPMへの上書き。先頭ビートの時刻を起点に、等間隔で全ビートを敷き直す。

    既知の制約: ダウンビートの再配置は元の `beat_in_bar == 1` の位置(インデックス)に
    基づく。`time_signature_override` で特定の小節の拍子を変えていても、この関数は
    ビート間隔だけを敷き直し小節構造そのものは変えないため、小節番号と実際の拍子が
    ユーザーの意図と厳密に対応するかは補償しない(`rotate_downbeat` の既知の制約と
    同種)。厳密な対応付けが要る場合は、小節番号ではなく時間範囲で拍子を管理する
    設計への変更が必要になる。
    """
    beats = beatmap["beats"]
    if not beats or bpm <= 0:
        return beatmap
    interval = 60.0 / bpm
    start = beats[0]["time_sec"]
    new_beat_times = [round(start + i * interval, 6) for i in range(len(beats))]
    downbeat_indices = [i for i, b in enumerate(beats) if b["beat_in_bar"] == 1]
    new_downbeats = [new_beat_times[i] for i in downbeat_indices if i < len(new_beat_times)]
    return _rebuild_preserving_time_signatures(beatmap, new_beat_times, new_downbeats)


def rotate_downbeat(beatmap: dict) -> dict:
    """(d) ダウンビートの位置ずらし(1拍分の回転)。各ダウンビートを直後のビートへ移す。

    `downbeats_sec` の値は `build_beatmap` の構築規則上、`beats[].time_sec` のいずれかと
    同一の値になる。そのため `beat_in_bar == 1` で照合すると先頭ビートが常に暗黙的に
    ダウンビート扱いされる仕様(`_assign_bars`)と衝突しうるが、時刻そのもので照合すれば
    誤マッチもタイブレークも不要になる(近接だが不一致な複数候補があっても許容誤差
    `_TIME_MATCH_TOLERANCE_SEC` 内でのみマッチする)。
    """
    beat_times = [b["time_sec"] for b in beatmap["beats"]]
    if not beat_times:
        return beatmap
    new_downbeats: list[float] = []
    for downbeat in beatmap["downbeats_sec"]:
        idx = _find_close_index(beat_times, downbeat)
        if idx is None or idx + 1 >= len(beat_times):
            continue
        new_downbeats.append(beat_times[idx + 1])
    if not new_downbeats:
        # 全ダウンビートが回転先を持たず脱落した場合(#20-M1レビュー指摘の追加
        # ラウンド)、そのまま _rebuild_preserving_time_signatures に渡すと
        # `_assign_bars` はダウンビートが1つも無い入力を「先頭ビートを暗黙の
        # bar=1」として扱い、以降ずっとbarを1のまま増やさず beat_in_bar だけ
        # 増え続ける構造になる。一方 time_signatures は元の(例: 4/4の)エントリを
        # 引き継ぐため、「1小節にビートが延々と詰まっているのに4/4」という
        # 内部矛盾した beatmap が無言で書き込まれてしまう。ダウンビートが1つも
        # 残せない回転は、他の3操作と同じno-opパターンとして入力を無変更で返す。
        return beatmap
    return _rebuild_preserving_time_signatures(beatmap, beat_times, new_downbeats)


def override_time_signature(beatmap: dict, *, bar: int, numerator: int, denominator: int) -> dict:
    """(c) 特定小節の拍子変更。指定小節以降に適用される拍子として上書きする。

    `bar` は現在のビート構造に実在する小節番号のみ受け付ける。検証しないと、
    PATCHリクエスト内で `rotate_downbeat` を先に適用して小節数が減った直後に
    (`_rebuild_preserving_time_signatures` が防いでいる)存在しない小節への
    死んだ参照を、この関数経由で再度作れてしまう(#20レビュー指摘)。

    `numerator`/`denominator` も検証する。未検証だと `{"numerator": 0,
    "denominator": 0}` のような無効な拍子が beatmap.json に書き込まれ、
    フロントのレンダリングを壊しうる(#20レビュー指摘)。`denominator` は単に
    `>=1` だけでは `4/3` のような楽譜として無効な拍子も通してしまうため、
    2の冪(1,2,4,8,16,32,...)に限定する(#20-M1レビュー指摘の追加ラウンド)。

    指定した拍子が既存の内容と完全に同一(実質no-op、例: 既に4/4のbarへ
    4/4を再指定)な場合は入力をそのまま返す。他の3操作(apply_offset/
    apply_bpm_override/rotate_downbeat)と同じno-opパターンに揃えることで、
    呼び出し元(api/media.py)がオブジェクト同一性で本当に変更があったかを
    一貫して検出できる(#20-M1レビュー指摘の追加ラウンド)。
    """
    max_bar = max((b["bar"] for b in beatmap["beats"]), default=0)
    if bar < 1 or bar > max_bar:
        raise ValueError(f"bar {bar} is out of range (1..{max_bar})")
    if numerator < 1:
        raise ValueError(f"numerator must be >= 1, got {numerator}")
    if denominator < 1 or (denominator & (denominator - 1)) != 0:
        raise ValueError(f"denominator must be a power of 2 (1,2,4,8,...), got {denominator}")
    new_entry = {"bar": bar, "numerator": numerator, "denominator": denominator}
    if new_entry in beatmap["time_signatures"]:
        return beatmap
    signatures = [ts for ts in beatmap["time_signatures"] if ts["bar"] != bar]
    signatures.append(new_entry)
    signatures.sort(key=lambda ts: ts["bar"])
    return {**beatmap, "time_signatures": signatures}
