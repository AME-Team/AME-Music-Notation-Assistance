"""Score IR編集オペレーション(#31, 設計書§10.4/§11.5)。

`POST /api/projects/{id}/score/ops`が受け取る「配列で一括適用」できる編集
オペレーション群。`domain/score.py`と同じ理由(#23参照)でPydanticを許容する
(データ定義のみ、インフラ層への非依存という方針とは独立)。

各opの適用ロジックは`services/score_ops.py`が持つ(データ定義と処理を分離)。
将来Undo/Redo(#32)やL2エージェントの`score_apply_ops`ツール(#75 §10.4)も
この同じop語彙を再利用する想定のため、api層(`api/schemas.py`)ではなく
domain層に置く。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class NoteAddOp(BaseModel):
    """新規ノートを追加する(FR-09)。

    `onset_tick`/`duration_tick`を指定する(ピアノロールはtick空間で操作する
    ため)。`onset_sec`/`duration_sec`は`services/score_ops.py`が現在のbeatmap
    から逆算する。
    """

    type: Literal["note.add"] = "note.add"
    part_id: str
    onset_tick: int = Field(ge=0)
    duration_tick: int = Field(gt=0)
    midi: int = Field(ge=0, le=127)
    velocity: int = Field(default=90, ge=0, le=127)
    voice: int = Field(default=1, ge=1, le=4)
    staff: int = Field(default=1, ge=1)


class NoteUpdateOp(BaseModel):
    """既存ノートのプロパティ変更(複数ノートへの一括適用可)。

    移動は`onset_tick`、リサイズは`duration_tick`の変更として表現する
    (設計書§10.4のop例が`note.update`を汎用的に使っているのと同じ設計)。
    未指定(`None`)のフィールドは変更しない。
    """

    type: Literal["note.update"] = "note.update"
    note_ids: list[int] = Field(min_length=1)
    onset_tick: int | None = Field(default=None, ge=0)
    duration_tick: int | None = Field(default=None, gt=0)
    midi: int | None = Field(default=None, ge=0, le=127)
    velocity: int | None = Field(default=None, ge=0, le=127)
    voice: int | None = Field(default=None, ge=1, le=4)
    staff: int | None = Field(default=None, ge=1)


class NoteDeleteOp(BaseModel):
    """論理削除(`status="deleted"`)。物理削除しない(§10.1)。"""

    type: Literal["note.delete"] = "note.delete"
    note_ids: list[int] = Field(min_length=1)


class NoteSplitOp(BaseModel):
    """1つのノートを`at_tick`で2つに分割する。"""

    type: Literal["note.split"] = "note.split"
    note_id: int
    at_tick: int = Field(ge=0)


class NoteMergeOp(BaseModel):
    """複数ノート(同一pitch/voice/staff)を1つに結合する。

    最も早い`onset_tick`から最も遅い終了tickまでを覆う1ノートに統合し、
    残りは論理削除する。
    """

    type: Literal["note.merge"] = "note.merge"
    note_ids: list[int] = Field(min_length=2)


class PartTransposeOctaveOp(BaseModel):
    """パート内の全activeノートを±1オクターブ移調する(R-6軽減策)。"""

    type: Literal["part.transpose_octave"] = "part.transpose_octave"
    part_id: str
    direction: Literal["up", "down"]


NoteOp = Annotated[
    NoteAddOp | NoteUpdateOp | NoteDeleteOp | NoteSplitOp | NoteMergeOp | PartTransposeOctaveOp,
    Field(discriminator="type"),
]
