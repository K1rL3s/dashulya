import asyncio
import json
import sys
from contextlib import AsyncExitStack
from typing import Any
from unittest.mock import patch

import httpx2 as httpx
import pytest
from ag2 import Agent, MemoryStream
from ag2.events import TextInput, ToolCallEvent, ToolResultEvent
from ag2.testing import TestConfig

from dashulya.domain.messages import ReplyBatch
from dashulya.main import create_container
from dashulya.main.config import Settings
from tests.support import (
    TEST_API_KEY,
    MemoryOverride,
    ModelOverrides,
    input_payload,
    reply_json,
)

VIDEO_ID = "UF8uR6Z6KLc"
VIDEO_URL = "https://www.youtube.com/watch?v=UF8uR6Z6KLc"


@pytest.mark.usefixtures("_setup")
class TestYouTubeTranscript:
    @pytest.fixture
    def _setup(self, cleanup: AsyncExitStack) -> None:
        self.cleanup = cleanup
        self.metadata: dict[str, Any] = {
            "id": VIDEO_ID,
            "title": "A commencement address",
            "channel": "University",
            "duration": 900,
            "language": "en",
            "subtitles": {
                "en": [
                    {
                        "ext": "json3",
                        "url": "https://www.youtube.com/api/timedtext?lang=en",
                    }
                ],
            },
            "automatic_captions": {
                "en-orig": [
                    {
                        "ext": "json3",
                        "url": "https://www.youtube.com/api/timedtext?lang=en-auto",
                    }
                ],
            },
        }
        self.captions: dict[str, Any] = {
            "events": [
                {
                    "tStartMs": 1250,
                    "dDurationMs": 2000,
                    "segs": [{"utf8": "Stay "}, {"utf8": "hungry."}],
                },
                {
                    "tStartMs": 4500,
                    "dDurationMs": 1750,
                    "segs": [{"utf8": "Stay foolish."}],
                },
            ],
        }
        self.commands: list[tuple[str, ...]] = []
        self.requests: list[httpx.Request] = []
        self.exit_code = 0
        self.http_status = 200
        self.caption_variants: dict[str, dict[str, Any]] = {}
        self.stall = False
        self.processes: list[asyncio.subprocess.Process] = []
        create_process = asyncio.create_subprocess_exec

        async def extract(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
            self.commands.append(args)
            script = "import json, sys; metadata = json.load(sys.stdin); "
            if args[1] == "-c":
                script += (
                    "import yt_dlp; "
                    "yt_dlp.YoutubeDL.extract_info = lambda self, url, *, download: "
                    "metadata if download is False else sys.exit('Video download attempted'); "
                    f"sys.argv = ['youtube-metadata', {args[-1]!r}]; "
                    f"exec({args[2]!r}); "
                )
            else:
                script += "print(json.dumps(metadata)); "
            script += f"sys.exit({self.exit_code})"
            if self.stall:
                script = "import time; time.sleep(30)"
            process = await create_process(
                sys.executable, "-c", script, stdin=asyncio.subprocess.PIPE, **kwargs
            )
            self.processes.append(process)
            assert process.stdin is not None
            process.stdin.write(json.dumps(self.metadata).encode())
            await process.stdin.drain()
            process.stdin.close()
            return process

        def respond(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(
                self.http_status,
                json=self.caption_variants.get(
                    request.url.params.get("lang", ""), self.captions
                ),
            )

        client_type = httpx.AsyncClient

        def client(**kwargs: Any) -> httpx.AsyncClient:
            return client_type(transport=httpx.MockTransport(respond), **kwargs)

        self.cleanup.enter_context(patch("asyncio.create_subprocess_exec", extract))
        self.cleanup.enter_context(patch("httpx2.AsyncClient", client))

    async def transcript(self, **arguments: Any) -> dict[str, Any]:
        return (await self.transcripts(arguments))[0]

    async def transcripts(self, *arguments: dict[str, Any]) -> list[dict[str, Any]]:
        container = create_container(
            Settings(
                bot_token="123456789:TEST_ONLY_NOT_A_REAL_BOT_TOKEN",
                openai_api_key=TEST_API_KEY,
                owner_user_id=42,
            ),
            MemoryOverride(),
            ModelOverrides(
                reply=TestConfig(
                    *(
                        ToolCallEvent("youtube_transcript", arguments=json.dumps(call))
                        for call in arguments
                    ),
                    reply_json("Готово"),
                ),
                decision=TestConfig("false"),
            ),
        )
        self.cleanup.push_async_callback(container.close)
        agent = await container.get(Agent[ReplyBatch])
        stream = MemoryStream()
        await agent.ask("Аннотируй ролик", stream=stream)
        results = [
            e for e in await stream.history.get_events() if isinstance(e, ToolResultEvent)
        ]
        payloads = []
        for result in results:
            part = result.result.parts[0]
            assert isinstance(part, TextInput)
            assert part.content.startswith('<external_input source="web">')
            data = json.loads(input_payload(part.content))
            assert isinstance(data, dict)
            payloads.append(data)
        return payloads

    async def test_reads_original_captions_with_timestamps_through_reply_agent(
        self,
    ) -> None:
        result = await self.transcript(url="https://youtu.be/UF8uR6Z6KLc?t=1")
        assert result == {
            "url": VIDEO_URL,
            "title": "A commencement address",
            "channel": "University",
            "duration_seconds": 900,
            "language": "en",
            "is_generated": False,
            "transcript": "[1.250] Stay hungry.\n[4.500] Stay foolish.",
            "offset": 0,
            "next_offset": None,
            "total_segments": 2,
        }
        assert self.requests[0].url.params["lang"] == "en"
        assert self.commands[0][-1] == VIDEO_URL

    async def test_large_multiformat_metadata_does_not_block_reading_captions(
        self,
    ) -> None:
        self.metadata["automatic_captions"]["en-orig"].extend(
            {
                "ext": "vtt",
                "url": "https://www.youtube.com/api/timedtext?unused=" + "x" * 1_000_000,
            }
            for _ in range(9)
        )
        result = await self.transcript(url=VIDEO_URL)
        assert result.get("transcript") == "[1.250] Stay hungry.\n[4.500] Stay foolish."
        assert result["next_offset"] is None
        assert len(self.requests) == 1

    @pytest.mark.parametrize("source", ["metadata", "captions"])
    async def test_local_size_limit_is_not_reported_as_youtube_access_failure(
        self, source: str
    ) -> None:
        if source == "metadata":
            self.metadata["subtitles"]["en"][0]["url"] += "&large=" + "x" * 9_000_000
        else:
            self.captions = {
                "events": [{"tStartMs": 0, "segs": [{"utf8": "x" * 9_000_000}]}]
            }
        result = await self.transcript(url=VIDEO_URL)
        assert result["error"] == "source_too_large"
        assert result["source"] == source
        assert "local size limit" in result["message"]
        assert "Do not retry automatically" in result["message"]
        assert "transcript" not in result

    async def test_uses_automatic_original_instead_of_a_translated_track(self) -> None:
        self.metadata.pop("language")
        self.metadata["subtitles"] = {
            "ru": [
                {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?lang=ru"}
            ],
        }
        self.captions["events"] = [
            {"tStartMs": 0, "segs": [{"utf8": "Original English speech."}]},
            {"tStartMs": 1000, "segs": [{"utf8": "\n"}]},
        ]
        result = await self.transcript(url=VIDEO_URL)
        assert result["language"] == "en"
        assert result["is_generated"] is True
        assert result["transcript"] == "[0.000] Original English speech."
        assert self.requests[0].url.params["lang"] == "en-auto"

    @pytest.mark.parametrize("has_captions", [True, False])
    async def test_reports_unavailable_captions_without_inventing_a_transcript(
        self, has_captions: bool
    ) -> None:
        if not has_captions:
            self.metadata["subtitles"] = {}
            self.metadata["automatic_captions"] = {}
        result = await self.transcript(url=VIDEO_URL, language="ja")
        assert result["error"] == (
            "language_unavailable" if has_captions else "no_subtitles"
        )
        assert result["available_languages"] == (["en"] if has_captions else [])
        assert "Do not retry automatically" in result["message"]
        assert "transcript" not in result
        assert self.requests == []

    async def test_pages_long_transcripts_without_losing_text_or_downloading_again(
        self,
    ) -> None:
        texts = [f"{i:03} " + "x" * 96 for i in range(150)] + ["Я" * 15000]
        self.captions["events"] = [
            {"tStartMs": i * 1000, "dDurationMs": 1000, "segs": [{"utf8": text}]}
            for i, text in enumerate(texts)
        ]
        result = await self.transcript(url=VIDEO_URL)
        assert result["next_offset"] is None, (
            "A 30,000-character transcript must fit in one call"
        )
        assert result["total_segments"] == 158
        lines = result["transcript"].splitlines()
        assert "".join(line.split("] ", 1)[1] for line in lines) == "".join(texts)
        assert lines[-1].startswith("[150.000] ")
        assert len(result["transcript"]) <= 60000
        assert len(self.commands) == len(self.requests) == 1

    async def test_language_selection_reuses_video_metadata_and_original_captions(
        self,
    ) -> None:
        self.metadata["subtitles"]["ru"] = [
            {"ext": "json3", "url": "https://www.youtube.com/api/timedtext?lang=ru"}
        ]
        self.caption_variants["ru"] = {
            "events": [{"tStartMs": 1000, "segs": [{"utf8": "Русская дорожка."}]}],
        }
        results = await self.transcripts(
            {"url": VIDEO_URL, "language": "ja"},
            {"url": VIDEO_URL},
            {"url": "https://youtu.be/UF8uR6Z6KLc", "language": "en"},
            {"url": VIDEO_URL, "language": "ru"},
            {"url": "https://youtu.be/jNQXAC9IVRw"},
        )
        assert results[0]["error"] == "language_unavailable"
        assert results[1] == results[2]
        assert results[3]["language"] == "ru"
        assert results[3]["transcript"] == "[1.000] Русская дорожка."
        assert results[4]["url"] == "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        assert len(self.commands) == 2, (
            "Selecting another language must reuse video metadata"
        )
        assert len(self.requests) == 3, (
            "Default and explicit original language share captions"
        )

    async def test_large_pages_preserve_every_caption_and_respect_the_text_budget(
        self,
    ) -> None:
        texts = [f"{i:02} " + "я" * 1997 for i in range(29)] + [
            "29 " + "я" * 1708,
            "Tail",
        ]
        self.captions["events"] = [
            {"tStartMs": i * 1000, "segs": [{"utf8": text}]}
            for i, text in enumerate(texts)
        ]
        results = await self.transcripts(
            {"url": VIDEO_URL},
            {"url": VIDEO_URL, "offset": 30},
        )
        assert [result["next_offset"] for result in results] == [30, None]
        assert [result["offset"] for result in results] == [0, 30]
        assert all(result["total_segments"] == 31 for result in results)
        assert len(results[0]["transcript"]) == 60000
        assert all(len(result["transcript"]) <= 60000 for result in results)
        assert results[1]["transcript"] == "[30.000] Tail"
        lines = "\n".join(result["transcript"] for result in results).splitlines()
        assert "".join(line.split("] ", 1)[1] for line in lines) == "".join(texts)
        assert len(self.commands) == len(self.requests) == 1

    @pytest.mark.parametrize("failure", ["extractor", "http", "malformed", "timeout"])
    async def test_failure_is_bounded_redacted_and_not_retried(
        self, failure: str
    ) -> None:
        self.exit_code = 1 if failure == "extractor" else 0
        self.http_status = 429 if failure == "http" else 200
        if failure == "malformed":
            self.captions = {"error": "private-provider-body"}
        self.stall = failure == "timeout"
        with patch(
            "dashulya.ai.tools.youtube.YOUTUBE_TIMEOUT_SECONDS", 0.3 if self.stall else 45
        ):
            result = await self.transcript(url=VIDEO_URL)
        assert result["error"] == ("timeout" if self.stall else "provider_error")
        assert "Do not retry automatically" in result["message"]
        assert "private-provider-body" not in str(result)
        assert "transcript" not in result
        assert len(self.commands) == 1
        assert all(process.returncode is not None for process in self.processes)

    @pytest.mark.parametrize(
        "url",
        [
            "https://youtube.com.evil.test/watch?v=UF8uR6Z6KLc",
            "http://127.0.0.1/",
            "https://youtube.com/playlist?list=test",
        ],
    )
    async def test_rejects_non_video_urls_before_any_network_access(
        self, url: str
    ) -> None:
        result = await self.transcript(url=url)
        assert result["error"] == "invalid_url"
        assert "transcript" not in result
        assert self.commands == []
        assert self.requests == []
