"""ステージ依存グラフ(#29, FR-14, §6冒頭)。

各ステージは「入力アーティファクト → 出力アーティファクト」の変換であり、
パラメータ・入力ハッシュが不変ならスキップする(§6)。逆に、あるステージが
実際に再実行され新しい成果物を書いた場合、下流ステージの成果物はもはや
最新の入力を反映していないため無効化されなければならない(UC-3)。

ここでは依存関係を外部ライブラリに依存しない純粋なデータとして定義する。
実際のファイル削除(`analysis/{stage}.meta.json`)は`services/stage_invalidation.py`
が担う(`domain/`はinfra非依存の方針のため、ファイルI/Oはここに置かない)。
"""

from __future__ import annotations

# 直接の依存関係: キーのステージが実際に再実行されたら、値のステージ群を
# 無効化する。`export`はジョブ/ステージではなく同期エンドポイントであり
# 独自のmeta.jsonを持たないため、このグラフの対象外(#27の技術的決定)。
_DIRECT_DOWNSTREAM: dict[str, tuple[str, ...]] = {
    "separate": ("transcribe",),
    "beat": ("quantize",),
    "transcribe": ("quantize",),
}


def downstream_of(stage: str) -> tuple[str, ...]:
    """`stage`が無効化された際に連鎖して無効化される、全ての下流ステージ。

    推移的閉包を含む(例: `separate`→`transcribe`→`quantize`)。M2時点の
    グラフは2段までだが、将来ステージが増えても正しく辿れるよう幅優先探索で
    求める。戻り値はステージ名の重複が無い、依存の近い順のタプル。
    """
    result: list[str] = []
    seen = {stage}
    frontier = [stage]
    while frontier:
        current = frontier.pop(0)
        for downstream in _DIRECT_DOWNSTREAM.get(current, ()):
            if downstream not in seen:
                seen.add(downstream)
                result.append(downstream)
                frontier.append(downstream)
    return tuple(result)
