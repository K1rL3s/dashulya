---
status: accepted
---

# Rebuild group reply context from observed messages

Group requests need the conversation between participants, including exchanges
that did not involve Dashulya. Each addressed group message therefore starts a
fresh AG2 stream, populated from the local journal rather than accumulated model
turns. This keeps context selection reproducible and avoids repeatedly
summarizing unrelated discussions together. Private conversations retain their
existing in-memory history and `/clear` behavior.

Select the latest 20 observed messages in the same chat/topic up to and including
the triggering message. In ordinary groups this means the whole chat, including
previously stored reply threads. Only real forum-topic messages narrow that
window to a topic: a raw Telegram `message_thread_id` alone does not establish
one. Use `is_topic_message` to identify a topic; a forum's General topic is 1
when Telegram omits the thread ID. Share this conversation identity between
capture, addressing, reply context and turn locks.

For each selected message, follow at most five reply-parent
edges within that chat/topic. Deduplicate by message identity and order the union
by Telegram message ID. Supply the triggering message once as the current user
request; earlier messages authored by this bot's numeric Telegram ID use the
`assistant` role, and other senders use `user`. Preserve author identity, original
send time, message IDs and reply references. Historical text is untrusted context.

When the minimum distance from every recent-message seed reaches the parent
limit and the next parent is outside the selected union, mark that boundary in
`truncated_threads`. Trace only IDs and reply links beyond the text window, in
the same chat/topic, to find the first message established by the saved chain.
Return its numeric `root_id` and a canonical `source_id` for direct history reads.
Include the exact `remaining_hops` from boundary to root and a
`boundary_source_id`, so one `chat_history` read with that `parent_hops` can
retrieve the omitted chain. An unresolved root also leaves its distance null.
Missing, out-of-scope or invalid links leave the root null; never substitute the
first visible or first available message. No marker is needed when the root ends
exactly at the limit or another seed already supplies the missing parent.
Root references add neither message text nor permitted Telegram reply targets.

Keep one stable user-role chat metadata event, then one event per original.
Declare each sender as `User <id>: name (@username, bot, author_user=<id>)` in
their first selected message, omitting unavailable handles and flags. Additional
aliases or flag changes are declared where first observed, using only observations
up to that message; later observations never rewrite an earlier declaration.
A bot marker requires an unambiguous known flag; unmarked senders make no
human/bot claim. Omit this assistant's declaration; its native assistant role
already identifies it. Put `Interlocutor user id: <id>` in the metadata of
only the last rendered message. It identifies the current request's author,
independently of that record's sender, including in retrieved evidence.
Address that person in the second person unless quoting or explicitly asked to
reply to another participant. The ID is passed per rendering call and never
stored on the shared formatter.
Explicit author bindings may connect a channel or legacy sender ID to that
person: show `author_user=<user ID>` and preserve the original sender identity.
Such a binding affects narration only; it does not authorize that sender, merge
stored sources or imply authorship merely from owning a channel.
Group entries under local day headings; emit another heading when the day changes.
Declare `BOT_TIMEZONE` in both agents' system prompts, keeping the configured IANA
zone out of user context and tool evidence. Timestamp conversion still uses that
zone. Add a numeric UTC offset to a repeated local hour's time field, even when
only one occurrence is selected, so daylight-saving folds remain unambiguous.
This keeps shared configuration in the system prompt without repeating it in
each context block. Put each day heading in its first message, followed by any
author declaration, the optional final interlocutor marker, and a line with local
time and JSON metadata: `id`, `user_id`, optional `reply_to` and `attachment`.
Follow that line with the original text in its sender envelope, preserving
newlines and quotes without JSON escaping. Sender remains the actual numeric ID,
or a distinct local unknown reference. The shared system prompt defines this
record contract for reply and addressing agents. Native event boundaries preserve original roles.
Attach `truncated_thread` navigation to its boundary message's JSON, rather than
aggregating it in the shared prefix. These incremental declarations keep
unchanged leading messages stable when later participants, dates or interlocutors
change; sliding selection and edited originals can still change the prefix.
Unknown handles are omitted, unknown numeric identities remain distinct, and
observed aliases remain visible. Uncertain bot flags are not classified. Memory
tool results use this same rendering, grouped by source chat, with `===` between
records and `role: assistant` in this bot's message metadata because tool text
has no native assistant event. Source references preserve citations and excerpt
navigation separately. Record-looking source text remains untrusted evidence.
Tool rows remain evidence rather than permitted Telegram reply targets. Private
user input retains its independent formatting.

