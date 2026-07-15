"""Vacancy moderation userbot with an in-chat control panel.

Two clients run in one process:
  * user  - the observer account. Monitors SOURCE_CHANNELS and publishes to TARGET_CHANNEL.
  * bot   - a BotFather bot. Renders inline keyboards (category approval + control panel).

Control panel (private chat with the bot, admins only):
  * start / stop parsing
  * log the observer account in *through the chat* (phone -> code -> 2FA)
  * log the observer account out
  * view parsing statistics and recent logs
  * edit the parsing keywords

Moderation flow:
  1. user sees a keyword-matching post in a source channel.
  2. user forwards it into MODERATION_GROUP; bot replies with a category keyboard.
  3. A moderator optionally sets a publish time (per post), then taps a category.
  4. user publishes the vacancy to TARGET_CHANNEL (category image + tags, foreign
     hashtags stripped), then both moderation messages are deleted.
"""
import asyncio
import html
import json
import logging
import os
import re
from collections import deque
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient, events, Button
from telethon.tl.types import MessageMediaWebPage
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    MediaCaptionTooLongError,
)

import config
from store import Store

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

user = TelegramClient(config.SESSION, config.API_ID, config.API_HASH)
bot = TelegramClient(config.BOT_SESSION, config.API_ID, config.API_HASH)
store = Store(config.DB_PATH)

# Runtime state, toggled from the control panel.
STATE = {"parsing": False, "authorized": False}
STATS = {"matched": 0, "forwarded": 0, "published": 0, "skipped": 0, "errors": 0,
         "last": None, "started": datetime.now()}
# Per-admin conversation state.
LOGIN: dict[int, dict] = {}    # login flow: uid -> {"step","phone","hash"}
PENDING: dict[int, dict] = {}  # other input flows: uid -> {"action": ...}

KEYWORDS_PATH = str(config.BASE_DIR / "keywords.json")
SCHEDULE_PATH = str(config.BASE_DIR / "schedule.txt")

# Time (hour, minute) at which published posts are scheduled, in POST_TZ.
SCHEDULE = {"hh": 10, "mm": 0}
POST_TZ = timezone(timedelta(hours=config.TZ_OFFSET))  # e.g. UTC+3 = МСК


def is_admin(uid: int) -> bool:
    return (not config.ADMIN_IDS) or uid in config.ADMIN_IDS


# ===========================================================================
# Scheduled-post time (editable at runtime, persisted to schedule.txt)
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
    log.info("Post schedule time: %02d:%02d %s (UTC%+d)",
             SCHEDULE["hh"], SCHEDULE["mm"], config.TZ_NAME, config.TZ_OFFSET)


def save_schedule() -> None:
    try:
        with open(SCHEDULE_PATH, "w", encoding="utf-8") as f:
            f.write(f"{SCHEDULE['hh']:02d}:{SCHEDULE['mm']:02d}")
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to save schedule.txt: %s", exc)


def _next_schedule_dt(hh: int | None = None, mm: int | None = None) -> datetime:
    """Next occurrence of HH:MM in POST_TZ (today, else tomorrow).

    Uses per-post hh/mm when given, otherwise the global SCHEDULE.
    """
    h = SCHEDULE["hh"] if hh is None else hh
    m = SCHEDULE["mm"] if mm is None else mm
    now = datetime.now(POST_TZ)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


# ===========================================================================
# Keywords persistence (editable at runtime from the bot)
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
    # Comma/newline separated so multi-word phrases ("looking for") stay intact.
    return [t.strip().lower() for t in re.split(r"[,\n]+", s) if t.strip()]


