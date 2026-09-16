"""L2 Coding Agentプロバイダの一覧API(#42, 設計書§11.3)。

利用可能なプロバイダ(#42時点では`dummy`のみ、#48でclaude、#52でopencodeが
追加される)と、各々が「設定済み」(認証情報等が揃っており実行可能)かどうかを
返す。project_idに依存しないグローバルな情報のため、他の`api/*.py`と異なり
`/api/projects/{project_id}`配下ではなく`/api/agent`配下に置く(設計書§11.3の
パス`GET /api/agent/providers`にそのまま合わせられる)。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/api/agent", tags=["agent"])


class AgentProviderInfo(BaseModel):
    name: str
    configured: bool


@router.get("/providers", response_model=list[AgentProviderInfo])
def list_providers() -> list[AgentProviderInfo]:
    """#48/#52でプロバイダが増えるたびにここへ追記する想定の静的レジストリ。

    `dummy`は外部依存が無く常に実行可能なため`configured=True`固定。
    """
    return [AgentProviderInfo(name="dummy", configured=True)]
