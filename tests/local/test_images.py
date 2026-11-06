import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from aiogram import Bot
from aiogram.types import File, Message

from dashulya.bot.images import TelegramImages
from dashulya.bot.media_frames import first_frame
from dashulya.bot.messages import image_file
from dashulya.domain.memory import MemoryMessage
from dashulya.domain.messages import ImageAttachment, ImageFile, ImageOmission
from dashulya.observability import JsonLogFormatter
from tests.media_fixtures import animation_bytes

PNG = b"\x89PNG\r\n\x1a\n" + b"image-data"
STICKER: dict[str, Any] = {
    "file_id": "sticker",
    "file_unique_id": "unique-sticker",
    "type": "regular",
    "width": 512,
    "height": 512,
    "is_animated": False,
    "is_video": False,
}
THUMBNAIL = {
    "file_id": "thumbnail",
    "file_unique_id": "unique-thumb",
    "width": 100,
    "height": 100,
}
ANIMATION: dict[str, Any] = {
    "file_id": "animation",
    "file_unique_id": "unique-animation",
    "width": 320,
    "height": 240,
    "duration": 3,
    "thumbnail": THUMBNAIL,
}


def image_message(**fields: Any) -> Message:
    return Message.model_validate({
        "message_id": 1,
        "date": 1700000000,
        "chat": {"id": 42, "type": "private"},
        "photo": [
            {"file_id": "photo", "file_unique_id": "unique", "width": 2, "height": 2}
        ],
        **fields,
    })