# ===========================================================================
# Vacancy detection / text cleanup
# ===========================================================================
def is_vacancy(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(kw in low for kw in config.KEYWORDS)


# A hashtag preceded by start-of-line or whitespace (so we never touch a '#'
# inside a URL fragment). \w includes Cyrillic under re.UNICODE.
_HASHTAG_RE = re.compile(r"(^|\s)#\w+", re.UNICODE | re.MULTILINE)


def strip_hashtags(text: str) -> str:
    """Remove the source post's own hashtags, then tidy the leftover whitespace."""
    text = _HASHTAG_RE.sub(r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+(\n|$)", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# Any leading bullet marker -> unified "— ".
_BULLET_RE = re.compile(r"^\s*[—–\-•*●▪‣·◦▸►]\s*")
# A t.me link (used to drop the source channel's promo footer).
_TME_RE = re.compile(r"(https?://)?t\.me/\S+", re.IGNORECASE)


def _is_section_header(line: str) -> bool:
    """A line that starts with an emoji/pictograph (💎 Что предлагаем: ...)."""
    s = line.lstrip()
    return bool(s) and ord(s[0]) >= 0x2190  # arrows/symbols/emoji region; excludes '—' (0x2014)


def format_post(text: str) -> str:
    """Reformat a vacancy into a consistent look and return HTML.

    Input is the message's HTML (message.text), so inline links/formatting are
    preserved. Steps:
    - bold title (first non-empty line)
    - unify bullets to "— "
    - blank line before each emoji section header
    - strip the source hashtags and the trailing promo footer (keeps body links)
    """
    text = strip_hashtags(text)
    lines = [ln.rstrip() for ln in text.splitlines()]

    # Drop the trailing promo/footer block only: blank or t.me-link lines at the
    # very end. Links inside the body are kept.
    while lines and (not lines[-1].strip() or _TME_RE.search(lines[-1])):
        lines.pop()

    # Normalize bullets + ensure spacing before section headers.
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

    # Text is already valid HTML from Telethon — do NOT escape (would kill links).
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
        f"Аккаунт: {'✅ авторизован' if STATE['authorized'] else '❌ не авторизован'}",
        f"Парсинг: {'🟢 включён' if STATE['parsing'] else '🔴 выключен'}",
        f"Время постинга: 🕒 {SCHEDULE['hh']:02d}:{SCHEDULE['mm']:02d} {config.TZ_NAME}",
        f"Источников: {len(config.SOURCE_CHANNELS)} · "
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
        f"Переслано в модерацию: <b>{s['forwarded']}</b>\n"
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
    """Return (text, buttons) for the control panel, reflecting current STATE."""
    if STATE["parsing"]:
        parse_btn = Button.inline("⏸ Остановить парсинг", b"ctl:stop")
    else:
        parse_btn = Button.inline("▶️ Запустить парсинг", b"ctl:start")
    if STATE["authorized"]:
        acc_btn = Button.inline("🚪 Выйти из аккаунта", b"ctl:logout")
    else:
        acc_btn = Button.inline("🔑 Войти в аккаунт", b"ctl:login")
    rows = [
        [parse_btn],
        [acc_btn],
        [Button.inline("📊 Статистика", b"ctl:stats"), Button.inline("📋 Логи", b"ctl:logs")],
        [Button.inline("🏷 Ключевые слова", b"ctl:keywords")],
        [Button.inline(f"🕒 Время постинга ({SCHEDULE['hh']:02d}:{SCHEDULE['mm']:02d})",
                       b"ctl:schedule")],
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


# --- 4-level navigation: Category -> Branch -> Channel -> Subcategory(tags) ------
_NAV_TITLES = {
    "root":   "🗂 Выберите категорию:",
    "cat":    "🌿 {} — выберите ответвление:",
    "branch": "📢 {} — выберите канал:",
    "chan":   "🏷 {} — выберите тег (вид поста):",
}


def _path_data(path: list[int]) -> str:
    """Encode a navigation path as callback data ('p', 'p:0', 'p:0:1', ...)."""
    return "p" if not path else "p:" + ":".join(map(str, path))


def _node_children(path: list[int]):
    """Return (kind, label, children) for a path. kind: root/cat/branch/chan/leaf."""
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
    """Callback data for the per-post time button ('time' or 'time:0:1')."""
    return b"time" if not path else ("time:" + ":".join(map(str, path))).encode()


def _time_hour_keyboard() -> list:
    """Inline keyboard: pick hour 0..23, then cancel."""
    hours = [Button.inline(f"{h:02d}", data=f"th:{h}".encode()) for h in range(24)]
    rows = _grid(hours, per_row=6)
    rows.append([Button.inline("❌ Отмена", data=b"timecancel")])
    return rows


def _time_minute_keyboard(hour: int) -> list:
    """Inline keyboard: pick minutes for a chosen hour."""
    rows = []
    # 00..55 step 5 — covers 10:00, 15:00 and other common slots.
    mins = list(range(0, 60, 5))
    btns = [Button.inline(f"{hour:02d}:{m:02d}", data=f"tm:{hour}:{m}".encode())
            for m in mins]
    rows.extend(_grid(btns, per_row=4))
    rows.append([
        Button.inline("⬅️ Час", data=b"thback"),
        Button.inline("❌ Отмена", data=b"timecancel"),
    ])
    return rows


def nav_keyboard(path: list[int], schedule_hh: int | None = None, schedule_mm: int | None = None):
    """Build (title, buttons) listing the children at `path`."""
    kind, label, children = _node_children(path)
    tmpl = _NAV_TITLES[kind]
    title = tmpl.format(label) if "{}" in tmpl else tmpl
    btns = [Button.inline(c["label"], data=_path_data(path + [i]).encode())
            for i, c in enumerate(children)]
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
# Observer: watch the source channels
# ===========================================================================
@user.on(events.NewMessage(chats=config.SOURCE_CHANNELS))
async def on_source_message(event: events.NewMessage.Event):
    if not STATE["parsing"] or not STATE["authorized"]:
        return
    if not is_vacancy(event.raw_text):
        return

    STATS["matched"] += 1
    STATS["last"] = datetime.now()
    chat_name = getattr(event.chat, "title", event.chat_id)
    log.info("Match in '%s' (msg %s)", chat_name, event.message.id)

    try:
        forwarded = await event.message.forward_to(config.MODERATION_GROUP)
    except Exception as exc:  # noqa: BLE001
        STATS["errors"] += 1
        log.error("Failed to forward to moderation group: %s", exc)
        return
    fwd = forwarded[0] if isinstance(forwarded, list) else forwarded

    try:
        title, kb = nav_keyboard([])
        kb_msg = await bot.send_message(
            config.MODERATION_GROUP,
            title,
            buttons=kb,
            reply_to=fwd.id,
        )
    except Exception as exc:  # noqa: BLE001
        STATS["errors"] += 1
        log.error("Failed to send keyboard: %s", exc)
        return

    STATS["forwarded"] += 1
    store.add(kb_msg.id, event.chat_id, event.message.id, fwd.id)
    log.info("Forwarded to moderation (fwd %s, kb %s)", fwd.id, kb_msg.id)


# ===========================================================================
# Bot: moderation category buttons (in the moderation group)
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
    source_chat = mapping["source_chat"]
    source_msg_id = mapping["source_msg"]
    fwd_id = mapping["fwd_msg_id"]
    schedule_hh = mapping["schedule_hh"]
    schedule_mm = mapping["schedule_mm"]

    if data == "skip":
        PENDING.pop(uid, None)
        store.remove(event.message_id)
        STATS["skipped"] += 1
        await _cleanup(fwd_id, event.message_id)
        await event.answer("Пропущено.")
        log.info("Skipped candidate %s/%s", source_chat, source_msg_id)
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

    # Navigation path: 'p' -> root, 'p:0:1:2' -> category0/channel1/rubric2, etc.
    parts = data.split(":")
    path = [int(x) for x in parts[1:]] if len(parts) > 1 else []
    kind, _, _ = _node_children(path)

    if kind != "leaf":
        # Still navigating — redraw the menu for this level.
        title, kb = nav_keyboard(path, schedule_hh, schedule_mm)
        try:
            await event.edit(title, buttons=kb)
        except Exception:  # noqa: BLE001 - unchanged message
            pass
        await event.answer()
        return

    # Leaf selected -> resolve channel (+ copy_media) and the subcategory target.
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
        source_chat, source_msg_id, target,
        schedule_hh=schedule_hh, schedule_mm=schedule_mm,
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
        await _cleanup(fwd_id, event.message_id)
    log.info("Published %s/%s -> channel %s as '%s' at %s",
             source_chat, source_msg_id, chan["channel_id"], leaf["label"],
             when.strftime("%Y-%m-%d %H:%M"))


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
    """Clear pending time input and restore the category keyboard. Returns True if cancelled."""
    pending = PENDING.pop(uid, None)
    if not pending or pending.get("action") != "post_schedule":
        return False
    kb_msg_id = pending.get("kb_msg_id")
    path = pending.get("path") or []
    if kb_msg_id:
        await _restore_moderation_keyboard(kb_msg_id, path)
    return True


async def _apply_post_schedule(uid: int, hh: int, mm: int) -> bool:
    """Persist per-post time and redraw the category keyboard."""
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
# Bot: control panel buttons (private chat, admins only)
# ===========================================================================
@bot.on(events.CallbackQuery(pattern=b"ctl:"))
async def on_control(event: events.CallbackQuery.Event):
    uid = event.sender_id
    if not is_admin(uid):
        await event.answer("Нет доступа.", alert=True)
        return
    action = event.data.decode().split(":", 1)[1]

    if action == "start":
        if not STATE["authorized"]:
            await event.answer("Сначала войдите в аккаунт.", alert=True)
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
    elif action == "login":
        if STATE["authorized"]:
            await event.answer("Аккаунт уже авторизован.", alert=True)
            return
        LOGIN[uid] = {"step": "phone"}
        await event.answer()
        await event.respond(
            "🔑 Отправьте номер телефона аккаунта-наблюдателя в формате "
            "<code>+79991234567</code>.\n\n/cancel — отмена"
        )
        return
    elif action == "logout":
        if not STATE["authorized"]:
            await event.answer("Аккаунт не авторизован.", alert=True)
            return
        await do_logout()
        await event.answer("Вы вышли из аккаунта.")

    text, rows = control_menu()
    try:
        await event.edit(text, buttons=rows)
    except Exception:  # noqa: BLE001 - message unchanged, ignore
        pass


# ===========================================================================
# Bot: private messages (menu + conversations)
# ===========================================================================
@bot.on(events.NewMessage(func=lambda e: e.is_private))
async def on_private(event: events.NewMessage.Event):
    uid = event.sender_id
    if not is_admin(uid):
        await event.respond(f"⛔ Нет доступа к управлению. Ваш ID: <code>{uid}</code>")
        return

    text = (event.raw_text or "").strip()

    if text == "/cancel":
        LOGIN.pop(uid, None)
        PENDING.pop(uid, None)
        _, rows = control_menu()
        await event.respond("Отменено.", buttons=rows)
        return

    if uid in LOGIN and not text.startswith("/"):
        await handle_login_step(event, uid, text)
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


async def handle_login_step(event, uid: int, text: str):
    st = LOGIN[uid]
    step = st["step"]

    if step == "phone":
        phone = text.replace(" ", "").strip()
        if not phone.startswith("+"):
            await event.respond(
                "Номер должен быть в международном формате, например "
                "<code>+79991234567</code>. Попробуйте ещё раз:"
            )
            return
        st["phone"] = phone
        try:
            if not user.is_connected():
                await user.connect()
            sent = await user.send_code_request(phone)
            code_hash = getattr(sent, "phone_code_hash", None)
            if not code_hash:
                LOGIN.pop(uid, None)
                await event.respond(
                    "Telegram не вернул phone_code_hash. Попробуйте снова или "
                    "войдите через консоль: <code>python login.py</code>"
                )
                return
            st["hash"] = code_hash
            st["step"] = "code"
            await event.respond(
                "💬 Код отправлен. Введите его, <b>разделяя цифры пробелами</b> "
                "(например <code>1 2 3 4 5</code>) — так Telegram не сбросит код.\n\n"
                "/cancel — отмена"
            )
        except Exception as exc:  # noqa: BLE001
            LOGIN.pop(uid, None)
            await event.respond(f"Ошибка отправки кода: {exc}")
        return

    if step == "code":
        code = re.sub(r"\D", "", text)
        code_hash = st.get("hash")
        phone = st.get("phone")
        if not code or not phone or not code_hash:
            LOGIN.pop(uid, None)
            await event.respond(
                "Сессия входа сброшена (нет phone_code_hash). "
                "Нажмите 🔑 Войти заново или выполните на сервере:\n"
                "<code>python login.py</code>"
            )
            return
        try:
            await user.sign_in(phone=phone, code=code, phone_code_hash=code_hash)
        except SessionPasswordNeededError:
            st["step"] = "password"
            await event.respond("🔒 Включена двухфакторная защита. Введите пароль (2FA):")
            return
        except PhoneCodeInvalidError:
            await event.respond("Неверный код. Попробуйте ещё раз:")
            return
        except PhoneCodeExpiredError:
            LOGIN.pop(uid, None)
            await event.respond("Код истёк. Начните вход заново кнопкой 🔑.")
            return
        except Exception as exc:  # noqa: BLE001
            LOGIN.pop(uid, None)
            await event.respond(f"Ошибка входа: {exc}")
            return
        await _finish_login(event, uid)
        return

    if step == "password":
        try:
            await user.sign_in(password=text)
        except Exception as exc:  # noqa: BLE001
            await event.respond(f"Неверный пароль или ошибка: {exc}. Попробуйте снова:")
            return
        await _finish_login(event, uid)
        return


async def _finish_login(event, uid: int):
    LOGIN.pop(uid, None)
    STATE["authorized"] = True
    if config.PARSING_ON_START:
        STATE["parsing"] = True
    user.parse_mode = "html"
    try:
        await event.delete()  # remove the message that held the code/password
    except Exception:  # noqa: BLE001
        pass
    me = await user.get_me()
    log.info("Observer logged in via bot: %s (id=%s)", me.first_name, me.id)
    _, rows = control_menu()
    await event.respond(
        f"✅ Вход выполнен: <b>{me.first_name}</b> (id=<code>{me.id}</code>).",
        buttons=rows,
    )


async def do_logout():
    try:
        await user.log_out()
    except Exception as exc:  # noqa: BLE001
        log.warning("log_out error: %s", exc)
    STATE["authorized"] = False
    STATE["parsing"] = False
    try:
        if not user.is_connected():
            await user.connect()
    except Exception as exc:  # noqa: BLE001
        log.warning("Reconnect after logout failed: %s", exc)
    log.info("Observer logged out.")


# ===========================================================================
# Publishing / cleanup
# ===========================================================================
_MESSAGE_LIMIT = 4096          # Telegram text-message limit


async def _send_post(target, media: list, text_html: str, schedule=None) -> None:
    """Publish text (+ optional media), scheduled for `schedule` (datetime) if set.

    Try the text as a single-message caption; only if Telegram rejects it (over the
    account's 1024/2048 limit) fall back to media on top + full text as a separate
    message. This respects Premium automatically without guessing the limit."""
    if not media:
        await user.send_message(target, text_html, schedule=schedule)
        return

    file = media if len(media) > 1 else media[0]
    try:
        await user.send_file(target, file=file, caption=text_html, schedule=schedule)
    except MediaCaptionTooLongError:
        await user.send_file(target, file=file, schedule=schedule)
        await user.send_message(target, text_html, schedule=schedule)


async def _publish(
    source_chat: int,
    source_msg_id: int,
    target: dict,
    schedule_hh: int | None = None,
    schedule_mm: int | None = None,
) -> bool:
    """Re-post the vacancy to the target's channel.

    target = {label, tags, image, channel_id, copy_media}.
    copy_media=True keeps the post's own photo/video instead of the category image.
    schedule_hh/mm override the global SCHEDULE when set for this post.
    """
    channel = target["channel_id"]
    try:
        original = await user.get_messages(source_chat, ids=source_msg_id)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not fetch original message: %s", exc)
        return False
    if original is None:
        log.error("Original message %s in %s no longer exists", source_msg_id, source_chat)
        return False

    if config.AUTO_FORMAT:
        body = format_post(original.text or "")
    else:
        body = strip_hashtags(original.text or "")

    # Footer: hashtags line + masked links line (user sees the text, click = url).
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

    source_media = (original.media
                    if original.media and not isinstance(original.media, MessageMediaWebPage)
                    else None)

    if target.get("copy_media"):
        # Copy the post's own photo/video; do NOT substitute the category image.
        media = [source_media] if source_media else []
    else:
        cat_image = target["image"]
        has_cat_image = bool(cat_image) and os.path.exists(cat_image)
        if cat_image and not has_cat_image:
            log.warning("Category image not found: %s (posting without it)", cat_image)
        if has_cat_image:
            media = [cat_image]
        elif source_media:
            media = [source_media]
        else:
            media = []

    when = _next_schedule_dt(schedule_hh, schedule_mm)
    try:
        await _send_post(channel, media, final_text, schedule=when)
        log.info("Scheduled for %s", when.strftime("%Y-%m-%d %H:%M"))
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to publish to channel %s: %s", channel, exc)
        try:  # last resort: text only
            if len(final_text) <= _MESSAGE_LIMIT:
                await user.send_message(channel, final_text, schedule=when)
                return True
        except Exception as exc2:  # noqa: BLE001
            log.error("Fallback publish also failed: %s", exc2)
        return False


async def _cleanup(fwd_id: int, kb_id: int) -> None:
    """Delete the forwarded post (user's message) and the keyboard (bot's message)."""
    try:
        await user.delete_messages(config.MODERATION_GROUP, [fwd_id])
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not delete forwarded message: %s", exc)
    try:
        await bot.delete_messages(config.MODERATION_GROUP, [kb_id])
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not delete keyboard message: %s", exc)


# ===========================================================================
# Startup
# ===========================================================================
async def main():
    load_keywords()
    load_schedule()
    await bot.start(bot_token=config.BOT_TOKEN)
    bot.parse_mode = "html"
    await user.connect()

    if await user.is_user_authorized():
        STATE["authorized"] = True
        user.parse_mode = "html"
        if config.PARSING_ON_START:
            STATE["parsing"] = True
        me = await user.get_me()
        log.info("Observer already logged in: %s (id=%s)", me.first_name, me.id)
    else:
        log.warning("Observer NOT logged in. Open the bot in Telegram and press '🔑 Войти в аккаунт'.")

    bot_me = await bot.get_me()
    log.info("Bot @%s ready | parsing=%s authorized=%s",
             bot_me.username, STATE["parsing"], STATE["authorized"])
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS is empty — anyone who opens the bot can control it. Set ADMIN_IDS in .env!")

    await asyncio.gather(
        user.run_until_disconnected(),
        bot.run_until_disconnected(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        store.close()
