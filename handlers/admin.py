import html
import logging
import re
from datetime import date, datetime, timedelta
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

import texts
from config import ADMIN_IDS, TZ
from database import (
    cancel_release,
    create_release,
    create_template,
    delete_template,
    filled_episodes,
    get_release_by_slot,
    get_template,
    list_templates,
)

logger = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == "private")
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

_KYIV = ZoneInfo(TZ)
_TAG_RE = re.compile(r"<[^>]+>")
MAX_EPISODES = 50


class TplFSM(StatesGroup):
    name = State()
    count = State()
    weekday = State()
    time = State()


class SlotFSM(StatesGroup):
    preview = State()
    mp4 = State()
    mkv = State()
    confirm = State()


# ── Клавіатури ────────────────────────────────────────────────────────────────

def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")],
    ])


def _confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_SCHEDULE, callback_data="rel_schedule")],
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")],
    ])


def _weekday_kb() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text=texts.WEEKDAYS_SHORT[i], callback_data=f"tplwd:{i}") for i in range(4)]
    row2 = [InlineKeyboardButton(text=texts.WEEKDAYS_SHORT[i], callback_data=f"tplwd:{i}") for i in range(4, 7)]
    return InlineKeyboardMarkup(inline_keyboard=[
        row1, row2,
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")],
    ])


def _main_menu() -> tuple[str, InlineKeyboardMarkup]:
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_NEW_ANIME, callback_data="anime_new")],
        [InlineKeyboardButton(text=texts.BTN_MY_ANIME, callback_data="anime_list")],
    ])
    return texts.START_WELCOME, kb


