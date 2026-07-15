import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatMemberUpdated,
)
from aiogram.exceptions import TelegramBadRequest

from openai import AsyncOpenAI

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")

# Кастомный OpenAI-совместимый эндпоинт (ваш прокси)
AI_API_KEY = os.getenv("AI_API_KEY", "sk-cdt-PUT_YOUR_KEY_HERE")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://conduit.ozdoev.net/v1")
AI_MODEL = os.getenv("AI_MODEL", "deepseek-v4-flash-free")

# Ваш личный Telegram user_id (числовой, узнать можно у @userinfobot).
# Только этот пользователь (плюс те, кого он добавит через /addmoder)
# может добавлять бота в группы.
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Сколько предупреждений нужно набрать, чтобы получить бан
WARN_LIMIT = 3

# Через сколько дней предупреждение "сгорает" само по себе
WARN_EXPIRY_DAYS = 21

# Права, которые бот попросит при добавлении в группу через кнопку /start
REQUESTED_ADMIN_RIGHTS = "restrict_members,delete_messages,invite_users"

BASE_DIR = Path(__file__).parent
WARNS_FILE = BASE_DIR / "warns.json"
BADWORDS_FILE = BASE_DIR / "badwords.txt"
MODERATORS_FILE = BASE_DIR / "moderators.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mod-bot")

ai_client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

BOT_USERNAME: str | None = None  # заполняется при старте


# ---------------------------------------------------------------------------
# СЛОВАРЬ ЗАПРЕЩЁННЫХ СЛОВ (грузится из badwords.txt, по одному слову на строку)
# ---------------------------------------------------------------------------

