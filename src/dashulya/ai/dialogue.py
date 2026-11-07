import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime

from ag2 import Agent, MemoryStream
from ag2.events import (
    BinaryInput,
    BinaryType,
    ImageInput,
    Input,
    ModelRequest,
    ModelResponse,
    TextInput,
    ToolResultEvent,
)
from ag2.history import close_unanswered_tool_calls, unanswered_tool_calls
from pydantic import ValidationError

from dashulya.ai.message_format import (
    MessageFormatter,
    attachment_tag,
    external_input,
    format_local_time,
    input_source,
)
from dashulya.ai.tools.history import HistoryImageTurn
from dashulya.ai.tools.reactions import ReactionTurn
from dashulya.domain.memory import (
    ChatHistory,
    ContextImages,
    ContextWindow,
    MemoryAccess,
    MemoryMessage,
    ReplyContext,
    TruncatedThread,
)
from dashulya.domain.messages import (
    ConversationKey,
    ImageAttachment,
    ImageOmission,
    Reply,
    ReplyBatch,
    ReplyRequest,
)
from dashulya.observability import exception_fields, reply_log_fields

logger = logging.getLogger(__name__)


@dataclass
class Conversation:
    stream: MemoryStream = field(default_factory=MemoryStream)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    delivery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_tasks: set[asyncio.Task[object]] = field(default_factory=set)


