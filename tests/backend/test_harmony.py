"""コード進行自動推定(#57, FR-16)の単体テスト。"""

from __future__ import annotations

from pathlib import Path

from app.agent.mcp.tools import ToolContext, score_context
from app.domain.score import (
    Clef,
    Note,
    Part,
    ScoreIR,
    SourceInfo,
    TimeSignatureEntry,
)
from app.pipeline.harmony import (
    estimate_chord_from_notes,
    estimate_chords_for_score,
    get_pitch_name,
)
from app.pipeline.refine.l1_chunker import build_chunks
from app.pipeline.refine.l1_prompt import build_song_context_message
from app.services.score_service import ScoreService


def test_pitch_naming_by_key_signature() -> None:
    # シャープ系 (fifths >= 0)
    assert get_pitch_name(1, fifths=0) == "C#"
    assert get_pitch_name(6, fifths=1) == "F#"
    assert get_pitch_name(10, fifths=2) == "A#"

    # フラット系 (fifths < 0)
    assert get_pitch_name(1, fifths=-1) == "Db"
    assert get_pitch_name(3, fifths=-2) == "Eb"
    assert get_pitch_name(10, fifths=-3) == "Bb"


def test_estimate_chord_triads() -> None:
    # C major: C(60), E(64), G(67) with bass C(48)
    notes_c = [(60, 1.0, False), (64, 1.0, False), (67, 1.0, False), (48, 1.0, True)]
    res_c = estimate_chord_from_notes(notes_c, fifths=0)
    assert res_c is not None
    sym, conf = res_c
    assert sym == "C"
    assert conf >= 0.8

    # A minor: A(57), C(60), E(64) with bass A(45)
    notes_am = [(57, 1.0, False), (60, 1.0, False), (64, 1.0, False), (45, 1.0, True)]
    res_am = estimate_chord_from_notes(notes_am, fifths=0)
    assert res_am is not None
    sym, conf = res_am
    assert sym == "Am"
    assert conf >= 0.8


def test_estimate_chord_sevenths() -> None:
    # G7: G(55), B(59), D(62), F(65)
    notes_g7 = [(55, 1.0, False), (59, 1.0, False), (62, 1.0, False), (65, 1.0, False)]
    res_g7 = estimate_chord_from_notes(notes_g7, fifths=1)
    assert res_g7 is not None
    assert res_g7[0] == "G7"

    # Cmaj7: C(60), E(64), G(67), B(71)
    notes_cmaj7 = [
        (60, 1.0, False),
        (64, 1.0, False),
        (67, 1.0, False),
        (71, 1.0, False),
    ]
    res_cmaj7 = estimate_chord_from_notes(notes_cmaj7, fifths=0)
    assert res_cmaj7 is not None
    assert res_cmaj7[0] == "Cmaj7"

    # Am7: A(57), C(60), E(64), G(67)
    notes_am7 = [(57, 1.0, False), (60, 1.0, False), (64, 1.0, False), (67, 1.0, False)]
    res_am7 = estimate_chord_from_notes(notes_am7, fifths=0)
    assert res_am7 is not None
    assert res_am7[0] == "Am7"


def test_estimate_chord_slash_chords() -> None:
    # C/E: C triad with bass E(40)
    notes_c_e = [(60, 1.0, False), (64, 1.0, False), (67, 1.0, False), (40, 1.5, True)]
    res = estimate_chord_from_notes(notes_c_e, fifths=0)
    assert res is not None
    assert res[0] == "C/E"

    # F/A: F triad with bass A(45)
    notes_f_a = [(65, 1.0, False), (69, 1.0, False), (72, 1.0, False), (45, 1.5, True)]
    res = estimate_chord_from_notes(notes_f_a, fifths=-1)
    assert res is not None
    assert res[0] == "F/A"


def test_estimate_chord_flat_keys() -> None:
    # Bb major in Bb major key (fifths = -2): Bb(58), D(62), F(65)
    notes_bb = [(58, 1.0, False), (62, 1.0, False), (65, 1.0, False)]
    res = estimate_chord_from_notes(notes_bb, fifths=-2)
    assert res is not None
    assert res[0] == "Bb"


