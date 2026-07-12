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

WARNS_FILE = Path(__file__).parent / "warns.json"

# Простой локальный список "триггеров", чтобы не гонять КАЖДОЕ сообщение
# через ИИ (экономия запросов/денег). Дополняйте под себя.
BAD_WORDS_PATTERN = re.compile(
    r"(пидар|хуй|бляд|сука|мраз|гандон|ебан|мудак|шлюх|тварь)",
    re.IGNORECASE,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mod-bot")

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel(GEMINI_MODEL)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

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

PROMPT_TEMPLATE = """Ты — модератор чата. Тебе дано одно сообщение пользователя.

Определи категорию сообщения и ответь СТРОГО в одном из трёх форматов,
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
# ПРОВЕРКА ПРАВ БОТА / АДМИНОВ
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

    if category == "RELIGION":
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