def load_bad_words_pattern() -> re.Pattern:
    if not BADWORDS_FILE.exists():
        log.warning("Файл %s не найден — локальный фильтр отключён", BADWORDS_FILE)
        return re.compile(r"(?!x)x")

    words = [
        line.strip()
        for line in BADWORDS_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    if not words:
        return re.compile(r"(?!x)x")

    pattern = "|".join(re.escape(w) for w in words)
    return re.compile(f"({pattern})", re.IGNORECASE)


BAD_WORDS_PATTERN = load_bad_words_pattern()

# ---------------------------------------------------------------------------
# МОДЕРАТОРЫ (кому, кроме владельца, разрешено добавлять бота в группы)
# ---------------------------------------------------------------------------

def load_moderators() -> set[int]:
    if MODERATORS_FILE.exists():
        try:
            return set(json.loads(MODERATORS_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_moderators(mods: set[int]) -> None:
    MODERATORS_FILE.write_text(json.dumps(sorted(mods)), encoding="utf-8")


moderators: set[int] = load_moderators()


def is_authorized_adder(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in moderators


# ---------------------------------------------------------------------------
# ХРАНИЛИЩЕ ВАРНОВ
# Формат: {"chat_id:user_id": [timestamp1, timestamp2, ...]}
# Каждый варн хранит время выдачи и автоматически "сгорает"
# через WARN_EXPIRY_DAYS дней.
# ---------------------------------------------------------------------------

def load_warns() -> dict:
    if WARNS_FILE.exists():
        try:
            return json.loads(WARNS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_warns(data: dict) -> None:
    WARNS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


warns: dict = load_warns()


def warn_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def _prune(chat_id: int, user_id: int) -> list:
    """Убирает протухшие (>WARN_EXPIRY_DAYS) варны и возвращает свежие."""
    key = warn_key(chat_id, user_id)
    entries = warns.get(key, [])
    cutoff = time.time() - WARN_EXPIRY_DAYS * 86400
    fresh = [t for t in entries if t > cutoff]
    if fresh != entries:
        if fresh:
            warns[key] = fresh
        else:
            warns.pop(key, None)
        save_warns(warns)
    return fresh


def get_warn_count(chat_id: int, user_id: int) -> int:
    return len(_prune(chat_id, user_id))


def add_warn(chat_id: int, user_id: int) -> int:
    key = warn_key(chat_id, user_id)
    fresh = _prune(chat_id, user_id)
    fresh.append(time.time())
    warns[key] = fresh
    save_warns(warns)
    return len(fresh)


def remove_one_warn(chat_id: int, user_id: int) -> int:
    """Снимает один (последний выданный) варн. Возвращает новое количество."""
    key = warn_key(chat_id, user_id)
    fresh = _prune(chat_id, user_id)
    if fresh:
        fresh.pop()
    if fresh:
        warns[key] = fresh
    else:
        warns.pop(key, None)
    save_warns(warns)
    return len(fresh)


def clear_warns(chat_id: int, user_id: int) -> None:
    """Снимает вообще все варны пользователя."""
    warns.pop(warn_key(chat_id, user_id), None)
    save_warns(warns)


# ---------------------------------------------------------------------------
# ЗАПРОС К GEMINI
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """Ты — модератор исламского чата.

Главное правило:
Если сообщение содержит нецензурную лексику, оскорбления, унижения, насмешки или проявления ненависти, направленные в адрес Ислама, Аллаха, Пророка Мухаммада ﷺ, Корана, мечетей, мусульман (именно как религиозной группы) или исламских святынь, немедленно выдай решение BAN.

Если нецензурная лексика присутствует, но она не направлена против Ислама или его святынь, не бань по этому правилу.

Если сообщение обсуждает Ислам нейтрально, задает вопросы, критикует в уважительной форме или ведет академическую дискуссию без оскорблений — не бань.

Другие религии (христианство, иудаизм, буддизм, индуизм и т.д.) по этому правилу не модерируются. Игнорируй оскорбления, мат или критику в их адрес.

Ответ должен быть только одним словом:

NOTHING
— если в сообщении нет оскорблений/мата, ИЛИ мат есть, но направлен
  на конкретного человека (не на религию/веру).

RELIGION|<короткая причина 2-4 слова на русском>
— если оскорбление/мат направлено на религию, веру, Бога, пророков,
  священные тексты, конфессии, верующих как группу.

Примеры причин: "оскорбление ислама", "оскорбление веры", "оскорбление религии".

Сообщение пользователя:
\"\"\"{text}\"\"\"

Ответ:"""


async def classify_message(text: str) -> tuple[str, str | None]:
    """Возвращает (category, reason). category: NOTHING | RELIGION"""
    prompt = PROMPT_TEMPLATE.format(text=text)
    try:
        response = await ai_client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=30,
        )
        raw = (response.choices[0].message.content or "").strip()
    except Exception as e:
        log.exception("AI request failed: %s", e)
        return "NOTHING", None

    if raw.upper().startswith("RELIGION"):
        parts = raw.split("|", 1)
        reason = parts[1].strip() if len(parts) > 1 else "оскорбление веры"
        return "RELIGION", reason

    return "NOTHING", None


# ---------------------------------------------------------------------------
# ПРОВЕРКА ПРАВ БОТА / СТАТУСА ПОЛЬЗОВАТЕЛЯ / ПОИСК СОЗДАТЕЛЯ ЧАТА
# ---------------------------------------------------------------------------

async def bot_can_ban(chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
        return (
            member.status == ChatMemberStatus.ADMINISTRATOR
            and getattr(member, "can_restrict_members", False)
        )
    except Exception:
        return False


async def get_user_status(chat_id: int, user_id: int) -> str:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status
    except Exception:
        return "member"


async def get_chat_creator_id(chat_id: int) -> int | None:
    try:
        admins = await bot.get_chat_administrators(chat_id)
        for a in admins:
            if a.status == ChatMemberStatus.CREATOR:
                return a.user.id
    except Exception as e:
        log.error("Не удалось получить список админов: %s", e)
    return None


async def notify_creator_about_admin(chat, admin_user, reason: str, message_text: str):
    creator_id = await get_chat_creator_id(chat.id)
    if creator_id is None or creator_id == admin_user.id:
        return
    try:
        await bot.send_message(
            creator_id,
            f"⚠️ В чате «{chat.title}» админ {admin_user.full_name} "
            f"(@{admin_user.username or 'без username'}) нарушил правила.\n"
            f"Причина: {reason}\n"
            f"Сообщение: {message_text}",
        )
    except TelegramBadRequest:
        log.warning(
            "Не удалось отправить ЛС создателю чата %s (%s) — он не начинал диалог с ботом",
            chat.id, creator_id,
        )


async def ban_permanently(chat_id: int, user_id: int) -> None:
    await bot.ban_chat_member(chat_id, user_id, revoke_messages=True)


async def kick_only(chat_id: int, user_id: int) -> None:
    await bot.ban_chat_member(chat_id, user_id)
    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


async def unban(chat_id: int, user_id: int) -> None:
    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


async def resolve_target(message: Message) -> tuple[int, str, str] | None:
    """
    Определяет цель команды и оставшийся текст (причину).
    Возвращает (user_id, отображаемое_имя, причина) либо None.
    Поддерживает: reply на сообщение ИЛИ числовой user_id/@username первым
    аргументом после команды.
    """
    parts = message.text.split(maxsplit=2)  # [cmd, arg?, остальное]

    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        reason = parts[1] if len(parts) > 1 else ""
        # если реплай — весь текст после команды это причина
        reason = message.text.partition(" ")[2].strip()
        return u.id, u.full_name, reason or "нарушение правил чата"

    if len(parts) > 1:
        arg = parts[1]
        reason = parts[2].strip() if len(parts) > 2 else ""
        reason = reason or "нарушение правил чата"

        if arg.lstrip("-").isdigit():
            return int(arg), f"ID {arg}", reason

        if arg.startswith("@"):
            try:
                chat = await bot.get_chat(arg)
                name = getattr(chat, "full_name", None) or arg
                return chat.id, name, reason
            except Exception:
                return None

    return None


# ---------------------------------------------------------------------------
# ОПИСАНИЕ КОМАНД (используется в /start и при запуске бота владельцу)
# ---------------------------------------------------------------------------

def commands_description() -> str:
    return (
        "🤖 Бот-модератор чата (Gemini 2.5 Flash)\n\n"
        "Автоматически: слежу за оскорблениями Ислама/веры (ИИ-классификация), "
        f"выдаю предупреждения, баню после {WARN_LIMIT} предупреждений. "
        f"Предупреждения сгорают сами через {WARN_EXPIRY_DAYS} дней.\n\n"
        "Команды администраторов (в группе, ответом на сообщение "
        "нарушителя ИЛИ с указанием ID/@username первым аргументом):\n"
        "!ban [причина] — бан навсегда\n"
        "!kick [причина] — исключить (сможет зайти снова)\n"
        "!varn [причина] — предупреждение "
        f"(на {WARN_LIMIT}-м автобан)\n"
        "!unwarn — снять одно (последнее) предупреждение\n"
        "!clearwarns — снять вообще все предупреждения\n"
        "!unban <id|@username> — снять бан\n\n"
        "Команда для всех участников (в группе):\n"
        "!varns — посмотреть свои предупреждения, "
        "или ответом/упоминанием (@ или тег) — чужие\n\n"
        "Команды владельца бота (в личке с ботом):\n"
        "/addmoder <id> — разрешить пользователю добавлять бота в группы\n"
        "/removemoder <id> — забрать это право\n"
        "/moders — показать список тех, кому разрешено добавлять бота\n\n"
        "Добавлять бота в новые группы может только владелец и те, "
        "кого он одобрил через /addmoder — если бота добавит кто-то "
        "другой, бот сам покинет чат."
    )


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.type != "private":
        return

    if message.from_user and is_authorized_adder(message.from_user.id):
        add_url = (
            f"https://t.me/{BOT_USERNAME}?startgroup=true&admin={REQUESTED_ADMIN_RIGHTS}"
            if BOT_USERNAME else None
        )
        keyboard = None
        if add_url:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить бота в группу", url=add_url)]]
            )
        await message.answer(commands_description(), reply_markup=keyboard)
    else:
        await message.answer(
            "Этот бот приватный. Добавлять его в группы может только владелец."
        )


# ---------------------------------------------------------------------------
# ВЛАДЕЛЕЦ: управление модераторами (кто может добавлять бота в группы)
# ---------------------------------------------------------------------------

@dp.message(Command("addmoder"))
async def cmd_addmoder(message: Message):
    if message.chat.type != "private" or message.from_user is None:
        return
    if message.from_user.id != OWNER_ID:
        return

    target_id = None
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().lstrip("-").isdigit():
        target_id = int(parts[1].strip())
    elif message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id

    if target_id is None:
        await message.reply(
            "Использование: /addmoder <user_id>\n"
            "(узнать свой ID можно у @userinfobot)"
        )
        return

    moderators.add(target_id)
    save_moderators(moderators)
    await message.reply(
        f"✅ Пользователь {target_id} теперь может добавлять бота в группы."
    )


@dp.message(Command("removemoder"))
async def cmd_removemoder(message: Message):
    if message.chat.type != "private" or message.from_user is None:
        return
    if message.from_user.id != OWNER_ID:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().lstrip("-").isdigit():
        target_id = int(parts[1].strip())
        moderators.discard(target_id)
        save_moderators(moderators)
        await message.reply(f"❌ Право добавлять бота у {target_id} отозвано.")
    else:
        await message.reply("Использование: /removemoder <user_id>")


@dp.message(Command("moders"))
async def cmd_moders(message: Message):
    if message.chat.type != "private" or message.from_user is None:
        return
    if message.from_user.id != OWNER_ID:
        return

    if not moderators:
        await message.reply(f"Владелец: {OWNER_ID}\nДополнительных модераторов нет.")
    else:
        ids = "\n".join(str(m) for m in sorted(moderators))
        await message.reply(f"Владелец: {OWNER_ID}\nМодераторы:\n{ids}")


# ---------------------------------------------------------------------------
# ЗАЩИТА: бот покидает чаты, куда его добавил не владелец/не модератор
# ---------------------------------------------------------------------------

@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated):
    if event.chat.type not in ("group", "supergroup"):
        return

    new_status = event.new_chat_member.status
    if new_status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return

    adder_id = event.from_user.id if event.from_user else None

    if adder_id is not None and is_authorized_adder(adder_id):
        return  # всё в порядке, бот остаётся

    log.warning(
        "Бота добавили в чат %s (%s) неавторизованный пользователь %s — выхожу",
        event.chat.id, event.chat.title, adder_id,
    )

    try:
        await bot.send_message(
            event.chat.id,
            "⛔ Этот бот приватный, добавлять его может только владелец. Покидаю чат.",
        )
    except Exception:
        pass

    try:
        await bot.leave_chat(event.chat.id)
    except Exception as e:
        log.error("Не удалось покинуть чат %s: %s", event.chat.id, e)

    if OWNER_ID:
        try:
            await bot.send_message(
                OWNER_ID,
                f"⚠️ Попытка добавить бота в чат «{event.chat.title}» "
                f"пользователем {adder_id} — бот вышел из чата.",
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# РУЧНЫЕ АДМИН-КОМАНДЫ: !ban / !kick / !varn / !unwarn / !clearwarns / !unban
# ---------------------------------------------------------------------------

ADMIN_COMMANDS = ("!ban", "!kick", "!varn", "!warn", "!unwarn", "!clearwarns", "!unban")


@dp.message(F.text.func(lambda t: bool(t) and t.split()[0].lower() in ADMIN_COMMANDS))
async def admin_commands(message: Message):
    if message.chat.type not in ("group", "supergroup") or message.from_user is None:
        return

    chat_id = message.chat.id
    sender_status = await get_user_status(chat_id, message.from_user.id)

    if sender_status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.reply("Эта команда доступна только администраторам.")
        return

    if not await bot_can_ban(chat_id):
        await message.reply(
            "У меня нет прав администратора (ограничение участников) в этом чате."
        )
        return

    cmd = message.text.split()[0].lower()
    resolved = await resolve_target(message)

    if resolved is None:
        await message.reply(
            "Укажите цель: ответьте (reply) на сообщение пользователя, "
            "либо укажите его ID/@username первым аргументом."
        )
        return

    target_id, target_name, reason = resolved

    # Для карательных команд нельзя трогать админов/создателя
    if cmd in ("!ban", "!kick", "!varn", "!warn"):
        target_status = await get_user_status(chat_id, target_id)
        if target_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            await message.reply(
                "Telegram не позволяет боту банить/кикать администраторов чата."
            )
            return

    try:
        if cmd == "!ban":
            await ban_permanently(chat_id, target_id)
            clear_warns(chat_id, target_id)
            await message.reply(f"⛔ {target_name} забанен навсегда. Причина: {reason}")

        elif cmd == "!kick":
            await kick_only(chat_id, target_id)
            await message.reply(f"👢 {target_name} исключён из чата. Причина: {reason}")

        elif cmd in ("!varn", "!warn"):
            count = add_warn(chat_id, target_id)
            if count >= WARN_LIMIT:
                await ban_permanently(chat_id, target_id)
                clear_warns(chat_id, target_id)
                await message.reply(
                    f"⛔ {target_name} достиг {WARN_LIMIT} предупреждений — забанен. "
                    f"Причина последнего: {reason}"
                )
            else:
                await message.reply(
                    f"⚠️ {target_name}, предупреждение {count}/{WARN_LIMIT}. Причина: {reason}"
                )

        elif cmd == "!unwarn":
            count = remove_one_warn(chat_id, target_id)
            await message.reply(
                f"➖ Снято одно предупреждение у {target_name}. Осталось: {count}/{WARN_LIMIT}"
            )

        elif cmd == "!clearwarns":
            clear_warns(chat_id, target_id)
            await message.reply(f"🧹 Все предупреждения {target_name} сняты.")

        elif cmd == "!unban":
            await unban(chat_id, target_id)
            await message.reply(f"✅ {target_name} разбанен.")

    except TelegramBadRequest as e:
        log.error("Не удалось выполнить команду %s: %s", cmd, e)
        await message.reply("Не получилось выполнить действие — проверьте права бота.")


# ---------------------------------------------------------------------------
# !varns — посмотреть количество предупреждений (себя или упомянутого)
# Доступно всем участникам, не только админам.
# ---------------------------------------------------------------------------

async def _resolve_mentioned_user(message: Message) -> tuple[int, str] | None:
    """Ищет упомянутого пользователя: через reply, @username или text_mention."""
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, u.full_name

    for entity in (message.entities or []):
        if entity.type == "text_mention" and entity.user:
            return entity.user.id, entity.user.full_name
        if entity.type == "mention":
            username = message.text[entity.offset: entity.offset + entity.length]
            try:
                chat = await bot.get_chat(username)
                name = getattr(chat, "full_name", None) or username
                return chat.id, name
            except Exception:
                return None

    return None


@dp.message(F.text.func(lambda t: bool(t) and t.split()[0].lower() == "!varns"))
async def cmd_varns(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return

    chat_id = message.chat.id
    mentioned = await _resolve_mentioned_user(message)

    if mentioned is not None:
        target_id, target_name = mentioned
    else:
        if message.from_user is None:
            return
        target_id, target_name = message.from_user.id, message.from_user.full_name

    count = get_warn_count(chat_id, target_id)
    await message.reply(
        f"📋 У {target_name} сейчас {count}/{WARN_LIMIT} предупреждений."
    )


# ---------------------------------------------------------------------------
# АВТОМАТИЧЕСКАЯ МОДЕРАЦИЯ (ИИ-классификация + варны/бан)
# ---------------------------------------------------------------------------

@dp.message(F.text)
async def moderate(message: Message):
    text = message.text or ""

    if not BAD_WORDS_PATTERN.search(text):
        return

    if message.from_user is None or message.from_user.is_bot:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    category, reason = await classify_message(text)

    if category != "RELIGION":
        return

    status = await get_user_status(chat_id, user_id)

    if status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        try:
            await message.reply(
                f"⚠️ {message.from_user.full_name}, вы администратор, но это "
                f"не освобождает от правил. Нарушение: {reason}."
            )
        except TelegramBadRequest as e:
            log.error("Не удалось отправить предупреждение админу: %s", e)

        if status == ChatMemberStatus.ADMINISTRATOR:
            await notify_creator_about_admin(message.chat, message.from_user, reason, text)
        return

    if not await bot_can_ban(chat_id):
        log.warning("Бот не админ или нет прав банить в чате %s", chat_id)
        return

    count = add_warn(chat_id, user_id)

    try:
        if count >= WARN_LIMIT:
            await ban_permanently(chat_id, user_id)
            clear_warns(chat_id, user_id)
            await message.answer(
                f"⛔️ {message.from_user.full_name} достиг {WARN_LIMIT} предупреждений "
                f"и забанен навсегда. Причина: {reason}"
            )
        else:
            await message.reply(
                f"⚠️ Предупреждение {count}/{WARN_LIMIT}, {message.from_user.full_name}: "
                f"{reason}."
            )
    except TelegramBadRequest as e:
        log.error("Не удалось выполнить действие: %s", e)


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------

async def main():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    log.info("Бот запущен как @%s", BOT_USERNAME)

    if not OWNER_ID:
        log.warning(
            "OWNER_ID не задан! Пока это не исправлено, добавлять бота в группы "
            "не сможет вообще никто (включая вас). Задайте переменную окружения OWNER_ID."
        )
    else:
        try:
            await bot.send_message(OWNER_ID, commands_description())
        except TelegramBadRequest:
            log.warning(
                "Не удалось отправить стартовое сообщение владельцу (id=%s) — "
                "напишите боту /start в личку хотя бы раз.", OWNER_ID,
            )

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
