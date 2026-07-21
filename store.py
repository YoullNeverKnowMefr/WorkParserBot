"""Persistent store: seen scraped posts + pending moderation items."""
import json
import sqlite3
import threading
from contextlib import closing


class Store:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(pending)")}
        # Old observer schema used integer source_chat and no text_html — rebuild.
        required = {"kb_msg_id", "source", "source_msg", "preview_msg_id", "text_html"}
        if cols and not required.issubset(cols):
            self._conn.execute("DROP TABLE pending")
            cols = set()
        if not cols:
            self._conn.execute(
                """
                CREATE TABLE pending (
                    kb_msg_id      INTEGER PRIMARY KEY,
                    source         TEXT NOT NULL,
                    source_msg     INTEGER NOT NULL,
                    preview_msg_id INTEGER NOT NULL,
                    text_html      TEXT NOT NULL,
                    media_urls     TEXT,
                    source_link    TEXT,
                    schedule_hh    INTEGER,
                    schedule_mm    INTEGER,
                    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        else:
            if "schedule_hh" not in cols:
                self._conn.execute("ALTER TABLE pending ADD COLUMN schedule_hh INTEGER")
            if "schedule_mm" not in cols:
                self._conn.execute("ALTER TABLE pending ADD COLUMN schedule_mm INTEGER")

        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen (
                source   TEXT NOT NULL,
                msg_id   INTEGER NOT NULL,
                PRIMARY KEY (source, msg_id)
            )
            """
        )
        self._conn.commit()

    def has_seen_any(self, source: str) -> bool:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("SELECT 1 FROM seen WHERE source = ? LIMIT 1", (source.lower(),))
            return cur.fetchone() is not None

    def is_seen(self, source: str, msg_id: int) -> bool:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT 1 FROM seen WHERE source = ? AND msg_id = ?",
                (source.lower(), msg_id),
            )
            return cur.fetchone() is not None

    def mark_seen(self, source: str, msg_id: int) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT OR IGNORE INTO seen (source, msg_id) VALUES (?, ?)",
                (source.lower(), msg_id),
            )
            self._conn.commit()

    def mark_seen_many(self, source: str, msg_ids: list[int]) -> None:
        if not msg_ids:
            return
        src = source.lower()
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.executemany(
                "INSERT OR IGNORE INTO seen (source, msg_id) VALUES (?, ?)",
                [(src, mid) for mid in msg_ids],
            )
            self._conn.commit()

    def add(
        self,
        kb_msg_id: int,
        source: str,
        source_msg: int,
        preview_msg_id: int,
        text_html: str,
        media_urls: list[str] | None = None,
        source_link: str = "",
    ) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT OR REPLACE INTO pending "
                "(kb_msg_id, source, source_msg, preview_msg_id, text_html, "
                " media_urls, source_link, schedule_hh, schedule_mm) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                (
                    kb_msg_id,
                    source.lower(),
                    source_msg,
                    preview_msg_id,
                    text_html,
                    json.dumps(media_urls or [], ensure_ascii=False),
                    source_link,
                ),
            )
            self._conn.commit()

    def get(self, kb_msg_id: int) -> dict | None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT source, source_msg, preview_msg_id, text_html, media_urls, "
                "source_link, schedule_hh, schedule_mm "
                "FROM pending WHERE kb_msg_id = ?",
                (kb_msg_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            try:
                media = json.loads(row[4] or "[]")
            except json.JSONDecodeError:
                media = []
            return {
                "source": row[0],
                "source_msg": row[1],
                "preview_msg_id": row[2],
                "text_html": row[3] or "",
                "media_urls": media if isinstance(media, list) else [],
                "source_link": row[5] or "",
                "schedule_hh": row[6],
                "schedule_mm": row[7],
            }

    def set_schedule(self, kb_msg_id: int, hh: int, mm: int) -> bool:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "UPDATE pending SET schedule_hh = ?, schedule_mm = ? WHERE kb_msg_id = ?",
                (hh, mm, kb_msg_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def remove(self, kb_msg_id: int) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute("DELETE FROM pending WHERE kb_msg_id = ?", (kb_msg_id,))
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
