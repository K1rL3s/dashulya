import logging
from dataclasses import dataclass
from time import perf_counter

from aiogram import Bot
from aiogram.types import Message, ReplyParameters

from dashulya.ai.dialogue import Dialogue
from dashulya.bot.chat_actions import typing
from dashulya.bot.formatting import render_markdown
from dashulya.bot.images import ImageInputError, TelegramImages
from dashulya.bot.messages import conversation_key
from dashulya.bot.stickers import TelegramStickers
from dashulya.domain.messages import ImageAttachment, ImageOmission, Reply, ReplyRequest
from dashulya.observability import exception_fields

logger = logging.getLogger(__name__)


@dataclass
class _DeliveryProgress:
    chunk_count: int = 0
    stage: str = "render"


class ReplyDelivery:
    def __init__(
        self,
        bot: Bot,
        dialogue: Dialogue,
        images: TelegramImages,
        stickers: TelegramStickers,
    ) -> None:
        self._bot = bot
        self._dialogue = dialogue
        self._images = images
        self._stickers = stickers

    async def reply(self, message: Message) -> None:
        async with self._dialogue.delivery(conversation_key(message)):
            await self._reply(message)

    async def _load_images(
        self, message: Message
    ) -> tuple[ImageAttachment | ImageOmission, ...]:
        started = perf_counter()
        images = await self._images.load(message)
        logger.info(
            "AI media loaded",
            extra={
                "chat_id": message.chat.id,
                "message_id": message.message_id,
                "duration_ms": round((perf_counter() - started) * 1000),
            },
        )
        return images

    async def _reply(self, message: Message) -> None:
        # Owner access and addressing have already succeeded before any download.
        assert message.from_user is not None
        started = perf_counter()
        context: dict[str, object] = {
            "chat_id": message.chat.id,
            "message_id": message.message_id,
        }
        logger.info("AI request started", extra=context)
        async with typing(self._bot, message):
            try:
                images = await self._load_images(message) if message.chat.id > 0 else ()
                reply = await self._dialogue.reply(
                    ReplyRequest(
                        conversation_key(message),
                        message.text
                        or message.caption
                        or "Please respond to this media in the context of our conversation.",
                        user_id=message.from_user.id,
                        user_name=message.from_user.full_name,
                        sent_at=message.date,
                        message_id=message.message_id,
                        images=images,
                        reply_to_id=message.reply_to_message.message_id
                        if message.reply_to_message is not None
                        else None,
                    )
                )
                messages = reply.messages
                logger.info(
                    "AI reply generated",
                    extra={
                        **context,
                        "duration_ms": round((perf_counter() - started) * 1000),
                        "message_count": len(messages),
                    },
                )
            except ImageInputError as error:
                messages = (Reply(str(error)),)
            except Exception as error:
                # Provider errors can contain request data; keep those out of logs and replies.
                logger.warning(
                    "AI reply failed",
                    extra={
                        **context,
                        **exception_fields(error),
                        "duration_ms": round((perf_counter() - started) * 1000),
                    },
                )
                messages = (
                    Reply(
                        "I couldn't get an AI reply right now. Please try again in a moment."
                    ),
                )
        if not messages:
            # Reaction outcomes were already handled within the agent's turn.
            return
        delivery_started = perf_counter()
        progress = _DeliveryProgress()
        try:
            await self._deliver(message, messages, progress)
        except Exception as error:
            logger.warning(
                "AI reply delivery failed",
                extra={
                    **context,
                    **exception_fields(error),
                    "message_count": len(messages),
                    "chunk_count": progress.chunk_count,
                    "delivery_stage": progress.stage,
                    "duration_ms": round((perf_counter() - delivery_started) * 1000),
                },
            )
            raise
        logger.info(
            "AI reply delivered",
            extra={
                **context,
                "duration_ms": round((perf_counter() - delivery_started) * 1000),
                "message_count": len(messages),
                "chunk_count": progress.chunk_count,
            },
        )

    async def _deliver(
        self, message: Message, messages: tuple[Reply, ...], progress: _DeliveryProgress
    ) -> None:
        for item in messages:
            reply_parameters = ReplyParameters(
                message_id=item.reply_to_message_id or message.message_id,
                allow_sending_without_reply=True,
            )
            if item.sticker is not None:
                progress.stage = "sticker_lookup"
                sticker = await self._stickers.file_id(item.sticker)
                progress.stage = "send"
                await message.answer_sticker(sticker, reply_parameters=reply_parameters)
                progress.chunk_count += 1
                progress.stage = "render"
                continue
            for chunk, entities in render_markdown(item.text):
                progress.stage = "send"
                await message.answer(
                    chunk,
                    entities=entities,
                    parse_mode=None,
                    reply_parameters=reply_parameters,
                )
                progress.chunk_count += 1
                progress.stage = "render"
