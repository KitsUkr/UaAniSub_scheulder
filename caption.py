"""Вставка посилань у готовий підпис превʼю.

Адмін пише підпис сам (із форматуванням Telegram), а в день публікації бот
перетворює слова «Переглянути» / «Завантажити» на гіперпосилання, що ведуть
на щойно опубліковані пости MP4 / MKV у release-каналі.

HTML-теги не входять у ліміт підпису фото (1024 видимих символи), тож вставка
посилань довжину не збільшує.
"""

import html
import re

WATCH_WORD = "Переглянути"
DOWNLOAD_WORD = "Завантажити"

# Розбиваємо наявне посилання на частини: відкривний тег / видимий текст / </a>.
# Так ми можемо переставити href, зберігши сам текст посилання.
_ANCHOR_PARTS_RE = re.compile(r"(<a\b[^>]*>)(.*?)(</a>)", re.IGNORECASE | re.DOTALL)
# Просто межі посилань — щоб не обгортати слово, що вже всередині <a> (без вкладень).
_ANCHOR_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)


def _anchor(word: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{word}</a>'


def _link_word(text: str, word: str, url: str) -> tuple[str, bool]:
    """Гарантує, що `word` у підписі веде на `url`. Повертає (новий_текст, чи_знайшли).

    Якщо слово вже є посиланням (адмін прислав превʼю зі старими посиланнями на
    попередню серію) — переставляємо href цього <a> на новий `url`, зберігши текст.
    Якщо слово — звичайний текст поза посиланнями — обгортаємо його в нове <a>."""
    # 1) Слово вже всередині <a>…</a> → міняємо лише href першого такого посилання.
    retargeted = False

    def _swap(m: re.Match) -> str:
        nonlocal retargeted
        if retargeted or word not in m.group(2):
            return m.group(0)
        retargeted = True
        return f'<a href="{html.escape(url, quote=True)}">{m.group(2)}</a>'

    new_text = _ANCHOR_PARTS_RE.sub(_swap, text)
    if retargeted:
        return new_text, True

    # 2) Слово — звичайний текст → обгортаємо перше входження поза наявними <a>.
    spans = [m.span() for m in _ANCHOR_RE.finditer(text)]
    start = 0
    while True:
        idx = text.find(word, start)
        if idx == -1:
            return text, False
        end = idx + len(word)
        if not any(idx < s_end and end > s_start for s_start, s_end in spans):
            return text[:idx] + _anchor(word, url) + text[end:], True
        start = end


def render_caption(caption_html: str | None, watch_url: str, download_url: str) -> str:
    """Готовий HTML-підпис превʼю з вставленими посиланнями.

    Якщо слова в підписі нема — дописуємо відповідне посилання рядком дій
    (у стилі чинного send_videos.py), щоб обидва переходи завжди були."""
    text = caption_html or ""
    text, has_watch = _link_word(text, WATCH_WORD, watch_url)
    text, has_download = _link_word(text, DOWNLOAD_WORD, download_url)

    missing: list[str] = []
    if not has_watch:
        missing.append(_anchor(WATCH_WORD, watch_url))
    if not has_download:
        missing.append(_anchor(DOWNLOAD_WORD, download_url))

    if missing:
        action = "👉 • " + " • ".join(missing) + " •"
        text = f"{text}\n\n{action}" if text.strip() else action

    return text