class Dialogue:
    def __init__(
        self,
        agent: Agent[ReplyBatch],
        *,
        timezone: str = "Europe/Moscow",
        history: ChatHistory | None = None,
        bot_id: int | None = None,
        max_image_bytes: int = 20_000_000,
        formatter: MessageFormatter | None = None,
        context_images: ContextImages | None = None,
    ) -> None:
        self._agent = agent
        self._formatter = formatter or MessageFormatter(timezone, bot_id)
        self._timezone = self._formatter.timezone
        self._history = history
        self._context_images = context_images
        self._max_image_bytes = max_image_bytes
        self._conversations: dict[ConversationKey, Conversation] = {}
        self._stopped_groups: set[int] = set()

    @asynccontextmanager
    async def turn(self, key: ConversationKey) -> AsyncGenerator[None]:
        """Track a whole turn, including classification and transport waits."""
        if key[0] in self._stopped_groups:
            raise asyncio.CancelledError
        task = asyncio.current_task()
        assert task is not None
        conversation = self._conversation(key)
        outermost = task not in conversation.active_tasks
        conversation.active_tasks.add(task)
        try:
            yield
        finally:
            if outermost:
                conversation.active_tasks.discard(task)

    async def stop(self, chat_id: int) -> None:
        """Block new turns and cancel active turns across every topic of a group."""
        self._stopped_groups.add(chat_id)
        current = asyncio.current_task()
        tasks = {
            task
            for key, conversation in self._conversations.items()
            if key[0] == chat_id
            for task in conversation.active_tasks
            if task is not current
        }
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def resume(self, chat_id: int) -> None:
        self._stopped_groups.discard(chat_id)

    def _conversation(self, key: ConversationKey) -> Conversation:
        if key not in self._conversations:
            self._conversations[key] = Conversation()
        return self._conversations[key]

    @asynccontextmanager
    async def delivery(self, key: ConversationKey) -> AsyncGenerator[None]:
        # Keep each Telegram reply batch together through persistence.
        async with self._conversation(key).delivery_lock:
            yield

    async def select_context(
        self,
        key: ConversationKey,
        message_id: int,
        *,
        current: MemoryMessage | None = None,
        limit: int = 20,
        parent_hops: int = 5,
        recursive: bool = True,
    ) -> ReplyContext:
        """Restore earlier request windows, preserving their rendered prefixes."""
        selected = await self._select_window(
            key, message_id, current=current, limit=limit, parent_hops=parent_hops
        )
        windows = [
            ContextWindow(message_id, selected.messages, selected.truncated_threads)
        ]
        unresolved: set[int] = set()
        before_id = message_id
        while recursive and key[0] < 0 and self._formatter.bot_id is not None:
            extension = None
            answer = None
            for item in windows[-1].messages:
                if (
                    item.author_id != self._formatter.bot_id
                    or item.message_id >= before_id
                ):
                    continue
                trigger = item.trigger_message_id
                if (
                    trigger is not None
                    and 0 < trigger < item.message_id
                    and trigger < before_id
                ):
                    candidate = await self._select_window(
                        key, trigger, limit=limit, parent_hops=parent_hops
                    )
                    if (
                        candidate.messages
                        and candidate.messages[-1].message_id == trigger
                    ):
                        # Replay the original media-only request's input, too.
                        anchor = candidate.messages[-1]
                        if not anchor.text:
                            candidate = replace(
                                candidate,
                                messages=(
                                    *candidate.messages[:-1],
                                    replace(
                                        anchor,
                                        text="Please respond to this media in the context of our conversation.",
                                    ),
                                ),
                            )
                        extension = candidate
                        answer = item
                        break
                unresolved.add(item.message_id)
            if extension is None:
                break
            assert answer is not None
            before_id = extension.messages[-1].message_id
            windows.append(
                ContextWindow(
                    before_id,
                    extension.messages,
                    extension.truncated_threads,
                    answer.message_id,
                )
            )
        seen: set[int] = set()
        messages: list[MemoryMessage] = []
        cuts: list[TruncatedThread] = []
        for window in reversed(windows):
            fresh = [item for item in window.messages if item.message_id not in seen]
            fresh_ids = {item.message_id for item in fresh}
            messages.extend(fresh)
            cuts.extend(
                item for item in window.truncated_threads if item.boundary_id in fresh_ids
            )
            seen.update(fresh_ids)
        return ReplyContext(
            tuple(messages), tuple(cuts), tuple(windows), tuple(sorted(unresolved))
        )

    async def _select_window(
        self,
        key: ConversationKey,
        message_id: int,
        *,
        current: MemoryMessage | None = None,
        limit: int = 20,
        parent_hops: int = 5,
    ) -> ReplyContext:
        """Select originals without formatting or creating an agent stream.

        When supplied, current replaces its saved copy and appears exactly once,
        last. Saved attachment and sender metadata survive the replacement.
        """
        if self._history is None:
            error_message = "Group replies require a chat history store."
            raise ValueError(error_message)
        context = await self._history.context(
            key, message_id, limit=limit, parent_hops=parent_hops
        )
        if current is None:
            return context
        saved = next(
            (item for item in context.messages if item.message_id == current.message_id),
            current,
        )
        current = replace(
            saved,
            text=current.text,
            sent_at=current.sent_at,
            author_id=current.author_id,
            author=current.author,
            reply_to_id=current.reply_to_id
            if current.reply_to_id is not None
            else saved.reply_to_id,
        )
        return replace(
            context,
            messages=(
                *(
                    item
                    for item in context.messages
                    if item.message_id != current.message_id
                ),
                current,
            ),
        )

    def _text(
        self, text: str, *, sent_at: datetime, user_id: int | None, user_name: str
    ) -> str:
        if sent_at.utcoffset() is None:
            error_message = "Message send time must include a timezone."
            raise ValueError(error_message)
        local_time = sent_at.astimezone(self._timezone)
        return (
            f"Sent at: {format_local_time(local_time, include_date=True)}\n"
            f"{user_name} (Telegram user ID: {user_id}; interlocutor):\n{text}"
        )

    async def _group_input(
        self,
        key: ConversationKey,
        current: MemoryMessage,
        images: tuple[ImageAttachment | ImageOmission, ...],
    ) -> tuple[MemoryStream, list[Input], tuple[MemoryMessage, ...]]:
        context = await self.select_context(key, current.message_id, current=current)
        if self._context_images is not None:
            images = await self._context_images.load_context(
                context.messages, images=images, request_message_id=current.message_id
            )
        events = self.context_events(context, images=images)
        request = events.pop()
        assert isinstance(request, ModelRequest)
        stream = MemoryStream()
        await stream.history.replace(events)
        return stream, request.parts, context.messages

    async def _whisper_input(
        self, request: ReplyRequest, message: str
    ) -> tuple[MemoryStream, list[Input]]:
        if request.key[0] >= 0:
            error_message = "Whispers require a group conversation."
            raise ValueError(error_message)
        stream = MemoryStream()
        if self._history is not None:
            context = await self._history.context(
                request.key, None, limit=20, parent_hops=5, before=request.sent_at
            )
            await stream.history.replace(
                self.context_events(context, interlocutor_id=request.user_id)
            )
        return stream, [
            TextInput(
                external_input(
                    "Private whisper in the current group. Reply privately with text or a sticker; "
                    "public reactions are unavailable.\n" + message,
                    input_source(request.user_id, owner_id=self._formatter.owner_id),
                )
            )
        ]

    def context_events(
        self,
        context: ReplyContext,
        *,
        images: tuple[ImageAttachment | ImageOmission, ...] = (),
        interlocutor_id: int | None = None,
    ) -> list[ModelRequest | ModelResponse]:
        """Render selected originals with native media next to their source text."""
        if not context.messages:
            return []
        if context.windows:
            events: list[ModelRequest | ModelResponse] = []
            seen: set[int] = set()
            for window in reversed(context.windows):
                fresh = tuple(
                    item for item in window.messages if item.message_id not in seen
                )
                fresh_ids = {item.message_id for item in fresh}
                block = ReplyContext(
                    fresh,
                    tuple(
                        item
                        for item in window.truncated_threads
                        if item.boundary_id in fresh_ids
                    ),
                )
                chunk = self.context_events(
                    block,
                    images=images,
                    interlocutor_id=interlocutor_id
                    if window is context.windows[0]
                    else None,
                )
                events.extend(chunk if not events else chunk[1:])
                seen.update(fresh_ids)
            return events
        current = context.messages[-1]
        messages = []
        attachments = []
        image_types = {
            image.message_id: (image.preview_kind or "image")
            if isinstance(image, ImageAttachment)
            else "image"
            for image in images
        }
        for original in context.messages:
            notes, parts = self._image_inputs(
                images, {original.message_id}, include_attachment=False
            )
            messages.append(
                replace(
                    original,
                    text=f"{original.text}\n{notes}" if notes else original.text,
                    media_type=original.media_type
                    or image_types.get(original.message_id),
                )
            )
            attachments.append(parts)
        formatted = self._formatter.events(
            messages,
            interlocutor_id=interlocutor_id
            if interlocutor_id is not None
            else current.author_id,
            truncated_threads=context.truncated_threads,
        )
        events = formatted[:1]
        for event, parts in zip(formatted[1:], attachments, strict=True):
            if isinstance(event, ModelRequest):
                event.parts.extend(parts)
                events.append(event)
            else:
                events.append(event)
                if parts:
                    # AG2 assistant responses accept text only; keep their media adjacent.
                    events.append(ModelRequest(parts))
        return events

    async def _trim_images(self, stream: MemoryStream, incoming_bytes: int) -> None:
        if incoming_bytes > self._max_image_bytes:
            error_message = "Images exceed the request byte budget."
            raise ValueError(error_message)
        events = list(await stream.history.get_events())
        retained = incoming_bytes + sum(
            len(part.data)
            for event in events
            if isinstance(event, (ModelRequest, ToolResultEvent))
            for part in (
                event.parts if isinstance(event, ModelRequest) else event.result.parts
            )
            if isinstance(part, BinaryInput) and part.kind == BinaryType.IMAGE
        )
        if retained <= self._max_image_bytes:
            return
        # Keep recent private images within the same budget as a new request. Text and model
        # responses survive eviction, and group streams remain local to their current request.
        for event in events:
            if not isinstance(event, (ModelRequest, ToolResultEvent)):
                continue
            parts = event.parts if isinstance(event, ModelRequest) else event.result.parts
            for index, part in enumerate(parts):
                if retained <= self._max_image_bytes:
                    break
                if isinstance(part, BinaryInput) and part.kind == BinaryType.IMAGE:
                    retained -= len(part.data)
                    parts[index] = TextInput(
                        external_input(
                            "[Earlier image omitted to stay within the image byte budget.]",
                            "external_user",
                        )
                    )
        await stream.history.replace(events)

    def _image_inputs(
        self,
        images: tuple[ImageAttachment | ImageOmission, ...],
        allowed_ids: set[int] | None = None,
        *,
        include_attachment: bool = True,
    ) -> tuple[str, list[Input]]:
        notes: list[str] = []
        parts: list[Input] = []
        for image in images:
            if allowed_ids is not None and image.message_id not in allowed_ids:
                continue
            if include_attachment:
                media_type = (
                    image.preview_kind if isinstance(image, ImageAttachment) else None
                )
                notes.append(attachment_tag(media_type or "image", image.message_id))
            if isinstance(image, ImageOmission):
                reason = {
                    "too_large": f"too large for the {self._max_image_bytes / 1_000_000:g} MB image byte budget. ",
                    "preview_unavailable": "Telegram did not provide a thumbnail for this media. ",
                    "decode_failed": "The original animation could not be decoded and no usable thumbnail was available. ",
                    "unavailable": "This image could not be downloaded. ",
                    "unsupported": "This attachment could not be read as a supported image. ",
                }[image.reason]
                notes.append(
                    f"[Image from Telegram message ID: {image.message_id} omitted: "
                    f"{reason}"
                    "Its contents are unavailable; do not guess what it shows.]"
                )
                continue
            if image.first_frame:
                notes.append(
                    "This is only the first frame from the original animation. Later frames and motion are unavailable; do not infer them from this image."
                )
            elif image.preview_kind is not None:
                notes.append(
                    f"This is only a static {image.preview_kind} thumbnail. The animation and motion are unavailable; do not infer them from this preview."
                )
            binary = ImageInput(data=image.data, media_type=image.media_type)
            # Bound resizing for dense images; original detail may reject oversized dimensions.
            binary.vendor_metadata["detail"] = "high"
            parts.append(binary)
        return "\n".join(notes), parts

    async def recent_context(
        self, key: ConversationKey, *, through_message_id: int | None = None
    ) -> list[dict[str, str]]:
        if through_message_id is not None:
            context = await self.select_context(key, through_message_id, recursive=False)
            return self._formatter.context(
                [
                    message
                    for message in context.messages
                    if message.message_id != through_message_id
                ],
                interlocutor_id=next(
                    (
                        message.author_id
                        for message in context.messages
                        if message.message_id == through_message_id
                    ),
                    None,
                ),
                truncated_threads=context.truncated_threads,
            )
        conversation = self._conversation(key)
        async with conversation.lock:
            recent: list[dict[str, str]] = []
            for event in await conversation.stream.history.get_events():
                if isinstance(event, ModelRequest):
                    text = "\n".join(
                        part.content
                        for part in event.parts
                        if isinstance(part, TextInput)
                    )
                    recent.append({"role": "user", "text": text[:1000]})
                elif isinstance(event, ModelResponse) and event.content:
                    recent.append({"role": "assistant", "text": event.content[:1000]})
            return recent[-10:]

    async def reply(self, request: ReplyRequest) -> ReplyBatch:
        async with self.turn(request.key):
            return await self._reply(request)

    async def _reply(self, request: ReplyRequest) -> ReplyBatch:
        key = request.key
        message_id = request.message_id
        images = request.images
        message = self._text(
            request.text,
            sent_at=request.sent_at,
            user_id=request.user_id,
            user_name=request.user_name,
        )
        # Group streams belong to individual requests; only private history is shared.
        async with self._conversation(key).lock if key[0] >= 0 else nullcontext():
            stream = self._conversation(key).stream
            selected: tuple[MemoryMessage, ...] = ()
            allowed_reply_ids: set[int] = set()
            if request.is_whisper:
                stream, inputs = await self._whisper_input(request, message)
            elif key[0] < 0:
                if message_id is None:
                    error_message = "Group replies require a Telegram message ID."
                    raise ValueError(error_message)
                stream, inputs, selected = await self._group_input(
                    key,
                    MemoryMessage(
                        key[0],
                        message_id,
                        request.text,
                        request.sent_at,
                        request.user_id,
                        request.user_name,
                        "",
                        thread_id=key[1],
                        reply_to_id=request.reply_to_id,
                    ),
                    images,
                )
                allowed_reply_ids = {item.message_id for item in selected}
            else:
                if message_id is not None:
                    message += f"\nTelegram message ID: {message_id}"
                notes, inputs = self._image_inputs(images)
                if notes:
                    message += f"\n{notes}"
                if key[0] > 0:
                    message = external_input(
                        message,
                        input_source(request.user_id, owner_id=self._formatter.owner_id),
                    )
                inputs.insert(0, TextInput(message))
            await self._trim_images(
                stream,
                sum(len(part.data) for part in inputs if isinstance(part, BinaryInput)),
            )
            if unanswered_tool_calls(list(await stream.history.get_events())):
                await close_unanswered_tool_calls(
                    stream.history,
                    result="This tool call was not completed because the previous turn was interrupted. No result is available.",
                )
                logger.warning(
                    "AI conversation repaired",
                    extra={
                        **reply_log_fields(request),
                    },
                )
            async with asyncio.timeout(300):
                access = MemoryAccess(
                    chat_id=key[0],
                    is_private=key[0] > 0,
                    before=request.sent_at,
                    before_id=None if request.is_whisper else message_id,
                )
                reactions = ReactionTurn()
                text_input = inputs[0]
                assert isinstance(text_input, TextInput)
                reply = await self._agent.ask(
                    text_input.content,
                    *inputs[1:],
                    stream=stream,
                    variables={"user_id": request.user_id, "chat_id": key[0]},
                    dependencies={
                        "reply_request": request,
                        "memory_access": access,
                        "reaction_turn": None if request.is_whisper else reactions,
                        "history_image_turn": await self._history_image_turn(
                            stream, inputs, request, selected
                        ),
                    },
                )
                try:
                    result = await reply.content()
                except ValidationError as error:
                    logger.warning(
                        "AI reply validation failed",
                        extra={
                            **reply_log_fields(request),
                            "validation_errors": [
                                {"type": item["type"], "loc": item["loc"]}
                                for item in error.errors(
                                    include_input=False,
                                    include_context=False,
                                )
                            ],
                            **exception_fields(error),
                        },
                    )
                    raise
            return _validate_replies(
                result,
                request=request,
                allowed_reply_ids=allowed_reply_ids,
                reactions_attempted=reactions.attempted,
            )

    async def _history_image_turn(
        self,
        stream: MemoryStream,
        inputs: list[Input],
        request: ReplyRequest,
        selected: tuple[MemoryMessage, ...],
    ) -> HistoryImageTurn | None:
        if self._context_images is None:
            return None
        if (
            request.key[0] > 0
            and self._history is not None
            and request.message_id is not None
            and request.images
        ):
            context = await self._history.context(
                request.key, request.message_id, limit=1, parent_hops=1
            )
            selected = tuple(
                source
                for source in context.messages
                if source.sent_at <= request.sent_at
                and (source.edited_at is None or source.edited_at <= request.sent_at)
            )
        retained = sum(
            len(part.data)
            for event in await stream.history.get_events()
            if isinstance(event, (ModelRequest, ToolResultEvent))
            for part in (
                event.parts if isinstance(event, ModelRequest) else event.result.parts
            )
            if isinstance(part, BinaryInput) and part.kind == BinaryType.IMAGE
        ) + sum(len(part.data) for part in inputs if isinstance(part, BinaryInput))
        return HistoryImageTurn(
            max(0, self._max_image_bytes - retained),
            request.message_id,
            self._context_images,
            chat_id=request.key[0],
            sources={
                source.message_id: source
                for source in selected
                if source.message_id == request.message_id
            },
        )

    async def clear(self, key: ConversationKey) -> None:
        conversation = self._conversations.get(key)
        if conversation is not None:
            # Reuse the lock so queued messages cannot continue on a discarded stream.
            async with conversation.delivery_lock, conversation.lock:
                conversation.stream = MemoryStream()


