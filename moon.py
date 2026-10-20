"""Автовивантаження MP4 на плеєр MoonAnime через їхнього бота-завантажувача.

Бот не може писати іншому боту (обмеження Telegram), тому пересилає юзербот —
сесія звичайного акаунта (Telethon). Пересилання не качає файл: відео лишається
на серверах Telegram, тож ліміту розміру немає.

Діалог із ботом-завантажувачем:
  1. пересилаємо пост MP4 з release-каналу;
  2. бот збирає «пачку» й сам закриває її карткою «Перевірка пачки»;
  3. тиснемо «✏️ MAL ID» / «✏️ Сезон» і відповідаємо на запитання бота за
     змістом («📺 Вкажи номер сезону…» → сезон), у якому б порядку він їх не
     ставив: MAL ID — із шаблону аніме, сезон — з мітки SxxEyy у назві файлу чи
     підписі MP4 (озвучку й номер серії бот визначає сам — з акаунта та імені файлу);
  4. коли блокуючих проблем нема — тиснемо кнопку підтвердження.
Картку звіряємо з тим, що знаємо самі (рівно 1 файл, та сама серія). Якщо щось
не так — нічого не підтверджуємо, а кличемо адміна.

Черга — у БД (releases.moon_status), тож переживає рестарт. Обробляємо по
одному: дві пересилки поспіль злилися б в одну пачку.
"""

import asyncio
import contextlib
import html
import logging
import re
import time

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from telethon import TelegramClient, events
from telethon.errors import ChatForwardsRestrictedError
from telethon.sessions import StringSession

import texts
from config import (
    ADMIN_IDS,
    MOON_API_HASH,
    MOON_API_ID,
    MOON_BOT,
    MOON_ENABLED,
    MOON_SESSION,
    RELEASE_CHANNEL,
)
from database import fail_interrupted_moon_uploads, pending_moon_uploads, set_moon_status

logger = logging.getLogger(__name__)

# Розмітка картки бота-завантажувача. Змінять тексти — правити тут.
CARD_MARK = "Перевірка пачки"
BLOCKERS_MARK = "Блокуючі проблеми"
# Кнопку підтвердження ще ніхто не бачив (вона зʼявляється, коли блокерів нема),
# тож шукаємо за словами. Не знайшли — не ризикуємо, кличемо адміна.
CONFIRM_WORDS = ("підтверд", "завантаж", "відправ", "почати", "запуст", "готово", "✅")
NEVER_CLICK = ("скасув", "виправ", "✏")

# Запитання бота → що відповісти. Порядок важливий: «MAL ID серіалу» — це MAL,
# «сезон для серії» — сезон.
PROMPT_KEYS = (("mal", "mal_id"), ("myanimelist", "mal_id"), ("сезон", "season"), ("сері", "episode"))
# Запитанням вважаємо лише прохання щось ввести, а не «✅ MAL ID збережено».
REQUEST_WORDS = ("вкаж", "введ", "надішл", "напиш")

CARD_TIMEOUT = 300   # бот закриває пачку сам, коли перестають надходити файли
STEP_TIMEOUT = 60    # тиша бота між кроками (запитання, картка, відповідь на підтвердження)
MAX_STEPS = 6        # захист від зациклення діалогу
MAX_REPEATS = 2      # скільки разів відповідаємо на те саме запитання, якщо бот перепитує

_FIELD_RE = re.compile(r"^([^:\n]+):[ \t]*(.*)$", re.MULTILINE)

_client: TelegramClient | None = None
_task: asyncio.Task | None = None
_wake = asyncio.Event()


