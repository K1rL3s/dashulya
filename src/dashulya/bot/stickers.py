"""Resolve Dashulya's published emotion stickers without exposing file IDs to AI."""

import asyncio

from aiogram import Bot
from cachetools import TTLCache

from dashulya.domain.messages import StickerEmotion

_SET_NAME = "DashulyaAI"
# The published pack's emoji are the stable lookup keys, independent of its order.
_EMOJI: dict[StickerEmotion, str] = {
    "joy": "😃",
    "love": "😘",
    "sad": "😢",
    "angry": "😠",
    "surprised": "😮",
    "skeptical": "🤨",
}


class TelegramStickers:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._cache: TTLCache[str, dict[StickerEmotion, str]] = TTLCache(
            maxsize=1, ttl=300
        )
        self._lock = asyncio.Lock()

    async def file_id(self, emotion: StickerEmotion) -> str:
        """Look up only the configured pack; an unavailable/ambiguous emotion fails."""
        async with self._lock:
            files = self._cache.get(_SET_NAME)
            if files is None:
                async with asyncio.timeout(10):
                    pack = await self._bot.get_sticker_set(_SET_NAME)
                files = {}
                for name, emoji in _EMOJI.items():
                    matches = [
                        sticker.file_id
                        for sticker in pack.stickers
                        if sticker.type == "regular" and sticker.emoji == emoji
                    ]
                    if len(matches) == 1:
                        files[name] = matches[0]
                self._cache[_SET_NAME] = files
        if emotion not in files:
            error_message = "DashulyaAI has no unique sticker for the selected emotion."
            raise ValueError(error_message)
        return files[emotion]
