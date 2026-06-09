import asyncio
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


class TplFSM(StatesGroup):
    has_zero = State()
    name = State()
    count = State()
    weekday = State()
    time = State()
    firstdate = State()


class SlotFSM(StatesGroup):
    collect = State()  # один крок: приймаємо превʼю + MP4 + MKV у будь-якому порядку


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
    prompt = texts.TPL_ASK_FIRSTDATE.format(weekday=wd_name)
    if data.get("has_zero"):
        prompt += "\n\n" + texts.TPL_FIRSTDATE_ZERO_NOTE
    return prompt


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
        done = len(_done_episodes(t, await filled_episodes(t["id"])))
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
    done = _done_episodes(tpl, await filled_episodes(template_id))
    wd = tpl["weekday"]
    start_date = date.fromisoformat(tpl["start_date"])

    start_ep = first + page * PER_PAGE
    end_ep = min(last, start_ep + PER_PAGE - 1)

    text = texts.TEMPLATE_HEADER.format(
        name=html.escape(tpl["name"]),
        count=total_slots,
        weekday=texts.WEEKDAYS_FULL[wd],
        time=tpl["send_time"],
        filled=len(done),
    )
    d1 = start_date + timedelta(days=7 * (start_ep - 1))
    d2 = start_date + timedelta(days=7 * (end_ep - 1))
    text += "\n\n" + texts.SLOT_RANGE.format(
        a=start_ep, b=end_ep, d1=d1.strftime("%d.%m"), d2=d2.strftime("%d.%m"),
    )

    # Слоти сторінки — компактні кнопки «✅/⬜ N», по PER_ROW у рядку.
    buttons = [
        InlineKeyboardButton(
            text=texts.SLOT_BTN.format(mark="✅" if ep in done else "⬜", n=ep),
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


def _past_episodes(start_date_iso: str, first_ep: int, last_ep: int) -> int:
    """Скільки перших серій уже мали б вийти (їх дата раніше за сьогодні, Київ).
    Дата серії e = start_date + 7*(e-1) (start_date — дата серії 1, нульова — на
    тиждень раніше). Використовується для авто-позначки «викладено вручну»."""
    start = date.fromisoformat(start_date_iso)
    today = datetime.now(_KYIV).date()
    n = 0
    for e in range(first_ep, last_ep + 1):
        if start + timedelta(days=7 * (e - 1)) < today:
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
    await state.set_state(TplFSM.name)
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
        has_zero=int(data.get("has_zero") or 0),
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
    manual_done = _past_episodes(start_date, _first_ep(data), data["count"])
    await _create_template_and_show(message.bot, state, data, start_date, manual_done)


@router.callback_query(TplFSM.firstdate, F.data == "tplskip")
async def tpl_skip(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    start_date = _compute_start_date(data["weekday"], data["time"])
    await _create_template_and_show(callback.bot, state, data, start_date, 1)
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
