from __future__ import annotations

from pathlib import Path

from agent_contract import ProviderContractTests
from app.agent.provider import AgentTask
from app.agent.providers.dummy import AgentRunNotFoundError, DummyAgentProvider


class TestDummyProviderContract(ProviderContractTests):
    def provider(self) -> DummyAgentProvider:
        return DummyAgentProvider()


async def test_full_run_produces_expected_event_sequence(tmp_path: Path) -> None:
    """#42完了条件: ダミープロバイダで start→stream→result の一連が動作すること。"""
    provider = DummyAgentProvider()
    task = AgentTask(
        task_type="test", project_id="proj-1", prompt="do something", workspace=tmp_path
    )

    handle = await provider.start(task)
    events = [e async for e in provider.stream(handle.run_id)]

    assert [e.kind for e in events] == ["thinking", "tool_use", "tool_result", "done"]
    assert events[1].tool_name == "score_read"
    assert events[2].tool_name == "score_read"
    assert events[-1].usage is not None
    assert events[-1].usage.input_tokens > 0

    result = await provider.result(handle.run_id)
    assert result.status == "completed"
    assert result.run_id == handle.run_id


async def test_unknown_run_id_raises(tmp_path: Path) -> None:
    provider = DummyAgentProvider()

    try:
        async for _ in provider.stream("does-not-exist"):
            pass
        raise AssertionError("expected AgentRunNotFoundError")
    except AgentRunNotFoundError:
        pass

    try:
        await provider.cancel("does-not-exist")
        raise AssertionError("expected AgentRunNotFoundError")
    except AgentRunNotFoundError:
        pass

    try:
        await provider.result("does-not-exist")
        raise AssertionError("expected AgentRunNotFoundError")
    except AgentRunNotFoundError:
        pass
