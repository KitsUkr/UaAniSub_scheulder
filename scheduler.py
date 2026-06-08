"""Публікація запланованих випусків через APScheduler.

Щохвилини (на нульовій секунді) беремо «дозрілі» випуски (run_at <= зараз,
Київ) і публікуємо. Порядок у день Х критичний: спершу відео в release-канал,
потім — одразу превʼю з посиланнями на ці пости.
"""

import asyncio
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import texts
from caption import render_caption
from config import ADMIN_IDS, PREVIEW_CHANNEL, RELEASE_CHANNEL, TZ
from database import due_releases, mark_failed, mark_sent

logger = logging.getLogger(__name__)

_KYIV = ZoneInfo(TZ)
_DT_FMT = "%Y-%m-%d %H:%M"


def _msg_link(channel: str, message_id: int) -> str:
    return f"https://t.me/{channel.lstrip('@')}/{message_id}"


async def _send_media(bot: Bot, file_id: str, kind: str, caption: str | None = None):
    """Репост файлу за file_id (без завантаження/перезаливки — будь-який розмір).
    Метод відповідає способу отримання: video → відтворюється inline,
    document → завантажується. caption (HTML) кріпиться під медіа."""
    if kind == "video":
        return await bot.send_video(
            RELEASE_CHANNEL, file_id, caption=caption, supports_streaming=True
        )
    return await bot.send_document(RELEASE_CHANNEL, file_id, caption=caption)


async def _publish_one(bot: Bot, r: dict) -> None:
    # Послідовні await гарантують порядок: MP4 → MKV → превʼю.
    mp4_msg = await _send_media(bot, r["mp4_file_id"], r["mp4_kind"], r.get("mp4_caption_html"))
    mkv_msg = await _send_media(bot, r["mkv_file_id"], r["mkv_kind"])

    watch = _msg_link(RELEASE_CHANNEL, mp4_msg.message_id)
    download = _msg_link(RELEASE_CHANNEL, mkv_msg.message_id)
    caption = render_caption(r["caption_html"], watch, download)

    await bot.send_photo(PREVIEW_CHANNEL, r["preview_file_id"], caption=caption)


async def _alert_admins(bot: Bot, r: dict, exc: Exception) -> None:
    text = texts.ALERT_PUBLISH_FAILED.format(
        id=r["id"], run_at=r["run_at"], error=html.escape(str(exc)),
    )
    for uid in ADMIN_IDS:
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass


async def _publish_due(bot: Bot) -> None:
    now = datetime.now(_KYIV).strftime(_DT_FMT)
    rows = await due_releases(now)
    if not rows:
        return

    logger.info("Публікація: %d випуск(ів) (час %s)", len(rows), now)
    for r in rows:
        try:
            await _publish_one(bot, r)
            await mark_sent(r["id"])
            logger.info("Випуск #%d опубліковано", r["id"])
        except Exception as exc:
            # Найчастіше — бот не адмін у каналі або канал недоступний.
            await mark_failed(r["id"])
            logger.exception("Не вдалося опублікувати випуск #%d: %s", r["id"], exc)
            await _alert_admins(bot, r, exc)
        await asyncio.sleep(0.05)  # делікатно щодо лімітів Telegram


def start(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=_KYIV)
    scheduler.add_job(_publish_due, "cron", second=0, args=[bot], id="publish_due")
    scheduler.start()
    logger.info("Планувальник публікацій запущено (TZ=%s)", TZ)
    return scheduler


def stop(scheduler: AsyncIOScheduler | None) -> None:
    if scheduler is not None:
        scheduler.shutdown(wait=False)
