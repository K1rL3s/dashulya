import json

import pytest
from ag2.testing import TestConfig, TrackingConfig
from pydantic import ValidationError

from dashulya.ai.agents.factory import create_continuation_agent
from dashulya.domain.messages import ContinuationAction


@pytest.mark.parametrize("action", list(ContinuationAction))
async def test_continuation_agent_selects_one_closed_action(
    action: ContinuationAction,
) -> None:
    config = TrackingConfig(TestConfig(json.dumps({"data": action})))
    agent = create_continuation_agent(config, timezone="Asia/Tokyo")

    assert await (await agent.ask("A follow-up message")).content() is action

    assert agent.name == "group_thread_continuation"
    assert config.calls[0].tools == ()
    prompt = "\n".join(config.calls[0].prompt)
    assert "<thread_continuation_workflow>" in prompt
    assert "<addressing_workflow>" not in prompt
    assert "<input_trust>" in prompt
    assert "<message_format>" in prompt
    assert prompt.count("Message timezone: Asia/Tokyo.") == 1


@pytest.mark.parametrize("invalid", ["🤡", "not_an_action", True])
async def test_continuation_agent_rejects_unlisted_actions(invalid: str | bool) -> None:
    agent = create_continuation_agent(TestConfig(json.dumps({"data": invalid})))
    reply = await agent.ask("A follow-up message")
    with pytest.raises(ValidationError):
        await reply.content()
