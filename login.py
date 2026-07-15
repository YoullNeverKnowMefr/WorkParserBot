"""One-time console login for the observer account.

Stop the bot first, then run:
  sudo systemctl stop workparser
  cd /opt/WorkParserBot && source venv/bin/activate
  python login.py

Enter phone, code, and 2FA (if any). Session is saved for bot.py.
"""
import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    SessionPasswordNeededError,
    FloodWaitError,
)

import config


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        print("\nНет ввода (stdin). Запускайте login.py в интерактивном SSH, не через pipe.")
        sys.exit(1)


async def main():
    session_path = str(config.BASE_DIR / config.SESSION)
    print(f"API_ID={config.API_ID}")
    print(f"Сессия: {session_path}.session")
    print("Убедитесь, что bot.py / workparser остановлены.\n")

    client = TelegramClient(session_path, config.API_ID, config.API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Уже авторизован: {me.first_name} (id={me.id})")
        await client.disconnect()
        return

    phone = _ask("Номер телефона (+79991234567): ").replace(" ", "")
    if not phone.startswith("+"):
        print("Номер должен начинаться с +, например +79991234567")
        await client.disconnect()
        sys.exit(1)

    try:
        sent = await client.send_code_request(phone)
    except FloodWaitError as exc:
        print(f"FloodWait: подождите {exc.seconds} сек. и попробуйте снова.")
        await client.disconnect()
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка send_code_request: {exc}")
        await client.disconnect()
        sys.exit(1)

    phone_code_hash = sent.phone_code_hash
    if not phone_code_hash:
        print("Telegram не вернул phone_code_hash. Проверьте API_ID/API_HASH в .env")
        await client.disconnect()
        sys.exit(1)

    print(f"Код отправлен (type={getattr(sent.type, '__class__', type(sent.type)).__name__}).")
    print("Введите код из Telegram / SMS обычными цифрами (здесь это безопасно).")
    code = _ask("Код: ").replace(" ", "").replace("-", "")

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        password = _ask("Пароль 2FA: ")
        try:
            await client.sign_in(password=password)
        except Exception as exc:  # noqa: BLE001
            print(f"Ошибка 2FA: {exc}")
            await client.disconnect()
            sys.exit(1)
    except PhoneCodeInvalidError:
        print("Неверный код. Запустите login.py снова и запросите новый код.")
        await client.disconnect()
        sys.exit(1)
    except PhoneCodeExpiredError:
        print("Код истёк. Запустите login.py снова.")
        await client.disconnect()
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка sign_in: {exc}")
        await client.disconnect()
        sys.exit(1)

    me = await client.get_me()
    print(f"\n✅ Авторизован: {me.first_name} (id={me.id})")
    print(f"Сессия сохранена: {session_path}.session")
    print("Запустите бота: sudo systemctl start workparser")
    await client.disconnect()


if __name__ == "__main__":
    # Avoid picking up a stale session lock from a running bot.
    lockish = [
        config.BASE_DIR / f"{config.SESSION}.session",
        config.BASE_DIR / f"{config.SESSION}.session-journal",
    ]
    for p in lockish:
        if p.exists():
            try:
                # If unauthorized/corrupt leftovers cause weird errors, user can delete manually.
                pass
            except OSError:
                pass

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОтменено.")
