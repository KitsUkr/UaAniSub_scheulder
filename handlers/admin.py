import asyncio
import html
import logging
import re
import time
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
    create_releases_bulk,
    create_template,
    delete_template,
    episode_statuses,
    get_release_by_slot,
    get_template,
    list_templates,
    set_repost_channel,
)

logger = logging.getLogger(__name__)

router = Router()
router.message.filter(F.chat.type == "private")
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

_KYIV = ZoneInfo(TZ)
_TAG_RE = re.compile(r"<[^>]+>")
MAX_EPISODES = 50
COUNT_PRESETS = (12, 24, 48)  # стандартні к-сті серій (1/2/4 кури) — кнопками
PER_ROW = 6        # кнопок-слотів у рядку
PER_PAGE = 12      # слотів на сторінці (2 ряди по 6)

# Серіалізує накопичення файлів слота: альбом приходить кількома update'ами
# майже одночасно — без локу read-modify-write стану може загубити файл.
_files_lock = asyncio.Lock()


def _first_ep(tpl: dict) -> int:
    """Номер першої серії: 0, якщо є нульова, інакше 1."""
    return 0 if tpl.get("has_zero") else 1


def _page_of(episode_no: int, first_ep: int = 1) -> int:
    return max(0, (episode_no - first_ep) // PER_PAGE)


def _done_episodes(tpl: dict, filled: set[int]) -> set[int]:
    """Серії, що вважаються готовими: реальні релізи ∪ перші manual_done
    (їх адмін викладає вручну поза ботом). Відлік — від першої серії (0 або 1)."""
    first = _first_ep(tpl)
    manual = int(tpl.get("manual_done") or 0)
    manual_set = set(range(first, min(first + manual, tpl["episodes_count"] + 1)))
    return filled | manual_set


def _is_manual(tpl: dict, ep: int) -> bool:
    """Чи входить серія в перші manual_done (викладена вручну, без релізу)."""
    first = _first_ep(tpl)
    return first <= ep < first + int(tpl.get("manual_done") or 0)


def _slot_mark(tpl: dict, statuses: dict[int, str], ep: int) -> str:
    """Позначка слота: ⏳ заплановано (pending), ✅ опубліковано (sent/вручну),
    ⬜ порожньо. Реальний реліз має пріоритет над ручною позначкою."""
    if statuses.get(ep) == "pending":
        return "⏳"
    if ep in statuses or _is_manual(tpl, ep):
        return "✅"
    return "⬜"


class TplFSM(StatesGroup):
    has_zero = State()
    firstday = State()
    name = State()
    count = State()
    weekday = State()
    time = State()
    firstdate = State()


class SlotFSM(StatesGroup):
    collect = State()  # один крок: приймаємо превʼю + MP4 + MKV у будь-якому порядку


class ImportFSM(StatesGroup):
    collect = State()  # приймаємо переслані повідомлення гілки, групуємо по 3


class RepostFSM(StatesGroup):
    channel = State()  # очікуємо @username або ID партнерського каналу


# ── Клавіатури ────────────────────────────────────────────────────────────────

def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")],
    ])


def _firstdate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_SKIP, callback_data="tplskip")],
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")],
    ])


def _firstday_kb() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=str(i), callback_data=f"tplfd:{i}") for i in range(1, 5)]
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")],
    ])


def _count_kb() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=str(n), callback_data=f"tplc:{n}") for n in COUNT_PRESETS]
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")],
    ])


def _yesno_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=texts.BTN_YES, callback_data="tplz:1"),
            InlineKeyboardButton(text=texts.BTN_NO, callback_data="tplz:0"),
        ],
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")],
    ])


