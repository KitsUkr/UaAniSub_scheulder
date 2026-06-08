import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import calendar_kb
import texts
from config import ADMIN_IDS, PREVIEW_CHANNEL, RELEASE_CHANNEL, TZ
from database import cancel_release, create_release, list_pending

logger = logging.getLogger(__name__)

router = Router()
# Суто приватний адмін-інструмент.
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
    waiting_date = State()
    waiting_time = State()
    confirm = State()


# ── Клавіатури ────────────────────────────────────────────────────────────────

def _menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_MENU_NEW, callback_data="menu_new")],
        [InlineKeyboardButton(text=texts.BTN_MENU_LIST, callback_data="menu_list")],
    ])


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")],
    ])


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_SCHEDULE, callback_data="rel_schedule")],
        [
            InlineKeyboardButton(text=texts.BTN_RESTART, callback_data="rel_restart"),
            InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel"),
        ],
    ])


# ── Допоміжне ─────────────────────────────────────────────────────────────────

def _title(caption_html: str | None) -> str:
    """Короткий заголовок для списку/підтвердження: перший непорожній рядок
    підпису без HTML-тегів, обрізаний."""
    if not caption_html:
        return texts.CAPTION_NONE
    plain = _TAG_RE.sub("", caption_html).strip()
    if not plain:
        return texts.CAPTION_NONE
    line = plain.splitlines()[0].strip()
    return (line[:60] + "…") if len(line) > 60 else line


def _parse_time(value: str) -> str | None:
    """'8:5' / '08:05' → '08:05'; невалідне → None."""
    value = (value or "").strip()
    if ":" not in value:
        return None
    hh, _, mm = value.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return f"{h:02d}:{m:02d}"


def _now_str() -> str:
    return datetime.now(_KYIV).strftime(_DT_FMT)


def _parse_id(data: str) -> int | None:
    try:
        return int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        return None


