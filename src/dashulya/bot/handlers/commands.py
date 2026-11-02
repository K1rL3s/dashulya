import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from dishka.integrations.aiogram import FromDishka, inject

from dashulya.bot.middlewares.group_tracking import GROUP_TYPES
from dashulya.domain.groups import GroupRegistry


@inject
async def start(message: Message, groups: FromDishka[GroupRegistry]) -> None:
    if message.chat.type in GROUP_TYPES:
        await groups.activate(message.chat.id)
        await message.answer(
            "I'm active in this group now 💋\n"
            "Mention me, reply to me, or call me Dashulya when you need me."
        )
        return
    await message.answer(
        "Hi! I'm Dashulya 💋\n"
        "Send me a message in a private chat to talk to me.\n"
        "Use /help to see what I can do."
    )


@inject
async def stop(message: Message, groups: FromDishka[GroupRegistry]) -> None:
    await groups.deactivate(message.chat.id)
    await message.answer(
        "Replies paused in this group 💋\n"
        "Message capture continues. Only my owner can enable replies again with /start."
    )


@inject
async def ban(
    message: Message, command: CommandObject, groups: FromDishka[GroupRegistry]
) -> None:
    username = (command.args or "").strip()
    if re.fullmatch(r"@[A-Za-z0-9_]{1,32}", username) is None:
        await message.answer("Usage: /ban @username — provide one Telegram username.")
        return
    username = username.lower()
    user_id = await groups.resolve_username(message.chat.id, username[1:])
    if user_id is None:
        await message.answer(
            f"I couldn't identify {username} in this group's saved messages. "
            "Ask them to send a message, then try again."
        )
        return
    await groups.ban(message.chat.id, user_id)
    await message.answer(f"{username} can no longer trigger my replies in this group.")


async def show_help(message: Message) -> None:
    await message.answer(
        "Here's what I can do 💋\n\n"
        "/start — introduce myself; activate me in this group\n"
        "/stop — owner or chat admins can pause group replies; keep saving messages\n"
        "/ban @username — owner or chat admins can block a user's reply triggers\n"
        "/help — show this guide\n"
        "/groups — list the groups I know I'm in\n"
        "/clear — reset your private conversation\n\n"
        "Send me a private message, mention my @username, or reply to me in a group. "
        "I also check whether messages naming Dashulya need an answer; "
        "/clear works only in private chats.\n\n"
        "Only my owner can enable group replies with /start. "
        "The owner or that group's administrators can pause them with /stop. "
        "Message capture continues while replies are paused.\n\n"
        "With chat history connected, I can also find earlier discussions "
        "and link to the original messages."
    )


def create_router() -> Router:
    router = Router(name=__name__)
    router.message.register(start, CommandStart())
    router.message.register(stop, Command("stop"), F.chat.type.in_(GROUP_TYPES))
    router.message.register(ban, Command("ban"), F.chat.type.in_(GROUP_TYPES))
    router.message.register(show_help, Command("help"))
    return router
