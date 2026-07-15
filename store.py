"""Persistent mapping: moderation keyboard message -> original source message.

Survives restarts so pending moderation items are not lost.
"""
import sqlite3
import threading
from contextlib import closing


class Store:
    _REQUIRED = {"kb_msg_id", "source_chat", "source_msg", "fwd_msg_id"}

    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        # Drop a stale table left over from an older schema version, then (re)create.
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(pending)")}
        if cols and not self._REQUIRED.issubset(cols):
            self._conn.execute("DROP TABLE pending")
            cols = set()
        if not cols:
            self._conn.execute(
                """
                CREATE TABLE pending (
                    kb_msg_id    INTEGER PRIMARY KEY,   -- bot keyboard message id (has the buttons)
                    source_chat  INTEGER NOT NULL,      -- original channel id
                    source_msg   INTEGER NOT NULL,      -- original message id
                    fwd_msg_id   INTEGER NOT NULL,      -- forwarded message id in the group
                    schedule_hh  INTEGER,               -- per-post publish hour (optional)
                    schedule_mm  INTEGER,               -- per-post publish minute (optional)
                    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        else:
            if "schedule_hh" not in cols:
                self._conn.execute("ALTER TABLE pending ADD COLUMN schedule_hh INTEGER")
            if "schedule_mm" not in cols:
                self._conn.execute("ALTER TABLE pending ADD COLUMN schedule_mm INTEGER")
        self._conn.commit()

    def add(self, kb_msg_id: int, source_chat: int, source_msg: int, fwd_msg_id: int) -> None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "INSERT OR REPLACE INTO pending "
                "(kb_msg_id, source_chat, source_msg, fwd_msg_id, schedule_hh, schedule_mm) "
                "VALUES (?, ?, ?, ?, NULL, NULL)",
                (kb_msg_id, source_chat, source_msg, fwd_msg_id),
            )
            self._conn.commit()

    def get(self, kb_msg_id: int) -> dict | None:
        with self._lock, closing(self._conn.cursor()) as cur:
            cur.execute(
                "SELECT source_chat, source_msg, fwd_msg_id, schedule_hh, schedule_mm "
                "FROM pending WHERE kb_msg_id = ?",
                (kb_msg_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "source_chat": row[0],
                "source_msg": row[1],
                "fwd_msg_id": row[2],
                "schedule_hh": row[3],
                "schedule_mm": row[4],
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