async def _start_wizard(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(ReleaseFSM.waiting_preview)
    await message.answer(texts.WIZARD_START_PREVIEW, reply_markup=_cancel_kb())


async def _build_list_view() -> tuple[str, InlineKeyboardMarkup | None]:
    rows = await list_pending()
    if not rows:
        return texts.LIST_EMPTY, None

    lines = [texts.LIST_HEADER]
    kb_rows: list[list[InlineKeyboardButton]] = []
    for r in rows:
        lines.append(texts.LIST_LINE.format(
            id=r["id"], run_at=r["run_at"], title=_title(r["caption_html"]),
        ))
        kb_rows.append([InlineKeyboardButton(
            text=texts.BTN_DEL_RELEASE.format(id=r["id"], run_at=r["run_at"]),
            callback_data=f"del:{r['id']}",
        )])
    kb_rows.append([InlineKeyboardButton(text=texts.BTN_CLOSE, callback_data="lst_close")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


# ══════════════════════════════════════════════════════════════════════════════
#   Команди й кнопки меню (реєструються до станових хендлерів)
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(texts.START_WELCOME, reply_markup=_menu_kb())


@router.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext):
    await _start_wizard(message, state)


@router.callback_query(F.data == "menu_new")
async def cb_menu_new(callback: CallbackQuery, state: FSMContext):
    await _start_wizard(callback.message, state)
    await callback.answer()


@router.message(Command("list"))
async def cmd_list(message: Message):
    await _send_list(message)


@router.callback_query(F.data == "menu_list")
async def cb_menu_list(callback: CallbackQuery):
    await _send_list(callback.message)
    await callback.answer()


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        await state.clear()
        await message.answer(texts.WIZARD_CANCELLED, reply_markup=_menu_kb())
    else:
        await message.answer(texts.NOTHING_TO_CANCEL, reply_markup=_menu_kb())


async def _send_list(message: Message) -> None:
    text, kb = await _build_list_view()
    await message.answer(text, reply_markup=kb)


# ══════════════════════════════════════════════════════════════════════════════
#   Майстер: кроки
# ══════════════════════════════════════════════════════════════════════════════

@router.message(ReleaseFSM.waiting_preview)
async def step_preview(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer(texts.ERR_NEED_PHOTO, reply_markup=_cancel_kb())
        return

    # html_text перетворює entities підпису на HTML (bold/курсив/наявні
    # посилання). Викликаємо лише за наявності підпису — інакше кине помилку.
    caption_html = message.html_text if message.caption else None
    await state.update_data(
        preview_file_id=message.photo[-1].file_id,
        caption_html=caption_html,
    )
    if not caption_html:
        await message.answer(texts.WARN_NO_CAPTION)
    await state.set_state(ReleaseFSM.waiting_mp4)
    await message.answer(texts.WIZARD_ASK_MP4, reply_markup=_cancel_kb())


@router.message(ReleaseFSM.waiting_mp4)
async def step_mp4(message: Message, state: FSMContext):
    if message.video:
        await state.update_data(mp4_file_id=message.video.file_id, mp4_kind="video")
    elif message.document and _is_ext(message.document, ".mp4", "video/mp4"):
        await state.update_data(mp4_file_id=message.document.file_id, mp4_kind="document")
        await message.answer(texts.WARN_MP4_AS_DOC)
    else:
        await message.answer(texts.ERR_NEED_MP4, reply_markup=_cancel_kb())
        return

    await state.set_state(ReleaseFSM.waiting_mkv)
    await message.answer(texts.WIZARD_ASK_MKV, reply_markup=_cancel_kb())


@router.message(ReleaseFSM.waiting_mkv)
async def step_mkv(message: Message, state: FSMContext):
    if not message.document:
        await message.answer(texts.ERR_NEED_MKV, reply_markup=_cancel_kb())
        return

    name = message.document.file_name or ""
    if not name.lower().endswith(".mkv"):
        await message.answer(texts.WARN_NOT_MKV.format(name=name or "—"))

    await state.update_data(mkv_file_id=message.document.file_id, mkv_kind="document")
    await state.set_state(ReleaseFSM.waiting_date)

    today = datetime.now(_KYIV).date()
    await message.answer(
        texts.WIZARD_ASK_DATE,
        reply_markup=calendar_kb.build_calendar(today.year, today.month, today),
    )


# ── Крок 4: дата (інлайн-календар) ────────────────────────────────────────────

@router.callback_query(ReleaseFSM.waiting_date, F.data == "cal:ignore")
async def cal_ignore(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(ReleaseFSM.waiting_date, F.data.startswith("cal:nav:"))
async def cal_nav(callback: CallbackQuery):
    year, month = map(int, callback.data.split(":")[2].split("-"))
    today = datetime.now(_KYIV).date()
    try:
        await callback.message.edit_reply_markup(
            reply_markup=calendar_kb.build_calendar(year, month, today)
        )
    except Exception:
        pass
    await callback.answer()


@router.callback_query(ReleaseFSM.waiting_date, F.data.startswith("cal:day:"))
async def cal_day(callback: CallbackQuery, state: FSMContext):
    iso = callback.data.split(":", 2)[2]  # YYYY-MM-DD
    await state.update_data(pick_date=iso)
    await state.set_state(ReleaseFSM.waiting_time)
    await callback.message.edit_text(
        texts.WIZARD_ASK_TIME.format(date=iso), reply_markup=_cancel_kb()
    )
    await callback.answer()


@router.message(ReleaseFSM.waiting_date)
async def step_date_stray(message: Message):
    await message.answer(texts.WIZARD_DATE_HINT)


# ── Крок 5: час (текст) → підтвердження ───────────────────────────────────────

@router.message(ReleaseFSM.waiting_time)
async def step_time(message: Message, state: FSMContext):
    hhmm = _parse_time(message.text or "")
    if hhmm is None:
        await message.answer(texts.ERR_BAD_TIME, reply_markup=_cancel_kb())
        return

    data = await state.get_data()
    run_at = f"{data['pick_date']} {hhmm}"
    if run_at < _now_str():
        await message.answer(texts.ERR_DATETIME_PAST, reply_markup=_cancel_kb())
        return

    await state.update_data(run_at=run_at)
    await state.set_state(ReleaseFSM.confirm)
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
        texts.SCHEDULED_OK.format(id=rel_id, run_at=data["run_at"]),
        reply_markup=_menu_kb(),
    )
    await callback.answer()
    logger.info("Заплановано випуск #%d на %s", rel_id, data["run_at"])


@router.callback_query(ReleaseFSM.confirm, F.data == "rel_restart")
async def cb_restart(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(texts.WIZARD_RESTARTED)
    await _start_wizard(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "rel_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text(texts.WIZARD_CANCELLED, reply_markup=_menu_kb())
    except Exception:
        # Повідомлення було медіа/незмінне — шлемо нове з меню.
        await callback.message.answer(texts.WIZARD_CANCELLED, reply_markup=_menu_kb())
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
#   Список: скасування запланованих інлайн-кнопками
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("del:"))
async def cb_del(callback: CallbackQuery):
    rel_id = _parse_id(callback.data)
    if rel_id is None:
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_DEL_CONFIRM, callback_data=f"delyes:{rel_id}")],
        [InlineKeyboardButton(text=texts.BTN_BACK, callback_data="lst_back")],
    ])
    await callback.message.edit_text(texts.CONFIRM_DELETE.format(id=rel_id), reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("delyes:"))
async def cb_del_yes(callback: CallbackQuery):
    rel_id = _parse_id(callback.data)
    if rel_id is not None:
        await cancel_release(rel_id)
    text, kb = await _build_list_view()
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer(texts.CANCEL_DONE_TOAST)


@router.callback_query(F.data == "lst_back")
async def cb_list_back(callback: CallbackQuery):
    text, kb = await _build_list_view()
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "lst_close")
async def cb_list_close(callback: CallbackQuery):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


# ── Утиліти ───────────────────────────────────────────────────────────────────

def _is_ext(document, ext: str, mime: str) -> bool:
    name = (document.file_name or "").lower()
    if name.endswith(ext):
        return True
    return (document.mime_type or "") == mime
