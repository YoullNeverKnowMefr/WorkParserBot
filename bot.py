"""Vacancy / event moderation bot — no user account required.

Architecture:
  * Scrapes public Telegram channels via https://t.me/s/<username>
  * BotFather bot handles moderation keyboards, admin panel, and publishing

Flow:
  1. Poller finds a keyword-matching post on a public channel preview.
  2. Bot posts a copy into MODERATION_GROUP with a category keyboard.
  3. Moderator sets optional publish time, then picks a subcategory.
  4. Bot publishes (scheduled) into the target channel from categories.json.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from io import BytesIO

import httpx
from telethon import Button, TelegramClient, events
from telethon.errors import MediaCaptionTooLongError

import config
from scraper import ScrapedPost, fetch_channel_posts, make_http_client, source_usernames
from store import Store
from tg_time import sync_telegram_time

# --- logging: console + an in-memory ring buffer readable from the bot chat ----
LOG_BUFFER: deque[str] = deque(maxlen=300)


class _BufferHandler(logging.Handler):
    def emit(self, record):
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("vacancybot")
_buf = _BufferHandler()
_buf.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
log.addHandler(_buf)

bot = TelegramClient(config.BOT_SESSION, config.API_ID, config.API_HASH)
store = Store(config.DB_PATH)
http: httpx.AsyncClient | None = None

STATE = {"parsing": False}
STATS = {
    "matched": 0,
    "forwarded": 0,
    "published": 0,
    "skipped": 0,
    "errors": 0,
    "last": None,
    "started": datetime.now(),
}
PENDING: dict[int, dict] = {}  # uid -> {"action": ...}

KEYWORDS_PATH = str(config.BASE_DIR / "keywords.json")
SCHEDULE_PATH = str(config.BASE_DIR / "schedule.txt")

SCHEDULE = {"hh": 10, "mm": 0}
POST_TZ = timezone(timedelta(hours=config.TZ_OFFSET))

SOURCES = source_usernames(config.SOURCE_CHANNELS)


def is_admin(uid: int) -> bool:
    return (not config.ADMIN_IDS) or uid in config.ADMIN_IDS


# ===========================================================================
# Scheduled-post time
# ===========================================================================
def _parse_hhmm(s: str) -> tuple[int, int] | None:
    s = s.strip().replace(".", ":").replace("-", ":")
    if ":" not in s:
        return None
    hh, _, mm = s.partition(":")
    try:
        h, m = int(hh), int(mm)
    except ValueError:
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def load_schedule() -> None:
    parsed = None
    if os.path.exists(SCHEDULE_PATH):
        try:
            parsed = _parse_hhmm(open(SCHEDULE_PATH, encoding="utf-8").read())
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to read schedule.txt: %s", exc)
    if parsed is None:
        parsed = _parse_hhmm(config.SCHEDULE_TIME) or (10, 0)
    SCHEDULE["hh"], SCHEDULE["mm"] = parsed
    log.info(
        "Post schedule time: %02d:%02d %s (UTC%+d)",
        SCHEDULE["hh"], SCHEDULE["mm"], config.TZ_NAME, config.TZ_OFFSET,
    )


def save_schedule() -> None:
    try:
        with open(SCHEDULE_PATH, "w", encoding="utf-8") as f:
            f.write(f"{SCHEDULE['hh']:02d}:{SCHEDULE['mm']:02d}")
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to save schedule.txt: %s", exc)


def _next_schedule_dt(hh: int | None = None, mm: int | None = None) -> datetime:
    h = SCHEDULE["hh"] if hh is None else hh
    m = SCHEDULE["mm"] if mm is None else mm
    now = datetime.now(POST_TZ)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


# ===========================================================================
# Keywords
# ===========================================================================
def load_keywords() -> None:
    if os.path.exists(KEYWORDS_PATH):
        try:
            data = json.loads(open(KEYWORDS_PATH, encoding="utf-8").read())
            kws = [str(k).strip().lower() for k in data if str(k).strip()]
            if kws:
                config.KEYWORDS = kws
                log.info("Loaded %d keywords from keywords.json", len(kws))
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to load keywords.json: %s", exc)


def save_keywords() -> None:
    try:
        with open(KEYWORDS_PATH, "w", encoding="utf-8") as f:
            json.dump(config.KEYWORDS, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to save keywords.json: %s", exc)


def _parse_kw_tokens(s: str) -> list[str]:
    return [t.strip().lower() for t in re.split(r"[,\n]+", s) if t.strip()]


# ===========================================================================
# Vacancy detection / text cleanup
# ===========================================================================
def is_vacancy(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(kw in low for kw in config.KEYWORDS)


_HASHTAG_RE = re.compile(r"(^|\s)#\w+", re.UNICODE | re.MULTILINE)


def strip_hashtags(text: str) -> str:
    text = _HASHTAG_RE.sub(r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+(\n|$)", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_BULLET_RE = re.compile(r"^\s*[—–\-•*●▪‣·◦▸►]\s*")
_TME_RE = re.compile(r"(https?://)?t\.me/\S+", re.IGNORECASE)


def _is_section_header(line: str) -> bool:
    s = line.lstrip()
    return bool(s) and ord(s[0]) >= 0x2190


def format_post(text: str) -> str:
    """Reformat a vacancy into a consistent look and return HTML."""
    text = strip_hashtags(text)
    lines = [ln.rstrip() for ln in text.splitlines()]

    while lines and (not lines[-1].strip() or _TME_RE.search(lines[-1])):
        lines.pop()

    out: list[str] = []
    for ln in lines:
        if _BULLET_RE.match(ln):
            ln = "— " + _BULLET_RE.sub("", ln).strip()
        if _is_section_header(ln) and out and out[-1].strip():
            out.append("")
        out.append(ln)

    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    if not body:
        return ""

    parts = body.split("\n", 1)
    title = parts[0].strip()
    rest = parts[1].lstrip("\n") if len(parts) > 1 else ""
    return f"<b>{title}</b>\n\n{rest}".rstrip() if rest else f"<b>{title}</b>"


# ===========================================================================
# Control panel UI
# ===========================================================================
def status_text() -> str:
    lines = [
        "🤖 <b>Панель управления</b>",
        "",
        "Режим: 🌐 публичный парсинг <code>t.me/s/…</code> (без аккаунта)",
        f"Парсинг: {'🟢 включён' if STATE['parsing'] else '🔴 выключен'}",
        f"Интервал опроса: {config.POLL_INTERVAL}с",
        f"Время постинга: 🕒 {SCHEDULE['hh']:02d}:{SCHEDULE['mm']:02d} {config.TZ_NAME}",
        f"В очереди на публикацию: {store.pending_publish_count()}",
        f"Источников: {len(SOURCES)} · "
        f"категорий: {len(config.CATEGORIES)} · ключевых слов: {len(config.KEYWORDS)}",
    ]
    if not config.ADMIN_IDS:
        lines += ["", "⚠️ ADMIN_IDS не задан — управлять может любой. Задайте его в .env."]
    return "\n".join(lines)


def stats_text() -> str:
    s = STATS
    last = s["last"].strftime("%d.%m %H:%M:%S") if s["last"] else "—"
    up = str(datetime.now() - s["started"]).split(".")[0]
    return (
        "📊 <b>Статистика парсинга</b>\n\n"
        f"Найдено по словам: <b>{s['matched']}</b>\n"
        f"Отправлено в модерацию: <b>{s['forwarded']}</b>\n"
        f"Опубликовано: <b>{s['published']}</b>\n"
        f"Пропущено: <b>{s['skipped']}</b>\n"
        f"Ошибок: <b>{s['errors']}</b>\n"
        f"Последнее совпадение: {last}\n"
        f"Аптайм: {up}"
    )


def logs_text(n: int = 25) -> str:
    lines = list(LOG_BUFFER)[-n:]
    if not lines:
        return "📋 Логи пусты."
    body = html.escape("\n".join(lines))
    if len(body) > 3500:
        body = body[-3500:]
    return f"📋 <b>Последние логи:</b>\n<pre>{body}</pre>"


def keywords_prompt() -> str:
    cur = html.escape(", ".join(config.KEYWORDS))
    return (
        "🏷 <b>Ключевые слова парсинга</b>\n"
        "По ним сообщения из каналов распознаются как вакансии.\n\n"
        f"Текущие ({len(config.KEYWORDS)}):\n<code>{cur}</code>\n\n"
        "Отправьте:\n"
        "• список через запятую — <b>заменить</b> целиком\n"
        "• <code>+ слово, фраза</code> — <b>добавить</b>\n"
        "• <code>- слово</code> — <b>удалить</b>\n\n"
        "/cancel — отмена"
    )


def control_menu():
    if STATE["parsing"]:
        parse_btn = Button.inline("⏸ Остановить парсинг", b"ctl:stop")
    else:
        parse_btn = Button.inline("▶️ Запустить парсинг", b"ctl:start")
    rows = [
        [parse_btn],
        [Button.inline("📊 Статистика", b"ctl:stats"), Button.inline("📋 Логи", b"ctl:logs")],
        [Button.inline("🏷 Ключевые слова", b"ctl:keywords")],
        [Button.inline(
            f"🕒 Время постинга ({SCHEDULE['hh']:02d}:{SCHEDULE['mm']:02d})",
            b"ctl:schedule",
        )],
        [Button.inline("ℹ️ Обновить", b"ctl:status")],
    ]
    return status_text(), rows


def _grid(buttons, per_row=2):
    rows, row = [], []
    for b in buttons:
        row.append(b)
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


_NAV_TITLES = {
    "root": "🗂 Выберите категорию:",
    "cat": "🌿 {} — выберите ответвление:",
    "branch": "📢 {} — выберите канал:",
    "chan": "🏷 {} — выберите тег (вид поста):",
}


def _path_data(path: list[int]) -> str:
    return "p" if not path else "p:" + ":".join(map(str, path))


def _node_children(path: list[int]):
    if not path:
        return "root", None, config.CATEGORIES
    cat = config.CATEGORIES[path[0]]
    if len(path) == 1:
        return "cat", cat["label"], cat["branches"]
    branch = cat["branches"][path[1]]
    if len(path) == 2:
        return "branch", branch["label"], branch["channels"]
    chan = branch["channels"][path[2]]
    if len(path) == 3:
        return "chan", chan["label"], chan["subcategories"]
    leaf = chan["subcategories"][path[3]]
    return "leaf", leaf["label"], None


def _time_button_label(schedule_hh: int | None = None, schedule_mm: int | None = None) -> str:
    if schedule_hh is not None and schedule_mm is not None:
        return f"🕒 {schedule_hh:02d}:{schedule_mm:02d}"
    return f"🕒 Время ({SCHEDULE['hh']:02d}:{SCHEDULE['mm']:02d})"


def _time_data(path: list[int]) -> bytes:
    return b"time" if not path else ("time:" + ":".join(map(str, path))).encode()


def _time_hour_keyboard() -> list:
    hours = [Button.inline(f"{h:02d}", data=f"th:{h}".encode()) for h in range(24)]
    rows = _grid(hours, per_row=6)
    rows.append([Button.inline("❌ Отмена", data=b"timecancel")])
    return rows


def _time_minute_keyboard(hour: int) -> list:
    mins = list(range(0, 60, 5))
    btns = [Button.inline(f"{hour:02d}:{m:02d}", data=f"tm:{hour}:{m}".encode()) for m in mins]
    rows = _grid(btns, per_row=4)
    rows.append([
        Button.inline("⬅️ Час", data=b"thback"),
        Button.inline("❌ Отмена", data=b"timecancel"),
    ])
    return rows


def nav_keyboard(path: list[int], schedule_hh: int | None = None, schedule_mm: int | None = None):
    kind, label, children = _node_children(path)
    tmpl = _NAV_TITLES[kind]
    title = tmpl.format(label) if "{}" in tmpl else tmpl
    btns = [
        Button.inline(c["label"], data=_path_data(path + [i]).encode())
        for i, c in enumerate(children)
    ]
    rows = _grid(btns)
    rows.append([Button.inline(
        _time_button_label(schedule_hh, schedule_mm), data=_time_data(path),
    )])
    nav = []
    if path:
        nav.append(Button.inline("⬅️ Назад", data=_path_data(path[:-1]).encode()))
    nav.append(Button.inline("❌ Пропустить", data=b"skip"))
    rows.append(nav)
    return title, rows


# ===========================================================================
# Scraper poller
# ===========================================================================
async def _send_to_moderation(post: ScrapedPost) -> None:
    header = (
        f"📥 <b>@{html.escape(post.username)}</b> · "
        f"<a href=\"{html.escape(post.link, quote=True)}\">оригинал</a>\n\n"
    )
    body = post.text_html or html.escape(post.text_plain) or "<i>(без текста)</i>"
    preview = header + body
    if len(preview) > 4000:
        preview = preview[:3990] + "…"

    try:
        if post.media_urls and http is not None:
            raw = await _download_media(post.media_urls[0])
            if raw is not None:
                preview_msg = await bot.send_file(
                    config.MODERATION_GROUP,
                    file=raw,
                    caption=preview,
                )
            else:
                preview_msg = await bot.send_message(config.MODERATION_GROUP, preview)
        else:
            preview_msg = await bot.send_message(config.MODERATION_GROUP, preview)
    except Exception as exc:  # noqa: BLE001
        STATS["errors"] += 1
        log.error("Failed to send preview to moderation: %s", exc)
        return

    try:
        title, kb = nav_keyboard([])
        kb_msg = await bot.send_message(
            config.MODERATION_GROUP,
            title,
            buttons=kb,
            reply_to=preview_msg.id,
        )
    except Exception as exc:  # noqa: BLE001
        STATS["errors"] += 1
        log.error("Failed to send keyboard: %s", exc)
        return

    STATS["forwarded"] += 1
    store.add(
        kb_msg.id,
        post.username,
        post.msg_id,
        preview_msg.id,
        post.text_html or html.escape(post.text_plain),
        post.media_urls,
        post.link,
    )
    log.info("Queued for moderation %s (preview %s, kb %s)", post.key, preview_msg.id, kb_msg.id)


async def _download_media(url: str) -> BytesIO | None:
    assert http is not None
    try:
        resp = await http.get(url)
        resp.raise_for_status()
        data = BytesIO(resp.content)
        # Telethon needs a name to infer type.
        suffix = ".jpg"
        ctype = (resp.headers.get("content-type") or "").lower()
        if "png" in ctype:
            suffix = ".png"
        elif "webp" in ctype:
            suffix = ".webp"
        elif "gif" in ctype:
            suffix = ".gif"
        elif "mp4" in ctype or "video" in ctype:
            suffix = ".mp4"
        data.name = f"media{suffix}"
        return data
    except Exception as exc:  # noqa: BLE001
        log.warning("Media download failed (%s): %s", url[:80], exc)
        return None


async def process_post(post: ScrapedPost) -> None:
    if store.is_seen(post.username, post.msg_id):
        return
    store.mark_seen(post.username, post.msg_id)

    if not STATE["parsing"]:
        return
    if not is_vacancy(post.text_plain or post.text_html):
        return

    STATS["matched"] += 1
    STATS["last"] = datetime.now()
    log.info("Match in @%s (msg %s)", post.username, post.msg_id)
    await _send_to_moderation(post)


async def poll_once() -> None:
    if http is None:
        return
    for username in SOURCES:
        try:
            posts = await fetch_channel_posts(http, username)
        except Exception as exc:  # noqa: BLE001
            STATS["errors"] += 1
            log.error("Poll @%s failed: %s", username, exc)
            continue

        if not posts:
            continue

        if config.SEED_SEEN_ON_START and not store.has_seen_any(username):
            store.mark_seen_many(username, [p.msg_id for p in posts])
            log.info("Seeded %d seen posts for @%s (no flood on first run)", len(posts), username)
            continue

        for post in posts:
            try:
                await process_post(post)
            except Exception as exc:  # noqa: BLE001
                STATS["errors"] += 1
                log.error("Process %s failed: %s", post.key, exc)


async def poll_loop() -> None:
    log.info(
        "Poller started: %d sources, interval %ss",
        len(SOURCES), config.POLL_INTERVAL,
    )
    # Stagger first poll slightly so bot.start settles.
    await asyncio.sleep(2)
    while True:
        try:
            await poll_once()
        except Exception as exc:  # noqa: BLE001
            STATS["errors"] += 1
            log.error("Poll loop error: %s", exc)
        await asyncio.sleep(config.POLL_INTERVAL)


# ===========================================================================
# Bot: moderation category buttons
# ===========================================================================
@bot.on(events.CallbackQuery(pattern=rb"(timecancel|thback|th:|tm:|time|skip|p)"))
async def on_category(event: events.CallbackQuery.Event):
    if config.MODERATOR_IDS and event.sender_id not in config.MODERATOR_IDS:
        await event.answer("Нет прав для модерации.", alert=True)
        return

    data = event.data.decode()
    uid = event.sender_id

    if data == "timecancel":
        cancelled = await _cancel_post_schedule(uid)
        if not cancelled and store.get(event.message_id):
            await _restore_moderation_keyboard(event.message_id, [])
        await event.answer("Отменено.")
        return

    if data == "thback":
        pending = PENDING.get(uid)
        if not pending or pending.get("action") != "post_schedule":
            await event.answer("Сначала нажмите «Время».", alert=True)
            return
        pending.pop("hh", None)
        try:
            await event.edit("🕒 Выберите час публикации:", buttons=_time_hour_keyboard())
        except Exception:  # noqa: BLE001
            pass
        await event.answer()
        return

    if data.startswith("th:"):
        pending = PENDING.get(uid)
        if not pending or pending.get("action") != "post_schedule":
            await event.answer("Сначала нажмите «Время».", alert=True)
            return
        try:
            hour = int(data.split(":", 1)[1])
        except ValueError:
            await event.answer("Некорректный час.", alert=True)
            return
        if not 0 <= hour <= 23:
            await event.answer("Некорректный час.", alert=True)
            return
        pending["hh"] = hour
        try:
            await event.edit(
                f"🕒 Час <b>{hour:02d}</b> — выберите минуты:",
                buttons=_time_minute_keyboard(hour),
            )
        except Exception:  # noqa: BLE001
            pass
        await event.answer()
        return

    if data.startswith("tm:"):
        pending = PENDING.get(uid)
        if not pending or pending.get("action") != "post_schedule":
            await event.answer("Сначала нажмите «Время».", alert=True)
            return
        parts = data.split(":")
        if len(parts) != 3:
            await event.answer("Некорректное время.", alert=True)
            return
        try:
            hh, mm = int(parts[1]), int(parts[2])
        except ValueError:
            await event.answer("Некорректное время.", alert=True)
            return
        ok = await _apply_post_schedule(uid, hh, mm)
        if not ok:
            await event.answer("Заявка уже обработана.", alert=True)
            return
        await event.answer(f"Время: {hh:02d}:{mm:02d}")
        return

    mapping = store.get(event.message_id)
    if not mapping:
        await event.answer("Эта заявка уже обработана.", alert=True)
        return
    source = mapping["source"]
    source_msg_id = mapping["source_msg"]
    preview_id = mapping["preview_msg_id"]
    schedule_hh = mapping["schedule_hh"]
    schedule_mm = mapping["schedule_mm"]

    if data == "skip":
        PENDING.pop(uid, None)
        store.remove(event.message_id)
        STATS["skipped"] += 1
        await _cleanup(preview_id, event.message_id)
        await event.answer("Пропущено.")
        log.info("Skipped candidate %s/%s", source, source_msg_id)
        return

    if data == "time" or data.startswith("time:"):
        path = [int(x) for x in data.split(":")[1:]] if ":" in data else []
        PENDING[uid] = {
            "action": "post_schedule",
            "kb_msg_id": event.message_id,
            "path": path,
        }
        try:
            await event.edit(
                "🕒 Выберите час публикации:",
                buttons=_time_hour_keyboard(),
            )
        except Exception:  # noqa: BLE001
            pass
        await event.answer()
        return

    parts = data.split(":")
    path = [int(x) for x in parts[1:]] if len(parts) > 1 else []
    kind, _, _ = _node_children(path)

    if kind != "leaf":
        title, kb = nav_keyboard(path, schedule_hh, schedule_mm)
        try:
            await event.edit(title, buttons=kb)
        except Exception:  # noqa: BLE001
            pass
        await event.answer()
        return

    chan = config.CATEGORIES[path[0]]["branches"][path[1]]["channels"][path[2]]
    leaf = chan["subcategories"][path[3]]
    if not chan["channel_id"]:
        await event.answer(f"У канала «{chan['label']}» не задан channel в конфиге.", alert=True)
        return
    target = {
        "label": leaf["label"],
        "tags": leaf["tags"],
        "links": leaf.get("links", []),
        "image": leaf["image"],
        "channel_id": chan["channel_id"],
        "copy_media": chan["copy_media"],
    }

    published = await _publish(
        mapping,
        target,
        schedule_hh=schedule_hh,
        schedule_mm=schedule_mm,
    )
    if not published:
        STATS["errors"] += 1
        await event.answer("Ошибка публикации, см. логи.", alert=True)
        return

    PENDING.pop(uid, None)
    store.remove(event.message_id)
    STATS["published"] += 1
    when = _next_schedule_dt(schedule_hh, schedule_mm)
    await event.answer(
        f"Опубликовано: {chan['label']} / {leaf['label']} "
        f"({when.strftime('%H:%M')})"
    )
    if config.DELETE_AFTER_PUBLISH:
        await _cleanup(preview_id, event.message_id)
    log.info(
        "Published %s/%s -> channel %s as '%s' at %s",
        source, source_msg_id, chan["channel_id"], leaf["label"],
        when.strftime("%Y-%m-%d %H:%M"),
    )


async def _restore_moderation_keyboard(kb_msg_id: int, path: list[int]) -> None:
    mapping = store.get(kb_msg_id)
    if not mapping:
        return
    title, kb = nav_keyboard(path, mapping["schedule_hh"], mapping["schedule_mm"])
    try:
        await bot.edit_message(config.MODERATION_GROUP, kb_msg_id, title, buttons=kb)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not restore moderation keyboard: %s", exc)


async def _cancel_post_schedule(uid: int) -> bool:
    pending = PENDING.pop(uid, None)
    if not pending or pending.get("action") != "post_schedule":
        return False
    kb_msg_id = pending.get("kb_msg_id")
    path = pending.get("path") or []
    if kb_msg_id:
        await _restore_moderation_keyboard(kb_msg_id, path)
    return True


async def _apply_post_schedule(uid: int, hh: int, mm: int) -> bool:
    pending = PENDING.get(uid) or {}
    if pending.get("action") != "post_schedule":
        return False
    kb_msg_id = pending.get("kb_msg_id")
    path = pending.get("path") or []
    if not kb_msg_id or not store.get(kb_msg_id):
        PENDING.pop(uid, None)
        return False
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return False
    store.set_schedule(kb_msg_id, hh, mm)
    PENDING.pop(uid, None)
    await _restore_moderation_keyboard(kb_msg_id, path)
    log.info("Post %s schedule set to %02d:%02d by %s", kb_msg_id, hh, mm, uid)
    return True


# ===========================================================================
# Bot: control panel
# ===========================================================================
@bot.on(events.CallbackQuery(pattern=b"ctl:"))
async def on_control(event: events.CallbackQuery.Event):
    uid = event.sender_id
    if not is_admin(uid):
        await event.answer("Нет доступа.", alert=True)
        return
    action = event.data.decode().split(":", 1)[1]

    if action == "start":
        if not SOURCES:
            await event.answer("Нет публичных источников (@username) в SOURCE_CHANNELS.", alert=True)
            return
        STATE["parsing"] = True
        await event.answer("Парсинг запущен.")
    elif action == "stop":
        STATE["parsing"] = False
        await event.answer("Парсинг остановлен.")
    elif action == "status":
        await event.answer()
    elif action == "stats":
        await event.answer()
        await event.respond(stats_text())
        return
    elif action == "logs":
        await event.answer()
        await event.respond(logs_text())
        return
    elif action == "keywords":
        PENDING[uid] = {"action": "keywords"}
        await event.answer()
        await event.respond(keywords_prompt())
        return
    elif action == "schedule":
        PENDING[uid] = {"action": "schedule"}
        await event.answer()
        await event.respond(
            f"🕒 <b>Время отложенной публикации</b>\n"
            f"Сейчас: <b>{SCHEDULE['hh']:02d}:{SCHEDULE['mm']:02d}</b> ({config.TZ_NAME}).\n\n"
            "Отправьте новое время в формате <code>ЧЧ:ММ</code> (например <code>10:00</code>).\n\n"
            "/cancel — отмена"
        )
        return

    text, rows = control_menu()
    try:
        await event.edit(text, buttons=rows)
    except Exception:  # noqa: BLE001
        pass


@bot.on(events.NewMessage(func=lambda e: e.is_private))
async def on_private(event: events.NewMessage.Event):
    uid = event.sender_id
    if not is_admin(uid):
        await event.respond(f"⛔ Нет доступа к управлению. Ваш ID: <code>{uid}</code>")
        return

    text = (event.raw_text or "").strip()

    if text == "/cancel":
        PENDING.pop(uid, None)
        _, rows = control_menu()
        await event.respond("Отменено.", buttons=rows)
        return

    if uid in PENDING and not text.startswith("/"):
        await handle_pending(event, uid, text)
        return

    menu_text, rows = control_menu()
    await event.respond(menu_text, buttons=rows)


async def handle_pending(event, uid: int, text: str):
    action = PENDING[uid].get("action")
    if action == "keywords":
        await apply_keywords_edit(event, uid, text)
    elif action == "schedule":
        await apply_schedule_edit(event, uid, text)


async def apply_schedule_edit(event, uid: int, text: str):
    parsed = _parse_hhmm(text)
    if parsed is None:
        await event.respond("Неверный формат. Введите время как <code>ЧЧ:ММ</code>, напр. 10:00.")
        return
    SCHEDULE["hh"], SCHEDULE["mm"] = parsed
    save_schedule()
    PENDING.pop(uid, None)
    _, rows = control_menu()
    await event.respond(
        f"✅ Время постинга установлено: <b>{SCHEDULE['hh']:02d}:{SCHEDULE['mm']:02d}</b>. "
        "Новые посты будут ставиться в отложку на это время.",
        buttons=rows,
    )
    log.info("Schedule time set to %02d:%02d by %s", SCHEDULE["hh"], SCHEDULE["mm"], uid)


async def apply_keywords_edit(event, uid: int, text: str):
    raw = text.strip()
    if raw[:1] == "+":
        toks = _parse_kw_tokens(raw[1:])
        for t in toks:
            if t not in config.KEYWORDS:
                config.KEYWORDS.append(t)
        msg = f"Добавлено: {', '.join(toks)}" if toks else "Нечего добавлять."
    elif raw[:1] == "-":
        toks = _parse_kw_tokens(raw[1:])
        config.KEYWORDS = [k for k in config.KEYWORDS if k not in toks]
        msg = f"Удалено: {', '.join(toks)}" if toks else "Нечего удалять."
    else:
        toks = _parse_kw_tokens(raw)
        if not toks:
            await event.respond("Пустой список — изменения отменены.")
            PENDING.pop(uid, None)
            return
        config.KEYWORDS = toks
        msg = "Список ключевых слов заменён."

    save_keywords()
    PENDING.pop(uid, None)
    cur = html.escape(", ".join(config.KEYWORDS))
    _, rows = control_menu()
    await event.respond(
        f"✅ {msg}\n\nТекущие ({len(config.KEYWORDS)}):\n<code>{cur}</code>",
        buttons=rows,
    )
    log.info("Keywords updated by %s -> %d words", uid, len(config.KEYWORDS))


# ===========================================================================
# Publishing / cleanup
# ===========================================================================
_MESSAGE_LIMIT = 4096


async def _send_post(target, media: list, text_html: str) -> None:
    """Send immediately. Bots cannot use Telegram's native schedule=."""
    if not media:
        await bot.send_message(target, text_html)
        return

    file = media if len(media) > 1 else media[0]
    try:
        await bot.send_file(target, file=file, caption=text_html)
    except MediaCaptionTooLongError:
        await bot.send_file(target, file=file)
        await bot.send_message(target, text_html)


