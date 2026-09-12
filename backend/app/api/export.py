"""Stage 6: MusicXML/MIDI書き出しAPI(#27, 設計書§11.6)。

モデル推論を伴わない同期処理のため、ジョブ化はしない(既存の`get_peaks`と
同じ判断)。生成物は`storage.musicxml_export_path()`/`midi_export_path()`
にも保存する(設計書§11.6)。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import ensure_project_exists, get_project_service, get_settings, read_score_or_404
from app.config import Settings
from app.infra import storage
from app.pipeline.export.midi import render_midi
from app.pipeline.export.musicxml import render_musicxml
from app.pipeline.export.score_builder import ExportError
from app.services.project_service import ProjectService

router = APIRouter(prefix="/api/projects/{project_id}", tags=["export"])


class ExportRequest(BaseModel):
    """設計書§11.6の`parts`による部分エクスポート(#36)。`options`は本Issueの

    作業内容・設計書抜粋のいずれにも具体的な中身の言及が無いため未実装のまま
    とする。`extra="forbid"`(#27-M2レビュー指摘)により`options`等の未知
    フィールドを黙って無視せず422で拒否する(Pydantic v2の既定`extra="ignore"`
    のままだと、送っても効果が無いのに送れてしまい、指定できたと誤認させる)。

    `parts`は`None`(既定、全パート)またはpart idのリスト。#30以来このプロジェクト
    は常にピアノ1パートのみを生成するため、実際に複数パートから選択する場面は
    現状無いが、将来複数パート対応時にそのまま使える汎用フィルタとして実装する
    (フロントエンドの選択UIは、選択肢が常に1つしかない現状では意味を持たない
    ため実装しない、#36設計判断)。
    """

    model_config = ConfigDict(extra="forbid")

    format: Literal["musicxml", "midi"]
    parts: list[str] | None = Field(default=None)


_EXPORT_MEDIA_TYPES = {
    "musicxml": "application/vnd.recordare.musicxml+xml",
    "midi": "audio/midi",
}

_AUDIO_BINARY_SCHEMA = {"type": "string", "format": "binary"}


@router.post(
    "/export",
    response_class=FileResponse,
    responses={
        200: {
            "content": {
                media_type: {"schema": _AUDIO_BINARY_SCHEMA}
                for media_type in _EXPORT_MEDIA_TYPES.values()
            }
        }
    },
)
def export_score(
    project_id: str,
    body: ExportRequest,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """量子化(#25)+L0(#26)実行済みが前提。未実行なら404、不正なScore IRなら422を返す。

    `async def` ではなく通常の `def` にする(`api/media.py`の`get_peaks`と同じ
    理由、#27-M2レビュー指摘): `render_musicxml`/`render_midi`はCPUバウンドの
    同期処理(partitura呼び出し)であり、`async def`のままだとイベントループを
    直接ブロックし、SSEでのジョブ進捗配信など他の同時リクエストを止めてしまう。
    """
    ensure_project_exists(project_id, service)
    # `read_score_or_404`はscore/current.json自体が未作成(採譜=transcribe
    # 未実行)の場合のみ404にする。量子化未実行はここではなく、build_score()
    # の検証によりExportError(422)側で検出される。
    score = read_score_or_404(project_id, settings)

    score_dict = score.model_dump(mode="json")
    if body.parts is not None:
        # #36: パート選択。`build_score`自体は変更せず、渡す前にdictを絞り込む
        # ことで完結させる(MusicXML/MIDI両方の書き出しロジックに影響しない)。
        available_ids = {part["id"] for part in score_dict["parts"]}
        missing = sorted(set(body.parts) - available_ids)
        if missing:
            raise HTTPException(status_code=422, detail=f"unknown part ids: {missing}")
        score_dict["parts"] = [p for p in score_dict["parts"] if p["id"] in body.parts]
    try:
        if body.format == "musicxml":
            data = render_musicxml(score_dict)
            out_path = storage.musicxml_export_path(settings.workspace_dir, project_id)
        else:
            data = render_midi(score_dict)
            out_path = storage.midi_export_path(settings.workspace_dir, project_id)
    except ExportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # `ensure_project_layout`(プロジェクト作成時)で"export"ディレクトリは
    # 既に作成済みのため、ここで改めてmkdirする必要はない。
    out_path.write_bytes(data)
    return FileResponse(
        out_path, media_type=_EXPORT_MEDIA_TYPES[body.format], filename=out_path.name
    )