def _firstdate_prompt(data: dict) -> str:
    wd_name = texts.WEEKDAYS_FULL[data["weekday"]].lower()
    ep_label = texts.EP_LABEL_ZERO if data.get("has_zero") else texts.EP_LABEL_ONE
    return texts.TPL_ASK_FIRSTDATE.format(weekday=wd_name, ep=ep_label)


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
        done = len(_done_episodes(t, set(await episode_statuses(t["id"]))))
        total = t["episodes_count"] - _first_ep(t) + 1
        rows.append([InlineKeyboardButton(
            text=texts.ANIME_BTN.format(name=t["name"], filled=done, count=total),
            callback_data=f"anime:{t['id']}",
        )])
    rows.append([InlineKeyboardButton(text=texts.BTN_NEW_ANIME, callback_data="anime_new")])
    rows.append([InlineKeyboardButton(text=texts.BTN_BACK, callback_data="menu")])
    header = texts.ANIME_LIST_HEADER if tpls else texts.ANIME_LIST_EMPTY
    return header, InlineKeyboardMarkup(inline_keyboard=rows)


async def _template_view(
    template_id: int, page: int = 0
) -> tuple[str | None, InlineKeyboardMarkup | None]:
    tpl = await get_template(template_id)
    if not tpl:
        return None, None
    first = _first_ep(tpl)
    last = tpl["episodes_count"]
    total_slots = last - first + 1
    pages = max(1, (total_slots + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, pages - 1))
    statuses = await episode_statuses(template_id)
    done = _done_episodes(tpl, set(statuses))
    wd = tpl["weekday"]

    start_ep = first + page * PER_PAGE
    end_ep = min(last, start_ep + PER_PAGE - 1)

    text = texts.TEMPLATE_HEADER.format(
        name=html.escape(tpl["name"]),
        count=total_slots,
        weekday=texts.WEEKDAYS_FULL[wd],
        time=tpl["send_time"],
        filled=len(done),
    )
    d1 = _slot_date(tpl, start_ep)
    d2 = _slot_date(tpl, end_ep)
    text += "\n\n" + texts.SLOT_RANGE.format(
        a=start_ep, b=end_ep, d1=d1.strftime("%d.%m"), d2=d2.strftime("%d.%m"),
    )

    # Слоти сторінки — компактні кнопки «✅/⏳/⬜ N», по PER_ROW у рядку.
    buttons = [
        InlineKeyboardButton(
            text=texts.SLOT_BTN.format(mark=_slot_mark(tpl, statuses, ep), n=ep),
            callback_data=f"slot:{template_id}:{ep}",
        )
        for ep in range(start_ep, end_ep + 1)
    ]
    rows: list[list[InlineKeyboardButton]] = [
        buttons[i:i + PER_ROW] for i in range(0, len(buttons), PER_ROW)
    ]

    if pages > 1:
        prev_cb = f"anpg:{template_id}:{page - 1}" if page > 0 else "noop"
        next_cb = f"anpg:{template_id}:{page + 1}" if page < pages - 1 else "noop"
        rows.append([
            InlineKeyboardButton(text="‹" if page > 0 else " ", callback_data=prev_cb),
            InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"),
            InlineKeyboardButton(text="›" if page < pages - 1 else " ", callback_data=next_cb),
        ])

    rows.append([
        InlineKeyboardButton(text=texts.BTN_IMPORT, callback_data=f"import:{template_id}"),
        InlineKeyboardButton(text=texts.BTN_REPOST, callback_data=f"repost:{template_id}"),
    ])
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


def _slot_date(tpl: dict, episode_no: int) -> date:
    """Дата виходу серії. Перші first_day серій — в один день (start_date),
    далі — по одній на тиждень. start_date — дата першої серії (0-ї або 1-ї)."""
    pos = episode_no - _first_ep(tpl)
    week = max(0, pos - int(tpl.get("first_day") or 1) + 1)
    return date.fromisoformat(tpl["start_date"]) + timedelta(days=7 * week)


def _slot_run_at(tpl: dict, episode_no: int) -> str:
    return f"{_slot_date(tpl, episode_no).isoformat()} {tpl['send_time']}"


