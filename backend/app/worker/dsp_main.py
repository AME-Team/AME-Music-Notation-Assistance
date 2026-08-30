"""DSP Worker エントリ(#13)。M0 ではダミーステージのみ実装する。

API サーバとは別プロセスで起動される(NFR-04)。進捗は stdout に JSON Lines で
1行ずつ出力し、親プロセス(job_service)がそれを読み取って SSE に流す。

使い方:
    python -m app.worker.dsp_main <job_id> <stage> <params_json>

params_json に `{"crash_at": 0.4}` を含めると、指定した進捗到達時に例外を投げて
異常終了する(#13完了条件: 「Workerがクラッシュしてもjobがfailedとして記録され、
APIサーバは生き残る」ことをテストするため)。
"""

from __future__ import annotations

import io
import json
import sys
import time


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def run_dummy_stage(job_id: str, params: dict) -> None:
    crash_at = params.get("crash_at")
    steps = 5
    for i in range(steps + 1):
        progress = i / steps
        if crash_at is not None and progress >= crash_at:
            emit(
                {
                    "job_id": job_id,
                    "stage": "dummy",
                    "progress": progress,
                    "message": "simulated crash",
                }
            )
            raise RuntimeError("simulated worker crash (crash_at reached)")
        emit(
            {
                "job_id": job_id,
                "stage": "dummy",
                "progress": progress,
                "message": f"dummy stage: {int(progress * 100)}%",
            }
        )
        time.sleep(0.05)


def main() -> int:
    # #79: Windows コンソールの既定コードページに関わらず UTF-8 で出力する。
    # sys.stdout/stderr は typeshed 上 TextIO 型で reconfigure() を持たないため、
    # 実体である TextIOWrapper の場合のみ呼び出す(pytest の capsys 等、
    # TextIOWrapper でないストリームに差し替えられていても worker を落とさない)。
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    if isinstance(sys.stderr, io.TextIOWrapper):
        sys.stderr.reconfigure(encoding="utf-8")

    job_id, stage, params_json = sys.argv[1], sys.argv[2], sys.argv[3]
    params = json.loads(params_json) if params_json else {}

    if stage != "dummy":
        emit(
            {
                "job_id": job_id,
                "stage": stage,
                "progress": 0.0,
                "message": f"unknown stage: {stage}",
            }
        )
        return 1

    try:
        run_dummy_stage(job_id, params)
    except Exception as exc:  # noqa: BLE001 — ワーカーは失敗を exit code で伝える設計
        print(f"worker error: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
