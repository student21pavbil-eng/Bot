import asyncio
import json
import logging
import os
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

import google.generativeai as genai

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "PUT_YOUR_GEMINI_KEY_HERE")
GEMINI_MODEL = "gemini-2.5-flash"

# Сколько предупреждений нужно набрать, чтобы получить бан
WARN_LIMIT = 3

# Права, которые бот попросит при добавлении в группу через кнопку /start
REQUESTED_ADMIN_RIGHTS = "restrict_members,delete_messages,invite_users"

BASE_DIR = Path(__file__).parent
WARNS_FILE = BASE_DIR / "warns.json"
BADWORDS_FILE = BASE_DIR / "badwords.txt"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mod-bot")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(GEMINI_MODEL)

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
# ХРАНИЛИЩЕ ВАРНОВ (простой json-файл: {"chat_id:user_id": count})
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


warns = load_warns()


def warn_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}:{user_id}"


def add_warn(chat_id: int, user_id: int) -> int:
    key = warn_key(chat_id, user_id)
    warns[key] = warns.get(key, 0) + 1
    save_warns(warns)
    return warns[key]


def reset_warns(chat_id: int, user_id: int) -> None:
    warns.pop(warn_key(chat_id, user_id), None)
    save_warns(warns)


# ---------------------------------------------------------------------------
# ЗАПРОС К GEMINI
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """Ты — модератор чата. Тебе дано одно сообщение пользователя.

Определи категорию сообщения и ответь СТРОГО в одном из двух форматов,
без каких-либо пояснений, кавычек или лишнего текста:

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
        response = await asyncio.to_thread(model.generate_content, prompt)
        raw = (response.text or "").strip()
    except Exception as e:
        log.exception("Gemini request failed: %s", e)
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
    """Возвращает статус: 'creator' | 'administrator' | 'member' и т.п."""
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
    """Личным сообщением уведомляет создателя чата о нарушении админа."""
    creator_id = await get_chat_creator_id(chat.id)
    if creator_id is None:
        return
    if creator_id == admin_user.id:
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
    """Полный бан без возможности вернуться (без until_date, без unban)."""
    await bot.ban_chat_member(chat_id, user_id, revoke_messages=True)


async def kick_only(chat_id: int, user_id: int) -> None:
    """Кик: убрать из группы, но разрешить зайти обратно."""
    await bot.ban_chat_member(chat_id, user_id)
    await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)


# ---------------------------------------------------------------------------
# /start — приветствие + кнопка добавления в группу
# ---------------------------------------------------------------------------

@dp.message(CommandStart())
async def cmd_start(message: Message):
    if message.chat.type != "private":
        return

    add_url = (
        f"https://t.me/{BOT_USERNAME}?startgroup=true&admin={REQUESTED_ADMIN_RIGHTS}"
        if BOT_USERNAME else None
    )

    keyboard = None
    if add_url:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="➕ Добавить бота в группу", url=add_url)]]
        )

    text = (
        "Привет! Я бот-модератор чатов.\n\n"
        "Автоматически слежу за оскорблениями религии/веры "
        f"(через ИИ-классификацию), выдаю предупреждения и баню после "
        f"{WARN_LIMIT} нарушений.\n\n"
        "Команды для админов (используются ответом на сообщение "
        "нарушителя, в группе):\n"
        "!ban [причина] — бан навсегда\n"
        "!kick [причина] — исключить (сможет зайти снова)\n"
        "!varn [причина] — выдать предупреждение "
        f"(на {WARN_LIMIT}-м автоматически бан)\n\n"
        "Кстати: раз вы написали мне сюда — если вы создатель чата, "
        "теперь я смогу присылать вам сюда уведомления о нарушениях "
        "админов группы."
    )

    await message.answer(text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# РУЧНЫЕ АДМИН-КОМАНДЫ: !ban / !kick / !varn
# ---------------------------------------------------------------------------

ADMIN_COMMANDS = ("!ban", "!kick", "!varn", "!warn")


@dp.message(F.text.func(lambda t: t.lower().split()[0] in ADMIN_COMMANDS if t else False))
async def admin_commands(message: Message):
    if message.chat.type not in ("group", "supergroup"):
        return

    if message.from_user is None:
        return

    chat_id = message.chat.id
    sender_status = await get_user_status(chat_id, message.from_user.id)

    if sender_status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.reply("Эта команда доступна только администраторам.")
        return

    if not message.reply_to_message or message.reply_to_message.from_user is None:
        await message.reply(
            "Используйте команду ответом (reply) на сообщение нарушителя."
        )
        return

    target = message.reply_to_message.from_user

    if target.is_bot:
        await message.reply("Нельзя применить санкции к боту.")
        return

    target_status = await get_user_status(chat_id, target.id)
    if target_status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.reply(
            "Telegram не позволяет боту банить/кикать администраторов чата."
        )
        return

    if not await bot_can_ban(chat_id):
        await message.reply(
            "У меня нет прав администратора (ограничение участников) в этом чате."
        )
        return

    cmd, _, reason = message.text.partition(" ")
    cmd = cmd.lower()
    reason = reason.strip() or "нарушение правил чата"

    try:
        if cmd == "!ban":
            await ban_permanently(chat_id, target.id)
            reset_warns(chat_id, target.id)
            await message.reply(f"⛔ {target.full_name} забанен навсегда. Причина: {reason}")

        elif cmd == "!kick":
            await kick_only(chat_id, target.id)
            await message.reply(f"👢 {target.full_name} исключён из чата. Причина: {reason}")

        elif cmd in ("!varn", "!warn"):
            count = add_warn(chat_id, target.id)
            if count >= WARN_LIMIT:
                await ban_permanently(chat_id, target.id)
                reset_warns(chat_id, target.id)
                await message.reply(
                    f"⛔ {target.full_name} достиг {WARN_LIMIT} предупреждений — забанен. "
                    f"Причина последнего: {reason}"
                )
            else:
                await message.reply(
                    f"⚠️ {target.full_name}, предупреждение {count}/{WARN_LIMIT}. "
                    f"Причина: {reason}"
                )
    except TelegramBadRequest as e:
        log.error("Не удалось выполнить команду %s: %s", cmd, e)
        await message.reply("Не получилось выполнить действие — проверьте права бота.")


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
        # Мат про человека или вообще не про то — ничего не делаем
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
            reset_warns(chat_id, user_id)
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
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
