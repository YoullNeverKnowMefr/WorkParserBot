"""Configuration loaded from a .env file (see .env.example)."""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no external dependency)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def _get(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required config value: {name} (set it in .env)")
    return val or ""


def _parse_ids(raw: str) -> list:
    """Parse a comma/space separated list of chat ids or @usernames."""
    items: list = []
    for part in raw.replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        try:
            items.append(int(part))
        except ValueError:
            items.append(part.lstrip("@"))
    return items


# --- Telegram API credentials (https://my.telegram.org -> API development tools)
API_ID = int(_get("API_ID", required=True))
API_HASH = _get("API_HASH", required=True)

# Bot token from @BotFather. The bot renders the inline keyboard in the group.
BOT_TOKEN = _get("BOT_TOKEN", required=True)

# Session names for the two clients.
SESSION = _get("SESSION", "observer")
BOT_SESSION = _get("BOT_SESSION", "modbot")

# Channels to monitor for vacancies. Ids (-100...) or @usernames, comma separated.
SOURCE_CHANNELS = _parse_ids(_get("SOURCE_CHANNELS", required=True))

# Closed moderation group. Both the observer AND the bot must be members.
MODERATION_GROUP = _parse_ids(_get("MODERATION_GROUP", required=True))[0]

# Target channel where approved vacancies get published.
# Deprecated: target channels are now defined per-channel in categories.json.
# Kept optional for backward compatibility.
_tc = _parse_ids(_get("TARGET_CHANNEL", ""))
TARGET_CHANNEL = _tc[0] if _tc else None

# Optional whitelist of moderator user ids allowed to press the buttons.
# Empty => anyone in the moderation group may approve.
MODERATOR_IDS = _parse_ids(_get("MODERATOR_IDS", ""))

# User ids allowed to use the bot's control panel (start/stop, login, logout).
# STRONGLY recommended to set this — login controls the observer account.
# Empty => anyone who opens the bot may control it (a warning is shown).
ADMIN_IDS = _parse_ids(_get("ADMIN_IDS", ""))

# Start parsing automatically on launch if the observer is already logged in.
PARSING_ON_START = _get("PARSING_ON_START", "true").lower() in ("1", "true", "yes")

# Keywords that mark a message as a vacancy (case-insensitive substring match).
_DEFAULT_KEYWORDS = (
    "вакансия,вакансии,ищем,ищу,требуется,требуются,нужен,нужна,нужны,"
    "разыскивается,в команду,в поисках,hiring,we are looking,we're looking,"
    "looking for,job,position,вакантн,открыта позиция,оплата,зарплата,з/п,ищется"
)
KEYWORDS = [k.strip().lower() for k in _get("KEYWORDS", _DEFAULT_KEYWORDS).split(",") if k.strip()]

# Delete the forwarded message + keyboard message after a successful publish.
DELETE_AFTER_PUBLISH = _get("DELETE_AFTER_PUBLISH", "true").lower() in ("1", "true", "yes")

# Auto-reformat posts (bold title, unified bullets, section spacing, strip source footer).
AUTO_FORMAT = _get("AUTO_FORMAT", "true").lower() in ("1", "true", "yes")

# Default time (HH:MM) to schedule published posts at, in TZ_OFFSET timezone.
# Editable at runtime from the control panel (persisted to schedule.txt).
SCHEDULE_TIME = _get("SCHEDULE_TIME", "10:00")

# Timezone offset (hours from UTC) used for scheduling. 3 = МСК (Moscow).
TZ_OFFSET = int(_get("TZ_OFFSET", "3"))
TZ_NAME = _get("TZ_NAME", "МСК")

# SQLite file used to map moderation messages back to their source message.
DB_PATH = str(BASE_DIR / _get("DB_PATH", "state.db"))


def _norm_target(entry: dict) -> dict:
    """Normalize a publishable target: {label, tags[], links[], image(abs path)}.

    links: list of {"text": "Ростов на Дону", "url": "https://..."} — rendered as
    masked hyperlinks appended to the post (the user sees the text, click = link).
    """
    tags = entry.get("tags", [])
    if isinstance(tags, str):
        tags = tags.split()
    tags = [t if t.startswith("#") else "#" + t.lstrip("#") for t in tags if t.strip()]
    links = []
    for l in entry.get("links", []):
        if isinstance(l, dict) and str(l.get("text", "")).strip() and str(l.get("url", "")).strip():
            links.append({"text": str(l["text"]).strip(), "url": str(l["url"]).strip()})
    image = entry.get("image", "").strip()
    image_path = str((BASE_DIR / image)) if image else ""
    return {"label": entry["label"], "tags": tags, "links": links, "image": image_path}


def _load_categories() -> list[dict]:
    """Load the 4-level moderation tree from categories.json.

    Category -> Branch (ответвление) -> Channel -> Subcategory(tags):

        [{
          "label": "Новостники",
          "branches": [{
              "label": "Ростов",
              "channels": [{
                  "label": "Взрослый",
                  "channel": "-1001234567890",   # target channel id or @username
                  "copy_media": true,            # true = keep the post's own photo/video
                  "subcategories": [
                      {"label": "вид поста 1", "tags": ["#design"], "links": [...],
                       "image": "images/design.jpg"}
                  ]
              }]
          }]
        }]

    In memory: category {label, branches}; branch {label, channels}; channel
    {label, channel_id, copy_media, subcategories}; subcategory {label, tags,
    links, image}.
    """
    path = BASE_DIR / _get("CATEGORIES_FILE", "categories.json")
    if not path.exists():
        raise RuntimeError(f"categories.json not found at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    cats: list[dict] = []
    for cat in raw:
        branches = []
        for br in cat.get("branches", []):
            channels = []
            for ch in br.get("channels", []):
                ch_ids = _parse_ids(str(ch.get("channel", "")))
                channels.append({
                    "label": ch["label"],
                    "channel_id": ch_ids[0] if ch_ids else None,
                    "copy_media": bool(ch.get("copy_media", False)),
                    "subcategories": [_norm_target(s) for s in ch.get("subcategories", [])],
                })
            branches.append({"label": br["label"], "channels": channels})
        cats.append({"label": cat["label"], "branches": branches})
    if not cats:
        raise RuntimeError("categories.json is empty")
    return cats


CATEGORIES = _load_categories()