@pytest.mark.usefixtures("_setup")
class TestImage:
    @classmethod
    def saved_media(cls, message: Message) -> MemoryMessage:
        return replace(
            cls.saved_image(message.message_id),
            image_file=image_file(message),
            media_type=message.content_type,
        )

    @staticmethod
    def saved_image(number: int, *, size: int | None = None) -> MemoryMessage:
        return MemoryMessage(
            chat_id=-123,
            message_id=number,
            text="",
            sent_at=datetime(2026, 10, 25, tzinfo=UTC),
            author_id=99,
            author="Boris",
            chat_title="Group",
            media_type="photo",
            image_file=ImageFile(f"photo-{number}", size),
        )

    @pytest.fixture
    async def _setup(self, cleanup: AsyncExitStack) -> None:
        self.cleanup = cleanup
        self.bot = Bot("123:TEST")
        self.cleanup.push_async_callback(self.bot.session.close)
        self.get_file = AsyncMock(
            return_value=File(
                file_id="photo", file_unique_id="unique", file_path="photos/image.png"
            )
        )
        self.chunks = [PNG]
        self.read_chunks = 0
        self.closed = False

        async def stream(*args: Any, **kwargs: Any) -> AsyncGenerator[bytes]:
            try:
                for chunk in self.chunks:
                    self.read_chunks += 1
                    yield chunk
            finally:
                self.closed = True

        self.stream = Mock(side_effect=stream)
        for target, name, value in (
            (self.bot, "get_file", self.get_file),
            (self.bot.session, "stream_content", self.stream),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.cleanup.callback(patcher.stop)
        self.images = TelegramImages(self.bot, max_bytes=len(PNG))

    async def test_image_at_limit_stays_in_memory_and_uses_content_mime(self) -> None:
        images = await self.images.load(image_message())
        assert len(images) == 1
        assert isinstance(images[0], ImageAttachment)
        assert images[0].data == PNG
        assert images[0].media_type == "image/png"
        assert images[0].message_id == 1
        self.stream.assert_called_once()
        assert self.stream.call_args.args[0] == (
            "https://api.telegram.org/file/bot123:TEST/photos/image.png"
        )
        assert self.closed
        assert "image-data" not in repr(images)

    async def test_context_images_share_budget_and_prefer_recent_sources(self) -> None:
        images = await self.images.load_context((
            self.saved_image(1),
            self.saved_image(2),
        ))
        assert images == (ImageAttachment(PNG, "image/png", 2), ImageOmission(1))
        assert self.get_file.await_args_list == [
            call("photo-2", request_timeout=10),
            call("photo-1", request_timeout=10),
        ]
        self.stream.assert_called_once()

    async def test_current_image_is_not_downloaded_twice_or_given_a_separate_budget(
        self,
    ) -> None:
        current = ImageAttachment(PNG, "image/png", 2)
        images = await self.images.load_context(
            (self.saved_image(1), self.saved_image(2)), images=(current,)
        )
        assert images == (current, ImageOmission(1))
        self.get_file.assert_not_awaited()

    async def test_context_filters_supplied_ids_before_loading_and_skips_large_files(
        self,
    ) -> None:
        images = await self.images.load_context(
            (self.saved_image(1, size=len(PNG) + 1),),
            images=(ImageAttachment(PNG, "image/png", 999),),
        )
        assert images == (ImageOmission(1),)
        self.get_file.assert_not_awaited()

    async def test_unavailable_context_image_does_not_hide_other_images_or_credentials(
        self,
    ) -> None:
        self.get_file.side_effect = [
            RuntimeError(self.bot.token),
            self.get_file.return_value,
        ]
        images = await self.images.load_context((
            self.saved_image(1),
            self.saved_image(2),
        ))
        assert images == (
            ImageOmission(2, "unavailable"),
            ImageAttachment(PNG, "image/png", 1),
        )
        assert self.bot.token not in repr(images)

    async def test_context_cancellation_propagates(self) -> None:
        self.get_file.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await self.images.load_context((self.saved_image(1),))

    async def test_context_streams_run_concurrently_and_keep_source_order(self) -> None:
        barrier = asyncio.Barrier(3)
        started: list[str] = []

        async def metadata(file_id: str, **kwargs: Any) -> File:
            return File(
                file_id=file_id,
                file_unique_id=file_id,
                file_path=f"photos/{file_id}",
                file_size=len(PNG),
            )

        async def stream(url: str, **kwargs: Any) -> AsyncGenerator[bytes]:
            started.append(url.rsplit("/", 1)[-1])
            await barrier.wait()
            yield PNG

        self.get_file.side_effect = metadata
        self.stream.side_effect = stream
        loader = TelegramImages(self.bot, max_bytes=3 * len(PNG))
        result = await asyncio.wait_for(
            loader.load_context(tuple(self.saved_image(number) for number in (1, 2, 3))),
            timeout=1,
        )
        assert started == ["photo-3", "photo-2", "photo-1"]
        assert result == tuple(
            ImageAttachment(PNG, "image/png", number) for number in (3, 2, 1)
        )

    @pytest.mark.parametrize("known_size", [None, len(PNG)])
    @pytest.mark.parametrize("max_images", [1, 2])
    async def test_ready_image_does_not_wait_for_another_images_metadata(
        self, known_size: int | None, max_images: int
    ) -> None:
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()
        ready_downloaded = asyncio.Event()

        async def metadata(file_id: str, **kwargs: Any) -> File:
            if file_id == "photo-2":
                slow_started.set()
                await release_slow.wait()
            return File(
                file_id=file_id,
                file_unique_id=file_id,
                file_path=file_id,
                file_size=len(PNG),
            )

        async def stream(url: str, **kwargs: Any) -> AsyncGenerator[bytes]:
            yield PNG
            if url.endswith("photo-1"):
                ready_downloaded.set()

        self.get_file.side_effect = metadata
        self.stream.side_effect = stream
        loader = TelegramImages(self.bot, max_bytes=max_images * len(PNG))
        task = asyncio.create_task(
            loader.load_context((
                self.saved_image(1, size=known_size),
                self.saved_image(2, size=known_size),
            ))
        )
        try:
            await asyncio.wait_for(slow_started.wait(), timeout=1)
            await asyncio.wait_for(ready_downloaded.wait(), timeout=0.2)
            assert not release_slow.is_set()
        finally:
            release_slow.set()
            result = await asyncio.wait_for(task, timeout=1)
        assert result == (
            ImageAttachment(PNG, "image/png", 2),
            ImageAttachment(PNG, "image/png", 1) if max_images == 2 else ImageOmission(1),
        )
        self.get_file.reset_mock()
        assert await loader.load_context((self.saved_image(1, size=known_size),)) == (
            ImageAttachment(PNG, "image/png", 1),
        )
        self.get_file.assert_not_awaited()

    async def test_ready_image_finishes_before_a_neighbors_metadata_failure(self) -> None:
        release_slow = asyncio.Event()
        ready_downloaded = asyncio.Event()

        async def metadata(file_id: str, **kwargs: Any) -> File:
            if file_id == "photo-2":
                await release_slow.wait()
                raise TimeoutError
            return File(
                file_id=file_id,
                file_unique_id=file_id,
                file_path=file_id,
                file_size=len(PNG),
            )

        async def stream(*args: Any, **kwargs: Any) -> AsyncGenerator[bytes]:
            yield PNG
            ready_downloaded.set()

        self.get_file.side_effect = metadata
        self.stream.side_effect = stream
        task = asyncio.create_task(
            self.images.load_context((self.saved_image(1), self.saved_image(2)))
        )
        try:
            await asyncio.wait_for(ready_downloaded.wait(), timeout=0.2)
            assert not release_slow.is_set()
        finally:
            release_slow.set()
            result = await asyncio.wait_for(task, timeout=1)
        assert result == (
            ImageOmission(2, "unavailable"),
            ImageAttachment(PNG, "image/png", 1),
        )

    async def test_cached_image_finishes_while_another_images_metadata_is_pending(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        loader = TelegramImages(self.bot, max_bytes=2 * len(PNG))
        await loader.load_context((self.saved_image(1),))
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()

        async def metadata(file_id: str, **kwargs: Any) -> File:
            slow_started.set()
            await release_slow.wait()
            return File(
                file_id=file_id,
                file_unique_id=file_id,
                file_path=file_id,
                file_size=len(PNG),
            )

        self.get_file.side_effect = metadata
        caplog.clear()
        with caplog.at_level("INFO", logger="dashulya.bot.images"):
            task = asyncio.create_task(
                loader.load_context((self.saved_image(1), self.saved_image(2)))
            )
            try:
                await asyncio.wait_for(slow_started.wait(), timeout=1)
                assert any(
                    record.message == "Media item completed"
                    and getattr(record, "message_id", None) == 1
                    and getattr(record, "cache_status", None) == "hit"
                    for record in caplog.records
                )
            finally:
                release_slow.set()
                await asyncio.wait_for(task, timeout=1)

    async def test_understated_sizes_keep_priority_without_deadlocking_streams(
        self,
    ) -> None:
        older_started = asyncio.Event()
        barrier = asyncio.Barrier(2)

        async def metadata(file_id: str, **kwargs: Any) -> File:
            if file_id == "photo-2":
                await older_started.wait()
            return File(
                file_id=file_id, file_unique_id=file_id, file_path=file_id, file_size=1
            )

        async def stream(url: str, **kwargs: Any) -> AsyncGenerator[bytes]:
            if url.endswith("photo-1"):
                older_started.set()
            await barrier.wait()
            yield PNG

        self.get_file.side_effect = metadata
        self.stream.side_effect = stream
        result = await asyncio.wait_for(
            self.images.load_context((self.saved_image(1), self.saved_image(2))),
            timeout=1,
        )
        assert result == (ImageAttachment(PNG, "image/png", 2), ImageOmission(1))

    async def test_request_and_its_parent_take_priority_over_newer_history(self) -> None:
        loader = TelegramImages(self.bot, max_bytes=2 * len(PNG))
        result = await loader.load_context(
            (
                self.saved_image(1),
                self.saved_image(2),
                replace(self.saved_image(3), reply_to_id=1),
            ),
            request_message_id=3,
        )
        assert result == (
            ImageAttachment(PNG, "image/png", 3),
            ImageAttachment(PNG, "image/png", 1),
            ImageOmission(2),
        )

    async def test_one_timed_out_stream_does_not_hide_other_images(self) -> None:
        barrier = asyncio.Barrier(2)
        closed: set[str] = set()

        async def metadata(file_id: str, **kwargs: Any) -> File:
            return File(
                file_id=file_id,
                file_unique_id=file_id,
                file_path=file_id,
                file_size=len(PNG),
            )

        async def stream(url: str, **kwargs: Any) -> AsyncGenerator[bytes]:
            name = url.rsplit("/", 1)[-1]
            try:
                await barrier.wait()
                if name == "photo-2":
                    await asyncio.Event().wait()
                yield PNG
            finally:
                closed.add(name)

        self.get_file.side_effect = metadata
        self.stream.side_effect = stream
        loader = TelegramImages(self.bot, max_bytes=2 * len(PNG))
        with patch("dashulya.bot.images.NETWORK_TIMEOUT_SECONDS", 0.05):
            result = await asyncio.wait_for(
                loader.load_context((self.saved_image(1), self.saved_image(2))), timeout=1
            )
        assert result == (
            ImageOmission(2, "unavailable"),
            ImageAttachment(PNG, "image/png", 1),
        )
        assert closed == {"photo-1", "photo-2"}

    async def test_positive_cache_reuses_bytes_with_the_current_message_identity(
        self,
    ) -> None:
        current = image_message()
        assert await self.images.load(current) == (ImageAttachment(PNG, "image/png", 1),)
        saved = replace(self.saved_media(current), message_id=99)
        assert await self.images.load_context((saved,)) == (
            ImageAttachment(PNG, "image/png", 99),
        )
        self.get_file.assert_awaited_once_with("photo", request_timeout=10)
        self.stream.assert_called_once()

    async def test_failure_cache_expires_and_allows_a_new_download(self) -> None:
        now = 0.0
        with patch("dashulya.bot.images.monotonic", side_effect=lambda: now):
            loader = TelegramImages(self.bot, max_bytes=len(PNG))
        self.get_file.side_effect = RuntimeError(self.bot.token)
        assert await loader.load(image_message()) == (ImageOmission(1, "unavailable"),)
        self.get_file.side_effect = None
        assert await loader.load(image_message(message_id=2)) == (
            ImageOmission(2, "unavailable"),
        )
        self.get_file.assert_awaited_once_with("photo", request_timeout=10)
        now = 31.0
        assert await loader.load(image_message(message_id=3)) == (
            ImageAttachment(PNG, "image/png", 3),
        )
        assert self.get_file.await_count == 2

    async def test_positive_cache_expires_and_evicts_by_bytes(self) -> None:
        now = 0.0
        with (
            patch("dashulya.bot.images.monotonic", side_effect=lambda: now),
            patch("dashulya.bot.images.CACHE_BYTES", 2 * len(PNG)),
        ):
            loader = TelegramImages(self.bot, max_bytes=len(PNG))
        for file_id in ("a", "b", "a", "c", "b"):
            message = image_message(
                photo=[
                    {
                        "file_id": file_id,
                        "file_unique_id": file_id,
                        "width": 2,
                        "height": 2,
                    }
                ]
            )
            assert isinstance((await loader.load(message))[0], ImageAttachment)
        assert self.get_file.await_args_list == [
            call("a", request_timeout=10),
            call("b", request_timeout=10),
            call("c", request_timeout=10),
            call("b", request_timeout=10),
        ]
        now = 301.0
        message = image_message(
            photo=[
                {
                    "file_id": "b",
                    "file_unique_id": "b",
                    "width": 2,
                    "height": 2,
                }
            ]
        )
        assert isinstance((await loader.load(message))[0], ImageAttachment)
        assert self.get_file.await_count == 5

    async def test_cached_images_still_share_the_context_byte_budget(self) -> None:
        loader = TelegramImages(self.bot, max_bytes=2 * len(PNG))
        originals = (self.saved_image(1), self.saved_image(2))
        await loader.load_context(originals)
        self.get_file.reset_mock()
        self.stream.reset_mock()
        supplied = ImageAttachment(PNG, "image/png", 3)
        assert await loader.load_context(
            (*originals, self.saved_image(3)), images=(supplied,)
        ) == (
            supplied,
            ImageAttachment(PNG, "image/png", 2),
            ImageOmission(1),
        )
        self.get_file.assert_not_awaited()
        self.stream.assert_not_called()
        assert await loader.load_context(originals) == (
            ImageAttachment(PNG, "image/png", 2),
            ImageAttachment(PNG, "image/png", 1),
        )

    async def test_cancelling_a_parallel_batch_closes_every_stream(self) -> None:
        ready = asyncio.Event()
        started: set[str] = set()
        closed: set[str] = set()

        async def metadata(file_id: str, **kwargs: Any) -> File:
            return File(
                file_id=file_id,
                file_unique_id=file_id,
                file_path=file_id,
                file_size=len(PNG),
            )

        async def stream(url: str, **kwargs: Any) -> AsyncGenerator[bytes]:
            name = url.rsplit("/", 1)[-1]
            try:
                started.add(name)
                if len(started) == 3:
                    ready.set()
                await asyncio.Event().wait()
                yield PNG
            finally:
                closed.add(name)

        self.get_file.side_effect = metadata
        self.stream.side_effect = stream
        loader = TelegramImages(self.bot, max_bytes=3 * len(PNG))
        task = asyncio.create_task(
            loader.load_context(tuple(self.saved_image(n) for n in (1, 2, 3)))
        )
        await asyncio.wait_for(ready.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == started == {"photo-1", "photo-2", "photo-3"}

    async def test_media_logs_include_stages_cache_and_failures_without_payloads(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO", logger="dashulya.bot.images"):
            await self.images.load_context((self.saved_image(1),), request_message_id=10)
            await self.images.load_context((self.saved_image(1),), request_message_id=11)
            self.get_file.side_effect = RuntimeError(f"{self.bot.token} PRIVATE SOURCE")
            await self.images.load_context((self.saved_image(2),), request_message_id=12)
        events = [
            json.loads(JsonLogFormatter().format(record)) for record in caplog.records
        ]
        stages = [
            e["media_stage"] for e in events if e["message"] == "Media stage completed"
        ]
        assert stages == ["metadata", "download"]
        items = [e for e in events if e["message"] == "Media item completed"]
        assert [e["cache_status"] for e in items] == ["miss", "hit", "miss"]
        assert [e["request_message_id"] for e in items] == [10, 11, 12]
        (failure,) = [e for e in events if e["message"] == "Media item failed"]
        assert failure["level"] == "ERROR"
        assert failure["media_stage"] == "metadata"
        assert failure["exceptions"][0]["frames"]
        rendered = json.dumps(events)
        for secret in (
            self.bot.token,
            "PRIVATE SOURCE",
            "photo-1",
            "image-data",
            "api.telegram.org",
        ):
            assert secret not in rendered

    async def test_oversized_update_and_getfile_size_skip_download(self) -> None:
        oversized = image_message(
            photo=[
                {
                    "file_id": "photo",
                    "file_unique_id": "unique",
                    "width": 2,
                    "height": 2,
                    "file_size": len(PNG) + 1,
                }
            ]
        )
        assert await self.images.load(oversized) == (ImageOmission(1),)
        self.get_file.assert_not_awaited()
        self.get_file.return_value = self.get_file.return_value.model_copy(
            update={"file_size": len(PNG) + 1}
        )
        assert await self.images.load(image_message()) == (ImageOmission(1),)
        self.stream.assert_not_called()

    async def test_unknown_or_false_size_cannot_bypass_stream_limit(
        self, subtests: pytest.Subtests
    ) -> None:
        self.chunks = [PNG, b"overflow", b"must-not-be-read"]
        for size in (None, 1):
            for partial in (False, True):
                with subtests.test(size=size, partial=partial):
                    self.read_chunks = 0
                    self.closed = False
                    self.get_file.return_value = self.get_file.return_value.model_copy(
                        update={"file_size": size}
                    )
                    if partial:
                        images = TelegramImages(self.bot, max_bytes=2 * len(PNG) - 1)
                        current = ImageAttachment(PNG, "image/png", 2)
                        result = await images.load_context(
                            (self.saved_image(1), self.saved_image(2)), images=(current,)
                        )
                        assert result == (current, ImageOmission(1))
                    else:
                        assert await self.images.load(image_message()) == (
                            ImageOmission(1),
                        )
                    assert self.read_chunks == (1 if partial else 2)
                    assert self.closed

    async def test_image_document_mime_is_derived_from_bytes(self) -> None:
        message = image_message(
            photo=None,
            document={
                "file_id": "doc",
                "file_unique_id": "doc",
                "file_name": "picture.jpg",
                "mime_type": "image/jpeg",
            },
        )
        images = await self.images.load(message)
        assert isinstance(images[0], ImageAttachment)
        assert images[0].media_type == "image/png"
        self.get_file.assert_awaited_once_with("doc", request_timeout=10)

    async def test_rejects_unsupported_documents_and_nonimage_bytes(self) -> None:
        document = image_message(
            photo=None,
            document={
                "file_id": "doc",
                "file_unique_id": "doc",
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
            },
        )
        assert await self.images.load(document) == (ImageOmission(1, "unsupported"),)
        self.get_file.assert_not_awaited()
        self.chunks = [b"not an image"]
        assert await self.images.load(image_message()) == (
            ImageOmission(1, "unsupported"),
        )

    async def test_cancellation_closes_the_download_stream(self) -> None:
        started = asyncio.Event()

        async def waiting_stream(*args: Any, **kwargs: Any) -> AsyncGenerator[bytes]:
            try:
                started.set()
                await asyncio.Event().wait()
                yield PNG
            finally:
                self.closed = True

        self.stream.side_effect = waiting_stream
        task = asyncio.create_task(self.images.load(image_message()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert self.closed

    async def test_stalled_current_and_saved_downloads_end_at_the_deadline(
        self, subtests: pytest.Subtests
    ) -> None:
        for saved in (False, True):
            with subtests.test(saved=saved):
                await self.check_stalled_download(saved)

    async def check_stalled_download(self, saved: bool) -> None:
        started = asyncio.Event()
        self.closed = False
        self.get_file.reset_mock()

        async def stalled(*args: Any, **kwargs: Any) -> AsyncGenerator[bytes]:
            try:
                started.set()
                await asyncio.Event().wait()
                yield PNG
            finally:
                self.closed = True

        self.stream.side_effect = stalled
        loop = asyncio.get_running_loop()
        real_time = loop.time
        elapsed = 0.0

        def time() -> float:
            return real_time() + elapsed

        with patch.object(loop, "time", time):
            task = asyncio.create_task(
                self.images.load_context((self.saved_image(1), self.saved_image(2)))
                if saved
                else self.images.load(image_message())
            )
            try:
                await asyncio.wait_for(started.wait(), timeout=1)
                elapsed = 10.0
                # Let the due timer cancel its task, then let that task
                # close the download and complete its public result.
                for _ in range(20):
                    if task.done():
                        break
                    await asyncio.sleep(0)
                assert task.done(), "Download exceeded its 10-second deadline"
                if saved:
                    assert await task == (
                        ImageOmission(2, "unavailable"),
                        ImageOmission(1, "unavailable"),
                    )
                else:
                    assert await task == (ImageOmission(1, "unavailable"),)
                assert self.closed
                assert self.get_file.await_args_list == (
                    [
                        call("photo-2", request_timeout=10),
                        call("photo-1", request_timeout=10),
                    ]
                    if saved
                    else [call("photo", request_timeout=10)]
                )
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_all_sticker_types_and_formats_use_supported_image_bytes(
        self, subtests: pytest.Subtests
    ) -> None:
        self.chunks = [b"RIFF\x08\x00\x00\x00WEBPdata"]
        for sticker_type in ("regular", "mask", "custom_emoji"):
            for sticker_format in ("static", "animated", "video"):
                with subtests.test(
                    sticker_type=sticker_type, sticker_format=sticker_format
                ):
                    sticker = {
                        **STICKER,
                        "type": sticker_type,
                        "is_animated": sticker_format == "animated",
                        "is_video": sticker_format == "video",
                        "thumbnail": THUMBNAIL,
                        "file_size": 20_000_001 if sticker_format != "static" else None,
                    }
                    message = image_message(photo=None, sticker=sticker)
                    for saved in (False, True):
                        self.images = TelegramImages(
                            self.bot, max_bytes=len(self.chunks[0])
                        )
                        with subtests.test(saved=saved):
                            self.get_file.reset_mock()
                            images = (
                                await self.images.load_context((
                                    self.saved_media(message),
                                ))
                                if saved
                                else await self.images.load(message)
                            )
                            assert isinstance(images[0], ImageAttachment)
                            assert images[0].data == self.chunks[0]
                            assert images[0].media_type == "image/webp"
                            assert images[0].preview_kind == (
                                "sticker" if sticker_format != "static" else None
                            )
                            self.get_file.assert_awaited_once_with(
                                "sticker" if sticker_format == "static" else "thumbnail",
                                request_timeout=10,
                            )

    async def test_saved_oversized_animations_and_stickers_use_thumbnail_size(
        self,
        subtests: pytest.Subtests,
    ) -> None:
        for fields in (
            {"animation": {**ANIMATION, "file_size": 20_000_001}},
            {
                "sticker": {
                    **STICKER,
                    "is_video": True,
                    "file_size": 20_000_001,
                    "thumbnail": THUMBNAIL,
                }
            },
        ):
            with subtests.test(fields=fields):
                self.get_file.reset_mock()
                images = await self.images.load_context((
                    self.saved_media(image_message(photo=None, **fields)),
                ))
                assert isinstance(images[0], ImageAttachment)
                self.get_file.assert_awaited_once_with("thumbnail", request_timeout=10)

    async def test_saved_animations_and_gif_documents_decode_the_original_first_frame(
        self,
        subtests: pytest.Subtests,
    ) -> None:
        self.chunks = [animation_bytes()]
        self.images = TelegramImages(self.bot, max_bytes=100_000)
        for fields, expected_id in (
            (
                {
                    "animation": ANIMATION,
                    "document": {
                        "file_id": "legacy",
                        "file_unique_id": "legacy",
                        "mime_type": "image/gif",
                    },
                },
                "animation",
            ),
            (
                {
                    "document": {
                        "file_id": "gif",
                        "file_unique_id": "gif",
                        "file_name": "picture.GIF",
                    }
                },
                "gif",
            ),
        ):
            with subtests.test(fields=fields):
                self.get_file.reset_mock()
                images = await self.images.load_context((
                    self.saved_media(image_message(photo=None, **fields)),
                ))
                assert isinstance(images[0], ImageAttachment)
                assert images[0].media_type == "image/jpeg"
                assert images[0].first_frame
                assert images[0].preview_kind == "animation"
                self.get_file.assert_awaited_once_with(expected_id, request_timeout=10)

    async def test_saved_undecodable_animation_uses_thumbnail_or_omission(self) -> None:
        for thumbnail in (THUMBNAIL, None):
            images = await self.images.load_context((
                self.saved_media(
                    image_message(
                        photo=None, animation={**ANIMATION, "thumbnail": thumbnail}
                    )
                ),
            ))
            if thumbnail:
                assert isinstance(images[0], ImageAttachment)
                assert images[0].data == PNG
                assert not images[0].first_frame
                assert images[0].preview_kind == "animation"
            else:
                assert images == (ImageOmission(1, "decode_failed"),)

    async def test_animated_sticker_without_thumbnail_produces_note_without_download(
        self,
        subtests: pytest.Subtests,
    ) -> None:
        for format_flag in ("is_animated", "is_video"):
            message = image_message(photo=None, sticker={**STICKER, format_flag: True})
            for saved in (False, True):
                with subtests.test(format_flag=format_flag, saved=saved):
                    images = (
                        await self.images.load_context((self.saved_media(message),))
                        if saved
                        else await self.images.load(message)
                    )
                    assert images == (ImageOmission(1, "preview_unavailable"),)
        self.get_file.assert_not_awaited()
        self.stream.assert_not_called()

    async def test_oversized_sticker_original_and_thumbnail_are_not_downloaded(
        self,
    ) -> None:
        for sticker in (
            {**STICKER, "file_size": len(PNG) + 1},
            {
                **STICKER,
                "is_video": True,
                "thumbnail": {**THUMBNAIL, "file_size": len(PNG) + 1},
            },
        ):
            images = await self.images.load(image_message(photo=None, sticker=sticker))
            assert images == (ImageOmission(1),)
        self.get_file.assert_not_awaited()
        self.stream.assert_not_called()

    async def test_reply_sticker_preview_and_current_photo_share_byte_budget(
        self,
    ) -> None:
        parent = image_message(
            photo=None, sticker={**STICKER, "is_animated": True, "thumbnail": THUMBNAIL}
        )
        images = await self.images.load(
            image_message(message_id=2, reply_to_message=parent.model_dump())
        )
        assert isinstance(images[0], ImageAttachment)
        assert images[1] == ImageOmission(1)
        assert self.get_file.await_args_list == [
            call("photo", request_timeout=10),
            call("thumbnail", request_timeout=10),
        ]
        self.stream.assert_called_once()

    async def test_oversized_animation_falls_back_to_thumbnail_ignoring_legacy_document(
        self,
        subtests: pytest.Subtests,
    ) -> None:
        for mime in ("image/gif", "video/mp4"):
            with subtests.test(mime=mime):
                self.get_file.reset_mock()
                images = await self.images.load(
                    image_message(
                        photo=None,
                        animation={
                            **ANIMATION,
                            "mime_type": mime,
                            "file_size": 20_000_001,
                        },
                        document={
                            "file_id": "legacy-document",
                            "file_unique_id": "legacy",
                            "mime_type": mime,
                            "file_size": 20_000_001,
                        },
                    )
                )
                assert isinstance(images[0], ImageAttachment)
                assert images[0].preview_kind == "animation"
                self.get_file.assert_awaited_once_with("thumbnail", request_timeout=10)

    async def test_animation_without_thumbnail_attempts_the_original(self) -> None:
        self.chunks = [animation_bytes()]
        self.images = TelegramImages(self.bot, max_bytes=100_000)
        images = await self.images.load(
            image_message(
                photo=None,
                animation={**ANIMATION, "thumbnail": None},
                document={
                    "file_id": "legacy",
                    "file_unique_id": "legacy",
                    "mime_type": "image/gif",
                },
            )
        )
        self.get_file.assert_awaited_once_with("animation", request_timeout=10)
        assert len(images) == 1
        assert isinstance(images[0], ImageAttachment)
        assert images[0].message_id == 1
        assert images[0].first_frame

        # The tiny GIF fits in RAM even when its JPEG cannot fit beside another image.
        clip = bytes.fromhex(
            "47494638396101000100800000000000ffffff2c00000000010001000002024401003b"
        )
        frame = first_frame(clip, max_bytes=100_000)
        assert len(clip) < len(frame) - 1
        self.chunks = [clip]
        current = ImageAttachment(PNG, "image/png", 2)
        source = self.saved_media(
            image_message(photo=None, animation={**ANIMATION, "thumbnail": None})
        )
        for remaining in (len(frame), len(frame) - 1):
            loader = TelegramImages(self.bot, max_bytes=len(PNG) + remaining)
            loaded = await loader.load_context(
                (source, self.saved_image(2)), images=(current,)
            )
            expected = (
                ImageAttachment(frame, "image/jpeg", 1, "animation", first_frame=True)
                if remaining == len(frame)
                else ImageOmission(1)
            )
            assert loaded == (current, expected)

    async def test_undecodable_gif_document_uses_preview_or_explicit_failure_note(
        self,
    ) -> None:
        document = {
            "file_id": "gif",
            "file_unique_id": "gif",
            "file_name": "picture.GIF",
            "mime_type": "application/octet-stream",
            "thumbnail": THUMBNAIL,
        }
        images = await self.images.load(image_message(photo=None, document=document))
        assert len(images) == 1
        assert isinstance(images[0], ImageAttachment)
        assert images[0].message_id == 1
        assert images[0].data == PNG
        assert images[0].media_type == "image/png"
        assert images[0].preview_kind == "animation"
        assert self.get_file.await_args_list == [
            call("gif", request_timeout=10),
            call("thumbnail", request_timeout=10),
        ]
        assert not images[0].first_frame
        self.get_file.reset_mock()
        assert await self.images.load(
            image_message(photo=None, document={**document, "thumbnail": None})
        ) == (ImageOmission(1, "decode_failed"),)
        self.get_file.assert_awaited_once_with("gif", request_timeout=10)

    async def test_gif_document_decodes_original_without_a_thumbnail(self) -> None:
        self.chunks = [animation_bytes(format="gif")]
        self.images = TelegramImages(self.bot, max_bytes=100_000)
        images = await self.images.load(
            image_message(
                photo=None,
                document={
                    "file_id": "gif",
                    "file_unique_id": "gif",
                    "mime_type": "image/gif",
                },
            )
        )
        assert len(images) == 1
        assert isinstance(images[0], ImageAttachment)
        assert images[0].first_frame
        self.get_file.assert_awaited_once_with("gif", request_timeout=10)

    async def test_oversized_animation_preview_skips_download(self) -> None:
        images = await self.images.load(
            image_message(
                photo=None,
                animation={
                    **ANIMATION,
                    "file_size": len(PNG) + 1,
                    "thumbnail": {**THUMBNAIL, "file_size": len(PNG) + 1},
                },
            )
        )
        assert images == (ImageOmission(1),)
        self.get_file.assert_not_awaited()
        self.stream.assert_not_called()

    async def test_reply_animation_uses_first_original_frame_from_same_chat(self) -> None:
        self.chunks = [animation_bytes()]
        self.images = TelegramImages(self.bot, max_bytes=100_000)
        parent = image_message(photo=None, animation=ANIMATION).model_dump()
        images = await self.images.load(
            image_message(
                photo=None, text="Explain", message_id=2, reply_to_message=parent
            )
        )
        assert isinstance(images[0], ImageAttachment)
        assert images[0].message_id == 1
        assert images[0].preview_kind == "animation"
        assert images[0].first_frame
        self.get_file.assert_awaited_once_with("animation", request_timeout=10)
        self.get_file.reset_mock()
        for changed in (
            {"chat": {"id": 43, "type": "private"}},
            {"message_thread_id": 7},
        ):
            assert (
                await self.images.load(
                    image_message(
                        photo=None, text="Explain", reply_to_message={**parent, **changed}
                    )
                )
                == ()
            )
        self.get_file.assert_not_awaited()

    async def test_ordinary_group_reply_threads_share_image_access(self) -> None:
        chat = {"id": -123, "type": "supergroup", "title": "Group"}
        parent = image_message(chat=chat, message_thread_id=7).model_dump()
        images = await self.images.load(
            image_message(
                chat=chat,
                photo=None,
                message_id=2,
                message_thread_id=8,
                reply_to_message=parent,
            )
        )
        assert isinstance(images[0], ImageAttachment)

    async def test_forum_topics_isolate_images_and_general_topic_is_normalized(
        self,
    ) -> None:
        chat = {"id": -123, "type": "supergroup", "title": "Forum", "is_forum": True}
        parent = image_message(
            chat=chat, message_thread_id=7, is_topic_message=True
        ).model_dump()
        images = await self.images.load(
            image_message(
                chat=chat,
                photo=None,
                message_id=2,
                message_thread_id=8,
                is_topic_message=True,
                reply_to_message=parent,
            )
        )
        assert images == ()
        self.get_file.assert_not_awaited()
        general = image_message(chat=chat, is_topic_message=True).model_dump()
        images = await self.images.load(
            image_message(
                chat=chat,
                photo=None,
                message_id=2,
                message_thread_id=1,
                is_topic_message=True,
                reply_to_message=general,
            )
        )
        assert isinstance(images[0], ImageAttachment)
