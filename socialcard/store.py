"""실행 이력과 발행 이력 저장소(SQLite). 중복 발행 방지의 근거가 되는 곳."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

KST = timezone(timedelta(hours=9))

SCHEMA = """
CREATE TABLE IF NOT EXISTS published (
    article_url   TEXT PRIMARY KEY,
    article_id    TEXT NOT NULL,
    title         TEXT NOT NULL,
    published_at  TEXT,
    media_id      TEXT,
    permalink     TEXT,
    run_id        TEXT NOT NULL,
    mode          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,
    mode          TEXT NOT NULL,
    source        TEXT NOT NULL,
    collected     INTEGER DEFAULT 0,
    skipped       INTEGER DEFAULT 0,
    published     INTEGER DEFAULT 0,
    failed        INTEGER DEFAULT 0,
    error         TEXT,
    detail        TEXT
);
CREATE TABLE IF NOT EXISTS run_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    article_url   TEXT NOT NULL,
    article_id    TEXT,
    status        TEXT NOT NULL,
    media_id      TEXT,
    detail        TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_items_run ON run_items(run_id);
"""


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------------------------------------------------------- 중복 방지
    def find_published(self, article_url: str) -> Optional[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM published WHERE article_url = ?", (article_url,)
        )
        return cur.fetchone()

    def is_published(self, article_url: str) -> bool:
        return self.find_published(article_url) is not None

    def mark_published(
        self,
        article_url: str,
        article_id: str,
        title: str,
        published_at: str,
        run_id: str,
        mode: str,
        media_id: Optional[str] = None,
        permalink: Optional[str] = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO published "
            "(article_url, article_id, title, published_at, media_id, permalink, run_id, mode, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (article_url, article_id, title, published_at, media_id, permalink, run_id, mode, now_iso()),
        )
        self.conn.commit()

    # ------------------------------------------------------------------- 실행 로그
    def start_run(self, run_id: str, mode: str, source: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, status, mode, source) VALUES (?,?,?,?,?)",
            (run_id, now_iso(), "running", mode, source),
        )
        self.conn.commit()

    def finish_run(
        self,
        run_id: str,
        status: str,
        collected: int = 0,
        skipped: int = 0,
        published: int = 0,
        failed: int = 0,
        error: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, collected=?, skipped=?, published=?, "
            "failed=?, error=?, detail=? WHERE run_id=?",
            (
                now_iso(),
                status,
                collected,
                skipped,
                published,
                failed,
                error,
                json.dumps(detail or {}, ensure_ascii=False),
                run_id,
            ),
        )
        self.conn.commit()

    def log_item(
        self,
        run_id: str,
        article_url: str,
        article_id: str,
        status: str,
        media_id: Optional[str] = None,
        detail: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO run_items (run_id, article_url, article_id, status, media_id, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, article_url, article_id, status, media_id, detail, now_iso()),
        )
        self.conn.commit()

    def recent_runs(self, limit: int = 10) -> List[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        )
        return cur.fetchall()

    def run_items(self, run_id: str) -> List[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM run_items WHERE run_id = ? ORDER BY id", (run_id,)
        )
        return cur.fetchall()
