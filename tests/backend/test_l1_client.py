"""#104: `pipeline/refine/l1_client.py`(claude CLI経由のStructured Outputs呼び出し)のテスト。

実際に`claude`バイナリは一切呼び出さない: `subprocess.run`をモックし、CLIの
`--output-format json`が返すJSONペイロードを模した戻り値を差し替えて、
配線(コマンド構築・レスポンス変換・エラー処理)のみを検証する。
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from app.pipeline.refine.l1_chunker import (
    ChunkBars,
    ChunkContext,
    ChunkInput,
    ChunkPart,
)
from app.pipeline.refine.l1_client import (
    L1ChunkResponse,
    L1ClientError,
    call_l1_chunk,
    is_claude_cli_available,
)


def _chunk() -> ChunkInput:
    return ChunkInput(
        context=ChunkContext(
            key_estimate="C major",
            key_confidence=0.8,
            time_signature="4/4",
            tempo_bpm=120.0,
            part=ChunkPart(id="piano", instrument="Piano", staves=2),
            chord_hints=[],
            bars=ChunkBars(target=(1, 4), context_before=[], context_after=[5]),
        ),
        notes=[],
    )


def _cli_payload(
    *,
    structured_output: dict | None,
    is_error: bool = False,
    result: str = "ok",
    usage: dict | None = None,
    total_cost_usd: float = 0.01,
) -> str:
    payload = {
        "is_error": is_error,
        "result": result,
        "usage": usage
        or {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 10,
        },
        "total_cost_usd": total_cost_usd,
    }
    if structured_output is not None:
        payload["structured_output"] = structured_output
    return json.dumps(payload)


def _mock_run(*, stdout: str, returncode: int = 0, stderr: str = ""):
    completed = subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )
    return patch(
        "app.pipeline.refine.l1_client.subprocess.run",
        return_value=completed,
    )


def test_call_l1_chunk_returns_parsed_output_and_usage() -> None:
    structured = {"bar_range": [1, 4], "decisions": [], "bar_annotations": []}
    with (
        _mock_run(stdout=_cli_payload(structured_output=structured)) as mock_run,
    ):
        result = call_l1_chunk(_chunk(), system_prompt="system", song_context="context")

    assert result.output == L1ChunkResponse(bar_range=[1, 4], decisions=[])
    assert result.usage == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 10,
    }
    assert result.cost_usd == 0.01

    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "claude"
    assert "--json-schema" in cmd
    assert "--allowedTools" in cmd
    assert "StructuredOutput" in cmd
    assert "--restricted" in cmd
    # 楽曲コンテキスト+チャンクJSONはコマンドライン引数ではなく標準入力経由
    # (#104 Gate2レビュー指摘: Windowsのコマンドライン長上限を回避するため)。
    assert "context" not in cmd
    assert mock_run.call_args.kwargs["input"].startswith("context\n\n")


def test_call_l1_chunk_passes_model_and_effort() -> None:
    structured = {"bar_range": [1, 4], "decisions": [], "bar_annotations": []}
    with (
        _mock_run(stdout=_cli_payload(structured_output=structured)) as mock_run,
    ):
        call_l1_chunk(
            _chunk(),
            system_prompt="system",
            song_context="context",
            model="claude-sonnet-5",
            effort="medium",
        )

    cmd = mock_run.call_args.args[0]
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-5"
    assert cmd[cmd.index("--effort") + 1] == "medium"
    assert cmd[cmd.index("--append-system-prompt") + 1] == "system"


def test_call_l1_chunk_raises_when_binary_missing() -> None:
    """`call_l1_chunk`自体は`is_claude_cli_available()`を呼ばない(#104 Gate2

    レビュー指摘、2巡目: 呼び出し元(`api/refine.py`)がrun開始前に一度だけ
    ゲートするため、チャンクごとに`claude auth status`を再実行する
    オーバーヘッドを避ける)。CLIが実際に見つからない場合は`subprocess.run`
    自体が`FileNotFoundError`(`OSError`のサブクラス)を送出し、既存の
    `except (OSError, subprocess.TimeoutExpired)`で`L1ClientError`に変換される。
    """
    with (
        patch(
            "app.pipeline.refine.l1_client.subprocess.run",
            side_effect=FileNotFoundError("claude"),
        ),
        pytest.raises(L1ClientError),
    ):
        call_l1_chunk(_chunk(), system_prompt="system", song_context="context")


def test_call_l1_chunk_raises_on_nonzero_exit() -> None:
    with (
        _mock_run(stdout="", returncode=1, stderr="boom"),
        pytest.raises(L1ClientError),
    ):
        call_l1_chunk(_chunk(), system_prompt="system", song_context="context")


def test_call_l1_chunk_raises_on_non_json_stdout() -> None:
    with (
        _mock_run(stdout="not json"),
        pytest.raises(L1ClientError),
    ):
        call_l1_chunk(_chunk(), system_prompt="system", song_context="context")


def test_call_l1_chunk_raises_when_is_error() -> None:
    with (
        _mock_run(
            stdout=_cli_payload(structured_output=None, is_error=True, result="refused")
        ),
        pytest.raises(L1ClientError),
    ):
        call_l1_chunk(_chunk(), system_prompt="system", song_context="context")


def test_call_l1_chunk_raises_when_structured_output_missing() -> None:
    with (
        _mock_run(stdout=_cli_payload(structured_output=None)),
        pytest.raises(L1ClientError),
    ):
        call_l1_chunk(_chunk(), system_prompt="system", song_context="context")


def test_call_l1_chunk_raises_on_schema_validation_failure() -> None:
    # `decisions`が不正な形(オブジェクトではなく文字列のリスト)。
    structured = {
        "bar_range": [1, 4],
        "decisions": ["not-a-decision"],
        "bar_annotations": [],
    }
    with (
        _mock_run(stdout=_cli_payload(structured_output=structured)),
        pytest.raises(L1ClientError),
    ):
        call_l1_chunk(_chunk(), system_prompt="system", song_context="context")


def test_call_l1_chunk_raises_on_malformed_usage_field() -> None:
    """`usage`がオブジェクトでない場合、TypeErrorをL1ClientErrorへ変換して隔離する

    (#104 Gate2レビュー指摘: 未捕捉のTypeError/ValueErrorが
    `run_l1_batch`の`executor.map`イテレーションを止め、他チャンクの結果ごと
    失われるのを防ぐ)。
    """
    structured = {"bar_range": [1, 4], "decisions": [], "bar_annotations": []}
    payload = json.loads(_cli_payload(structured_output=structured))
    payload["usage"] = "not-an-object"
    with (
        _mock_run(stdout=json.dumps(payload)),
        pytest.raises(L1ClientError),
    ):
        call_l1_chunk(_chunk(), system_prompt="system", song_context="context")


def test_call_l1_chunk_raises_on_malformed_cost_field() -> None:
    structured = {"bar_range": [1, 4], "decisions": [], "bar_annotations": []}
    payload = json.loads(_cli_payload(structured_output=structured))
    payload["total_cost_usd"] = "not-a-number"
    with (
        _mock_run(stdout=json.dumps(payload)),
        pytest.raises(L1ClientError),
    ):
        call_l1_chunk(_chunk(), system_prompt="system", song_context="context")


def test_call_l1_chunk_raises_when_top_level_json_is_not_an_object() -> None:
    """`stdout`が有効なJSONだがオブジェクトでない(#104 Gate2レビュー指摘、2巡目:

    `payload.get(...)`が`AttributeError`を送出し`L1ClientError`を経由せず
    伝播していた実バグ)。
    """
    with (
        _mock_run(stdout=json.dumps(None)),
        pytest.raises(L1ClientError),
    ):
        call_l1_chunk(_chunk(), system_prompt="system", song_context="context")


def test_call_l1_chunk_raises_on_subprocess_timeout() -> None:
    with (
        patch(
            "app.pipeline.refine.l1_client.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=180),
        ),
        pytest.raises(L1ClientError),
    ):
        call_l1_chunk(_chunk(), system_prompt="system", song_context="context")


def test_is_claude_cli_available_false_when_binary_missing() -> None:
    with patch("app.pipeline.refine.l1_client.shutil.which", return_value=None):
        assert is_claude_cli_available() is False


def test_is_claude_cli_available_false_when_not_logged_in() -> None:
    """`claude` CLIはPATH上にあるが未認証(#104 Gate2レビュー指摘)。

    PATH上の存在確認だけでは検出できず、`claude auth status`の
    `loggedIn: false`まで見て初めてゲートで弾けることを確認する。
    """
    with (
        patch(
            "app.pipeline.refine.l1_client.shutil.which", return_value="/usr/bin/claude"
        ),
        _mock_run(stdout=json.dumps({"loggedIn": False})),
    ):
        assert is_claude_cli_available() is False


def test_is_claude_cli_available_true_when_logged_in() -> None:
    with (
        patch(
            "app.pipeline.refine.l1_client.shutil.which", return_value="/usr/bin/claude"
        ),
        _mock_run(stdout=json.dumps({"loggedIn": True})),
    ):
        assert is_claude_cli_available() is True


def test_is_claude_cli_available_false_when_auth_status_is_not_an_object() -> None:
    """`claude auth status --json`が有効なJSONだがオブジェクトでない場合

    (#104 Gate2レビュー指摘、2巡目: `.get("loggedIn")`が`AttributeError`を
    送出しゲート関数自体が例外で落ちていた実バグ)。
    """
    with (
        patch(
            "app.pipeline.refine.l1_client.shutil.which", return_value="/usr/bin/claude"
        ),
        _mock_run(stdout=json.dumps(None)),
    ):
        assert is_claude_cli_available() is False


def test_is_claude_cli_available_false_when_auth_check_errors() -> None:
    with (
        patch(
            "app.pipeline.refine.l1_client.shutil.which", return_value="/usr/bin/claude"
        ),
        _mock_run(stdout="", returncode=1),
    ):
        assert is_claude_cli_available() is False


def test_l1_chunk_response_reuses_domain_decision_model() -> None:
    """`decisions`が`domain.invariants.Decision`をそのまま再利用していることの

    回帰確認(検証層と同一スキーマで受け取ることで変換ミスを防ぐ設計)。
    """
    from app.domain.invariants import Decision

    response = L1ChunkResponse(
        bar_range=[1, 4],
        decisions=[Decision(note_id=1, action="delete", reason="test")],
    )
    assert isinstance(response.decisions[0], Decision)