async def _anime_list_view() -> tuple[str, InlineKeyboardMarkup]:
    tpls = await list_templates()
    rows: list[list[InlineKeyboardButton]] = []
    for t in tpls:
        filled = len(await filled_episodes(t["id"]))
        rows.append([InlineKeyboardButton(
            text=texts.ANIME_BTN.format(name=t["name"], filled=filled, count=t["episodes_count"]),
            callback_data=f"anime:{t['id']}",
        )])
    rows.append([InlineKeyboardButton(text=texts.BTN_NEW_ANIME, callback_data="anime_new")])
    rows.append([InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu")])
    header = texts.ANIME_LIST_HEADER if tpls else texts.ANIME_LIST_EMPTY
    return header, InlineKeyboardMarkup(inline_keyboard=rows)


async def _template_view(template_id: int) -> tuple[str | None, InlineKeyboardMarkup | None]:
    tpl = await get_template(template_id)
    if not tpl:
        return None, None
    filled = await filled_episodes(template_id)
    wd = tpl["weekday"]
    text = texts.TEMPLATE_HEADER.format(
        name=html.escape(tpl["name"]),
        count=tpl["episodes_count"],
        weekday=texts.WEEKDAYS_FULL[wd],
        time=tpl["send_time"],
        filled=len(filled),
    )
    rows: list[list[InlineKeyboardButton]] = []
    for ep in range(1, tpl["episodes_count"] + 1):
        d = date.fromisoformat(tpl["start_date"]) + timedelta(days=7 * (ep - 1))
        mark = "✅" if ep in filled else "⬜"
        rows.append([InlineKeyboardButton(
            text=texts.SLOT_BTN.format(
                mark=mark, n=ep, wd=texts.WEEKDAYS_SHORT[wd], date=d.strftime("%d.%m"),
            ),
            callback_data=f"slot:{template_id}:{ep}",
        )])
    rows.append([InlineKeyboardButton(text=texts.BTN_DELETE_ANIME, callback_data=f"anime_del:{template_id}")])
    rows.append([InlineKeyboardButton(text=texts.BTN_BACK, callback_data="anime_list")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# ── Допоміжне ─────────────────────────────────────────────────────────────────

def _title(caption_html: str | None) -> str:
    if not caption_html:
        return texts.CAPTION_NONE
    plain = _TAG_RE.sub("", caption_html).strip()
    if not plain:
        return texts.CAPTION_NONE
    line = plain.splitlines()[0].strip()
    return (line[:60] + "…") if len(line) > 60 else line


def _parse_time(value: str) -> str | None:
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


def _compute_start_date(weekday: int, send_time: str) -> str:
    """Дата 1-ї серії: найближчий потрібний день тижня ≥ сьогодні
    (якщо сьогодні і час уже минув — наступний тиждень)."""
    now = datetime.now(_KYIV)
    today = now.date()
    delta = (weekday - today.weekday()) % 7
    start = today + timedelta(days=delta)
    if delta == 0 and now.strftime("%H:%M") >= send_time:
        start += timedelta(days=7)
    return start.isoformat()


def _slot_run_at(tpl: dict, episode_no: int) -> str:
    d = date.fromisoformat(tpl["start_date"]) + timedelta(days=7 * (episode_no - 1))
    return f"{d.isoformat()} {tpl['send_time']}"


def _is_ext(document, ext: str, mime: str) -> bool:
    name = (document.file_name or "").lower()
    if name.endswith(ext):
        return True
    return (document.mime_type or "") == mime


async def _delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def _edit_cb(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        pass


async def _edit_panel(bot, data: dict, text: str, kb: InlineKeyboardMarkup | None) -> None:
    try:
        await bot.edit_message_text(
            text,
            chat_id=data["panel_chat"],
            message_id=data["panel_msg"],
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#   /start — створення панелі
# ══════════════════════════════════════════════════════════════════════════════

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    text, kb = _main_menu()
    await message.answer(text, reply_markup=kb)


# ══════════════════════════════════════════════════════════════════════════════
#   Навігація (редагування панелі)
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = _main_menu()
    await _edit_cb(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "anime_list")
async def cb_anime_list(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await _anime_list_view()
    await _edit_cb(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("anime:"))
async def cb_anime(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    tid = int(callback.data.split(":")[1])
    text, kb = await _template_view(tid)
    if text is None:
        text, kb = await _anime_list_view()
    await _edit_cb(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data.startswith("anime_del:"))
async def cb_anime_del(callback: CallbackQuery):
    tid = int(callback.data.split(":")[1])
    tpl = await get_template(tid)
    if not tpl:
        text, kb = await _anime_list_view()
        await _edit_cb(callback, text, kb)
        await callback.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_DEL_CONFIRM, callback_data=f"anime_delyes:{tid}")],
        [InlineKeyboardButton(text=texts.BTN_BACK, callback_data=f"anime:{tid}")],
    ])
    await _edit_cb(callback, texts.CONFIRM_DELETE_ANIME.format(name=html.escape(tpl["name"])), kb)
    await callback.answer()


@router.callback_query(F.data.startswith("anime_delyes:"))
async def cb_anime_delyes(callback: CallbackQuery):
    tid = int(callback.data.split(":")[1])
    await delete_template(tid)
    text, kb = await _anime_list_view()
    await _edit_cb(callback, text, kb)
    await callback.answer(texts.DELETED_TOAST)


# ══════════════════════════════════════════════════════════════════════════════
#   Майстер створення аніме
# ══════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "anime_new")
async def cb_anime_new(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(TplFSM.name)
    await state.update_data(
        panel_chat=callback.message.chat.id, panel_msg=callback.message.message_id,
    )
    await _edit_cb(callback, texts.TPL_ASK_NAME, _cancel_kb())
    await callback.answer()


@router.message(TplFSM.name)
async def tpl_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    await _delete(message)
    data = await state.get_data()
    if not name:
        await _edit_panel(message.bot, data, texts.TPL_ASK_NAME, _cancel_kb())
        return
    await state.update_data(name=name)
    await state.set_state(TplFSM.count)
    await _edit_panel(message.bot, data, texts.TPL_ASK_COUNT, _cancel_kb())


@router.message(TplFSM.count)
async def tpl_count(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await _delete(message)
    data = await state.get_data()
    if not raw.isdigit() or not (1 <= int(raw) <= MAX_EPISODES):
        await _edit_panel(
            message.bot, data, f"{texts.ERR_BAD_COUNT}\n\n{texts.TPL_ASK_COUNT}", _cancel_kb()
        )
        return
    await state.update_data(count=int(raw))
    await state.set_state(TplFSM.weekday)
    await _edit_panel(message.bot, data, texts.TPL_ASK_WEEKDAY, _weekday_kb())


@router.callback_query(TplFSM.weekday, F.data.startswith("tplwd:"))
async def tpl_weekday(callback: CallbackQuery, state: FSMContext):
    wd = int(callback.data.split(":")[1])
    await state.update_data(weekday=wd)
    await state.set_state(TplFSM.time)
    await _edit_cb(callback, texts.TPL_ASK_TIME, _cancel_kb())
    await callback.answer()


@router.message(TplFSM.weekday)
async def tpl_weekday_stray(message: Message):
    await _delete(message)


@router.message(TplFSM.time)
async def tpl_time(message: Message, state: FSMContext):
    hhmm = _parse_time(message.text or "")
    await _delete(message)
    data = await state.get_data()
    if hhmm is None:
        await _edit_panel(
            message.bot, data, f"{texts.ERR_BAD_TIME}\n\n{texts.TPL_ASK_TIME}", _cancel_kb()
        )
        return
    start_date = _compute_start_date(data["weekday"], hhmm)
    tid = await create_template(
        name=data["name"], weekday=data["weekday"], send_time=hhmm,
        episodes_count=data["count"], start_date=start_date,
    )
    await state.clear()
    text, kb = await _template_view(tid)
    await _edit_panel(message.bot, data, text, kb)
    logger.info("Створено аніме #%d «%s» (%s)", tid, data["name"], start_date)


# ══════════════════════════════════════════════════════════════════════════════
#   Слот: відкриття / заповнений екран / заміна / скасування
# ══════════════════════════════════════════════════════════════════════════════

async def _begin_slot_wizard(
    callback: CallbackQuery, state: FSMContext, tid: int, ep: int, old_release_id: int | None
) -> None:
    tpl = await get_template(tid)
    if not tpl:
        await state.clear()
        text, kb = await _anime_list_view()
        await _edit_cb(callback, text, kb)
        return
    await state.clear()
    await state.set_state(SlotFSM.preview)
    await state.update_data(
        panel_chat=callback.message.chat.id, panel_msg=callback.message.message_id,
        template_id=tid, episode_no=ep, run_at=_slot_run_at(tpl, ep),
        old_release_id=old_release_id,
    )
    await _edit_cb(callback, texts.SLOT_ASK_PREVIEW.format(n=ep), _cancel_kb())


@router.callback_query(F.data.startswith("slot:"))
async def cb_slot(callback: CallbackQuery, state: FSMContext):
    _, tid_s, ep_s = callback.data.split(":")
    tid, ep = int(tid_s), int(ep_s)
    rel = await get_release_by_slot(tid, ep)
    if rel:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_REPLACE, callback_data=f"screp:{tid}:{ep}")],
            [InlineKeyboardButton(text=texts.BTN_CANCEL_PUB, callback_data=f"scancel:{rel['id']}:{tid}")],
            [InlineKeyboardButton(text=texts.BTN_BACK, callback_data=f"anime:{tid}")],
        ])
        await _edit_cb(
            callback,
            texts.SLOT_FILLED.format(n=ep, run_at=rel["run_at"], caption=_title(rel["caption_html"])),
            kb,
        )
        await callback.answer()
        return
    await _begin_slot_wizard(callback, state, tid, ep, old_release_id=None)
    await callback.answer()


@router.callback_query(F.data.startswith("screp:"))
async def cb_slot_replace(callback: CallbackQuery, state: FSMContext):
    _, tid_s, ep_s = callback.data.split(":")
    tid, ep = int(tid_s), int(ep_s)
    rel = await get_release_by_slot(tid, ep)
    await _begin_slot_wizard(callback, state, tid, ep, old_release_id=rel["id"] if rel else None)
    await callback.answer()


@router.callback_query(F.data.startswith("scancel:"))
async def cb_slot_cancelpub(callback: CallbackQuery, state: FSMContext):
    _, rel_s, tid_s = callback.data.split(":")
    await cancel_release(int(rel_s))
    await state.clear()
    text, kb = await _template_view(int(tid_s))
    if text is None:
        text, kb = await _anime_list_view()
    await _edit_cb(callback, text, kb)
    await callback.answer(texts.CANCEL_PUB_TOAST)


# ══════════════════════════════════════════════════════════════════════════════
#   Майстер заповнення слота
# ══════════════════════════════════════════════════════════════════════════════

@router.message(SlotFSM.preview)
async def slot_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    ep = data["episode_no"]
    if not message.photo:
        await _delete(message)
        await _edit_panel(
            message.bot, data,
            f"{texts.ERR_NEED_PHOTO}\n\n{texts.SLOT_ASK_PREVIEW.format(n=ep)}", _cancel_kb(),
        )
        return
    caption_html = message.html_text if message.caption else None
    await _delete(message)
    await state.update_data(preview_file_id=message.photo[-1].file_id, caption_html=caption_html)
    await state.set_state(SlotFSM.mp4)
    prompt = texts.SLOT_ASK_MP4.format(n=ep)
    if not caption_html:
        prompt = f"{texts.WARN_NO_CAPTION}\n\n{prompt}"
    await _edit_panel(message.bot, data, prompt, _cancel_kb())


@router.message(SlotFSM.mp4)
async def slot_mp4(message: Message, state: FSMContext):
    data = await state.get_data()
    ep = data["episode_no"]
    warn = None
    if message.video:
        fid, kind = message.video.file_id, "video"
    elif message.document and _is_ext(message.document, ".mp4", "video/mp4"):
        fid, kind = message.document.file_id, "document"
        warn = texts.WARN_MP4_AS_DOC
    else:
        await _delete(message)
        await _edit_panel(
            message.bot, data,
            f"{texts.ERR_NEED_MP4}\n\n{texts.SLOT_ASK_MP4.format(n=ep)}", _cancel_kb(),
        )
        return
    await _delete(message)
    await state.update_data(mp4_file_id=fid, mp4_kind=kind)
    await state.set_state(SlotFSM.mkv)
    prompt = texts.SLOT_ASK_MKV.format(n=ep)
    if warn:
        prompt = f"{warn}\n\n{prompt}"
    await _edit_panel(message.bot, data, prompt, _cancel_kb())


@router.message(SlotFSM.mkv)
async def slot_mkv(message: Message, state: FSMContext):
    data = await state.get_data()
    ep = data["episode_no"]
    if not message.document:
        await _delete(message)
        await _edit_panel(
            message.bot, data,
            f"{texts.ERR_NEED_MKV}\n\n{texts.SLOT_ASK_MKV.format(n=ep)}", _cancel_kb(),
        )
        return
    name = message.document.file_name or ""
    fid = message.document.file_id
    await _delete(message)
    await state.update_data(mkv_file_id=fid, mkv_kind="document")
    await state.set_state(SlotFSM.confirm)
    text = texts.SLOT_CONFIRM.format(n=ep, run_at=data["run_at"], caption=_title(data.get("caption_html")))
    if not name.lower().endswith(".mkv"):
        text = f"{texts.WARN_NOT_MKV.format(name=name or '—')}\n\n{text}"
    await _edit_panel(message.bot, data, text, _confirm_kb())


@router.message(SlotFSM.confirm)
async def slot_confirm_stray(message: Message):
    await _delete(message)


@router.callback_query(SlotFSM.confirm, F.data == "rel_schedule")
async def cb_slot_schedule(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("old_release_id"):
        await cancel_release(data["old_release_id"])
    await create_release(
        preview_file_id=data["preview_file_id"], caption_html=data.get("caption_html"),
        mp4_file_id=data["mp4_file_id"], mp4_kind=data["mp4_kind"],
        mkv_file_id=data["mkv_file_id"], mkv_kind=data["mkv_kind"],
        run_at=data["run_at"], template_id=data["template_id"], episode_no=data["episode_no"],
    )
    tid = data["template_id"]
    await state.clear()
    text, kb = await _template_view(tid)
    await _edit_cb(callback, text, kb)
    await callback.answer(texts.SCHEDULED_TOAST)
    logger.info("Заплановано серію %s аніме #%s на %s", data["episode_no"], tid, data["run_at"])


# ── Скасування активного майстра ──────────────────────────────────────────────

@router.callback_query(F.data == "rel_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tid = data.get("template_id")
    await state.clear()
    if tid:
        text, kb = await _template_view(tid)
        if text is None:
            text, kb = await _anime_list_view()
    else:
        text, kb = await _anime_list_view()
    await _edit_cb(callback, text, kb)
    await callback.answer()