def _validate_replies(
    result: ReplyBatch | None,
    *,
    request: ReplyRequest,
    allowed_reply_ids: set[int],
    reactions_attempted: bool,
) -> ReplyBatch:
    messages = (
        [
            item
            for item in result.messages
            if item.text.strip() or item.sticker is not None
        ]
        if result is not None
        else []
    )
    if not messages:
        if result is not None and reactions_attempted:
            return ReplyBatch(())
        return ReplyBatch((
            Reply("Сейчас не удалось подготовить ответ. Попробуй ещё раз."),
        ))
    validated: list[Reply] = []
    for index, item in enumerate(messages):
        if item.reply_to_message_id is None:
            target = None
            target_reason = (
                "default_current_request" if index == 0 else "unthreaded_followup"
            )
        elif item.reply_to_message_id in allowed_reply_ids:
            target = item.reply_to_message_id
            target_reason = "selected"
        else:
            target = request.message_id if index > 0 else None
            target_reason = "outside_context"
        validated.append(
            replace(item, text=item.text.strip(), reply_to_message_id=target)
        )
        if request.key[0] < 0:
            logger.info(
                "Group reply target selected",
                extra={
                    **reply_log_fields(request),
                    "requested_reply_to_message_id": item.reply_to_message_id,
                    "reply_to_message_id": target
                    if target is not None or index > 0
                    else request.message_id,
                    "reply_target_reason": target_reason,
                    "context_message_ids": sorted(allowed_reply_ids),
                },
            )
    return ReplyBatch(tuple(validated))
