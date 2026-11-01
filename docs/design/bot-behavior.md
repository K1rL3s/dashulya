# Bot behavior

## Access and activation

Authorize private messages and commands using the actual sender's numeric
`from_user.id == OWNER_USER_ID`. Forwarded identities and authorship bindings
grant no access. Silently ignore bot senders, missing senders and unauthorized
users before command execution or model invocation.

After activation, human participants may invoke group dialogue in
`PUBLIC_GROUP_IDS`; the owner retains access to other groups. Addressing rules
still apply. Group commands remain owner-only.

Every group starts inactive, including existing groups on upgrade. Only the
actual human owner's `/start` in that group activates replies. Until then, all
other commands, addressing decisions and reply generation remain silent,
including the owner's. Activation preserves participant access rules.

## Routing and capture

Authorized private messages and explicit group mentions/replies to this bot
invoke the reply agent directly. Group messages naming Dashulya, including
“Дашуль” in any letter case, first pass an AG2 OpenAI Decisions classifier with
bounded chat/topic context. A negative decision or classifier failure stays silent.
`/clear` resets the current private conversation.

Group tracking and message capture run before sender access checks, including
while a group is inactive. Passive messages update group metadata; membership
updates also maintain the registry. Save received group messages from all
participants, owner-private messages and successful deliveries; exclude other
private senders. Index messages with background cached embeddings. Capture,
edits and worker recovery follow the
[incoming-message rules](chat-memory.md#incoming-group-messages).

## Conversation context and delivery

Reuse one AG2 `Agent`. Follow
[ADR 0002](../adr/0002-rebuild-group-reply-context.md) for group context selection,
roles, incremental author/date declarations, media and ordered reply targets.
Private histories remain in memory until restart or `/clear`. Serialize
generation, delivery and recording within a conversation while allowing other
conversations to progress.

Carry original Telegram `message.date` in `BOT_TIMEZONE`. Both agents' system
prompts declare the IANA timezone; user context omits that declaration. Private
inputs use `DD Month YYYY (Weekday) HH:MM:SS`, adding a numeric UTC offset during
repeated local hours. Compute the weekday after conversion and preserve offset
changes. Interpret relative dates using send time, including delayed messages.

Render Markdown into ordinary Telegram messages with explicit entities using
the slop-writer-based renderer in
[`bot/formatting.py`](../../src/dashulya/bot/formatting.py). Preserve formatting
and UTF-16 offsets when chunking; tables become monospace blocks. Keep the
typing indicator and Telegram-length limits. Attempt each chunk once, including
after ambiguous network errors. Keep provider errors and credentials out of
logs and replies. The Jester persona and response instructions live in
[`ai/prompts.py`](../../src/dashulya/ai/prompts.py).

## Group registry

Preserve observed groups across restarts, refresh names, remove departed groups
and replace old IDs on supergroup migration. Telegram supplies no complete chat
list; missed updates during downtime can leave the registry stale.

Preserve activation across restarts, metadata refresh and migration. Departure
removes activation, so rejoining requires a new owner `/start`. `/groups` shows
activation status. Registry migration does not establish full chat-memory
migration continuity; see [the memory design](chat-memory.md).
