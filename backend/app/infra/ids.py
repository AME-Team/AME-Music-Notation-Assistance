"""#79: Windows の MAX_PATH(260)対策として、ID は短い固定長にする。

`workspace/{project_id}/agent/{run_id}/...` のように階層が深いパスがあるため、
UUID(36文字)ではなく base32 の短い ID を使う。
"""

from __future__ import annotations

import os
import time

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32 (紛らわしい文字を除外)


def new_id(prefix: str) -> str:
    """`{prefix}_` + 12文字の短縮ID。例: `proj_8f3k2m9qA0zx`。"""
    raw = int.from_bytes(os.urandom(8), "big") ^ (time.time_ns() & 0xFFFFFFFFFFFFFFFF)
    chars = []
    n = raw
    for _ in range(12):
        n, rem = divmod(n, len(_ALPHABET))
        chars.append(_ALPHABET[rem])
    return f"{prefix}_{''.join(chars)}"
