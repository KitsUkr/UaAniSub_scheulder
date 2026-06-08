"""Адмін-майстер планування випуску серії + /list, /cancel.

Працює лише в приватному чаті й лише для ADMIN_IDS (фільтри на рівні router).
"""

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import texts
from config import ADMIN_IDS, PREVIEW_CHANNEL, RELEASE_CHANNEL, TZ
from database import cancel_release, create_release, list_pending

logger = logging.getLogger(__name__)

router = Router()
# Майстер — суто приватний адмін-інструмент.
router.message.filter(F.chat.type == "private")
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

_KYIV = ZoneInfo(TZ)
_DT_FMT = "%Y-%m-%d %H:%M"
_TAG_RE = re.compile(r"<[^>]+>")


class ReleaseFSM(StatesGroup):
    waiting_preview = State()
    waiting_mp4 = State()
    waiting_mkv = State()
    waiting_datetime = State()
    confirm = State()


# ── Допоміжне ─────────────────────────────────────────────────────────────────

def _title(caption_html: str | None) -> str:
    """Короткий заголовок для підтвердження/списку: перший непорожній рядок
    підпису без HTML-тегів, обрізаний."""
    if not caption_html:
        return texts.CAPTION_NONE
    plain = _TAG_RE.sub("", caption_html).strip()
    if not plain:
        return texts.CAPTION_NONE
    line = plain.splitlines()[0].strip()
    return (line[:60] + "…") if len(line) > 60 else line


def _parse_run_at(raw: str) -> str | None:
    """'2026-06-10 19:00' → нормалізований рядок або None."""
    try:
        dt = datetime.strptime(raw.strip(), _DT_FMT)
    except ValueError:
        return None
    return dt.strftime(_DT_FMT)


def _now_str() -> str:
    return datetime.now(_KYIV).strftime(_DT_FMT)


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_SCHEDULE, callback_data="rel_schedule")],
        [
            InlineKeyboardButton(text=texts.BTN_RESTART, callback_data="rel_restart"),
            InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel"),
        ],
    ])


# ── /start ────────────────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(texts.START_WELCOME)


# ── /new — старт майстра ──────────────────────────────────────────────────────

@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(ReleaseFSM.waiting_preview)
    await message.answer(texts.WIZARD_START_PREVIEW)


# ── /cancel — скасувати майстер або видалити випуск за id ──────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, command: CommandObject, state: FSMContext):
    if command.args:
        raw = command.args.strip()
        if not raw.isdigit():
            await message.answer(texts.CANCEL_BAD_ID)
            return
        rel_id = int(raw)
        if await cancel_release(rel_id):
            await message.answer(texts.CANCEL_DONE.format(id=rel_id))
        else:
            await message.answer(texts.CANCEL_NOT_FOUND.format(id=rel_id))
        return

    if await state.get_state() is not None:
        await state.clear()
        await message.answer(texts.WIZARD_CANCELLED)
    else:
        await message.answer(texts.NOTHING_TO_CANCEL)


# ── /list — заплановані випуски ───────────────────────────────────────────────

@router.message(Command("list"))
async def cmd_list(message: Message):
    rows = await list_pending()
    if not rows:
        await message.answer(texts.LIST_EMPTY)
        return
    lines = [texts.LIST_HEADER]
    for r in rows:
        lines.append(texts.LIST_LINE.format(
            id=r["id"], run_at=r["run_at"], title=_title(r["caption_html"]),
        ))
    await message.answer("\n".join(lines))


# ── Крок 1: превʼю (фото + підпис) ────────────────────────────────────────────

