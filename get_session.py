"""Получение .session файла для аккаунта-наблюдателя (Telethon).

Запуск (бот должен быть ОСТАНОВЛЕН):

  Windows:
    python get_session.py

  Linux:
    sudo systemctl stop workparser
    cd /opt/WorkParserBot && source venv/bin/activate
    python get_session.py

После успеха появится файл observer.session (имя из SESSION в .env).
Его можно скопировать на сервер, если логинились на ПК.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

# Подтягиваем API_ID / API_HASH / SESSION из .env через config
import config
from tg_time import sync_telegram_time


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        print("\n[!] Нужен интерактивный терминал (обычный SSH / PowerShell).")
        sys.exit(1)


def digits_only(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


async def main() -> None:
    session_name = str(config.BASE_DIR / config.SESSION)
    session_file = Path(f"{session_name}.session")

    print("=" * 50)
    print("  Получение Telegram-сессии (observer)")
    print("=" * 50)
    print(f"API_ID : {config.API_ID}")
    print(f"Сессия: {session_file}")
    print()
    print("Перед запуском остановите бота, иначе код может «сгорать».")
    print("Код вводите ЗДЕСЬ в консоли — не в чат бота.\n")

    client = TelegramClient(session_name, config.API_ID, config.API_HASH)
    await client.connect()
    await sync_telegram_time(client, label="get_session", warn=lambda m: print(f"[time] {m}"))

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Уже авторизован: {me.first_name} (id={me.id})")
        print(f"Файл сессии: {session_file}")
        await client.disconnect()
        return

    phone = ask("Номер телефона (+79991234567): ").replace(" ", "")
    if not phone.startswith("+"):
        print("[!] Номер должен быть в формате +79991234567")
        await client.disconnect()
        sys.exit(1)

    try:
        sent = await client.send_code_request(phone)
    except FloodWaitError as e:
        print(f"[!] FloodWait: подождите {e.seconds} сек. (~{e.seconds // 60} мин.)")
        await client.disconnect()
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"[!] Ошибка запроса кода: {e}")
        await client.disconnect()
        sys.exit(1)

    phone_code_hash = sent.phone_code_hash
    if not phone_code_hash:
        print("[!] Telegram не вернул phone_code_hash. Проверьте API_ID и API_HASH в .env")
        await client.disconnect()
        sys.exit(1)

    print(f"Код отправлен ({type(sent.type).__name__}).")
    print("Смотрите ПОСЛЕДНЕЕ сообщение от Telegram / SMS.")
    code = digits_only(ask("Код из Telegram: "))
    if len(code) < 5:
        print("[!] Код слишком короткий")
        await client.disconnect()
        sys.exit(1)

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        password = ask("Пароль 2FA: ")
        try:
            await client.sign_in(password=password)
        except Exception as e:  # noqa: BLE001
            print(f"[!] Ошибка 2FA: {e}")
            await client.disconnect()
            sys.exit(1)
    except PhoneCodeInvalidError:
        print("[!] Неверный код. Запустите скрипт снова и введите НОВЫЙ код.")
        await client.disconnect()
        sys.exit(1)
    except PhoneCodeExpiredError:
        print("[!] Код истёк. Подождите 15–20 мин, удалите старую сессию и повторите:")
        print(f"    rm -f {session_file} {session_file}-journal")
        await client.disconnect()
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        print(f"[!] Ошибка входа: {e}")
        await client.disconnect()
        sys.exit(1)

    me = await client.get_me()
    print()
    print("✅ Готово")
    print(f"   Аккаунт : {me.first_name} (id={me.id})")
    if me.username:
        print(f"   Username: @{me.username}")
    print(f"   Файл    : {session_file.resolve()}")
    print()
    print("Дальше на сервере:")
    print("  sudo systemctl start workparser")
    print("Или скопируйте .session на сервер в папку бота.")
    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОтменено.")
