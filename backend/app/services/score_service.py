"""Score IR の読み書きユースケース層(#23, 設計書§10.3)。

`score/current.json` の唯一の読み書き経路。`storage.write_json` のアトミック書き込みを
そのまま使う(#23時点ではまだ複数プロセスからの同時書き込みは発生しないが、Stage 3/4が
別プロセス(DSP Worker)から書くようになるM2内で必要になる前提を先取りしておく)。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.domain.migrations import migrate_to_current
from app.domain.score import ScoreIR
from app.infra import storage


class ScoreNotFoundError(LookupError):
    pass


@dataclass
class ScoreService:
    workspace_dir: Path

    def read_score(self, project_id: str) -> ScoreIR:
        score = self.read_score_optional(project_id)
        if score is None:
            raise ScoreNotFoundError(project_id)
        return score

    def read_score_optional(self, project_id: str) -> ScoreIR | None:
        """Score IR が未作成(Stage 3 未実行)なら `None` を返す。"""
        path = storage.score_current_path(self.workspace_dir, project_id)
        if not path.exists():
            return None
        raw = storage.read_json(path)
        migrated = migrate_to_current(raw)
        return ScoreIR.model_validate(migrated)

    def write_score(self, project_id: str, score: ScoreIR) -> None:
        path = storage.score_current_path(self.workspace_dir, project_id)
        # `mode="json"` でPydanticにJSON互換型(Literal等)へ変換させてから書く。
        # `storage.write_json` 自体は `json.dump` を呼ぶだけで、Pydanticモデルを
        # 直接受け付けるようにはなっていないため。
        storage.write_json(path, score.model_dump(mode="json"))
