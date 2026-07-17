"""Синхронизация времени клиента с серверами Telegram (Telethon time_offset).

Telegram отклоняет запросы, если локальные msg_id «из будущего/прошлого».
Telethon хранит смещение в sender._state.time_offset; после connect делаем
round-trip, чтобы подтянуть offset с DC.
"""
from __future__ import annotations

import logging
from typing import Callable

from telethon import TelegramClient
from telethon.tl.functions.help import GetConfigRequest, GetNearestDcRequest

log = logging.getLogger("vacancybot.tg_time")

WarnFn = Callable[[str], None]


def get_time_offset(client: TelegramClient) -> float | None:
    sender = getattr(client, "_sender", None)
    state = getattr(sender, "_state", None) if sender else None
    offset = getattr(state, "time_offset", None)
    if offset is None:
        return None
    try:
        return float(offset)
    except (TypeError, ValueError):
        return None


async def sync_telegram_time(
    client: TelegramClient,
    *,
    label: str = "client",
    warn: WarnFn | None = None,
) -> float | None:
    """Force RPC round-trips so Telethon updates time_offset from Telegram."""
    _warn = warn or (lambda msg: log.info("%s", msg))
    before = get_time_offset(client)

    try:
        nearest = await client(GetNearestDcRequest())
        _warn(
            f"[{label}] nearest DC: this={getattr(nearest, 'this_dc', '?')} "
            f"nearest={getattr(nearest, 'nearest_dc', '?')} "
            f"country={getattr(nearest, 'country', '?')}"
        )
    except Exception as exc:  # noqa: BLE001
        _warn(f"[{label}] GetNearestDc failed: {type(exc).__name__}: {exc}")

    try:
        await client(GetConfigRequest())
    except Exception as exc:  # noqa: BLE001
        _warn(f"[{label}] GetConfig failed: {type(exc).__name__}: {exc}")

    after = get_time_offset(client)
    if after is None:
        _warn(f"[{label}] time_offset недоступен (Telethon не отдал state)")
        return None

    before_s = f"{before:.3f}s" if before is not None else "n/a"
    _warn(f"[{label}] Telegram time sync: offset {before_s} -> {after:.3f}s")

    if abs(after) >= 30:
        _warn(
            f"[{label}] ВНИМАНИЕ: часы ПК расходятся с Telegram на {after:.0f} сек. "
            "Включите автосинхронизацию времени Windows (Параметры → Время и язык)."
        )
    return after
