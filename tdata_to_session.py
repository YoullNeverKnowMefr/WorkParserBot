"""Конвертация Telegram Desktop tdata -> Telethon observer.session.

Читает MTP authorization напрямую (без map — он ломается на новых lskType).

Пример:
  py -3.12 tdata_to_session.py +79381108467
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "_tgcrypto_stub"))

from PyQt5.QtCore import QByteArray, QDataStream  # noqa: E402
from opentele.td import shared as td  # noqa: E402
from telethon import TelegramClient  # noqa: E402
from telethon.crypto import AuthKey  # noqa: E402
from telethon.sessions import SQLiteSession  # noqa: E402

import config  # noqa: E402

TDATA = ROOT / "tdata"
TARGET_PHONE = "".join(
    c for c in (sys.argv[1] if len(sys.argv) > 1 else "+79381108467") if c.isdigit()
)
OUT_BASE = ROOT / config.SESSION

DC_ADDR = {
    1: ("149.154.175.53", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("91.108.56.130", 443),
}


def compose_data_string(data_name: str, index: int) -> str:
    return data_name if index <= 0 else f"{data_name}#{index + 1}"


def load_local_key(tdata: Path, passcode: bytes = b""):
    key_data = td.Storage.ReadFile("key_data", str(tdata))
    salt, key_encrypted, info_encrypted = QByteArray(), QByteArray(), QByteArray()
    key_data.stream >> salt >> key_encrypted >> info_encrypted
    passcode_key = td.Storage.CreateLocalKey(salt, QByteArray(passcode))
    key_inner = td.Storage.DecryptLocal(key_encrypted, passcode_key)
    local_key = td.AuthKey(key_inner.stream.readRawData(256))

    info = td.Storage.DecryptLocal(info_encrypted, local_key)
    count = info.stream.readInt32()
    indices = [info.stream.readInt32() for _ in range(count)]
    print(f"tdata version={key_data.version} accounts={count} indices={indices}")
    return local_key, indices


def parse_mtp_auth(serialized: QByteArray) -> tuple[int, int, bytes]:
    stream = QDataStream(serialized)
    stream.setVersion(QDataStream.Version.Qt_5_1)

    user_id = stream.readInt32()
    main_dc = stream.readInt32()
    if ((user_id << 32) | main_dc) == int(~0):
        user_id = stream.readUInt64()
        main_dc = stream.readInt32()

    key_count = stream.readInt32()
    auth_key = None
    for _ in range(key_count):
        dc_id = stream.readInt32()
        key = bytes(stream.readRawData(256))
        if dc_id == main_dc:
            auth_key = key

    if not auth_key:
        raise RuntimeError(f"auth_key for dc={main_dc} not found")
    return int(user_id), int(main_dc), auth_key


def extract_account(tdata: Path, local_key: td.AuthKey, index: int):
    data_name = compose_data_string("data", index)
    part = td.Storage.ToFilePart(td.Storage.ComputeDataNameKey(data_name))
    mtp_path = tdata / f"{part}s"
    print(f"\n--- index {index} ({data_name}) part={part} exists={mtp_path.exists()} ---")
    if not mtp_path.exists():
        raise FileNotFoundError(mtp_path)

    mtp = td.Storage.ReadEncryptedFile(part, str(tdata), local_key)
    block_id = mtp.stream.readInt32()
    if block_id != 75:
        raise RuntimeError(f"unexpected mtp blockId={block_id} (want 75)")
    serialized = QByteArray()
    mtp.stream >> serialized
    return parse_mtp_auth(serialized)


def write_telethon_session(path_base: Path, dc_id: int, auth_key: bytes) -> None:
    for p in (Path(f"{path_base}.session"), Path(f"{path_base}.session-journal")):
        if p.exists():
            p.unlink()
    addr, port = DC_ADDR.get(dc_id, DC_ADDR[2])
    session = SQLiteSession(str(path_base))
    session.set_dc(dc_id, addr, port)
    session.auth_key = AuthKey(auth_key)
    session.save()
    session.close()


def norm_phone(p) -> str:
    return "".join(c for c in str(p or "") if c.isdigit())


async def verify_and_match(path_base: Path, expect_phone: str) -> tuple[bool, str]:
    client = TelegramClient(str(path_base), config.API_ID, config.API_HASH)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return False, "not authorized"
        me = await client.get_me()
        phone = norm_phone(me.phone)
        uname = f"@{me.username}" if me.username else "-"
        print(
            f"  online: id={me.id} phone=+{phone or '?'} "
            f"name={me.first_name} username={uname}"
        )
        matched = bool(phone) and (
            phone == expect_phone or phone.endswith(expect_phone[-10:])
        )
        return matched, phone or "?"
    finally:
        await client.disconnect()


async def main() -> None:
    print(f"tdata : {TDATA}")
    print(f"phone : +{TARGET_PHONE}")
    print(f"out   : {OUT_BASE}.session")
    print(f"api   : {config.API_ID}")

    local_key, indices = load_local_key(TDATA)
    seen: list[str] = []
    found = False

    for index in indices:
        try:
            user_id, dc_id, auth_key = extract_account(TDATA, local_key, index)
        except Exception as exc:  # noqa: BLE001
            print(f"  extract FAIL: {type(exc).__name__}: {exc}")
            seen.append(str(exc))
            continue

        print(f"  user_id={user_id} dc_id={dc_id} auth_key_len={len(auth_key)}")
        tmp = ROOT / f"_tmp_acc{index}"
        try:
            write_telethon_session(tmp, dc_id, auth_key)
            matched, phone = await verify_and_match(tmp, TARGET_PHONE)
            seen.append(phone)
            if matched:
                write_telethon_session(OUT_BASE, dc_id, auth_key)
                ok, phone2 = await verify_and_match(OUT_BASE, TARGET_PHONE)
                print(f"  MATCH -> {OUT_BASE}.session (verify={ok}, +{phone2})")
                found = True
                break
        finally:
            for p in (Path(f"{tmp}.session"), Path(f"{tmp}.session-journal")):
                if p.exists():
                    p.unlink()

    print()
    if found:
        print(f"Done: {OUT_BASE}.session")
    else:
        print(f"Account +{TARGET_PHONE} not found.")
        print(f"Seen: {seen}")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
