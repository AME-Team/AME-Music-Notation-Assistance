"""Score IR取得API(#23, 設計書§11.5)。

`GET /score/preview.musicxml`(部分小節プレビュー)はM2完了条件に含まれない
ため未実装(ユーザー確認済み、将来PRで対応)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import ensure_project_exists, get_project_service, get_settings, read_score_or_404
from app.config import Settings
from app.services.project_service import ProjectService

router = APIRouter(prefix="/api/projects/{project_id}", tags=["score"])


@router.get("/score")
def get_score(
    project_id: str,
    service: ProjectService = Depends(get_project_service),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Stage 3(採譜)未実行なら404を返す。

    `async def` ではなく通常の `def` にする(`api/export.py`の`export_score`と
    同じ理由、#27-M2レビュー指摘): `ScoreService.read_score`はファイルI/Oを
    伴う同期処理であり、`async def`のままだとイベントループを直接ブロックし、
    SSEでのジョブ進捗配信など他の同時リクエストを止めてしまう。

    `response_model`に`domain.score.ScoreIR`を直接指定しない: そのネストする
    `TempoMapEntry`/`TimeSignatureEntry`が`api/schemas.py`(beatmap用)の同名
    クラスとOpenAPIコンポーネント名で衝突し、生成TSの型名が
    `app__api__schemas__TempoMapEntry`等へ改名されてしまう(#23-M2レビュー
    指摘)。M2のフロントエンドはこのエンドポイントの型付けを必要としない
    (設計書§11.5の完了条件外)ため、素の`dict`で返し衝突を避ける。
    """
    ensure_project_exists(project_id, service)
    score = read_score_or_404(project_id, settings)
    return score.model_dump(mode="json")
