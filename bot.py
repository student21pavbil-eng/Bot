import asyncio
import json
import logging
import os
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatMemberStatus
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest

import google.generativeai as genai

# ---------------------------------------------------------------------------
# КОНФИГУРАЦИЯ
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "PUT_YOUR_GEMINI_KEY_HERE")
GEMINI_MODEL = "gemini-2.5-flash"

BASE_DIR = Path(__file__).parent
WARNS_FILE = BASE_DIR / "warns.json"
BADWORDS_FILE = BASE_DIR / "badwords.txt"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mod-bot")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(GEMINI_MODEL)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# ---------------------------------------------------------------------------
# СЛОВАРЬ ЗАПРЕЩЁННЫХ СЛОВ (грузится из badwords.txt, по одному слову на строку)
# ---------------------------------------------------------------------------

def load_bad_words_pattern() -> re.Pattern:
    if not BADWORDS_FILE.exists():
        log.warning("Файл %s не найден — локальный фильтр отключён", BADWORDS_FILE)
        # Пустой паттерн, который никогда не совпадёт
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


# ---------------------------------------------------------------------------
# ЗАПРОС К GEMINI
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """Ты — модератор в исламском чате. Тебе дано одно сообщение пользователя.

Определи категорию сообщения и ответь СТРОГО в одном из трёх форматов,
без каких-либо пояснений, кавычек или лишнего текста:

NOTHING
— если в сообщении нет оскорблений/мата, ИЛИ мат есть, но направлен
  на конкретного человека или мат про Иисуса (не на религию/веру).

RELIGION|<короткая причина 2-4 слова на русском>
— если оскорбление/мат направлено на религию, веру, Бога, пророков,
  священные тексты, конфессии, верующих как группу чисто связано про ислам.

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
        # Нарушитель и есть создатель — уведомлять некого
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
        # Создатель ни разу не писал боту в личку — Telegram не даст отправить
        log.warning(
            "Не удалось отправить ЛС создателю чата %s (%s) — он не начинал диалог с ботом",
            chat.id, creator_id,
        )


# ---------------------------------------------------------------------------
# ОСНОВНОЙ ХЕНДЛЕР СООБЩЕНИЙ
# ---------------------------------------------------------------------------

@dp.message(F.text)
async def moderate(message: Message):
    text = message.text or ""

    # 1. Быстрый локальный фильтр — если мата нет, ИИ не трогаем
    if not BAD_WORDS_PATTERN.search(text):
        return

    if message.from_user is None or message.from_user.is_bot:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    # 2. Классификация через Gemini
    category, reason = await classify_message(text)

    if category == "NOTHING":
        # Мат про человека или вообще не про то — ничего не делаем
        return

    if category != "RELIGION":
        return

    # 3. Проверяем статус нарушителя — админа/создателя банить нельзя
    status = await get_user_status(chat_id, user_id)

    if status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        # Бот физически не может забанить админа/создателя чата.
        # Вместо этого: предупреждение в чат + уведомление создателю в ЛС.
        try:
            await message.reply(
                f"⚠️ {message.from_user.full_name}, вы администратор, но это "
                f"не освобождает от правил. Нарушение: {reason}."
            )
        except TelegramBadRequest as e:
            log.error("Не удалось отправить предупреждение админу: %s", e)

        if status == ChatMemberStatus.ADMINISTRATOR:
            # Создателя о самом себе не уведомляем (см. notify_creator_about_admin)
            await notify_creator_about_admin(message.chat, message.from_user, reason, text)
        return

    # 4. Обычный участник — работает прежняя схема варн → бан
    if not await bot_can_ban(chat_id):
        log.warning("Бот не админ или нет прав банить в чате %s", chat_id)
        return

    key = warn_key(chat_id, user_id)
    already_warned = warns.get(key, 0)

    try:
        if already_warned == 0:
            # Первое нарушение — варн
            warns[key] = 1
            save_warns(warns)
            await message.reply(
                f"⚠️ Предупреждение, {message.from_user.full_name}: {reason}.\n"
                f"При повторном нарушении — бан."
            )
        else:
            # Повторное нарушение — бан
            await message.chat.ban(user_id)
            await message.answer(
                f"⛔️ Пользователь {message.from_user.full_name} забанен: {reason} (повторно)."
            )
            warns.pop(key, None)
            save_warns(warns)
    except TelegramBadRequest as e:
        log.error("Не удалось выполнить действие: %s", e)


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------

async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
