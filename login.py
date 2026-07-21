"""Консольный вход в аккаунт-наблюдатель (Telethon).

Почему не через бота:
  если отправить код в чат бота, Telegram часто сразу аннулирует код
  и сессия «сбрасывается». Здесь код вводится только в терминале.

Запуск (бот ОБЯЗАТЕЛЬНО остановлен):

  Windows:
    python login.py

  Linux:
    sudo systemctl stop workparser
    cd /opt/WorkParserBot && source venv/bin/activate
    python login.py
    sudo systemctl start workparser

После успеха появится / обновится файл <SESSION>.session (по умолчанию observer.session).
Бот при старте подхватит его сам — повторно логиниться в чате не нужно.
"""
from __future__ import annotations

import asyncio
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

import config
from tg_time import get_time_offset, sync_telegram_time


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        print("\n[!] Нужен интерактивный терминал (PowerShell / SSH), не фоновый запуск.")
        sys.exit(1)


def digits_only(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def log(msg: str) -> None:
    print(f"[{now_iso()}] {msg}")


def mask_hash(value: str | None, keep: int = 6) -> str:
    if not value:
        return "<empty>"
    if len(value) <= keep * 2:
        return f"{value[:2]}…({len(value)} chars)"
    return f"{value[:keep]}…{value[-keep:]} (len={len(value)})"


def mask_phone(phone: str) -> str:
    digits = digits_only(phone)
    if len(digits) < 6:
        return phone
    return f"+{digits[:2]}***{digits[-2:]}"


def describe_sent_type(sent) -> str:
    t = getattr(sent, "type", None)
    if t is None:
        return "unknown"
    name = type(t).__name__
    extra = []
    for attr in ("length", "pattern", "next_type", "timeout"):
        val = getattr(t, attr, None)
        if val is not None:
            extra.append(f"{attr}={val}")
    return f"{name}" + (f" ({', '.join(extra)})" if extra else "")


def print_code_failure(
    kind: str,
    *,
    phone: str,
    code_raw: str,
    code_digits: str,
    phone_code_hash: str,
    sent_type: str,
    requested_at: float,
    entered_at: float,
    exc: BaseException,
) -> None:
    elapsed = entered_at - requested_at
    print()
    log(f"ОШИБКА ВХОДА: {kind}")
    print("-" * 54)
    print(f"  exception     : {type(exc).__name__}: {exc}")
    print(f"  phone         : {mask_phone(phone)}")
    print(f"  delivery      : {sent_type}")
    print(f"  code_raw      : {code_raw!r}")
    print(f"  code_digits   : {code_digits!r} (len={len(code_digits)})")
    print(f"  phone_code_hash: {mask_hash(phone_code_hash)}")
    print(f"  requested_at  : {datetime.fromtimestamp(requested_at).strftime('%H:%M:%S')}")
    print(f"  entered_at    : {datetime.fromtimestamp(entered_at).strftime('%H:%M:%S')}")
    print(f"  elapsed_sec   : {elapsed:.1f}")
    print(f"  api_id        : {config.API_ID}")
    print(f"  api_hash      : {mask_hash(config.API_HASH, keep=4)}")
    print("-" * 54)
    if kind == "PhoneCodeExpired":
        print("Что обычно значит:")
        print("  • код протух по времени (ждали слишком долго)")
        print("  • был запрошен НОВЫЙ код (бот / второй скрипт) — старый hash уже мёртв")
        print("  • параллельно кто-то логинился тем же номером")
    else:
        print("Что обычно значит:")
        print("  • неверный код (взяли не последнее сообщение)")
        print("  • код уже сброшен: его отправили в чат бота / другому клиенту")
        print("  • опечатка / лишние символы (скрипт берёт только цифры)")
    print()
    print("Что сделать:")
    print("  1) Полностью остановите бота (bot.py / systemctl stop workparser)")
    print("  2) Не жмите «Войти» в Telegram-боте")
    print("  3) Подождите 1–2 минуты (при частых попытках — 15–30 мин)")
    print("  4) Запустите login.py снова и введите ТОЛЬКО свежий код в консоль")
    print("  5) API_ID/API_HASH должны быть теми же, что на сервере")
    if elapsed > 180:
        print()
        print(f"  Подсказка: между запросом и вводом прошло {elapsed:.0f} сек — для Telegram это много.")
    if code_raw != code_digits:
        print()
        print(f"  Подсказка: из ввода {code_raw!r} взяты цифры {code_digits!r}.")


async def main() -> None:
    session_name = str(config.BASE_DIR / config.SESSION)
    session_file = Path(f"{session_name}.session")
    journal = Path(f"{session_name}.session-journal")

    print("=" * 54)
    print("  Вход в observer (код только в консоли)")
    print("=" * 54)
    log(f"API_ID      : {config.API_ID}")
    log(f"API_HASH    : {mask_hash(config.API_HASH, keep=4)}")
    log(f"SESSION     : {config.SESSION}")
    log(f"session file: {session_file} (exists={session_file.exists()}, "
        f"size={session_file.stat().st_size if session_file.exists() else 0})")
    if journal.exists():
        log(f"journal     : {journal} EXISTS — бот/другой процесс мог держать сессию")
    print()
    print("1) Остановите workparser / bot.py перед входом.")
    print("2) Код из Telegram вводите СЮДА, не в чат бота.")
    print("3) После успеха снова запустите бота — сессию не трогайте.\n")

    client = TelegramClient(session_name, config.API_ID, config.API_HASH)
    log("Подключение к Telegram…")
    await client.connect()
    log(f"connected={client.is_connected()}")

    log("Синхронизация времени с серверами Telegram…")
    offset = await sync_telegram_time(client, label="login", warn=log)
    if offset is not None:
        log(f"time_offset={offset:.3f}s (локальные часы "
            f"{'отстают' if offset > 0 else 'спешат' if offset < 0 else 'совпадают'} "
            f"относительно Telegram)")

    if await client.is_user_authorized():
        me = await client.get_me()
        log(f"Уже авторизован: {me.first_name} (id={me.id})")
        if me.username:
            print(f"Username: @{me.username}")
        print(f"Файл сессии: {session_file.resolve()}")
        print("\nНичего делать не нужно — просто запустите бота.")
        await client.disconnect()
        return

    log("Сессия не авторизована — нужен вход по коду.")
    phone = ask("Номер телефона (+79991234567): ").replace(" ", "")
    if not phone.startswith("+"):
        log("Номер должен быть в формате +79991234567")
        await client.disconnect()
        sys.exit(1)

    # Ещё раз подтянуть offset прямо перед запросом кода
    await sync_telegram_time(client, label="pre-code", warn=log)
    log(f"time_offset перед кодом: {get_time_offset(client)}")

    log(f"Запрос кода для {mask_phone(phone)}…")
    requested_at = time.time()
    try:
        sent = await client.send_code_request(phone)
    except FloodWaitError as e:
        mins = max(1, e.seconds // 60)
        log(f"FloodWait: подождите {e.seconds} сек. (~{mins} мин.)")
        await client.disconnect()
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        log(f"Ошибка запроса кода: {type(e).__name__}: {e}")
        traceback.print_exc()
        await client.disconnect()
        sys.exit(1)

    phone_code_hash = sent.phone_code_hash
    sent_type = describe_sent_type(sent)
    if not phone_code_hash:
        log("Telegram не вернул phone_code_hash. Проверьте API_ID / API_HASH в .env")
        await client.disconnect()
        sys.exit(1)

    log(f"Код запрошен OK | delivery={sent_type}")
    log(f"phone_code_hash={mask_hash(phone_code_hash)}")
    timeout = getattr(getattr(sent, "type", None), "timeout", None)
    if timeout:
        log(f"Telegram timeout подсказки: {timeout} сек — не затягивайте ввод")
    print()
    print("Откройте Telegram и возьмите ПОСЛЕДНИЙ код.")
    print("Вводите его ТОЛЬКО здесь (не в чат бота).")
    code_raw = ask("Код: ")
    entered_at = time.time()
    code = digits_only(code_raw)
    log(f"Ввод получен: raw={code_raw!r} digits={code!r} len={len(code)} "
        f"after={entered_at - requested_at:.1f}s")

    if len(code) < 5:
        log(f"Код слишком короткий после очистки цифр: {code!r}")
        await client.disconnect()
        sys.exit(1)

    log(f"sign_in… (time_offset={get_time_offset(client)})")
    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        log("Нужен пароль 2FA")
        password = ask("Пароль 2FA: ")
        try:
            await client.sign_in(password=password)
        except Exception as e:  # noqa: BLE001
            log(f"Ошибка 2FA: {type(e).__name__}: {e}")
            traceback.print_exc()
            await client.disconnect()
            sys.exit(1)
    except PhoneCodeInvalidError as e:
        print_code_failure(
            "PhoneCodeInvalid",
            phone=phone,
            code_raw=code_raw,
            code_digits=code,
            phone_code_hash=phone_code_hash,
            sent_type=sent_type,
            requested_at=requested_at,
            entered_at=entered_at,
            exc=e,
        )
        await client.disconnect()
        sys.exit(1)
    except PhoneCodeExpiredError as e:
        print_code_failure(
            "PhoneCodeExpired",
            phone=phone,
            code_raw=code_raw,
            code_digits=code,
            phone_code_hash=phone_code_hash,
            sent_type=sent_type,
            requested_at=requested_at,
            entered_at=entered_at,
            exc=e,
        )
        await client.disconnect()
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        log(f"Ошибка входа: {type(e).__name__}: {e}")
        traceback.print_exc()
        await client.disconnect()
        sys.exit(1)

    me = await client.get_me()
    print()
    log("Готово — сессия сохранена.")
    print(f"  Аккаунт : {me.first_name} (id={me.id})")
    if me.username:
        print(f"  Username: @{me.username}")
    print(f"  Файл    : {session_file.resolve()}")
    print(f"  size    : {session_file.stat().st_size if session_file.exists() else 0} bytes")
    print()
    print("Дальше:")
    print("  1) Не жмите «Войти» / «Выйти» в боте")
    print("  2) Запустите бота — он подхватит observer.session")
    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОтменено.")
