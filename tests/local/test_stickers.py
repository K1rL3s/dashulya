import asyncio
import json
from contextlib import AsyncExitStack
from typing import get_args
from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot
from pydantic import TypeAdapter, ValidationError

from dashulya.bot.stickers import TelegramStickers
from dashulya.domain.messages import Reply, ReplyBatch, StickerEmotion
from tests.support import dashulya_sticker_set


@pytest.mark.parametrize("emotion", get_args(StickerEmotion))
def test_sticker_reply_schema_accepts_only_known_emotions(
    emotion: StickerEmotion,
) -> None:
    result = TypeAdapter(ReplyBatch).validate_json(
        json.dumps({"messages": [{"text": "", "sticker": emotion}]})
    )
    assert result == ReplyBatch((Reply(sticker=emotion),))


@pytest.mark.parametrize(
    "message",
    [
        {"text": "", "sticker": "https://example.com/sticker.webp"},
        {"text": "", "sticker": "joy-file"},
        {"text": "", "sticker": "😃"},
        {"text": "Hello", "sticker": "joy"},
    ],
)
def test_sticker_reply_schema_rejects_arbitrary_files_and_mixed_content(
    message: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(ReplyBatch).validate_json(json.dumps({"messages": [message]}))


@pytest.fixture
async def bot(cleanup: AsyncExitStack) -> Bot:
    bot = Bot("123:TEST")
    cleanup.push_async_callback(bot.session.close)
    return bot


async def test_published_emotions_resolve_independently_of_order_and_share_cache(
    bot: Bot,
) -> None:
    pack = dashulya_sticker_set()
    pack.stickers.reverse()
    lookup = AsyncMock(return_value=pack)
    stickers = TelegramStickers(bot)
    emotions = get_args(StickerEmotion)
    with patch.object(bot, "get_sticker_set", lookup):
        files = await asyncio.gather(*(stickers.file_id(name) for name in emotions))
        assert files == [name + "-file" for name in emotions]
        assert await stickers.file_id("joy") == "joy-file"
    lookup.assert_awaited_once_with("DashulyaAI")


@pytest.mark.parametrize("duplicate", [False, True])
async def test_unavailable_or_ambiguous_emotion_does_not_select_another_sticker(
    bot: Bot, duplicate: bool
) -> None:
    pack = dashulya_sticker_set()
    if duplicate:
        pack.stickers.append(pack.stickers[0].model_copy(update={"file_id": "another"}))
    else:
        pack.stickers.pop(0)
    with patch.object(bot, "get_sticker_set", AsyncMock(return_value=pack)):
        stickers = TelegramStickers(bot)
        with pytest.raises(ValueError, match="no unique sticker"):
            await stickers.file_id("joy")
        assert await stickers.file_id("love") == "love-file"


async def test_lookup_failure_propagates_and_is_not_cached(bot: Bot) -> None:
    lookup = AsyncMock(side_effect=[TimeoutError, dashulya_sticker_set()])
    with patch.object(bot, "get_sticker_set", lookup):
        stickers = TelegramStickers(bot)
        with pytest.raises(TimeoutError):
            await stickers.file_id("joy")
        assert lookup.await_count == 1
        assert await stickers.file_id("joy") == "joy-file"
    assert lookup.await_count == 2


async def test_cancelled_lookup_propagates_and_does_not_poison_cache(bot: Bot) -> None:
    lookup = AsyncMock(side_effect=[asyncio.CancelledError, dashulya_sticker_set()])
    with patch.object(bot, "get_sticker_set", lookup):
        stickers = TelegramStickers(bot)
        with pytest.raises(asyncio.CancelledError):
            await stickers.file_id("joy")
        assert await stickers.file_id("joy") == "joy-file"
