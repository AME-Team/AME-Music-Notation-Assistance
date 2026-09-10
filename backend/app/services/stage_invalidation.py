"""ステージ無効化のユースケース層(#29, FR-14)。

上流ステージが実際に再実行(またはBeatGridEditor等での手動編集)された際に、
`domain.stages`の依存グラフに従って下流ステージの`analysis/{stage}.meta.json`
を削除する。成果物ファイル自体は残す(§6の設計意図): UIは古い結果を表示
しつつ、`project_service`の`stale`判定で「要再実行」を示せる。

**呼び出し契約(重要、#29-M2レビュー指摘)**: 呼び出し元(`worker/dsp_main.py`の
各`run_xxx_stage`)は、**自ステージ自身の`stage_metadata`を書き込むより前に**
`invalidate_downstream()`を呼ぶこと。逆順(自ステージのmeta書き込み後に
呼ぶ)だと、`invalidate_downstream()`がリトライ上限超過で例外送出しても、
次回実行時は`should_skip_stage`が自ステージを(meta.jsonが既に新しい状態
なので)スキップしてしまい、`invalidate_downstream()`が二度と呼ばれず
下流の無効化漏れが恒久的に残る。本モジュール単体でのfail-closed
(例外送出)は、呼び出し元がこの順序を守って初めて「次回再実行時に
リカバーする」という意味で機能する。
"""

from __future__ import annotations

import time
from pathlib import Path

from app.domain.stages import downstream_of
from app.infra import storage

# Windows(本アプリの対象OS、NFR-08')では、他プロセス/スレッドがファイルを
# 開いている間の削除は`PermissionError`(WinError 32、共有違反)になる。
# `storage.read_json`はファイルを短時間だけ開いて即座に閉じる設計のため、
# 短い間隔で数回リトライすれば十分回避できる想定(#29-M2レビュー指摘)。
_UNLINK_RETRY_ATTEMPTS = 3
_UNLINK_RETRY_DELAY_SEC = 0.05


def _unlink_if_exists(path: Path) -> bool:
    """`path`の削除を試み、実際に削除できたら`True`を返す。

    `exists()`確認後に`unlink()`しない(TOCTOU対策): 複数ジョブから並行して
    呼ばれる前提(dsp_main.pyの楽観的並行性制御と同じ思想)のため、存在確認から
    削除までの間に別プロセスが同じファイルを削除すると`FileNotFoundError`に
    なりうる。`unlink()`を直接試み、`FileNotFoundError`は「既に無効化済み」
    として無視する。

    `PermissionError`(Windowsで他プロセスがファイルを開いている間の削除、
    #29-M2レビュー指摘)は短い間隔でリトライする。全て失敗した場合は
    最後の例外をそのまま送出する(呼び出し元のジョブは異常終了し、次回の
    再実行に委ねる — fail-closed。無効化されないまま古いmeta.jsonが残る
    ことを、無言で見逃すよりも安全側に倒す)。
    """
    for attempt in range(_UNLINK_RETRY_ATTEMPTS):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except PermissionError:
            if attempt == _UNLINK_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_UNLINK_RETRY_DELAY_SEC)
    return False  # pragma: no cover — ループは必ずreturn/raiseで終わる


def invalidate_downstream(workspace_dir: Path, project_id: str, stage: str) -> list[str]:
    """`stage`の下流ステージ群の`meta.json`を削除し、無効化する。

    既にmeta.jsonが存在しない(未実行、または既に無効化済みの)下流ステージは
    スキップする。実際に無効化した(meta.jsonを削除した)ステージ名の一覧を
    返す(呼び出し元がログ/emit用に使う)。
    """
    invalidated: list[str] = []
    for downstream_stage in downstream_of(stage):
        meta_path = storage.stage_metadata_path(workspace_dir, project_id, downstream_stage)
        if _unlink_if_exists(meta_path):
            invalidated.append(downstream_stage)
    return invalidated
