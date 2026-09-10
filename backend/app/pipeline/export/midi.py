"""FR-13: Standard MIDI File 書き出し。

`score_builder.build_score`(Score IR → partitura `Score`)を共有し、
`partitura.save_score_midi`でSMFバイト列を生成する。
"""

from __future__ import annotations

from typing import Any

from app.pipeline.export.score_builder import ExportError, build_score


def _apply_midi_programs(midi_file: Any, parts_data: list[dict]) -> None:
    """各トラックの先頭にprogram changeを挿入する(#27-M2レビュー指摘)。

    `partitura.save_score_midi`はPart経由の書き出し経路ではMIDIプログラムを
    一切反映しない(常にプログラム0=ピアノとして書き出す)ため、ここで
    `midi_program`を反映する。`part_voice_assign_mode=0`(既定、パートごとに
    1トラック)を前提に、トラック順序がScore IRの`parts`順と一致することを
    利用する。`strict=True`(#27-M2レビュー2巡目の指摘): partituraの出力仕様が
    将来変わりトラック数がパート数と食い違うと、余剰トラック/パートを黙って
    切り捨てると誤ったトラックへprogram changeを挿入しかねない。前提が
    崩れたら`ExportError`で明示的に検出する。

    **既知の制約**: 単一声部のパートは既定でMIDIチャンネル0に割り当てられる
    (partituraの挙動)ため、複数パート(かつ単一声部)を持つスコアでは
    チャンネルが衝突し、後段のprogram changeが競合しうる。M2はピアノ1パート
    のみのためこの制約は顕在化しない。複数パート対応はチャンネル割り当ての
    見直しとあわせて将来課題とする。
    """
    import mido

    try:
        # zip(strict=True) は遅延評価のため、for分の内側で例外が出ると
        # そこまでの処理が中途半端に適用されてしまう。list()化して即座に
        # 長さ不一致を検出する(#27-M2レビュー2巡目の指摘)。
        pairs = list(zip(midi_file.tracks, parts_data, strict=True))
    except ValueError as exc:
        raise ExportError(
            f"MIDI track count ({len(midi_file.tracks)}) does not match part count "
            f"({len(parts_data)}); the part_voice_assign_mode=0 -> one-track-per-part "
            "assumption no longer holds"
        ) from exc

    for track, part_data in pairs:
        program = int(part_data.get("midi_program", 0))
        channels = {msg.channel for msg in track if hasattr(msg, "channel")}
        insert_at = next(
            (i for i, msg in enumerate(track) if not isinstance(msg, mido.MetaMessage)), 0
        )
        for channel in sorted(channels):
            track.insert(
                insert_at, mido.Message("program_change", program=program, channel=channel, time=0)
            )


def render_midi(score: dict[str, Any]) -> bytes:
    """Score IRのJSON dict形状からStandard MIDI Fileバイト列を生成する(FR-13)。

    量子化(#25)+L0(#26)実行済みが前提(`build_score`と同じ制約、未実行なら
    `score_builder.ExportError`)。
    """
    import io

    import partitura

    partitura_score = build_score(score)
    midi_file = partitura.save_score_midi(partitura_score, out=None)
    _apply_midi_programs(midi_file, score.get("parts", []))
    buffer = io.BytesIO()
    midi_file.save(file=buffer)
    return buffer.getvalue()