@router.message(ReleaseFSM.waiting_preview)
async def step_preview(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer(texts.ERR_NEED_PHOTO)
        return

    # html_text перетворює entities підпису на HTML (bold/курсив/наявні
    # посилання), щоб коректно відрендерити в каналі. Викликаємо лише за
    # наявності підпису — інакше html_text кине помилку.
    caption_html = message.html_text if message.caption else None
    await state.update_data(
        preview_file_id=message.photo[-1].file_id,
        caption_html=caption_html,
    )
    if not caption_html:
        await message.answer(texts.WARN_NO_CAPTION)
    await state.set_state(ReleaseFSM.waiting_mp4)
    await message.answer(texts.WIZARD_ASK_MP4)


# ── Крок 2: MP4 ───────────────────────────────────────────────────────────────

@router.message(ReleaseFSM.waiting_mp4)
async def step_mp4(message: Message, state: FSMContext):
    if message.video:
        await state.update_data(
            mp4_file_id=message.video.file_id, mp4_kind="video",
        )
    elif message.document and _is_ext(message.document, ".mp4", "video/mp4"):
        await state.update_data(
            mp4_file_id=message.document.file_id, mp4_kind="document",
        )
        await message.answer(texts.WARN_MP4_AS_DOC)
    else:
        await message.answer(texts.ERR_NEED_MP4)
        return

    await state.set_state(ReleaseFSM.waiting_mkv)
    await message.answer(texts.WIZARD_ASK_MKV)


# ── Крок 3: MKV ───────────────────────────────────────────────────────────────

@router.message(ReleaseFSM.waiting_mkv)
async def step_mkv(message: Message, state: FSMContext):
    if not message.document:
        await message.answer(texts.ERR_NEED_MKV)
        return

    name = message.document.file_name or ""
    if not name.lower().endswith(".mkv"):
        await message.answer(texts.WARN_NOT_MKV.format(name=name or "—"))

    await state.update_data(
        mkv_file_id=message.document.file_id, mkv_kind="document",
    )
    await state.set_state(ReleaseFSM.waiting_datetime)
    await message.answer(texts.WIZARD_ASK_DATETIME)


# ── Крок 4: дата й час → підтвердження ────────────────────────────────────────

@router.message(ReleaseFSM.waiting_datetime)
async def step_datetime(message: Message, state: FSMContext):
    run_at = _parse_run_at(message.text or "")
    if run_at is None:
        await message.answer(texts.ERR_BAD_DATETIME)
        return
    if run_at < _now_str():
        await message.answer(texts.ERR_DATETIME_PAST)
        return

    await state.update_data(run_at=run_at)
    await state.set_state(ReleaseFSM.confirm)

    data = await state.get_data()
    await message.answer(
        texts.WIZARD_CONFIRM.format(
            run_at=run_at,
            caption=_title(data.get("caption_html")),
            release=RELEASE_CHANNEL,
            preview=PREVIEW_CHANNEL,
        ),
        reply_markup=_confirm_kb(),
    )


@router.message(ReleaseFSM.confirm)
async def step_confirm_stray(message: Message):
    # У стані підтвердження чекаємо натискання кнопки, а не повідомлення.
    await message.answer(texts.WIZARD_CONFIRM_HINT, reply_markup=_confirm_kb())


# ── Кнопки підтвердження ──────────────────────────────────────────────────────

@router.callback_query(ReleaseFSM.confirm, F.data == "rel_schedule")
async def cb_schedule(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    rel_id = await create_release(
        preview_file_id=data["preview_file_id"],
        caption_html=data.get("caption_html"),
        mp4_file_id=data["mp4_file_id"],
        mp4_kind=data["mp4_kind"],
        mkv_file_id=data["mkv_file_id"],
        mkv_kind=data["mkv_kind"],
        run_at=data["run_at"],
    )
    await state.clear()
    await callback.message.edit_text(
        texts.SCHEDULED_OK.format(id=rel_id, run_at=data["run_at"])
    )
    await callback.answer()
    logger.info("Заплановано випуск #%d на %s", rel_id, data["run_at"])


@router.callback_query(ReleaseFSM.confirm, F.data == "rel_restart")
async def cb_restart(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(ReleaseFSM.waiting_preview)
    await callback.message.edit_text(texts.WIZARD_RESTARTED)
    await callback.message.answer(texts.WIZARD_START_PREVIEW)
    await callback.answer()


@router.callback_query(ReleaseFSM.confirm, F.data == "rel_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(texts.WIZARD_CANCELLED)
    await callback.answer()


# ── Утиліти ───────────────────────────────────────────────────────────────────

def _is_ext(document, ext: str, mime: str) -> bool:
    name = (document.file_name or "").lower()
    if name.endswith(ext):
        return True
    return (document.mime_type or "") == mime
