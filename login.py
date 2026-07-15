"""One-time console login for the observer account.

Run this once:  python login.py
It asks for the phone number, the code from Telegram, and the 2FA password
(if enabled), then saves the session file. After that `python bot.py` starts
already authorized and won't crash.
"""
import asyncio
from telethon import TelegramClient
import config


async def main():
    client = TelegramClient(config.SESSION, config.API_ID, config.API_HASH)
    await client.start()  # interactive: prompts phone -> code -> 2FA in the console
    me = await client.get_me()
    print(f"\n✅ Авторизован: {me.first_name} (id={me.id}). Сессия сохранена: {config.SESSION}.session")
    print("Теперь запусти: python bot.py")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
