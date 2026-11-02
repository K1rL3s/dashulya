# Bot behavior

## Access and activation

Authorize private messages, `/start` and other owner commands using the actual
sender's numeric `from_user.id == OWNER_USER_ID`. Forwarded identities and
authorship bindings grant no access. Group `/stop`, `/ban` and `/unban` are also available to the current chat's creator
and administrators, checked with `getChatAdministrators` for that chat on each
request. A failed or timed-out lookup denies access and logs a redacted failure;
the configured owner's commands do not depend on this lookup.

Anonymous administrators may use `/stop`, `/ban` and `/unban` only when Telegram's authenticated
`sender_chat.id` equals the current group's numeric ID. A forwarded identity or
another sender chat grants no authority. Other bot senders, missing senders and
unauthorized users are silently ignored before command execution or model
invocation.

After the owner activates a group, human participants outside its blacklist may
invoke group dialogue there. Addressing rules still apply. Other group commands remain
owner-only.

The owner and current chat administrators may use `/ban @username` in groups,
including inactive groups, with the same authority checks as `/stop`. Accept
exactly one `@username` using ASCII letters, digits and underscores, up to 32
characters after `@`; invalid arguments receive an English usage message.
Resolve usernames without case sensitivity from each human sender's latest saved
identity in this group. Ignore bot and channel identities; unknown, outdated or
ambiguous names receive an English explanation asking for a new message from
that participant. Store the ban by numeric user ID so a username change does not
remove it. Repeated bans are idempotent.
`/unban @username` removes the resolved sender's ban in the current group, using
the same authority, argument validation and latest-username lookup. Repeating it
confirms the removal even when no ban exists. The removal persists across
restarts, leaves other users and groups unchanged, and does not activate a paused
group. Both commands accept the bot-qualified form and work while inactive.
The blacklist is scoped to the group across topics and persists in PostgreSQL.
Check it before addressing, image loading or reply generation for all human
senders, including the owner. Authorized administrative commands remain available.
Capture and saved history remain unchanged; banned users' originals can still
appear in context and memory. Private access and other groups are unaffected.
Preserve bans across metadata updates and supergroup migration; remove them when
the bot departs, along with the group registry entry.

Every group starts inactive, including existing groups on upgrade. Only the
actual human owner's `/start` in that group activates replies. The bot owner or a
chat administrator can use `/stop` to pause replies throughout the current group,
across topics. The command confirms the pause even if the group is already
inactive. `/stop` is group-only; `/start`
resumes group dialogue. Both activation and deactivation persist across restarts.
While inactive, the owner may use `/start`, `/stop`, `/ban` and `/unban`, and chat administrators
may use `/stop`, `/ban` and `/unban`; other commands, addressing decisions and reply generation remain
silent, including the owner's. Activation grants group dialogue access to human
participants outside the blacklist.

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
Private histories remain in memory until restart or `/clear`. Serialize private
generation, delivery and recording within a conversation. Group requests generate
concurrently in separate streams; serialize only sending and recording of each
complete reply batch within its chat/topic. Ready group replies may overtake slow
generations, while messages from different batches cannot interleave.

Carry original Telegram `message.date` in `BOT_TIMEZONE`. Both agents' system
prompts declare the IANA timezone; user context omits that declaration. Private
inputs use `DD Month YYYY (Weekday) HH:MM:SS`, adding a numeric UTC offset during
repeated local hours. Compute the weekday after conversion and preserve offset
changes. Interpret relative dates using send time, including delayed messages.

Render Markdown using the slop-writer-based renderer in
[`bot/formatting.py`](../../src/dashulya/bot/formatting.py). Preserve formatting
and UTF-16 offsets when chunking; tables become monospace blocks. All replies
use ordinary Telegram messages with explicit entities. The application marks
each reply with more than 600 rendered Unicode characters, or any code block or
table, with an `expandable_blockquote` spanning its body. This policy does not
depend on model markup. Keep the first prose paragraph outside that span only
when it has at most 240 characters and two lines and other paragraphs follow.
Headings, lists and quotes stay inside. Every continuation chunk is marked for
collapse, including short final chunks. Telegram controls the visible preview
and expansion arrow; do not add a separate disclosure title or padding.

Preserve text, links, authored emphasis, inline code and preformatted languages
inside the collapsible span. Telegram cannot nest quotations, so render inner
quoted passages as italic spans instead. Preserve all UTF-16 offsets and the full
rendered text in recorded history. Use the ordinary message renderer to avoid
the heavier default font observed for Rich Messages in the user's client.
Keep the typing indicator and Telegram-length limits. Attempt each chunk once, including
after ambiguous network errors. Keep provider errors and credentials out of
logs and replies. The Jester persona and response instructions live in
[`ai/prompts.py`](../../src/dashulya/ai/prompts.py).

## Group registry

Preserve observed groups across restarts, refresh names, remove departed groups
and replace old IDs on supergroup migration. Telegram supplies no complete chat
list; missed updates during downtime can leave the registry stale.

Treat both absent `my_chat_member` status and a service message whose
`left_chat_member.id` is this bot's ID as departure. The service message must
remove the group rather than re-register it, regardless of which event arrives
first or who removed the bot. Another participant's departure preserves this
bot's group membership and activation.

Preserve activation across restarts, metadata refresh and migration. Departure
removes activation, so rejoining requires a new owner `/start`. `/groups` shows
activation status. Registry migration does not establish full chat-memory
migration continuity; see [the memory design](chat-memory.md).
