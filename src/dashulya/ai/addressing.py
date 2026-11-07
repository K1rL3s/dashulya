import asyncio
import json
import re

from ag2 import Agent

from dashulya.ai.message_format import external_input, input_source
from dashulya.domain.messages import (
    AddressingDecision,
    ContinuationAction,
    GroupMessage,
)

_AGENT_NAME_PATTERN = re.compile(
    r"(?<!\w)(?:дашул(?:ь|я|и|е|ю|ей|ею)|dashulya)(?!\w)",  # ruff: ignore[ambiguous-unicode-character-string] -- Russian case endings and vocative.
    re.IGNORECASE,
)


class GroupAddressing:
    def __init__(
        self,
        agent: Agent[bool],
        *,
        continuation_agent: Agent[ContinuationAction],
        bot_id: int,
        bot_username: str,
        owner_id: int | None = None,
    ) -> None:
        self._agent = agent
        self._continuation_agent = continuation_agent
        self._bot_id = bot_id
        self._bot_username = bot_username.casefold()
        self._owner_id = owner_id

    async def should_reply(
        self,
        message: GroupMessage,
        *,
        recent_conversation: list[dict[str, str]],
    ) -> bool:
        return (
            await self.decide(message, recent_conversation=recent_conversation)
        ).should_reply

    async def decide(
        self,
        message: GroupMessage,
        *,
        recent_conversation: list[dict[str, str]],
    ) -> AddressingDecision:
        original = message.reply_to
        if (
            original is not None
            and original.sender_is_bot
            and original.sender_id == self._bot_id
        ):
            return AddressingDecision(True, "reply_to_bot")
        if self._bot_id in message.mentioned_user_ids or any(
            username.casefold() == self._bot_username
            for username in message.mentioned_usernames
        ):
            return AddressingDecision(True, "explicit_mention")
        if _AGENT_NAME_PATTERN.search(message.text) is not None:
            decision = await self._classify(self._agent, message, recent_conversation)
            if decision is None:
                return AddressingDecision(False, "classifier_no_answer")
            if decision is True:
                return AddressingDecision(True, "classifier_accepted")
            return AddressingDecision(False, "classifier_rejected")
        if original is not None:
            return AddressingDecision(False, "reply_to_other")
        recent_answer = any(
            item.get("role") == "assistant" for item in recent_conversation[-4:]
        )
        if not recent_answer:
            return AddressingDecision(False, "name_not_found")
        action = await self._classify(
            self._continuation_agent, message, recent_conversation
        )
        if action is None:
            return AddressingDecision(False, "continuation_no_answer")
        if action is ContinuationAction.TEXT:
            return AddressingDecision(True, "continuation_accepted")
        if action is ContinuationAction.IGNORE:
            return AddressingDecision(False, "continuation_rejected")
        return AddressingDecision(False, "continuation_reaction", reaction=action.value)

    async def _classify[T](
        self,
        agent: Agent[T],
        message: GroupMessage,
        recent_conversation: list[dict[str, str]],
    ) -> T | None:
        # Each classification has a fresh stream; decisions never enter reply history.
        async with asyncio.timeout(12):
            reply = await agent.ask(
                external_input(
                    json.dumps(
                        {
                            "current_message": message.context(),
                            "recent_conversation": recent_conversation,
                        },
                        ensure_ascii=False,
                    ),
                    input_source(
                        message.sender_id,
                        owner_id=self._owner_id,
                        is_bot=message.sender_is_bot,
                    ),
                )
            )
            return await reply.content()