def _past_episodes(start_date_iso: str, first_ep: int, last_ep: int, first_day: int) -> int:
    """Скільки перших серій уже мали б вийти (їх дата раніше за сьогодні, Київ).
    Враховує, що перші first_day серій виходять одного дня (start_date)."""
    start = date.fromisoformat(start_date_iso)
    today = datetime.now(_KYIV).date()
    n = 0
    for e in range(first_ep, last_ep + 1):
        week = max(0, (e - first_ep) - first_day + 1)
        if start + timedelta(days=7 * week) < today:
            n += 1
        else:
            break
    return n


def _classify(message: Message) -> tuple[str, str, str | None] | None:
    """Розпізнає вкладення → (роль, file_id, kind) або None.
    роль ∈ {'preview','mp4','mkv'}; kind — як слати ('video'/'document')."""
    if message.photo:
        return ("preview", message.photo[-1].file_id, None)
    if message.video:
        return ("mp4", message.video.file_id, "video")
    if message.document:
        name = (message.document.file_name or "").lower()
        mime = (message.document.mime_type or "").lower()
        if name.endswith(".mkv") or "matroska" in mime:
            return ("mkv", message.document.file_id, "document")
        if name.endswith(".mp4") or mime == "video/mp4":
            return ("mp4", message.document.file_id, "document")
    return None


def _complete(data: dict) -> bool:
    return bool(
        data.get("preview_file_id") and data.get("mp4_file_id") and data.get("mkv_file_id")
    )


def _collect_text(data: dict, note: str | None = None) -> str:
    def mark(key: str) -> str:
        return "✅" if data.get(key) else "⬜"

    body = "\n".join([
        texts.SLOT_COLLECT.format(n=data["episode_no"]),
        "",
        f"{mark('preview_file_id')} Превʼю",
        f"{mark('mp4_file_id')} MP4 — Переглянути",
        f"{mark('mkv_file_id')} MKV — Завантажити",
    ])
    if note:
        body = f"{note}\n\n{body}"
    if _complete(data):
        body += "\n\n" + texts.SLOT_READY.format(
            run_at=data["run_at"], caption=_title(data.get("caption_html")),
        )
    return body


