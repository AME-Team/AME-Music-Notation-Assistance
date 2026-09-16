"""`AgentProvider`実装が共通で満たすべき契約テスト(#42 R-12)。

#48(ClaudeAgentProvider)・#52(OpenCodeProvider)実装時、このクラスを継承する
だけで同じ契約を検証できるようにする。クラス名を`Test`で始めないのは、
これ単体をpytestに直接収集させないため(具象サブクラスのみを収集対象とする、
`test_dummy_provider.py`の`TestDummyProviderContract`参照)。

各サブクラスは`provider()`をオーバーライドし、対象の`AgentProvider`実装を
返す。`AgentTask`のフィールド値そのものは本契約では検証しない(プロバイダ
ごとに解釈が異なりうるため)。ここで固定するのは、どの実装でも共通のはずの
「正規化されたイベント形」の骨格のみ。
"""

from __future__ import annotations

from pathlib import Path

from app.agent.provider import AgentEvent, AgentProvider, AgentTask


class ProviderContractTests:
    def provider(self) -> AgentProvider:
        raise NotImplementedError

    def _make_task(self, tmp_path: Path) -> AgentTask:
        return AgentTask(
            task_type="test",
            project_id="proj-1",
            prompt="do something",
            workspace=tmp_path,
        )

    async def test_start_returns_run_handle_with_id(self, tmp_path: Path) -> None:
        provider = self.provider()
        handle = await provider.start(self._make_task(tmp_path))
        assert handle.run_id

    async def test_stream_yields_events_ending_in_done_or_error(
        self, tmp_path: Path
    ) -> None:
        provider = self.provider()
        handle = await provider.start(self._make_task(tmp_path))
        events: list[AgentEvent] = [e async for e in provider.stream(handle.run_id)]

        assert events, "stream must yield at least one event"
        assert events[-1].kind in ("done", "error")
        for event in events:
            assert event.run_id == handle.run_id

    async def test_stream_events_have_monotonic_seq(self, tmp_path: Path) -> None:
        provider = self.provider()
        handle = await provider.start(self._make_task(tmp_path))
        events = [e async for e in provider.stream(handle.run_id)]

        seqs = [e.seq for e in events]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs))

    async def test_result_after_stream_reports_terminal_status(
        self, tmp_path: Path
    ) -> None:
        provider = self.provider()
        handle = await provider.start(self._make_task(tmp_path))
        async for _ in provider.stream(handle.run_id):
            pass

        result = await provider.result(handle.run_id)
        assert result.run_id == handle.run_id
        assert result.status in ("completed", "failed", "cancelled")

    async def test_cancel_before_stream_completes_is_reflected_in_result(
        self, tmp_path: Path
    ) -> None:
        provider = self.provider()
        handle = await provider.start(self._make_task(tmp_path))
        await provider.cancel(handle.run_id)
        async for _ in provider.stream(handle.run_id):
            pass

        result = await provider.result(handle.run_id)
        assert result.status == "cancelled"
