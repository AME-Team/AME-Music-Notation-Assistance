"""#38: `pipeline/refine/l1_prompt.py`(L1システムプロンプト構築)のテスト。"""

from __future__ import annotations

import json

from app.domain.score import (
    Clef,
    Note,
    Part,
    ScoreIR,
    SourceInfo,
    TempoMapEntry,
    TimeSignatureEntry,
)
from app.pipeline.refine.l1_chunker import (
    ChunkBars,
    ChunkContext,
    ChunkInput,
    ChunkPart,
)
from app.pipeline.refine.l1_prompt import (
    NOTATION_RULES_MARKDOWN,
    build_chunk_message,
    build_song_context_message,
    build_system_prompt,
)


def test_system_prompt_is_deterministic_across_calls() -> None:
    """§7.3のプロンプトキャッシュ効率のため、システムプロンプトは常に同一。"""
    assert build_system_prompt() == build_system_prompt()


def test_system_prompt_contains_notation_rules() -> None:
    assert NOTATION_RULES_MARKDOWN in build_system_prompt()


def test_system_prompt_does_not_contain_volatile_placeholders() -> None:
    """回帰(§7.3禁止事項): タイムスタンプ・project_id・チャンク番号等の

    可変情報を埋め込まないこと。
    """
    prompt = build_system_prompt()
    for forbidden in ("project_id", "prj_", "timestamp", "chunk_index"):
        assert forbidden not in prompt


def test_system_prompt_repeats_the_voice_rule_right_before_generation() -> None:
    """#109: 同時発音のvoice分割規則の遵守率が低かったため、規則と具体例を

    システムプロンプトの**最後**(モデルが直前に読む位置)に置く。
    """
    prompt = build_system_prompt()

    assert prompt.rstrip().endswith(
        "最高音G4が voice 1(同一staff内でvoice番号1〜4を重複させない)。"
    )
    assert "同じvoiceに\n  まとめてはならない" in prompt
    assert "別のvoiceへ回すこと" in prompt
    # #109レビュー指摘: 文言は実装(検証層の自動修復)と一致していること。
    assert "決定論的に再割当して自動修復する" in prompt
    assert "チャンク全体の提案が採用されない" in prompt


def test_notation_rules_state_the_interval_rule_for_shared_l2_workspace() -> None:
    """規則集はL2の`notation_rules.md`と共有のため、区間の規則もここに置く

    (L1とL2で記譜方針が食い違わないようにする、モジュールdocstring参照)。
    """
    assert (
        "同時発音するノートを全て同じvoiceへまとめてはならない"
        in NOTATION_RULES_MARKDOWN
    )
    assert "先行する音が鳴っている途中で新しい音が始まるなら" in NOTATION_RULES_MARKDOWN


def test_song_context_message_includes_part_and_tempo() -> None:
    score = ScoreIR(
        project_id="p1",
        source=SourceInfo(filename="song.wav", duration_sec=60.0, sample_rate=44100),
        time_signatures=[TimeSignatureEntry(bar=1, numerator=3, denominator=4)],
        tempo_map=[TempoMapEntry(bar=1, beat=1.0, bpm=140.0)],
    )
    part = Part(
        id="piano",
        name="Piano",
        midi_program=0,
        staves=2,
        clefs=[Clef(staff=1, sign="G", line=2)],
    )
    score.parts.append(part)

    message = build_song_context_message(score, "piano")
    assert "Piano" in message
    assert "3/4" in message
    assert "140.0bpm" in message


def test_song_context_message_includes_key_estimated_from_whole_part() -> None:
    """回帰(#38 Gate2レビュー指摘): docstringが「調」を含むと謳いながら

    実装に無かったバグ。パート全体のノートから推定した調を含める。
    """
    score = ScoreIR(
        project_id="p1",
        source=SourceInfo(filename="song.wav", duration_sec=60.0, sample_rate=44100),
    )
    part = Part(id="piano", name="Piano", midi_program=0, staves=2)
    for i, midi in enumerate([60, 60, 60, 64, 64, 67, 67, 62, 69, 71]):
        part.notes.append(
            Note(
                id=i + 1,
                onset_sec=float(i),
                duration_sec=0.4,
                midi=midi,
                velocity=90,
                provenance="amt",
            )
        )
    score.parts.append(part)

    message = build_song_context_message(score, "piano")
    assert "調:" in message
    assert "C major" in message


def test_song_context_message_raises_for_unknown_part() -> None:
    score = ScoreIR(
        project_id="p1",
        source=SourceInfo(filename="song.wav", duration_sec=1.0, sample_rate=44100),
    )
    try:
        build_song_context_message(score, "nonexistent")
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_chunk_message_is_sorted_json() -> None:
    chunk = ChunkInput(
        context=ChunkContext(
            key_estimate="C major",
            key_confidence=0.8,
            time_signature="4/4",
            tempo_bpm=120.0,
            part=ChunkPart(id="piano", instrument="piano", staves=2),
            chord_hints=[],
            bars=ChunkBars(target=(1, 4), context_before=[], context_after=[5]),
        ),
        notes=[],
    )
    message = build_chunk_message(chunk)
    parsed = json.loads(message)
    assert parsed["context"]["key_estimate"] == "C major"
    # json.dumps(..., sort_keys=True)により、シリアライズされた文字列上でも
    # トップレベルキーがアルファベット順になっていることを確認する。
    assert message.index('"context"') < message.index('"notes"')
