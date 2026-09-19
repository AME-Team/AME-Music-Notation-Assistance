"""Stage 3 AMT: 学習済みモデルファイル取得の共通ヘルパー(#56レビュー指摘)。

`piano.ensure_checkpoint`(Zenodo, サイズ閾値検証)と`guitar.ensure_onnx_model`
(GitHub, SHA256全体検証)は、検証方式こそ異なるが「httpx取得+FileLockに
よるプロセス間直列化+アトミック配置」というダウンロード基盤は同一だった。
両者が独立に同じロジックを再実装していると、タイムアウト/リトライ/一時
ファイル掃除等の修正が片方にしか反映されず不整合になるリスクがあるため、
ここへ一元化する。検証方式自体は呼び出し元が`is_valid`コールバックで注入する。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path


def fetch_verified_model(
    url: str,
    dest: Path,
    *,
    is_valid: Callable[[Path], bool],
    describe_failure: Callable[[Path], str] | None = None,
    timeout: float = 300.0,
) -> Path:
    """`dest`に`is_valid`を満たすファイルが無ければ`url`から取得する。

    プロセス間ロック(#24-M2レビュー2巡目の指摘を踏襲): 複数プロセスが初回
    ダウンロードに同時に入ると、ロック無しでは重複してダウンロードして
    しまう。`filelock`でダウンロード区間を直列化し、ロック取得後にもう一度
    `is_valid`を確認することで、待っていた他プロセスは再ダウンロードせず
    そのまま再利用できるようにする。ロック保持中は自分以外がダウンロード中
    であることはあり得ないため、残存する`.part`(前回いずれかのプロセスが
    SIGKILL等でクラッシュした際の孤立ファイル)もここで安全に掃除できる。

    ダウンロードは一時ファイルへ書き込み、`is_valid`で検証してから
    `os.replace`(POSIX/Windowsともにアトミック)で`dest`へ置き換える。
    検証に失敗した場合は`RuntimeError`を送出する(通信が途中で切れて破損
    したファイルがそのまま`dest`に残ると、次回起動時も`is_valid`が偽になり
    毎回無駄な再ダウンロードを繰り返すため、`dest`へは絶対に置かない)。

    エラーメッセージは「`{url}`から取得したファイルの検証に失敗した」という
    定型の前置き・末尾(`download likely truncated or corrupted`)をここで
    一元管理し、`describe_failure`はモデル固有の診断情報(実測サイズ・期待値・
    ハッシュ値等)の断片だけを返す(#56レビュー2巡目の指摘: 呼び出し元に
    メッセージ全文を委ねる設計だと、各呼び出し元がURL文脈や定型文言を
    重複して書く必要がありドリフトの温床になるため、共通部分は一元化し
    可変部分だけをコールバックへ委譲する設計に改めた)。
    """
    if dest.exists() and is_valid(dest):
        return dest

    import httpx
    from filelock import FileLock

    dest.parent.mkdir(parents=True, exist_ok=True)
    lock_path = dest.parent / f"{dest.name}.lock"
    with FileLock(str(lock_path)):
        if dest.exists() and is_valid(dest):
            return dest

        for stale_part in dest.parent.glob(f"{dest.name}.*.part"):
            stale_part.unlink(missing_ok=True)

        tmp_path = dest.parent / f"{dest.name}.{os.getpid()}.part"
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=timeout) as resp:
                resp.raise_for_status()
                with tmp_path.open("wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                        f.write(chunk)
            if not is_valid(tmp_path):
                detail = (
                    describe_failure(tmp_path) if describe_failure else "size/checksum mismatch"
                )
                raise RuntimeError(
                    f"downloaded file from {url} failed validation ({detail}); "
                    "download likely truncated or corrupted"
                )
            tmp_path.replace(dest)
        finally:
            tmp_path.unlink(missing_ok=True)
    return dest
