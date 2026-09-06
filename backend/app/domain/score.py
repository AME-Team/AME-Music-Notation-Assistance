"""Score IR(#23, 設計書§10)。パイプライン全体を貫く中核データ構造。

AMT(Stage 3) → クオンタイズ(Stage 4) → L0/L1/L2 整音 → 編集 → MusicXML 出力(Stage 6)の
すべてがこの構造を読み書きする。設計書§5.2の「`domain/` は外部ライブラリに依存しない」は
FastAPI・SQLite・DSPライブラリ等のインフラ層への非依存を指すと解釈し、データ定義のための
Pydantic は許容する(スキーマの二重管理を避けるための意図的な判断。#23)。

設計原則(§10.1):
1. 生データ(`onset_sec`/`duration_sec`)と量子化データ(`onset_tick`/`duration_tick`)を
   両方保持する。`onset_sec` を捨てないことで、テンポマップ修正後の再量子化が可能になる
2. 削除は論理削除。`status: "deleted"` とし、物理削除しない
3. 出自を全ノートに記録する(`provenance`)
4. ノートIDは生涯不変。`ScoreIR.allocate_note_id()` 経由でのみ採番する
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CURRENT_SCHEMA_VERSION = 3

NoteStatus = Literal["active", "deleted", "muted"]
NoteProvenance = Literal["amt", "baseline", "llm", "agent", "user"]
PitchStep = Literal["C", "D", "E", "F", "G", "A", "B"]
ClefSign = Literal["G", "F", "C", "percussion"]
KeyMode = Literal["major", "minor"]


class SourceInfo(BaseModel):
    filename: str
    duration_sec: float
    sample_rate: int


class TempoMapEntry(BaseModel):
    """§10.2。`api.schemas.TempoMapEntry`(beatmap.json用)とは構造が似るが、

    domain層をAPI層から独立させるため意図的に別定義とする(#23)。
    """

    bar: int
    beat: float
    bpm: float


class TimeSignatureEntry(BaseModel):
    bar: int
    numerator: int = Field(ge=1)
    denominator: int = Field(ge=1)


class KeySignatureEntry(BaseModel):
    bar: int
    fifths: int = Field(ge=-7, le=7)
    mode: KeyMode


class ChordEntry(BaseModel):
    bar: int
    beat: float
    symbol: str
    confidence: float = Field(ge=0.0, le=1.0)


class Spelling(BaseModel):
    """異名同音表記(§7.4)。`step`+`alter` のピッチクラスが `midi % 12` と一致すること

    (V-4)、`octave` が MIDI 番号と整合すること(V-5、B#/Cbの境界を考慮)は
    `domain.pitch` の検証ロジックで別途チェックする(モデル自体はデータ構造のみ)。
    """

    step: PitchStep
    alter: int = Field(ge=-2, le=2)
    octave: int


class Tie(BaseModel):
    start: bool = False
    stop: bool = False


class SnapCandidate(BaseModel):
    """Stage 4(#25)が生成する量子化候補。`id` は同一ノート内で一意な短い識別子

    ("a","b","c",...)。AIやユーザーが `Note.selected_snap` でこれを指す。
    """

    id: str
    resolution: str  # "1/4" | "1/8" | "1/8T" | "1/16" | "1/16T" | "1/32"
    tick: int
    score: float


class Note(BaseModel):
    id: int
    onset_sec: float = Field(ge=0.0)
    duration_sec: float = Field(gt=0.0)
    onset_tick: int | None = None
    duration_tick: int | None = None
    midi: int = Field(ge=0, le=127)
    velocity: int = Field(ge=0, le=127)
    spelling: Spelling | None = None
    voice: int = Field(default=1, ge=1, le=4)
    staff: int = Field(default=1, ge=1)
    tie: Tie = Field(default_factory=Tie)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    provenance: NoteProvenance
    provenance_run_id: str | None = None
    flags: list[str] = Field(default_factory=list)
    status: NoteStatus = "active"
    snap_candidates: list[SnapCandidate] = Field(default_factory=list)
    selected_snap: str | None = None
    ai_reason: str | None = None


class Pedal(BaseModel):
    """ペダルイベント(#24)。§10.2のスキーマ本文には無いフィールドだが、

    ByteDance Piano Transcriptionの出力(ペダルon/off)とMusicXML `<pedal>`(#27)の
    両方に必要なため、パート単位で意図的に追加する(設計書からの拡張として記録)。
    """

    start_sec: float = Field(ge=0.0)
    stop_sec: float
    start_tick: int | None = None
    stop_tick: int | None = None


class Clef(BaseModel):
    staff: int = Field(ge=1)
    sign: ClefSign
    line: int


class Part(BaseModel):
    id: str
    name: str
    midi_program: int = Field(ge=0, le=127)
    stem_source: str | None = None
    staves: int = Field(default=1, ge=1)
    clefs: list[Clef] = Field(default_factory=list)
    notes: list[Note] = Field(default_factory=list)
    pedals: list[Pedal] = Field(default_factory=list)


class ScoreMeta(BaseModel):
    """§10.2 の `meta.stages`。各ステージの実行メタデータを緩く保持する。

    L1/L2(M4/M5)まで含めた最終形は現時点で決め切らないため、値は `dict` のまま
    許容し、詳細な構造化は各ステージの実装時に個別のヘルパで検証する。
    """

    stages: dict[str, dict] = Field(default_factory=dict)


class ScoreIR(BaseModel):
    """§10.2 準拠のトップレベルモデル。`services/score_service.py` がこれを

    `score/current.json` として読み書きする。
    """

    schema_version: int = CURRENT_SCHEMA_VERSION
    project_id: str
    source: SourceInfo
    divisions: int = 480
    tempo_map: list[TempoMapEntry] = Field(default_factory=list)
    time_signatures: list[TimeSignatureEntry] = Field(default_factory=list)
    key_signatures: list[KeySignatureEntry] = Field(default_factory=list)
    chords: list[ChordEntry] = Field(default_factory=list)
    parts: list[Part] = Field(default_factory=list)
    meta: ScoreMeta = Field(default_factory=ScoreMeta)
    # ノートIDの生涯不変性(§10.1)を守るための単調増加カウンタ。個々のステージは
    # ノートを直接 `Note(id=...)` で作らず、必ず `allocate_note_id()` を経由すること。
    next_note_id: int = 1

    def allocate_note_id(self) -> int:
        note_id = self.next_note_id
        self.next_note_id += 1
        return note_id

    def find_part(self, part_id: str) -> Part | None:
        return next((p for p in self.parts if p.id == part_id), None)
