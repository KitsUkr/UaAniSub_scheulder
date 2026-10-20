import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Помилка: {name} не задано в .env файлі!")
    return value


BOT_TOKEN = _require("BOT_TOKEN")

try:
    ADMIN_IDS: list[int] = [
        int(uid.strip())
        for uid in _require("ADMIN_IDS").split(",")
        if uid.strip()
    ]
except ValueError:
    sys.exit(
        "Помилка: ADMIN_IDS має містити числові Telegram ID через кому, "
        "напр. ADMIN_IDS=123456789,987654321"
    )

# Канал-вітрина, куди йде превʼю з посиланнями.
PREVIEW_CHANNEL = _require("PREVIEW_CHANNEL")
# Канал-сховище, куди заливаються самі відео (MP4) та контейнери (MKV).
RELEASE_CHANNEL = _require("RELEASE_CHANNEL")

DB_PATH = os.getenv("DB_PATH", "releases.db").strip() or "releases.db"

# MoonAnime: після публікації юзербот пересилає MP4 боту-завантажувачу плеєра
# (бот не може писати іншому боту — обмеження Telegram). Сесію створює
# make_session.py. Не задано — вивантаження вимкнене, решта працює як раніше.
try:
    MOON_API_ID = int(os.getenv("MOON_API_ID", "").strip() or 0)
except ValueError:
    sys.exit("Помилка: MOON_API_ID має бути числом (api_id з my.telegram.org).")
MOON_API_HASH = os.getenv("MOON_API_HASH", "").strip()
MOON_SESSION = os.getenv("MOON_SESSION", "").strip()
MOON_BOT = os.getenv("MOON_BOT", "").strip() or "@moonanime_up_bot"
MOON_ENABLED = bool(MOON_API_ID and MOON_API_HASH and MOON_SESSION)

# Часовий пояс — уся логіка часу (планування, публікація) рахується в київському.
TZ = "Europe/Kyiv"
