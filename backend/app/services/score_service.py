"""Score IR の読み書きユースケース層(#23, 設計書§10.3)。

`score/current.json` の唯一の読み書き経路。`storage.write_json` のアトミック書き込みを
そのまま使う(#23時点ではまだ複数プロセスからの同時書き込みは発生しないが、Stage 3/4が
別プロセス(DSP Worker)から書くようになるM2内で必要になる前提を先取りしておく)。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.domain.migrations import migrate_to_current
from app.domain.score import ScoreIR
from app.infra import storage

if TYPE_CHECKING:
    from app.domain.harmony import ChordEntry


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

    @staticmethod
    def ensure_chords(score: ScoreIR, *, force_recompute: bool = False) -> list[ChordEntry]:
        """ScoreIR に最新・有効なコード進行 (ChordEntry) を保証する。

        `force_recompute=False` かつ `score.chords` が存在する場合は既存のコード進行を返す。
        未設定または `force_recompute=True` の場合は `pipeline.harmony.estimate_chords_for_score`
        でノートからコードを推定し、`score.chords` を更新して返す。
        """
        if force_recompute or not score.chords:
            from app.pipeline.harmony import estimate_chords_for_score

            try:
                score.chords = estimate_chords_for_score(score)
            except Exception as exc:  # noqa: BLE001
                print(f"[ScoreService] warning: chord estimation failed: {exc}", file=sys.stderr)
        return score.chords

    def write_score(self, project_id: str, score: ScoreIR, *, update_chords: bool = True) -> None:
        """Score IR を `score/current.json` へ書き込む。

        `update_chords=True` (既定) の場合、保存前にノート配置からコード進行を自動再推定
        (`ensure_chords(..., force_recompute=True)`) して永続化する。
        これにより、quantize 完了時だけでなく `/score/ops`、undo/redo、L1 diff 適用など
        ノート編集のあらゆる経路で最新のコード進行が常に維持される。
        """
        if update_chords:
            self.ensure_chords(score, force_recompute=True)

        path = storage.score_current_path(self.workspace_dir, project_id)
        # `mode="json"` でPydanticにJSON互換型(Literal等)へ変換させてから書く。
        # `storage.write_json` 自体は `json.dump` を呼ぶだけで、Pydanticモデルを
        # 直接受け付けるようにはなっていないため。
        storage.write_json(path, score.model_dump(mode="json"))
