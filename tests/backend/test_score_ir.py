"""#23: Score IR のプロパティテスト(hypothesis)と往復(round-trip)テスト。

「採譜の正しさは自動テストしにくいが、IRの不変条件は完全にテストできる」(設計書§10)
という方針に基づき、Pydanticのフィールド制約自体が正しく機能しているか、JSON往復で
情報が失われないか、マイグレーション枠組みが未知/将来バージョンを正しく拒否するかを
検証する。異名同音表記の検証(V-4/V-5)は `test_pitch.py` を参照。
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.domain.migrations import (
    FutureSchemaVersionError,
    UnknownSchemaVersionError,
    migrate_to_current,
)
from app.domain.score import (
    CURRENT_SCHEMA_VERSION,
    Note,
    NoteProvenance,
    NoteStatus,
    Part,
    ScoreIR,
    SourceInfo,
    Spelling,
)

_PROVENANCE_VALUES = get_args(NoteProvenance)
_STATUS_VALUES = get_args(NoteStatus)

_onset_sec = st.floats(
    min_value=0.0, max_value=600.0, allow_nan=False, allow_infinity=False
)
_duration_sec = st.floats(
    min_value=0.001, max_value=60.0, allow_nan=False, allow_infinity=False
)
_midi = st.integers(min_value=0, max_value=127)
_velocity = st.integers(min_value=0, max_value=127)
_provenance = st.sampled_from(_PROVENANCE_VALUES)
_status = st.sampled_from(_STATUS_VALUES)


def _note_kwargs_strategy() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "id": st.integers(min_value=1, max_value=10_000),
            "onset_sec": _onset_sec,
            "duration_sec": _duration_sec,
            "midi": _midi,
            "velocity": _velocity,
            "provenance": _provenance,
            "status": _status,
        }
    )


def _source() -> SourceInfo:
    return SourceInfo(filename="song.mp3", duration_sec=187.4, sample_rate=44100)


class TestNoteRoundTrip:
    """回帰(#23): Note が JSON(dict)往復で情報を失わないこと。"""

    @given(_note_kwargs_strategy())
    def test_note_round_trips_through_json(self, kwargs: dict) -> None:
        note = Note(**kwargs)
        dumped = note.model_dump(mode="json")
        restored = Note.model_validate(dumped)
        assert restored == note


class TestNoteInvariants:
    """#23: Pydanticのフィールド制約がScore IRの不変条件を守っていることを確認する。"""

    @given(_note_kwargs_strategy(), st.floats(max_value=0.0, allow_nan=False))
    def test_duration_must_be_positive(
        self, kwargs: dict, non_positive_duration: float
    ) -> None:
        kwargs["duration_sec"] = non_positive_duration
        with pytest.raises(ValidationError):
            Note(**kwargs)

    @given(_note_kwargs_strategy(), st.floats(max_value=-0.001, allow_nan=False))
    def test_onset_must_be_non_negative(
        self, kwargs: dict, negative_onset: float
    ) -> None:
        kwargs["onset_sec"] = negative_onset
        with pytest.raises(ValidationError):
            Note(**kwargs)

    @given(_note_kwargs_strategy(), st.integers().filter(lambda v: not (1 <= v <= 4)))
    def test_voice_out_of_range_is_rejected(self, kwargs: dict, bad_voice: int) -> None:
        kwargs["voice"] = bad_voice
        with pytest.raises(ValidationError):
            Note(**kwargs)

    @given(_note_kwargs_strategy(), st.integers(max_value=0))
    def test_staff_below_one_is_rejected(self, kwargs: dict, bad_staff: int) -> None:
        kwargs["staff"] = bad_staff
        with pytest.raises(ValidationError):
            Note(**kwargs)

    def test_unknown_provenance_is_rejected(self) -> None:
        kwargs = {
            "id": 1,
            "onset_sec": 0.0,
            "duration_sec": 0.5,
            "midi": 60,
            "velocity": 90,
            "provenance": "not-a-real-provenance",
        }
        with pytest.raises(ValidationError):
            Note(**kwargs)


class TestScoreIRRoundTrip:
    """#23: ScoreIR 全体の往復(dict/JSON経由)と `services.score_service` 経由の

    ファイル往復(完了条件そのもの)を検証する。
    """

    def _sample_score(self) -> ScoreIR:
        score = ScoreIR(project_id="proj_test", source=_source())
        note = Note(
            id=score.allocate_note_id(),
            onset_sec=6.021,
            duration_sec=0.482,
            midi=66,
            velocity=85,
            spelling=Spelling(step="F", alter=1, octave=4),
            provenance="amt",
        )
        score.parts.append(
            Part(id="piano", name="Piano", midi_program=0, staves=2, notes=[note])
        )
        return score

    def test_score_ir_round_trips_through_dict(self) -> None:
        score = self._sample_score()
        dumped = score.model_dump(mode="json")
        restored = ScoreIR.model_validate(dumped)
        assert restored == score

    def test_score_ir_round_trips_through_storage(self, tmp_path: Path) -> None:
        from app.services.score_service import ScoreService

        service = ScoreService(workspace_dir=tmp_path)
        score = self._sample_score()
        service.write_score("proj_test", score)

        restored = service.read_score("proj_test")
        assert restored == score

    def test_allocate_note_id_is_monotonic_and_never_reused(self) -> None:
        score = ScoreIR(project_id="proj_test", source=_source())
        ids = [score.allocate_note_id() for _ in range(5)]
        assert ids == sorted(ids)
        assert len(set(ids)) == len(ids)


class TestMigrations:
    """#23 R-8: schema_version マイグレーション枠組み。"""

    def test_current_version_is_identity(self) -> None:
        data = {"schema_version": CURRENT_SCHEMA_VERSION, "project_id": "x"}
        assert migrate_to_current(data) == data

    def test_missing_schema_version_raises(self) -> None:
        with pytest.raises(UnknownSchemaVersionError):
            migrate_to_current({"project_id": "x"})

    def test_future_version_raises(self) -> None:
        with pytest.raises(FutureSchemaVersionError):
            migrate_to_current({"schema_version": CURRENT_SCHEMA_VERSION + 1})

    def test_unregistered_old_version_raises(self) -> None:
        """現時点では旧バージョンからのマイグレーションは1件も登録されていないため、

        `CURRENT_SCHEMA_VERSION` 未満のバージョンは常に未登録エラーになる。
        """
        if CURRENT_SCHEMA_VERSION <= 1:
            pytest.skip(
                "no version below CURRENT_SCHEMA_VERSION exists to test against"
            )
        with pytest.raises(UnknownSchemaVersionError):
            migrate_to_current({"schema_version": CURRENT_SCHEMA_VERSION - 1})