async def _resolve_media(media_path: str | None, media_url: str | None) -> list:
    if media_path and os.path.exists(media_path):
        return [media_path]
    if media_url:
        raw = await _download_media(media_url)
        if raw is not None:
            return [raw]
    return []


async def _publish(
    mapping: dict,
    target: dict,
    schedule_hh: int | None = None,
    schedule_mm: int | None = None,
) -> bool:
    """Queue a post for local delayed send (bots cannot schedule via Telegram API)."""
    channel = target["channel_id"]
    raw_html = mapping.get("text_html") or ""

    if config.AUTO_FORMAT:
        body = format_post(raw_html)
    else:
        body = strip_hashtags(raw_html)

    footer_parts = []
    tag_line = " ".join(target["tags"])
    if tag_line:
        footer_parts.append(tag_line)
    links_line = " ".join(
        f'<a href="{html.escape(l["url"], quote=True)}">{html.escape(l["text"])}</a>'
        for l in target.get("links", [])
    )
    if links_line:
        footer_parts.append(links_line)
    footer = "\n".join(footer_parts)
    final_text = f"{body}\n\n{footer}".strip() if footer else body

    media_path: str | None = None
    media_url: str | None = None
    if target.get("copy_media"):
        urls = mapping.get("media_urls") or []
        media_url = urls[0] if urls else None
    else:
        cat_image = target["image"]
        if cat_image and os.path.exists(cat_image):
            media_path = cat_image
        elif cat_image:
            log.warning("Category image not found: %s (posting without it)", cat_image)
        if not media_path:
            urls = mapping.get("media_urls") or []
            media_url = urls[0] if urls else None

    when = _next_schedule_dt(schedule_hh, schedule_mm)
    # Store as UTC ISO so comparisons are timezone-safe.
    due_at = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        job_id = store.enqueue_publish(
            channel=channel,
            text_html=final_text,
            due_at_iso=due_at,
            media_path=media_path,
            media_url=media_url,
        )
        log.info(
            "Queued publish #%s -> %s at %s %s",
            job_id, channel, when.strftime("%Y-%m-%d %H:%M"), config.TZ_NAME,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to queue publish to channel %s: %s", channel, exc)
        return False


async def _flush_due_publishes() -> None:
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    jobs = store.due_publishes(now_iso)
    for job in jobs:
        try:
            media = await _resolve_media(job["media_path"], job["media_url"])
            text = job["text_html"]
            channel = job["channel"]
            try:
                target = int(channel)
            except ValueError:
                target = channel
            if len(text) > _MESSAGE_LIMIT and not media:
                text = text[: _MESSAGE_LIMIT - 1] + "…"
            await _send_post(target, media, text)
            store.remove_publish(job["id"])
            log.info("Published queued job #%s -> %s", job["id"], channel)
        except Exception as exc:  # noqa: BLE001
            STATS["errors"] += 1
            log.error("Queued publish #%s failed: %s", job["id"], exc)
            _bump_publish_retry(job["id"])


def _bump_publish_retry(job_id: int) -> None:
    """Push failed job 2 minutes forward so we don't spin."""
    retry = (datetime.now(timezone.utc) + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        store.bump_publish_due(job_id, retry)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not bump retry for job #%s: %s", job_id, exc)


async def publish_loop() -> None:
    log.info("Local publish scheduler started")
    await asyncio.sleep(3)
    while True:
        try:
            await _flush_due_publishes()
        except Exception as exc:  # noqa: BLE001
            STATS["errors"] += 1
            log.error("Publish loop error: %s", exc)
        await asyncio.sleep(20)


async def _cleanup(preview_id: int, kb_id: int) -> None:
    for mid in (preview_id, kb_id):
        try:
            await bot.delete_messages(config.MODERATION_GROUP, [mid])
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not delete message %s: %s", mid, exc)


# ===========================================================================
# Startup
# ===========================================================================
async def main():
    global http

    load_keywords()
    load_schedule()

    if not SOURCES:
        log.error(
            "No public @usernames in SOURCE_CHANNELS — nothing to scrape. "
            "Numeric channel ids cannot be scraped without a user account."
        )

    http = make_http_client()
    await bot.start(bot_token=config.BOT_TOKEN)
    bot.parse_mode = "html"
    await sync_telegram_time(bot, label="bot", warn=lambda m: log.info("%s", m))

    if config.PARSING_ON_START and SOURCES:
        STATE["parsing"] = True

    bot_me = await bot.get_me()
    log.info(
        "Bot @%s ready | parsing=%s | sources=%s | poll=%ss | queued=%s",
        bot_me.username, STATE["parsing"], len(SOURCES), config.POLL_INTERVAL,
        store.pending_publish_count(),
    )
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS is empty — anyone who opens the bot can control it.")

    try:
        await asyncio.gather(
            bot.run_until_disconnected(),
            poll_loop(),
            publish_loop(),
        )
    finally:
        await http.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        store.close()