def _collect_kb(data: dict) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if _complete(data):
        rows.append([InlineKeyboardButton(text=texts.BTN_SCHEDULE, callback_data="rel_schedule")])
    rows.append([InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


@router.callback_query(F.data.startswith("anpg:"))
async def cb_anime_page(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    _, tid_s, pg_s = callback.data.split(":")
    text, kb = await _template_view(int(tid_s), int(pg_s))
    if text is None:
        text, kb = await _anime_list_view()
    await _edit_cb(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
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
    await state.set_state(TplFSM.has_zero)
    await state.update_data(
        panel_chat=callback.message.chat.id, panel_msg=callback.message.message_id,
    )
    await _edit_cb(callback, texts.TPL_ASK_ZERO, _yesno_kb())
    await callback.answer()


@router.callback_query(TplFSM.has_zero, F.data.startswith("tplz:"))
async def tpl_has_zero(callback: CallbackQuery, state: FSMContext):
    hz = int(callback.data.split(":")[1])
    await state.update_data(has_zero=hz)
    await state.set_state(TplFSM.firstday)
    await _edit_cb(callback, texts.TPL_ASK_FIRSTDAY, _firstday_kb())
    await callback.answer()


@router.callback_query(TplFSM.firstday, F.data.startswith("tplfd:"))
async def tpl_firstday(callback: CallbackQuery, state: FSMContext):
    fd = int(callback.data.split(":")[1])
    await state.update_data(first_day=fd)
    await state.set_state(TplFSM.name)
    await _edit_cb(callback, texts.TPL_ASK_NAME, _cancel_kb())
    await callback.answer()


@router.message(TplFSM.firstday)
async def tpl_firstday_stray(message: Message):
    await _delete(message)


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
    await _edit_panel(message.bot, data, texts.TPL_ASK_COUNT, _count_kb())


@router.callback_query(TplFSM.count, F.data.startswith("tplc:"))
async def tpl_count_preset(callback: CallbackQuery, state: FSMContext):
    cnt = int(callback.data.split(":")[1])
    await state.update_data(count=cnt)
    await state.set_state(TplFSM.weekday)
    await _edit_cb(callback, texts.TPL_ASK_WEEKDAY, _weekday_kb())
    await callback.answer()


@router.message(TplFSM.count)
async def tpl_count(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await _delete(message)
    data = await state.get_data()
    if not raw.isdigit() or not (1 <= int(raw) <= MAX_EPISODES):
        await _edit_panel(
            message.bot, data, f"{texts.ERR_BAD_COUNT}\n\n{texts.TPL_ASK_COUNT}", _count_kb()
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
    await state.update_data(time=hhmm)
    await state.set_state(TplFSM.firstdate)
    data = await state.get_data()
    await _edit_panel(message.bot, data, _firstdate_prompt(data), _firstdate_kb())


async def _create_template_and_show(
    bot, state: FSMContext, data: dict, start_date: str, manual_done: int
) -> None:
    tid = await create_template(
        name=data["name"], weekday=data["weekday"], send_time=data["time"],
        episodes_count=data["count"], start_date=start_date, manual_done=manual_done,
        has_zero=int(data.get("has_zero") or 0), first_day=int(data.get("first_day") or 1),
    )
    await state.clear()
    text, kb = await _template_view(tid)
    await _edit_panel(bot, data, text, kb)
    logger.info(
        "Створено аніме #%d «%s» (старт %s, вручну=%d)",
        tid, data["name"], start_date, manual_done,
    )


@router.message(TplFSM.firstdate)
async def tpl_firstdate(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await _delete(message)
    data = await state.get_data()
    wd = data["weekday"]
    wd_name = texts.WEEKDAYS_FULL[wd].lower()
    try:
        d = date.fromisoformat(raw)
    except ValueError:
        await _edit_panel(
            message.bot, data, f"{texts.ERR_BAD_DATE}\n\n{_firstdate_prompt(data)}", _firstdate_kb()
        )
        return
    if d.weekday() != wd:
        await _edit_panel(
            message.bot, data,
            f"{texts.ERR_DATE_WEEKDAY.format(date=d.isoformat(), weekday=wd_name)}\n\n"
            f"{_firstdate_prompt(data)}",
            _firstdate_kb(),
        )
        return
    start_date = d.isoformat()
    first_day = int(data.get("first_day") or 1)
    manual_done = _past_episodes(start_date, _first_ep(data), data["count"], first_day)
    await _create_template_and_show(message.bot, state, data, start_date, manual_done)


@router.callback_query(TplFSM.firstdate, F.data == "tplskip")
async def tpl_skip(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    start_date = _compute_start_date(data["weekday"], data["time"])
    # Новий старт: перший день (first_day серій) викладається вручну → позначаємо готовим.
    await _create_template_and_show(
        callback.bot, state, data, start_date, int(data.get("first_day") or 1)
    )
    await callback.answer()


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
    await state.set_state(SlotFSM.collect)
    await state.update_data(
        panel_chat=callback.message.chat.id, panel_msg=callback.message.message_id,
        template_id=tid, episode_no=ep, run_at=_slot_run_at(tpl, ep),
        old_release_id=old_release_id,
    )
    data = await state.get_data()
    await _edit_cb(callback, _collect_text(data), _collect_kb(data))


@router.callback_query(F.data.startswith("slot:"))
async def cb_slot(callback: CallbackQuery, state: FSMContext):
    _, tid_s, ep_s = callback.data.split(":")
    tid, ep = int(tid_s), int(ep_s)
    tpl = await get_template(tid)
    if not tpl:
        text, kb = await _anime_list_view()
        await _edit_cb(callback, text, kb)
        await callback.answer()
        return
    page = _page_of(ep, _first_ep(tpl))
    rel = await get_release_by_slot(tid, ep)
    if rel:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_REPLACE, callback_data=f"screp:{tid}:{ep}")],
            [InlineKeyboardButton(text=texts.BTN_CANCEL_PUB, callback_data=f"scancel:{rel['id']}:{tid}:{ep}")],
            [InlineKeyboardButton(text=texts.BTN_BACK, callback_data=f"anpg:{tid}:{page}")],
        ])
        await _edit_cb(
            callback,
            texts.SLOT_FILLED.format(n=ep, run_at=rel["run_at"], caption=_title(rel["caption_html"])),
            kb,
        )
        await callback.answer()
        return
    # Серія з перших manual_done — викладається вручну поза ботом.
    if _is_manual(tpl, ep):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=texts.BTN_FILL_VIA_BOT, callback_data=f"slotfill:{tid}:{ep}")],
            [InlineKeyboardButton(text=texts.BTN_BACK, callback_data=f"anpg:{tid}:{page}")],
        ])
        await _edit_cb(callback, texts.SLOT_MANUAL.format(n=ep), kb)
        await callback.answer()
        return
    await _begin_slot_wizard(callback, state, tid, ep, old_release_id=None)
    await callback.answer()