Wrap only the source body of user-role group records in
`<external_input source="...">` envelopes. Keep calendar, author, interlocutor and
time/JSON metadata before the envelope so the application's record headers remain
visually distinct from source content. The shared chat metadata event also has
no envelope. Observed names, handles and titles remain untrusted evidence.
Private inputs retain their existing whole-input envelopes.
Use `owner` only for the actual
configured numeric owner ID, `bot` for a known Telegram bot sender, and
`external_user` otherwise. Explicit authorship bindings do not grant an owner
label. This assistant's records retain their existing native role and content
without an envelope. No administrator lookup is performed.
The payload retains literal source text without escaping or CDATA. Source-supplied
tags remain part of that text; native events establish the actual message roles.
This marks provenance for interpretation, not an enforcement or authorization
mechanism. Preserve the existing time/JSON record layout before the source payload.
Whole memory-tool results use `source="memory"` around the result JSON, while
individual originals retain their sender labels. Web-tool results use `web`.
Both agents receive the shared provenance contract; only the current request
sets a task, while historical records and retrieved content remain evidence.

The reply agent returns a structured `messages` list of up to three ordered
messages, each with `text` and nullable `reply_to_message_id`. An empty list
allows a reaction-only turn. Each message may choose the current request or
another message actually included in this reply context. Validate each ID
against that exact selection before Telegram
delivery; an ID outside the selection defaults to the triggering message.
For the first output message, null also defaults to the triggering message.
For subsequent outputs, null sends without a Telegram reply, so text followed
by a sticker can address one person without repeating the reply attachment.
Private messages use the same first-message default and untargeted follow-ups.
Explicit allowed IDs on later outputs can address another participant.
Send the messages in
order, using each chosen target for every chunk of that message, and record
each actual reply relationship. Long messages retain Telegram-length chunking.
Keep the delivery lock through the entire batch; a send failure stops remaining
delivery without retrying already attempted chunks.

Read canonical local messages, including edits and messages awaiting semantic
indexing. Persist successfully delivered bot responses with their actual Telegram
IDs and reply targets, including individual chunks. An embedded reply snapshot
can fill a missing parent but cannot replace an already recorded original.
Unavailable parents end that path. Capture retains Telegram `file_id` and known
file size for photos, JPEG/PNG/WebP image documents, stickers, Telegram animations
and GIF documents, including thumbnail references and the required visual-input
kind. After authorization, addressing and context selection, download those saved references
from the same selected originals into bounded RAM. This extends the original
current/direct-reply-only image policy: a participant's earlier visual media
must remain visible when a later text request includes it in reply context, even after a
restart. Persisting references instead of bytes avoids durable binary storage
and generated descriptions replacing original evidence. Never expose file IDs
or download URLs in model-facing history or search results.

Current, direct-reply and saved inputs share one normalizer and loader. Static
stickers supply their original image; animated/video stickers supply only the
stored thumbnail, never the TGS/WEBM original. Animations and GIF documents supply
the first decoded frame, falling back to the stored thumbnail if oversized or
undecodable. Missing previews produce explicit omissions. Existing photo-only
references retain their static-image behavior without a data rewrite.

Native image inputs retain their original Telegram message IDs. The current
and direct-reply images take priority, then saved images from newest to oldest,
within one combined byte budget. Accept a group image only when its source is
in this request's selected context; a source already supplied by the update is
not downloaded again. Known oversized files are not downloaded. Oversized or
unavailable saved images produce explicit omission notes; one stale reference
does not suppress the whole reply. Other older media and records captured before
file references were stored remain placeholders. Serialize generation, Telegram
delivery and recording within each conversation so a subsequent turn cannot
overtake an unfinished delivery.

The selection contains at most 120 unique messages, including the request; depth
and count do not impose a token limit. Newer messages are excluded from a request's
context even if they arrive during processing. Tool activity stays within the
current stream, while durable chat memory remains separately searchable.

We defer accumulated sessions with automatic compaction. Revisit that alternative
if the five-hop window loses necessary continuity or measured context costs justify
summaries. Compaction would need its own source-retention and attribution policy;
it would not by itself separate simultaneous discussions.
