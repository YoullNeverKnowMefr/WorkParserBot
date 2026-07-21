"""Public Telegram channel scraper via https://t.me/s/<username>.

No user account / MTProto session required. Only public channels with a
username are supported (private / invite-only channels cannot be scraped).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import unquote

import httpx

log = logging.getLogger("vacancybot.scraper")

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_POST_START_RE = re.compile(
    r'<div class="tgme_widget_message[^"]*"\s+data-post="([^"/]+)/(\d+)"',
)
_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL,
)
_PHOTO_RE = re.compile(
    r'class="tgme_widget_message_photo_wrap[^"]*"[^>]*style="[^"]*background-image:url\(\'([^\']+)\'\)',
    re.DOTALL,
)
_VIDEO_THUMB_RE = re.compile(
    r'class="tgme_widget_message_video_thumb"[^>]*style="[^"]*background-image:url\(\'([^\']+)\'\)',
    re.DOTALL,
)


@dataclass
class ScrapedPost:
    username: str
    msg_id: int
    text_html: str = ""
    text_plain: str = ""
    media_urls: list[str] = field(default_factory=list)
    link: str = ""

    @property
    def key(self) -> str:
        return f"{self.username}/{self.msg_id}"


class _TelegramHTMLToMarkup(HTMLParser):
    """Convert t.me preview HTML into Telegram-compatible HTML subset."""

    _KEEP = {"b", "strong", "i", "em", "u", "s", "strike", "del", "code", "pre", "a"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._plain: list[str] = []
        self._stack: list[str] = []

    @staticmethod
    def _norm(tag: str) -> str:
        tag = tag.lower()
        if tag == "strong":
            return "b"
        if tag == "em":
            return "i"
        if tag in ("strike", "del"):
            return "s"
        return tag

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "br":
            self._out.append("\n")
            self._plain.append("\n")
            return
        if tag not in self._KEEP:
            return
        norm = self._norm(tag)
        if norm == "a":
            href = next((v for k, v in attrs if k == "href" and v), "") or ""
            if href.startswith("tg://") or not href:
                self._stack.append("")  # placeholder: skip matching end
                return
            self._out.append(f'<a href="{_escape_attr(href)}">')
            self._stack.append("a")
            return
        self._out.append(f"<{norm}>")
        self._stack.append(norm)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "br" or tag not in self._KEEP:
            return
        norm = self._norm(tag)
        if not self._stack:
            return
        opened = self._stack.pop()
        if not opened:
            return
        if opened != norm:
            # Mismatched markup from preview — close what we opened.
            norm = opened
        self._out.append(f"</{norm}>")

    def handle_data(self, data: str) -> None:
        if not data:
            return
        self._out.append(_escape_text(data))
        self._plain.append(data)

    def result(self) -> tuple[str, str]:
        while self._stack:
            opened = self._stack.pop()
            if opened:
                self._out.append(f"</{opened}>")
        html = "".join(self._out)
        html = re.sub(r"<a[^>]*>\s*</a>", "", html)
        html = re.sub(r"\n{3,}", "\n\n", html).strip()
        plain = re.sub(r"\n{3,}", "\n\n", "".join(self._plain)).strip()
        return html, plain


def _escape_text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def html_fragment_to_telegram(fragment: str) -> tuple[str, str]:
    parser = _TelegramHTMLToMarkup()
    parser.feed(fragment)
    parser.close()
    return parser.result()


def _normalize_username(raw: str | int) -> str | None:
    if isinstance(raw, int):
        return None
    s = str(raw).strip().lstrip("@")
    if not s or s.lstrip("-").isdigit():
        return None
    return s


def parse_channel_html(username: str, html: str) -> list[ScrapedPost]:
    """Parse a t.me/s/<username> page into posts (oldest → newest)."""
    starts = list(_POST_START_RE.finditer(html))
    posts: list[ScrapedPost] = []
    for i, m in enumerate(starts):
        uname, mid_s = m.group(1), m.group(2)
        if uname.lower() != username.lower():
            continue
        end = starts[i + 1].start() if i + 1 < len(starts) else len(html)
        body = html[m.start() : end]
        msg_id = int(mid_s)

        text_m = _TEXT_RE.search(body)
        text_html, text_plain = ("", "")
        if text_m:
            text_html, text_plain = html_fragment_to_telegram(text_m.group(1))

        media: list[str] = []
        for url in _PHOTO_RE.findall(body):
            media.append(unquote(url))
        if not media:
            for url in _VIDEO_THUMB_RE.findall(body):
                media.append(unquote(url))

        seen_u: set[str] = set()
        uniq_media: list[str] = []
        for u in media:
            if u not in seen_u:
                seen_u.add(u)
                uniq_media.append(u)

        posts.append(
            ScrapedPost(
                username=uname,
                msg_id=msg_id,
                text_html=text_html,
                text_plain=text_plain,
                media_urls=uniq_media,
                link=f"https://t.me/{uname}/{msg_id}",
            )
        )

    posts.sort(key=lambda p: p.msg_id)
    return posts


async def fetch_channel_posts(
    client: httpx.AsyncClient,
    username: str,
) -> list[ScrapedPost]:
    uname = _normalize_username(username)
    if not uname:
        log.warning("Skip non-public source (need @username): %r", username)
        return []
    url = f"https://t.me/s/{uname}"
    try:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to fetch %s: %s", url, exc)
        return []
    return parse_channel_html(uname, resp.text)


def source_usernames(raw_sources: Iterable[str | int]) -> list[str]:
    out: list[str] = []
    for item in raw_sources:
        u = _normalize_username(item)
        if u:
            out.append(u)
        else:
            log.warning(
                "SOURCE_CHANNELS entry %r ignored — public @username required "
                "(numeric channel ids cannot be scraped without an account)",
                item,
            )
    # Preserve order, drop duplicates.
    seen: set[str] = set()
    uniq: list[str] = []
    for u in out:
        key = u.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(u)
    return uniq


def make_http_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
    )