@router.callback_query(F.data.startswith("slotfill:"))
async def cb_slot_fill(callback: CallbackQuery, state: FSMContext):
    _, tid_s, ep_s = callback.data.split(":")
    await _begin_slot_wizard(callback, state, int(tid_s), int(ep_s), old_release_id=None)
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
    _, rel_s, tid_s, ep_s = callback.data.split(":")
    await cancel_release(int(rel_s))
    await state.clear()
    tpl = await get_template(int(tid_s))
    page = _page_of(int(ep_s), _first_ep(tpl)) if tpl else 0
    text, kb = await _template_view(int(tid_s), page)
    if text is None:
        text, kb = await _anime_list_view()
    await _edit_cb(callback, text, kb)
    await callback.answer(texts.CANCEL_PUB_TOAST)


# ══════════════════════════════════════════════════════════════════════════════
#   Майстер заповнення слота
# ══════════════════════════════════════════════════════════════════════════════

@router.message(SlotFSM.collect)
async def slot_collect(message: Message, state: FSMContext):
    """Приймає превʼю/MP4/MKV у будь-якому порядку (зокрема альбомом),
    сам розкладає за типом. Коли всі три зібрані — зʼявляється «Запланувати»."""
    res = _classify(message)
    # Підпис беремо з будь-якого вкладення, що має caption: превʼю → у пост превʼю,
    # MP4 → у пост відео в release-каналі.
    cap_html = message.html_text if (res and message.caption) else None
    await _delete(message)
    # Лок: альбом дає кілька паралельних update'ів — read-modify-write має бути атомарним.
    async with _files_lock:
        data = await state.get_data()
        if res is None:
            await _edit_panel(
                message.bot, data, _collect_text(data, note=texts.ERR_NEED_FILE), _collect_kb(data)
            )
            return
        role, fid, kind = res
        if role == "preview":
            await state.update_data(preview_file_id=fid, caption_html=cap_html)
        elif role == "mp4":
            await state.update_data(mp4_file_id=fid, mp4_kind=kind, mp4_caption_html=cap_html)
        else:
            await state.update_data(mkv_file_id=fid, mkv_kind=kind)
        data = await state.get_data()
        await _edit_panel(message.bot, data, _collect_text(data), _collect_kb(data))


