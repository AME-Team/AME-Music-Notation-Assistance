"""共有 Pydantic モデル(#14: ここから OpenAPI → TS 型を生成する)。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class StageStatus(BaseModel):
    status: str
    progress: float


class Project(BaseModel):
    id: str
    name: str
    original_filename: str
    audio_format: str
    created_at: str
    stages: dict[str, StageStatus]


class ProjectList(BaseModel):
    projects: list[Project]


class RunStageRequest(BaseModel):
    params: dict = {}


class RunStageResponse(BaseModel):
    job_id: str


class Job(BaseModel):
    id: str
    project_id: str
    stage: str
    status: str
    progress: float
    message: str | None = None
    exit_code: int | None = None
    created_at: str
    updated_at: str


class ErrorResponse(BaseModel):
    detail: str


class PeaksResponse(BaseModel):
    duration_sec: float
    sample_rate: int
    peaks: list[list[float]]


class BeatEntry(BaseModel):
    time_sec: float
    beat_in_bar: int
    bar: int


class TimeSignatureEntry(BaseModel):
    bar: int
    numerator: int
    denominator: int


class TempoMapEntry(BaseModel):
    bar: int
    beat: float
    bpm: float


class Beatmap(BaseModel):
    beats: list[BeatEntry]
    downbeats_sec: list[float]
    time_signatures: list[TimeSignatureEntry]
    tempo_map: list[TempoMapEntry]
    confidence: float
    source: str = "auto"


class BeatmapEditRequest(BaseModel):
    """#20 BeatGridEditor: 送られたフィールドのみ順に適用する(オフセット→BPM→回転→拍子)。"""

    # `int` フィールド(TimeSignatureEntryのnumerator/denominator等)はPydantic v2の
    # 既定でNaN/Infinityを自動的に拒否するが、`float` フィールドは既定で許可してしまう
    # (#20-M1レビュー指摘の追加ラウンド)。NaNが通ると beatmap.json に非標準JSON
    # (NaNリテラル)が書き込まれ、フロントの`JSON.parse`が壊れる。
    offset_sec: float | None = Field(default=None, allow_inf_nan=False)
    bpm_override: float | None = Field(default=None, allow_inf_nan=False)
    rotate_downbeat: bool = False
    time_signature_override: TimeSignatureEntry | None = None
