"""List groups/channels the observer is in, with correct ids."""
import asyncio
from telethon import TelegramClient
import config

user = TelegramClient(config.SESSION, config.API_ID, config.API_HASH)


async def main():
    await user.start()
    print(f"{'ID':>16}  kind        can_post  title")
    print("-" * 70)
    async for d in user.iter_dialogs():
        e = d.entity
        if d.is_user:
            continue
        kind = "group" if d.is_group else ("channel" if d.is_channel else "chat")
        post = ""
        if d.is_channel and not d.is_group:
            post = "post" if getattr(e, "creator", False) or getattr(e, "admin_rights", None) else "read"
        title = (d.title or "")[:38]
        print(f"{d.id:>16}  {kind:<10}  {post:<8}  {title}")
    await user.disconnect()


asyncio.run(main())
