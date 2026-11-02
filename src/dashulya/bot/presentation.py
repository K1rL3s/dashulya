"""Application-owned disclosure of long replies, independent of model markup."""

import re
from html.parser import HTMLParser
from typing import override

from aiogram.types import MessageEntity


class _ReplyText(HTMLParser):
    """Recover generated reply HTML without its application-owned summary."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.in_summary = False

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "summary":
            self.in_summary = True
        elif tag == "details" and self.parts:
            self.parts.append("\n\n")
        elif tag == "br" and not self.in_summary:
            self.parts.append("\n")

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "summary":
            self.in_summary = False

    @override
    def handle_data(self, data: str) -> None:
        if not self.in_summary:
            self.parts.append(data)


def rich_html_text(html: str) -> str:
    parser = _ReplyText()
    parser.feed(html)
    parser.close()
    return "".join(parser.parts)


def collapse_entities(
    text: str, entities: list[MessageEntity], *, markdown: str | None = None
) -> list[MessageEntity]:
    """Fold the body with ordinary Telegram entities, keeping a short prose lead."""
    lead, separator, body = text.partition("\n\n")
    prose = markdown is not None and not re.match(
        r"\s*(?:#{1,6}\s|>|[-+*•]\s|\d+[.)]\s|```|~~~|\|)", markdown
    )
    if prose and markdown is not None:
        first_block = re.split(r"\n[ \t]*\n", markdown.lstrip(), maxsplit=1)[0]
        prose = not re.search(r"(?:^|\n)[ \t]*(?:=+|-+)[ \t]*$", first_block)
    keep_lead = (
        prose
        and bool(separator and body)
        and len(lead) <= 240
        and lead.count("\n") <= 1
        and not any(
            entity.type in {"pre", "blockquote"}
            and entity.offset < len(lead.encode("utf-16-le")) // 2
            for entity in entities
        )
    )
    offset = len((lead + separator).encode("utf-16-le")) // 2 if keep_lead else 0
    length = len(text.encode("utf-16-le")) // 2
    # Telegram cannot nest quotations; retain inner quoted passages as italic text.
    result = [
        entity.model_copy(update={"type": "italic"})
        if entity.type in {"blockquote", "expandable_blockquote"}
        and entity.offset + entity.length > offset
        else entity
        for entity in entities
    ]
    result.append(
        MessageEntity(type="expandable_blockquote", offset=offset, length=length - offset)
    )
    return sorted(
        result,
        key=lambda entity: (
            entity.offset,
            -entity.length,
            entity.type != "expandable_blockquote",
        ),
    )
