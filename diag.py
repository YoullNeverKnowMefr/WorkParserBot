"""Read-only diagnostic: why isn't the bot forwarding/posting?"""
import asyncio
from telethon import TelegramClient
from telethon.tl.types import Channel, Chat, User
import config
from bot import is_vacancy

user = TelegramClient(config.SESSION, config.API_ID, config.API_HASH)
bot = TelegramClient(config.BOT_SESSION, config.API_ID, config.API_HASH)


async def main():
    await user.start()
    await bot.start(bot_token=config.BOT_TOKEN)
    me = await user.get_me()
    bot_me = await bot.get_me()
    print(f"Observer: {me.first_name} id={me.id}")
    print(f"Bot: @{bot_me.username} id={bot_me.id}")

    # dialogs the observer is actually part of (needed to RECEIVE channel updates)
    dialog_ids = set()
    async for d in user.iter_dialogs():
        dialog_ids.add(d.id)
    print(f"\nObserver is in {len(dialog_ids)} dialogs total.\n")

    print("=== SOURCE CHANNELS ===")
    for ch in config.SOURCE_CHANNELS:
        try:
            ent = await user.get_entity(ch)
            joined = ent.id in dialog_ids or (-1000000000000 - ent.id) in dialog_ids
            # find last message + keyword check
            last = await user.get_messages(ent, limit=1)
            txt = (last[0].raw_text if last else "") or ""
            preview = txt[:60].replace("\n", " ")
            print(f"  @{ch}: id={ent.id} joined={'YES' if joined else 'NO (не подписан!)'} "
                  f"last_matches={is_vacancy(txt)} last='{preview}'")
        except Exception as e:
            print(f"  @{ch}: ОШИБКА доступа -> {e}")

    print("\n=== MODERATION GROUP ===")
    try:
        g = await user.get_entity(config.MODERATION_GROUP)
        print(f"  observer sees group: {getattr(g,'title',g)} id={g.id} in_dialogs={g.id in dialog_ids}")
    except Exception as e:
        print(f"  observer НЕ видит группу -> {e}")
    try:
        gb = await bot.get_entity(config.MODERATION_GROUP)
        print(f"  bot sees group: {getattr(gb,'title',gb)}  (бот в группе — OK)")
    except Exception as e:
        print(f"  BOT НЕ в группе / не видит -> {e}  (добавь бота в группу!)")

    print("\n=== TARGET CHANNEL ===")
    try:
        t = await user.get_entity(config.TARGET_CHANNEL)
        print(f"  observer sees target: {getattr(t,'title',t)} id={t.id} in_dialogs={t.id in dialog_ids}")
    except Exception as e:
        print(f"  observer НЕ видит целевой канал -> {e}")

    await user.disconnect()
    await bot.disconnect()


asyncio.run(main())
