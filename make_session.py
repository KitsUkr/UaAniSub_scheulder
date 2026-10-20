"""Одноразово: рядок сесії юзербота для MOON_SESSION у .env.

    python make_session.py              # вхід за QR-кодом
    python make_session.py --phone      # вхід за номером телефону і кодом
    python make_session.py --no-invert  # якщо QR не сканується (світла тема терміналу)

QR: Telegram на телефоні → Налаштування → Пристрої → Підключити пристрій →
навести камеру на код у терміналі. Код живе близько хвилини, скрипт оновлює
його сам, доки не відскануєш. Акаунт нікуди не виходить — додається ще один сеанс.

Входь тим акаунтом, який бот-завантажувач MoonAnime знає як учасника команди —
саме за ним він підставляє озвучку і приймає файли.

Рядок сесії = повний доступ до акаунта. Нікому не показуй і не коміть.
"""

import asyncio
import os
import sys
from getpass import getpass

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

load_dotenv()

PASSWORD_TRIES = 3


def _print_qr(url: str, invert: bool) -> bool:
    """Малює QR у терміналі. False — не вийшло (нема qrcode або консоль не тягне юнікод)."""
    try:
        import qrcode
    except ImportError:
        print("Немає бібліотеки qrcode — постав: pip install qrcode")
        return False
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=invert)
    except Exception as exc:
        print(f"Не вдалося намалювати QR ({exc}).")
        return False
    return True


async def _ask_2fa(client: TelegramClient) -> None:
    for attempt in range(PASSWORD_TRIES):
        try:
            await client.sign_in(password=getpass("Пароль 2FA (не відображається): "))
            return
        except Exception as exc:
            left = PASSWORD_TRIES - attempt - 1
            if not left:
                raise
            print(f"Не підійшов ({exc}). Спроб лишилось: {left}")


async def _qr_login(client: TelegramClient, invert: bool) -> None:
    qr = await client.qr_login()
    while True:
        print("\nTelegram на телефоні → Налаштування → Пристрої → Підключити пристрій\n")
        if not _print_qr(qr.url, invert):
            print(f"\nАбо відкрий це посилання на телефоні:\n{qr.url}\n")
        print("Чекаю на сканування…  (Ctrl+C — вийти)")
        try:
            await qr.wait()
        except asyncio.TimeoutError:
            await qr.recreate()  # код протермінувався — малюємо новий
            print("\nQR протермінувався, ось новий.")
            continue
        except SessionPasswordNeededError:
            await _ask_2fa(client)  # на акаунті ввімкнена двоетапна перевірка
        return


async def main() -> None:
    args = sys.argv[1:]
    invert = "--no-invert" not in args

    raw_id = (os.getenv("MOON_API_ID") or input("api_id (my.telegram.org): ")).strip()
    if not raw_id.isdigit():
        sys.exit("api_id має бути числом — візьми його на my.telegram.org.")
    api_hash = (os.getenv("MOON_API_HASH") or input("api_hash: ")).strip()

    client = TelegramClient(StringSession(), int(raw_id), api_hash)
    await client.connect()
    try:
        if "--phone" in args:
            await client.start()  # сам спитає номер, код і, якщо треба, пароль 2FA
        else:
            await _qr_login(client, invert)
        me = await client.get_me()
        print(f"\nУвійшли як {me.first_name} (@{me.username}). Додай у .env:\n")
        print(f"MOON_SESSION={client.session.save()}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nСкасовано.")