class MoonError(Exception):
    """Очікувана зупинка діалогу: картка не така, бот не відповів тощо."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.bot_text = ""  # остання відповідь бота — для алерту адміну


class _Inbox:
    """Нові й відредаговані повідомлення бота за час діалогу — у черзі.
    Картку бот може як редагувати, так і надсилати заново; нам байдуже."""

    def __init__(self, client: TelegramClient, chat):
        self.client = client
        self.chat = chat
        self.last_text = ""
        self.answered: set[tuple[int, str]] = set()  # (id, текст) запитань, на які вже відповіли
        self._queue: asyncio.Queue = asyncio.Queue()

    async def _on(self, event) -> None:
        self._queue.put_nowait(event.message)

    def __enter__(self) -> "_Inbox":
        self.client.add_event_handler(self._on, events.NewMessage(chats=self.chat, incoming=True))
        self.client.add_event_handler(self._on, events.MessageEdited(chats=self.chat, incoming=True))
        return self

    def __exit__(self, *exc) -> None:
        self.client.remove_event_handler(self._on)

    def drain(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()

    async def next(self, deadline: float, what: str):
        """Наступне повідомлення бота; MoonError, якщо до deadline нічого не прийшло."""
        if self._queue.empty():
            try:
                msg = await asyncio.wait_for(self._queue.get(), max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                raise MoonError(f"{what}: так і не дочекався потрібної відповіді від бота.") from None
        else:
            msg = self._queue.get_nowait()
        self.last_text = msg.raw_text or ""
        return msg

    async def wait(self, pred, timeout: float, what: str):
        """Перше повідомлення, що задовольняє pred; MoonError, якщо за timeout нема."""
        deadline = time.monotonic() + timeout
        while True:
            msg = await self.next(deadline, what)
            if pred(msg):
                return msg

    async def settle(self, timeout: float) -> None:
        """Чекає будь-яку реакцію бота, але не вимагає її."""
        with contextlib.suppress(asyncio.TimeoutError):
            msg = await asyncio.wait_for(self._queue.get(), timeout)
            self.last_text = msg.raw_text or ""


def _fields(msg) -> dict[str, str]:
    """Рядки «Назва: значення» з картки → {назва в нижньому регістрі: значення}."""
    return {k.strip().lower(): v.strip() for k, v in _FIELD_RE.findall(msg.raw_text or "")}


def _is_card(msg) -> bool:
    return CARD_MARK in (msg.raw_text or "")


def _shows(fields: dict[str, str], field: str, value: int) -> bool:
    """Чи стоїть у полі картки саме це число (а не частина іншого)."""
    return re.search(rf"(?<!\d){value}(?!\d)", fields.get(field, "")) is not None


async def _button(msg, words, avoid=()):
    for row in await msg.get_buttons() or []:
        for btn in row:
            label = (btn.text or "").lower()
            if any(w in label for w in words) and not any(a in label for a in avoid):
                return btn
    return None


def _peer(value: str) -> int | str:
    return int(value) if value.lstrip("-").isdigit() else value


async def _input_entity(peer):
    """Свіжа StringSession не знає каналів за числовим ID — прогріваємо кеш діалогами."""
    try:
        return await _client.get_input_entity(peer)
    except ValueError:
        await _client.get_dialogs()
    try:
        return await _client.get_input_entity(peer)
    except ValueError:
        raise MoonError("юзербот не бачить release-канал — додай акаунт у канал.") from None


def _answer_for(text: str, answers: dict) -> tuple[str, int] | None:
    """Запитання бота → (що питають, наша відповідь). None — це не запитання
    (а, скажімо, «✅ Збережено») або питають те, чого ми не знаємо."""
    low = (text or "").lower()
    if not (any(w in low for w in REQUEST_WORDS) or low.rstrip().endswith((":", "?"))):
        return None
    for word, key in PROMPT_KEYS:
        if word in low:
            value = answers.get(key)
            return (key, value) if value is not None else None
    return None


async def _answer_until(inbox: _Inbox, answers: dict, done, what: str):
    """Відповідає на запитання бота, доки не прийде картка, для якої done(поля)
    правдиве. Скільки запитань і в якому порядку бот їх ставить — не важливо."""
    asked: dict[str, int] = {}
    deadline = time.monotonic() + STEP_TIMEOUT
    while True:
        msg = await inbox.next(deadline, what)
        if _is_card(msg):
            if done(_fields(msg)):
                return msg
            continue
        pair = _answer_for(msg.raw_text, answers)
        if pair is None:
            continue
        # Бот може відредагувати запитання (скажімо, прибрати кнопки) — це те
        # саме запитання, вдруге не відповідаємо. Правка в нове запитання — нова.
        seen = (msg.id, msg.raw_text)
        if seen in inbox.answered:
            continue
        inbox.answered.add(seen)
        key, value = pair
        asked[key] = asked.get(key, 0) + 1
        if asked[key] > MAX_REPEATS:
            raise MoonError(f"бот не приймає значення {value} — перепитує знову й знову.")
        await inbox.client.send_message(inbox.chat, str(value))
        deadline = time.monotonic() + STEP_TIMEOUT  # бот живий — даємо час на наступний крок


async def _press(inbox: _Inbox, card, button: str, answers: dict, done):
    """Тисне «✏️ …» у картці й веде діалог, доки картка не покаже потрібне."""
    btn = await _button(card, (button.lower(),))
    if btn is None:
        raise MoonError(f"у картці нема кнопки «{button}».")
    await btn.click()
    return await _answer_until(inbox, answers, done, f"«{button}»")


_SEASON_RE = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})[ ._-]?E\d{1,4}(?!\d)", re.IGNORECASE)


def season_from(*sources: str | None) -> int | None:
    """Сезон із мітки SxxEyy: «Малюй_до_кривавих_мозолів_S02E05» → 2.
    Джерела перевіряються по черзі; S00 (спешли) не рахуємо."""
    for text in sources:
        m = _SEASON_RE.search(text or "")
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
    return None


async def _season(client, channel, job: dict) -> int:
    """Сезон беремо з назви файлу MP4, далі — з підпису поста в release-каналі;
    мітки SxxEyy нема ніде — запасний сезон із шаблону аніме, інакше 1."""
    post = await client.get_messages(channel, ids=job["mp4_msg_id"])
    if post is None:
        raise MoonError("пост MP4 не знайдено в release-каналі — його видалили?")
    name = post.file.name if post.file else None
    return season_from(name, post.raw_text) or job["season"] or 1


async def _dialog(inbox: _Inbox, job: dict, channel) -> str:
    """Пересилає MP4 і доводить картку до підтвердження. Повертає відповідь бота."""
    mal_id, ep = job["mal_id"], job["episode_no"]
    season = await _season(inbox.client, channel, job)
    answers = {"mal_id": mal_id, "season": season, "episode": ep}
    logger.info("MoonAnime: випуск #%d — MAL %s, сезон %s, серія %s", job["id"], mal_id, season, ep)
    try:
        await inbox.client.forward_messages(inbox.chat, job["mp4_msg_id"], channel)
    except ChatForwardsRestrictedError:
        raise MoonError("у release-каналі увімкнено захист контенту — пересилання заборонене.") from None

    card = await inbox.wait(_is_card, CARD_TIMEOUT, "картка пачки")
    for _ in range(MAX_STEPS):
        f = _fields(card)
        if "файлів" not in f:
            raise MoonError("картка не схожа на очікувану — бот змінив формат?")
        if f["файлів"] != "1":
            raise MoonError(
                f"у пачці файлів: {f['файлів']}, очікував 1 — схоже, у боті лишилась незакрита пачка."
            )
        if ep is not None and not _shows(f, "серії", ep):
            raise MoonError(f"бот розпізнав серію «{f.get('серії', '?')}», а це серія {ep}.")

        if not _shows(f, "mal id", mal_id):
            card = await _press(inbox, card, "MAL ID", answers, lambda f: _shows(f, "mal id", mal_id))
        elif not _shows(f, "сезон", season):
            card = await _press(inbox, card, "Сезон", answers, lambda f: _shows(f, "сезон", season))
        elif BLOCKERS_MARK in (card.raw_text or ""):
            raise MoonError("у картці лишились блокуючі проблеми.")
        else:
            btn = await _button(card, CONFIRM_WORDS, NEVER_CLICK)
            if btn is None:
                raise MoonError("не знайшов кнопку підтвердження — підтверди пачку вручну.")
            inbox.drain()
            await btn.click()
            await inbox.settle(STEP_TIMEOUT)
            logger.info("MoonAnime: натиснуто «%s», відповідь: %s", btn.text, inbox.last_text[:300])
            return inbox.last_text
    raise MoonError(f"картку не вдалося заповнити за {MAX_STEPS} кроків.")


async def _upload(job: dict) -> str:
    if not job["mal_id"]:
        raise MoonError("для аніме не задано MAL ID.")
    bot_entity = await _client.get_entity(MOON_BOT)
    channel = await _input_entity(_peer(RELEASE_CHANNEL))
    with _Inbox(_client, bot_entity) as inbox:
        try:
            return await _dialog(inbox, job, channel)
        except MoonError as exc:
            exc.bot_text = inbox.last_text
            raise


def _bot_reply(text: str) -> str:
    if not text:
        return ""
    return texts.MOON_BOT_REPLY.format(text=html.escape(text[:700]))


def _retry_kb(release_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=texts.BTN_MOON_RETRY, callback_data=f"moonretry:{release_id}",
    )]])


async def _notify_admins(bot: Bot, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    for uid in ADMIN_IDS:
        try:
            await bot.send_message(uid, text, reply_markup=kb)
        except Exception:
            pass


async def _notify_failed(bot: Bot, job: dict, reason: str, bot_text: str = "") -> None:
    await _notify_admins(
        bot,
        texts.MOON_FAILED.format(
            ep=job["episode_no"], name=html.escape(job["name"] or "?"),
            reason=html.escape(reason), reply=_bot_reply(bot_text), bot=html.escape(MOON_BOT),
        ),
        _retry_kb(job["id"]),
    )


async def _process(bot: Bot, job: dict) -> None:
    rid = job["id"]
    await set_moon_status(rid, "uploading")
    logger.info("MoonAnime: вивантажую випуск #%d (серія %s «%s»)", rid, job["episode_no"], job["name"])
    try:
        reply = await _upload(job)
    except MoonError as exc:
        reason, bot_text = str(exc), exc.bot_text
    except Exception as exc:
        logger.exception("MoonAnime: збій вивантаження випуску #%d", rid)
        reason, bot_text = f"{type(exc).__name__}: {exc}", ""
    else:
        await set_moon_status(rid, "done")
        logger.info("MoonAnime: випуск #%d відправлено", rid)
        await _notify_admins(bot, texts.MOON_DONE.format(
            ep=job["episode_no"], name=html.escape(job["name"] or "?"), reply=_bot_reply(reply),
        ))
        return
    await set_moon_status(rid, "failed", reason)
    logger.warning("MoonAnime: випуск #%d не вивантажено: %s", rid, reason)
    await _notify_failed(bot, job, reason, bot_text)


async def _worker(bot: Bot) -> None:
    # Діалог, перерваний рестартом, не повторюємо наосліп: пачка могла вже піти.
    for job in await fail_interrupted_moon_uploads(texts.MOON_INTERRUPTED):
        await _notify_failed(bot, job, texts.MOON_INTERRUPTED)
    while True:
        _wake.clear()
        try:
            for job in await pending_moon_uploads():
                await _process(bot, job)
        except Exception:
            logger.exception("MoonAnime: збій черги вивантаження")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=60)


def enabled() -> bool:
    """Чи налаштоване вивантаження. Черга наповнюється, навіть якщо юзербот
    зараз не зміг зайти, — серії підуть, щойно він запрацює."""
    return MOON_ENABLED


def kick() -> None:
    """Розбудити воркер — у черзі зʼявилась робота."""
    _wake.set()


async def start(bot: Bot) -> None:
    global _client, _task
    if not MOON_ENABLED:
        logger.info("MoonAnime: вимкнено (MOON_API_ID / MOON_API_HASH / MOON_SESSION не задано)")
        return
    client = TelegramClient(StringSession(MOON_SESSION), MOON_API_ID, MOON_API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("сесія недійсна — згенеруй нову: python make_session.py")
        me = await client.get_me()
    except Exception as exc:
        logger.error("MoonAnime: юзербот не запустився: %s", exc)
        await client.disconnect()
        await _notify_admins(bot, texts.MOON_START_FAILED.format(error=html.escape(str(exc))))
        return
    _client = client
    _task = asyncio.create_task(_worker(bot))
    logger.info("MoonAnime: юзербот @%s, вивантаження через %s", me.username or me.id, MOON_BOT)


async def stop() -> None:
    global _client, _task
    if _task is not None:
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
        _task = None
    if _client is not None:
        await _client.disconnect()
        _client = None