def test_estimate_chords_for_score_multi_bar() -> None:
    # 4/4 拍子で
    # m.1: C major (beat 1〜4)
    # m.2: Am (beat 1〜2), G (beat 3〜4)
    # m.3: 空小節
    # m.4: F/A (beat 1〜4)
    divisions = 480
    notes = [
        # m.1 (tick 0 ~ 1920): C major
        Note(
            id=1,
            onset_sec=0.0,
            duration_sec=2.0,
            onset_tick=0,
            duration_tick=1920,
            midi=48,
            velocity=80,
            voice=1,
            staff=2,
            provenance="amt",
        ),
        Note(
            id=2,
            onset_sec=0.0,
            duration_sec=2.0,
            onset_tick=0,
            duration_tick=1920,
            midi=60,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        Note(
            id=3,
            onset_sec=0.0,
            duration_sec=2.0,
            onset_tick=0,
            duration_tick=1920,
            midi=64,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        Note(
            id=4,
            onset_sec=0.0,
            duration_sec=2.0,
            onset_tick=0,
            duration_tick=1920,
            midi=67,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        # m.2 前半 (tick 1920 ~ 2880): Am
        Note(
            id=5,
            onset_sec=2.0,
            duration_sec=1.0,
            onset_tick=1920,
            duration_tick=960,
            midi=45,
            velocity=80,
            voice=1,
            staff=2,
            provenance="amt",
        ),
        Note(
            id=6,
            onset_sec=2.0,
            duration_sec=1.0,
            onset_tick=1920,
            duration_tick=960,
            midi=57,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        Note(
            id=7,
            onset_sec=2.0,
            duration_sec=1.0,
            onset_tick=1920,
            duration_tick=960,
            midi=60,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        Note(
            id=8,
            onset_sec=2.0,
            duration_sec=1.0,
            onset_tick=1920,
            duration_tick=960,
            midi=64,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        # m.2 後半 (tick 2880 ~ 3840): G
        Note(
            id=9,
            onset_sec=3.0,
            duration_sec=1.0,
            onset_tick=2880,
            duration_tick=960,
            midi=43,
            velocity=80,
            voice=1,
            staff=2,
            provenance="amt",
        ),
        Note(
            id=10,
            onset_sec=3.0,
            duration_sec=1.0,
            onset_tick=2880,
            duration_tick=960,
            midi=55,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        Note(
            id=11,
            onset_sec=3.0,
            duration_sec=1.0,
            onset_tick=2880,
            duration_tick=960,
            midi=59,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        Note(
            id=12,
            onset_sec=3.0,
            duration_sec=1.0,
            onset_tick=2880,
            duration_tick=960,
            midi=62,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        # m.4 (tick 5760 ~ 7680): F/A
        Note(
            id=13,
            onset_sec=6.0,
            duration_sec=2.0,
            onset_tick=5760,
            duration_tick=1920,
            midi=45,
            velocity=80,
            voice=1,
            staff=2,
            provenance="amt",
        ),
        Note(
            id=14,
            onset_sec=6.0,
            duration_sec=2.0,
            onset_tick=5760,
            duration_tick=1920,
            midi=65,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        Note(
            id=15,
            onset_sec=6.0,
            duration_sec=2.0,
            onset_tick=5760,
            duration_tick=1920,
            midi=69,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        Note(
            id=16,
            onset_sec=6.0,
            duration_sec=2.0,
            onset_tick=5760,
            duration_tick=1920,
            midi=72,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
    ]

    score = ScoreIR(
        project_id="test_chord_proj",
        source=SourceInfo(filename="test.wav", duration_sec=8.0, sample_rate=44100),
        divisions=divisions,
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
        parts=[
            Part(
                id="piano",
                name="Piano",
                midi_program=0,
                staves=2,
                clefs=[
                    Clef(staff=1, sign="G", line=2),
                    Clef(staff=2, sign="F", line=4),
                ],
                notes=notes,
            )
        ],
        next_note_id=17,
    )

    chords = estimate_chords_for_score(score)
    assert len(chords) >= 3

    # m.1 -> C
    c1 = next((c for c in chords if c.bar == 1), None)
    assert c1 is not None
    assert c1.symbol == "C"
    assert c1.beat == 1.0

    # m.2 -> beat 1.0: Am, beat 3.0: G
    c2_1 = next((c for c in chords if c.bar == 2 and c.beat == 1.0), None)
    c2_2 = next((c for c in chords if c.bar == 2 and c.beat == 3.0), None)
    assert c2_1 is not None and c2_1.symbol == "Am"
    assert c2_2 is not None and c2_2.symbol == "G"

    # m.3 は空小節なので登録なし
    assert not any(c.bar == 3 for c in chords)

    # m.4 -> F/A
    c4 = next((c for c in chords if c.bar == 4), None)
    assert c4 is not None
    assert c4.symbol == "F/A"


def test_l1_context_and_chunk_injection() -> None:
    divisions = 480
    notes = [
        Note(
            id=1,
            onset_sec=0.0,
            duration_sec=1.0,
            onset_tick=0,
            duration_tick=960,
            midi=60,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        Note(
            id=2,
            onset_sec=0.0,
            duration_sec=1.0,
            onset_tick=0,
            duration_tick=960,
            midi=64,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        Note(
            id=3,
            onset_sec=0.0,
            duration_sec=1.0,
            onset_tick=0,
            duration_tick=960,
            midi=67,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
    ]
    score = ScoreIR(
        project_id="test_l1_proj",
        source=SourceInfo(filename="test.wav", duration_sec=4.0, sample_rate=44100),
        divisions=divisions,
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
        parts=[
            Part(
                id="piano",
                name="Piano",
                midi_program=0,
                staves=1,
                clefs=[Clef(staff=1, sign="G", line=2)],
                notes=notes,
            )
        ],
        next_note_id=4,
    )

    # L1 楽曲全体プロンプトにコード進行が含まれる
    msg = build_song_context_message(score, "piano")
    assert "コード進行:" in msg
    assert "C" in msg

    # L1 チャンク文脈に chord_hints が含まれる
    chunks = build_chunks(score, "piano")
    assert len(chunks) >= 1
    assert any(h.symbol == "C" for h in chunks[0].context.chord_hints)


def test_mcp_score_context_fallback(tmp_path: Path) -> None:
    divisions = 480
    notes = [
        Note(
            id=1,
            onset_sec=0.0,
            duration_sec=1.0,
            onset_tick=0,
            duration_tick=960,
            midi=60,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        Note(
            id=2,
            onset_sec=0.0,
            duration_sec=1.0,
            onset_tick=0,
            duration_tick=960,
            midi=64,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
        Note(
            id=3,
            onset_sec=0.0,
            duration_sec=1.0,
            onset_tick=0,
            duration_tick=960,
            midi=67,
            velocity=80,
            voice=1,
            staff=1,
            provenance="amt",
        ),
    ]
    score = ScoreIR(
        project_id="proj_mcp",
        source=SourceInfo(filename="test.wav", duration_sec=4.0, sample_rate=44100),
        divisions=divisions,
        time_signatures=[TimeSignatureEntry(bar=1, numerator=4, denominator=4)],
        parts=[
            Part(
                id="piano",
                name="Piano",
                midi_program=0,
                staves=1,
                clefs=[Clef(staff=1, sign="G", line=2)],
                notes=notes,
            )
        ],
        next_note_id=4,
    )

    ScoreService(workspace_dir=tmp_path).write_score("proj_mcp", score)
    ctx = ToolContext(workspace_dir=tmp_path, project_id="proj_mcp", run_id="run_1")

    res = score_context(ctx)
    assert "chords" in res
    assert len(res["chords"]) >= 1
    assert res["chords"][0]["symbol"] == "C"
