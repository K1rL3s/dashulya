# Reply context preview

A local prototype for exploring which saved group messages enter one request's
reply context, and how those messages become AG2 events. It uses the real
`Dialogue.select_context()` and `Dialogue.context_events()` implementations.

From the project root, with the existing Compose PostgreSQL service running:

```sh
just preview
```

Open <http://localhost:8765>. Ostrikov's chat (`-1003576522523`) opens when saved;
otherwise the chat with the most saved messages opens automatically.
Click any message on the left; the right pane displays the
actual formatted text of each AG2 event. Selection
colors distinguish the current request, recent messages and added reply parents.
Use **Load older messages** to walk back through history. Reply references and
event message IDs navigate to their saved originals. The **JSON** tab shows
the same events as a copyable JSON array. Both panes fill the window, with no
global header or filter toolbar.

Events show their ordered parts: one `TextInput` with
`Attached image (untrusted) ID: <message ID>` appended to its original text,
then an `ImageInput` placeholder in the same card. This bot's images appear in
a separate user event immediately after its assistant event. Placeholder images
use saved file references and the bot's shared event builder without downloading
bytes; actual availability, decoding and byte-budget omissions are not simulated.
The JSON view explicitly marks unloaded bytes and unknown MIME types.

Each event shows a local day heading where needed, then a time/JSON metadata
line. Every message includes `message_id`, `user_id` and `user` (the display
name), including the bot's own messages. Optional `username`, `bot` and
`author_user` preserve observed handles, known bot flags and configured authorship
bindings. Only `bot:true` is emitted for other known bots; omit it for this
assistant and false or unknown bot status. There are no separate author
declarations. Optional `reply_to` holds the
original parent message ID and is omitted for messages without a parent.
All of that metadata precedes `external_input`;
only the source text and attached-media notes are inside the envelope. The shared
chat header has no envelope. Only the last record includes `Interlocutor user id`.
Names and flags belong to their original message, preserving earlier records.
Truncated-chain navigation appears in the boundary message's JSON.

The compact **Recent ↑** and **Hops** inputs adjust selection immediately for
the selected message. Recent accepts any positive message count, including
the selected message; hops accepts any nonnegative number of reply-parent edges
from each recent message. Neither has an upper limit. Zero hops disables parent
expansion. Defaults are 20 and 5, matching the running
bot. These controls change only the preview request, not the bot's settings.
Selection colors, counts, truncated-chain notes and both event views update
together.

The **Old / Recursive** toggle compares two selection algorithms for the same
message. **Old** uses the real bot's selection unchanged. **Recursive** is a
preview-only experiment:

1. Build the usual recent window (20 messages including the request) and five
   reply-parent hops from every seed.
2. Find the earliest previous answer authored by Dashulya's configured numeric
   bot ID anywhere in that selection, including added parents. Resolve the
   incoming request that triggered that answer. Rebuild its full context using
   the same 20-message window and five parent hops.
3. Find the earliest still older Dashulya answer in this added window and its
   parents. Again anchor at its triggering request and apply exactly the same
   algorithm, until no earlier verifiable Dashulya answer remains.

An anchor must move strictly backwards; the current request, another bot's
messages and unknown authors cannot start expansion. All steps stay in the
selected chat/topic. The controls adjust the recent count and hop count at every
level. The old shrinking 20/10/5 windows are replaced with the same 20/5 selection
at every request (or the values currently selected in the controls).

Restore the earliest request first. Each newer request keeps the entire earlier
recursive input as its prefix and appends only originals not already present,
in that window's order. Do not globally sort the union: a newly discovered older
parent would otherwise be inserted into the cached prefix. Such parents append
in the new block with their original date/time. There is one event per original,
plus any adjacent assistant-image events, and one chat header from the oldest
restored block. The current request remains last.