@router.callback_query(SlotFSM.collect, F.data == "rel_schedule")
async def cb_slot_schedule(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not _complete(data):
        await callback.answer(texts.NEED_ALL_FILES, show_alert=True)
        return
    if data.get("old_release_id"):
        await cancel_release(data["old_release_id"])
    await create_release(
        preview_file_id=data["preview_file_id"], caption_html=data.get("caption_html"),
        mp4_file_id=data["mp4_file_id"], mp4_kind=data["mp4_kind"],
        mp4_caption_html=data.get("mp4_caption_html"),
        mkv_file_id=data["mkv_file_id"], mkv_kind=data["mkv_kind"],
        run_at=data["run_at"], template_id=data["template_id"], episode_no=data["episode_no"],
    )
    tid = data["template_id"]
    ep = data["episode_no"]
    await state.clear()
    tpl = await get_template(tid)
    page = _page_of(ep, _first_ep(tpl)) if tpl else 0
    text, kb = await _template_view(tid, page)
    await _edit_cb(callback, text, kb)
    await callback.answer(texts.SCHEDULED_TOAST)
    logger.info("Заплановано серію %s аніме #%s на %s", ep, tid, data["run_at"])


# ── Скасування активного майстра ──────────────────────────────────────────────

@router.callback_query(F.data == "rel_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tid = data.get("template_id")
    ep = data.get("episode_no")
    await state.clear()
    if tid:
        tpl = await get_template(tid)
        page = _page_of(ep, _first_ep(tpl)) if (tpl and ep is not None) else 0
        text, kb = await _template_view(tid, page)
        if text is None:
            text, kb = await _anime_list_view()
    else:
        text, kb = await _anime_list_view()
    await _edit_cb(callback, text, kb)
    await callback.answer()


# ══════════════════════════════════════════════════════════════════════════════
#   Масовий імпорт серій (пересиланням з гілки супергрупи-сховища)
# ══════════════════════════════════════════════════════════════════════════════

def _import_kb(has_items: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if has_items:
        rows.append([InlineKeyboardButton(text=texts.BTN_IMPORT_LAYOUT, callback_data="import_layout")])
        rows.append([InlineKeyboardButton(text=texts.BTN_IMPORT_RESET, callback_data="import_reset")])
    rows.append([InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _empty_slots(tpl: dict, occupied: set[int]) -> list[int]:
    """Вільні слоти серій по порядку: ті, що не зайняті релізом і не викладені вручну."""
    first = _first_ep(tpl)
    last = tpl["episodes_count"]
    return [ep for ep in range(first, last + 1) if ep not in occupied]


def _build_episodes(items: list[dict]) -> tuple[list[dict] | None, int]:
    """Ріже відсортований буфер на трійки й розкладає кожну за типом.
    Повертає (список_епізодів, 0) або (None, номер_проблемної_трійки 1-based).
    Усередині трійки порядок не важливий — беремо по ролі."""
    episodes: list[dict] = []
    for i in range(0, len(items), 3):
        chunk = items[i:i + 3]
        by_role: dict[str, dict] = {}
        for it in chunk:
            by_role.setdefault(it["role"], it)
        if not ({"preview", "mp4", "mkv"} <= set(by_role)):
            return None, i // 3 + 1
        prev, mp4, mkv = by_role["preview"], by_role["mp4"], by_role["mkv"]
        episodes.append({
            "preview_file_id": prev["file_id"],
            "caption_html": prev.get("caption_html"),
            "mp4_file_id": mp4["file_id"],
            "mp4_kind": mp4["kind"],
            "mp4_caption_html": mp4.get("caption_html"),
            "mkv_file_id": mkv["file_id"],
            "mkv_kind": mkv["kind"],
        })
    return episodes, 0


@router.callback_query(F.data.startswith("import:"))
async def cb_import_start(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":")[1])
    tpl = await get_template(tid)
    if not tpl:
        text, kb = await _anime_list_view()
        await _edit_cb(callback, text, kb)
        await callback.answer()
        return
    await state.clear()
    await state.set_state(ImportFSM.collect)
    await state.update_data(
        panel_chat=callback.message.chat.id, panel_msg=callback.message.message_id,
        template_id=tid, items=[], last_panel_ts=0.0,
    )
    await _edit_cb(callback, texts.IMPORT_PROMPT, _import_kb(has_items=False))
    await callback.answer()


@router.message(ImportFSM.collect)
async def import_collect(message: Message, state: FSMContext):
    """Приймає переслані повідомлення гілки (превʼю/MP4/MKV) у буфер.
    Сторонні повідомлення тихо ігноруємо — щоб не засмічувати панель."""
    res = _classify(message)
    cap_html = message.html_text if (res and message.caption) else None
    msg_id = message.message_id
    await _delete(message)
    if res is None:
        return
    role, fid, kind = res
    # Лок: пересилання десятків повідомлень = багато паралельних update'ів,
    # read-modify-write буфера має бути атомарним.
    async with _files_lock:
        data = await state.get_data()
        items = list(data.get("items") or [])
        items.append({
            "message_id": msg_id, "role": role, "file_id": fid,
            "kind": kind, "caption_html": cap_html,
        })
        await state.update_data(items=items)
        # Троттлимо оновлення панелі (≤1/сек), щоб не впертися в ліміти Telegram.
        now = time.monotonic()
        if now - float(data.get("last_panel_ts") or 0.0) >= 1.0:
            await state.update_data(last_panel_ts=now)
            n = len(items)
            await _edit_panel(
                message.bot, data,
                texts.IMPORT_PROGRESS.format(n=n, eps=n // 3),
                _import_kb(has_items=True),
            )


@router.callback_query(ImportFSM.collect, F.data == "import_reset")
async def cb_import_reset(callback: CallbackQuery, state: FSMContext):
    await state.update_data(items=[], last_panel_ts=0.0, plan_episodes=[], plan_slots=[])
    await _edit_cb(callback, texts.IMPORT_PROMPT, _import_kb(has_items=False))
    await callback.answer()


@router.callback_query(ImportFSM.collect, F.data == "import_layout")
async def cb_import_layout(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tid = data["template_id"]
    items = sorted(data.get("items") or [], key=lambda x: x["message_id"])

    if not items:
        await callback.answer(texts.ERR_IMPORT_EMPTY, show_alert=True)
        return
    if len(items) % 3 != 0:
        await _edit_cb(
            callback, texts.ERR_IMPORT_NOT_TRIPLE.format(count=len(items)), _import_kb(True)
        )
        await callback.answer()
        return

    episodes, bad = _build_episodes(items)
    if episodes is None:
        await _edit_cb(callback, texts.ERR_IMPORT_BAD_GROUP.format(g=bad), _import_kb(True))
        await callback.answer()
        return

    tpl = await get_template(tid)
    if not tpl:
        await state.clear()
        text, kb = await _anime_list_view()
        await _edit_cb(callback, text, kb)
        await callback.answer()
        return

    occupied = _done_episodes(tpl, set(await episode_statuses(tid)))
    empty = _empty_slots(tpl, occupied)
    if not empty:
        await callback.answer(texts.ERR_IMPORT_NO_SLOTS, show_alert=True)
        return

    k = min(len(episodes), len(empty))
    target_slots = empty[:k]
    overflow = len(episodes) - k
    await state.update_data(plan_episodes=episodes[:k], plan_slots=target_slots)

    a, b = target_slots[0], target_slots[-1]
    d1 = _slot_date(tpl, a).strftime("%d.%m.%Y")
    d2 = _slot_date(tpl, b).strftime("%d.%m.%Y")
    overflow_line = texts.WARN_IMPORT_OVERFLOW.format(extra=overflow) if overflow else ""

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=texts.BTN_IMPORT_CONFIRM, callback_data="import_confirm")],
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")],
    ])
    await _edit_cb(callback, texts.IMPORT_CONFIRM.format(
        name=html.escape(tpl["name"]), count=k, a=a, b=b, d1=d1, d2=d2, overflow=overflow_line,
    ), kb)
    await callback.answer()


@router.callback_query(ImportFSM.collect, F.data == "import_confirm")
async def cb_import_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tid = data.get("template_id")
    episodes = data.get("plan_episodes") or []
    slots = data.get("plan_slots") or []
    tpl = await get_template(tid) if tid else None
    if not tpl or not episodes or not slots:
        await state.clear()
        text, kb = await _anime_list_view()
        await _edit_cb(callback, text, kb)
        await callback.answer()
        return

    await create_releases_bulk([
        {
            "preview_file_id": ep_data["preview_file_id"],
            "caption_html": ep_data.get("caption_html"),
            "mp4_file_id": ep_data["mp4_file_id"], "mp4_kind": ep_data["mp4_kind"],
            "mp4_caption_html": ep_data.get("mp4_caption_html"),
            "mkv_file_id": ep_data["mkv_file_id"], "mkv_kind": ep_data["mkv_kind"],
            "run_at": _slot_run_at(tpl, ep_no),
            "template_id": tid, "episode_no": ep_no,
        }
        for ep_data, ep_no in zip(episodes, slots)
    ])
    created = len(slots)

    await state.clear()
    text, kb = await _template_view(tid)
    await _edit_cb(callback, text, kb)
    await callback.answer(texts.IMPORT_DONE_TOAST.format(count=created))
    logger.info("Імпортовано %d серій у аніме #%s (слоти %s)", created, tid, slots)


# ══════════════════════════════════════════════════════════════════════════════
#   Репост з партнерського каналу
# ══════════════════════════════════════════════════════════════════════════════

def _parse_channel(raw: str) -> int | str | None:
    """@username або ID (-100…) → значення для Bot API; None, якщо не схоже на канал."""
    raw = (raw or "").strip()
    if raw.startswith("@") and len(raw) > 1 and " " not in raw:
        return raw
    if raw.lstrip("-").isdigit():
        return int(raw)
    return None


def _repost_prompt(tpl: dict) -> str:
    channel = tpl.get("repost_channel")
    current = (
        texts.REPOST_CURRENT_ON.format(channel=html.escape(str(channel)))
        if channel else texts.REPOST_CURRENT_OFF
    )
    return texts.REPOST_PROMPT.format(current=current)


def _repost_kb(template_id: int, has_channel: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_channel:
        rows.append([InlineKeyboardButton(
            text=texts.BTN_REPOST_OFF, callback_data=f"repost_off:{template_id}",
        )])
    rows.append([InlineKeyboardButton(text=texts.BTN_BACK, callback_data=f"anime:{template_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("repost:"))
async def cb_repost_start(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":")[1])
    tpl = await get_template(tid)
    if not tpl:
        text, kb = await _anime_list_view()
        await _edit_cb(callback, text, kb)
        await callback.answer()
        return
    await state.clear()
    await state.set_state(RepostFSM.channel)
    await state.update_data(
        panel_chat=callback.message.chat.id, panel_msg=callback.message.message_id,
        template_id=tid,
    )
    await _edit_cb(callback, _repost_prompt(tpl), _repost_kb(tid, bool(tpl.get("repost_channel"))))
    await callback.answer()


@router.message(RepostFSM.channel)
async def repost_channel_input(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    await _delete(message)
    data = await state.get_data()
    tid = data["template_id"]
    tpl = await get_template(tid)
    if not tpl:
        await state.clear()
        return

    channel = _parse_channel(raw)
    if channel is not None:
        try:
            await message.bot.get_chat(channel)
        except Exception:
            channel = None
    if channel is None:
        await _edit_panel(
            message.bot, data,
            texts.ERR_REPOST_CHANNEL + "\n\n" + _repost_prompt(tpl),
            _repost_kb(tid, bool(tpl.get("repost_channel"))),
        )
        return

    await set_repost_channel(tid, str(channel))
    await state.clear()
    text, kb = await _template_view(tid)
    await _edit_panel(message.bot, data, text, kb)
    logger.info("Аніме #%d: репост через канал %s", tid, channel)


@router.callback_query(F.data.startswith("repost_off:"))
async def cb_repost_off(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":")[1])
    await set_repost_channel(tid, None)
    await state.clear()
    text, kb = await _template_view(tid)
    if text is None:
        text, kb = await _anime_list_view()
    await _edit_cb(callback, text, kb)
    await callback.answer(texts.REPOST_OFF_TOAST)
