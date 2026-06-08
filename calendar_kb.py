import calendar as _calmod
from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import texts

CB = "cal"

_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
_MONTHS = [
    "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]

_CAL = _calmod.Calendar(firstweekday=0)  # 0 = понеділок


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def build_calendar(year: int, month: int, today: date) -> InlineKeyboardMarkup:
    """Сітка місяця. Минулі дні некликабельні; назад не далі поточного місяця."""
    rows: list[list[InlineKeyboardButton]] = []

    at_min_month = (year, month) <= (today.year, today.month)
    prev_y, prev_m = _shift_month(year, month, -1)
    next_y, next_m = _shift_month(year, month, 1)
    rows.append([
        InlineKeyboardButton(
            text=" " if at_min_month else "‹",
            callback_data=f"{CB}:ignore" if at_min_month else f"{CB}:nav:{prev_y}-{prev_m:02d}",
        ),
        InlineKeyboardButton(text=f"{_MONTHS[month - 1]} {year}", callback_data=f"{CB}:ignore"),
        InlineKeyboardButton(text="›", callback_data=f"{CB}:nav:{next_y}-{next_m:02d}"),
    ])

    rows.append([InlineKeyboardButton(text=d, callback_data=f"{CB}:ignore") for d in _WEEKDAYS])

    for week in _CAL.monthdayscalendar(year, month):
        row: list[InlineKeyboardButton] = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data=f"{CB}:ignore"))
                continue
            d = date(year, month, day)
            if d < today:
                row.append(InlineKeyboardButton(text="·", callback_data=f"{CB}:ignore"))
            else:
                label = f"[{day}]" if d == today else str(day)
                row.append(InlineKeyboardButton(text=label, callback_data=f"{CB}:day:{d.isoformat()}"))
        rows.append(row)

    rows.append([InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="rel_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
