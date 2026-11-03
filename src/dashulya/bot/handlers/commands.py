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
            "Теперь я отвечаю в этой группе 💋\n"
            "Упомяните меня через @, ответьте на моё сообщение или позовите Дашулю."
        )
        return
    await message.answer(
        "Привет! Я Дашуля 💋\n"
        "Напиши мне в личку, чтобы поболтать.\n"
        "Команды и подсказки — в /help."
    )


@inject
async def stop(message: Message, groups: FromDishka[GroupRegistry]) -> None:
    await groups.deactivate(message.chat.id)
    await message.answer(
        "Мои ответы в этой группе приостановлены 💋\n"
        "Сообщения продолжают сохраняться. "
        "Снова включить ответы может только мой владелец командой /start."
    )


@inject
async def manage_blacklist(
    message: Message, command: CommandObject, groups: FromDishka[GroupRegistry]
) -> None:
    username = (command.args or "").strip()
    if re.fullmatch(r"@[A-Za-z0-9_]{1,32}", username) is None:
        await message.answer(
            f"Формат: /{command.command} @username — укажите один юзернейм в Telegram."
        )
        return
    username = username.lower()
    user_id = await groups.resolve_username(message.chat.id, username[1:])
    if user_id is None:
        await message.answer(
            f"Не удалось найти {username} в сохранённых сообщениях этой группы. "  # ruff: ignore[ambiguous-unicode-character-string]
            "Попросите участника написать сообщение и попробуйте ещё раз."
        )
        return
    if command.command == "ban":
        await groups.ban(message.chat.id, user_id)
        await message.answer(
            f"Больше не буду отвечать на обращения {username} в этой группе."
        )
    else:
        await groups.unban(message.chat.id, user_id)
        await message.answer(
            f"Снова могу отвечать на обращения {username} в этой группе."
        )


async def show_help(message: Message) -> None:
    await message.answer(
        "Вот что я умею 💋\n\n"
        "Справка и личный диалог\n"
        "/help — показать эту справку\n"
        "/clear — начать личный диалог заново\n\n"
        "Управление группами\n"
        "/groups — показать известные мне группы и статус ответов\n"
        "/start — познакомиться в личке или включить ответы в текущей группе\n"
        "/stop — приостановить ответы в текущей группе\n\n"
        "Ограничения участников\n"
        "/ban @username — запретить участнику вызывать мои ответы в этой группе\n"
        "/unban @username — снова разрешить участнику вызывать мои ответы\n\n"
        "Напиши мне в личку, упомяни мой @username в группе или ответь "
        "на моё сообщение. Ещё можно позвать меня по имени — Дашуля. "
        "/clear работает только в личных сообщениях.\n\n"
        "Личный диалог и команды доступны моему владельцу. "
        "Включить ответы в группе через /start может только он. "
        "Команды /stop, /ban и /unban также доступны администраторам этой группы. "
        "Во время паузы сообщения продолжают сохраняться.\n\n"  # ruff: ignore[ambiguous-unicode-character-string]
        "Если подключена история чата, могу находить прошлые обсуждения "
        "и давать ссылки на исходные сообщения."
    )


def create_router() -> Router:
    router = Router(name=__name__)
    router.message.register(start, CommandStart())
    router.message.register(stop, Command("stop"), F.chat.type.in_(GROUP_TYPES))
    router.message.register(
        manage_blacklist, Command("ban", "unban"), F.chat.type.in_(GROUP_TYPES)
    )
    router.message.register(show_help, Command("help"))
    return router