Render each appended block independently. Earlier date headings, interlocutor
markers, image placeholders and truncated-chain metadata stay unchanged;
the final block supplies the new request's interlocutor marker. A previous
truncated-chain marker may describe an ancestor supplied later in the suffix.
This is intentional: removing it would change the reused prefix. The **Restored
prefix** note shows the previous request ID, event/message counts and text size.

The preview verifies triggers against local generation and delivery logs:
rendered output text must match the saved answer within its delivery interval,
with exactly one originating request. A reply target is not a trigger: Dashulya
can choose another recipient or send an unthreaded follow-up. Missing or ambiguous
matches, including service messages, are skipped and counted in the viewer.
The next verifiable earlier answer may still expand the context. Compose mounts
`var/logs` read-only; host startup uses that directory by default, or
`PREVIEW_LOG_DIR` when set. Log changes refresh the cached matching index.

The content of an earlier recursive preview is reproduced as the newer preview's
full prefix when data, logs, formatting settings and selection parameters stay
the same. Compare roles and ordered content parts, not AG2 event UUIDs/timestamps.
This is a reconstruction from current originals, not an archived model payload.
Source edits or changing trigger-log coverage can change reconstruction. Actual
provider cache hits also depend on unchanged system prompts, tools and native
media; this viewer does not download image bytes or reproduce tool traces.
It makes no paid model calls and does not report provider cache-hit metrics.

Blue selection marks earlier chronological windows. Added events and message
labels show their expansion step; **expansion steps** lists each triggering
request, its answer and how many new messages it contributed. Character counts, image placeholders and JSON
reflect the selected algorithm. Recursive selection can grow substantially in
a dense bot conversation; inspect the displayed size before adopting a policy.
This toggle does not change the running Telegram bot.

Open the image incident directly:
<http://localhost:8765/?chat_id=-1003576522523&message_id=113856&algorithm=recursive>.
Use the toggle to compare which earlier image originals return to the context.

The context total and each event card show character counts of the formatted
text, including spaces, newlines, JSON punctuation and shared metadata. Counts
use Unicode code points, so supplementary characters such as emoji are not
counted twice. The total is the sum of all event text counts; serialized event
envelopes, global prompts, tools and image bytes are not included.

Optional URL parameters select another conversation, for example
`/?chat_id=-1001234567890&topic_id=1`. Omit `topic_id` for ordinary groups,
including legacy reply threads. Set it only when simulating a forum topic;
General is topic 1. The database does
not retain Telegram's `is_forum` flag, so the preview does not guess it from old
thread IDs. Only numerically bound group collections are shown.
Add `message_id` to open and select a saved message directly.
Set `algorithm=recursive` to select the experiment; the default is `classic`.
Switching algorithms preserves the selected message and updates its shareable URL.

The preview reads current saved originals, not historical edit versions. It
shows the message context and AG2 event parts, excluding global agent prompts,
tool definitions/results and native image bytes. Media with saved file references
show image-placement placeholders; older attachments without references remain
text placeholders. There are no Telegram downloads or model calls. Bot messages can
also be selected for inspection, although they do not trigger bot replies in
the real Telegram router. This is a local inspection tool, not a public app.

Database connections enforce PostgreSQL read-only mode. The preview does not
initialize/migrate the schema, write messages, index data or alter bot state.
Compose publishes only the preview on loopback; PostgreSQL keeps its existing
internal port. Stop only the preview with:

```sh
docker compose -f docker-compose.yaml -f preview/compose.yaml stop preview
```

For an already host-accessible database, use the existing Python environment:

```sh
uv run --locked python preview/server.py
```

This reads `.env` from the current directory, with environment variables taking
precedence. `DATABASE_URL` and `BOT_TIMEZONE` configure the connection and dates.
`BOT_TOKEN` supplies only the numeric bot ID, without any Telegram API call;
`OWNER_USER_ID` and `OWNER_AUTHOR_IDS` preserve configured author bindings.
Use `--port` to change the default 8765, or `--host` to change the default
loopback listener. No frontend dependencies or build step are needed.
