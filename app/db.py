"""SQLite 持久化：采集轮次、公告快照与正文变更记录。

设计要点：
- 每一轮采集对应 rounds 表一行，公告挂在 round_id 下，轮次之间互不影响；
- 公告只在整轮抓取全部成功后，于同一事务内写入并标记轮次成功，
  因此失败轮次不会留下半截数据，也不会污染上一轮结果（重抓不叠加）；
- 正文变更在同一事务内与快照一并写入：以公告编号为键，比较同一来源
  （同源目录）相邻两次成功轮次的 .notice-body 文本（空白已合并），
  文字不同才记录；新公告没有旧版本，不算变更；
- 变更记录独立于快照保留，后续抓取若正文不变则继续待核对；只有点击
  “已核对”才会给该条记录写上确认时间，日后再次改动会另生成新记录；
- 页面始终展示最近一次 status='success' 的轮次结果。
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Iterable
from urllib.parse import urlparse

from scraper import Announcement

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    start_url TEXT NOT NULL,
    source_key TEXT NOT NULL DEFAULT '',        -- 同源同目录，用于相邻轮次比较
    status TEXT NOT NULL DEFAULT 'running',   -- running / success / failed
    list_pages INTEGER NOT NULL DEFAULT 0,
    detail_pages INTEGER NOT NULL DEFAULT 0,
    error_url TEXT,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS announcements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    project_no TEXT NOT NULL,
    notice_no TEXT NOT NULL,
    notice_type TEXT NOT NULL,
    publish_time TEXT NOT NULL,
    deadline TEXT,
    target_notice_no TEXT,
    title TEXT,
    url TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_announcements_round ON announcements(round_id);
CREATE TABLE IF NOT EXISTS body_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_no TEXT NOT NULL,
    project_no TEXT NOT NULL,
    title TEXT,
    old_body TEXT NOT NULL,
    new_body TEXT NOT NULL,
    old_round_id INTEGER NOT NULL REFERENCES rounds(id),
    new_round_id INTEGER NOT NULL REFERENCES rounds(id),
    old_url TEXT NOT NULL,
    new_url TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_body_changes_new_round ON body_changes(new_round_id);
CREATE INDEX IF NOT EXISTS idx_body_changes_notice ON body_changes(notice_no);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def source_key(start_url: str) -> str:
    """采集来源标识：同源（协议+主机+端口）且同一目录视为同一声明目录。

    分页参数不参与比较，因此 /list.html?page=1 与 ?page=2 同属一个目录。
    """
    p = urlparse(start_url)
    directory = p.path.rpartition("/")[0]
    port = p.port or {"http": 80, "https": 443}.get(p.scheme)
    return f"{p.scheme}://{p.hostname}:{port}{directory}"


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(SCHEMA)
        # 兼容旧库：补上后加的列。旧库历史快照没有正文，body 保持 NULL，
        # 表示“基线未知”，升级后首轮不与它比较，避免误报全部变更。
        if "body" not in _columns(conn, "announcements"):
            conn.execute("ALTER TABLE announcements ADD COLUMN body TEXT")
        if "source_key" not in _columns(conn, "rounds"):
            conn.execute("ALTER TABLE rounds ADD COLUMN source_key TEXT NOT NULL DEFAULT ''")
            for r in conn.execute("SELECT id, start_url FROM rounds WHERE source_key=''").fetchall():
                conn.execute("UPDATE rounds SET source_key=? WHERE id=?", (source_key(r["start_url"]), r["id"]))
        # 上次进程退出可能留下 running 状态的轮次，统一标记为失败
        conn.execute(
            "UPDATE rounds SET status='failed', finished_at=?, error_message='进程中断，轮次未完成' "
            "WHERE status='running'",
            (_now(),),
        )


def create_round(start_url: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO rounds (started_at, start_url, source_key, status) VALUES (?, ?, ?, 'running')",
            (_now(), start_url, source_key(start_url)),
        )
        return cur.lastrowid


def _previous_success_round(conn: sqlite3.Connection, round_id: int, key: str) -> sqlite3.Row | None:
    """同一目录下、本轮回之前最近一次成功轮次（失败轮次不参与比较）。"""
    return conn.execute(
        "SELECT * FROM rounds WHERE status='success' AND source_key=? AND id<? ORDER BY id DESC LIMIT 1",
        (key, round_id),
    ).fetchone()


def _bodies(conn: sqlite3.Connection, round_id: int) -> dict[str, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM announcements WHERE round_id=?", (round_id,)).fetchall()
    return {r["notice_no"]: r for r in rows}


def finish_round_success(
    round_id: int, announcements: Iterable[Announcement], list_pages: int, detail_pages: int
) -> int:
    """整轮成功后一次性写入公告快照与正文变更记录（单事务，失败则整体回滚）。

    返回本轮新记录的正文变更条数。比较对象是同一目录上一成功轮次：
    仅对两轮都存在的公告编号比较正文；新公告不产生变更。
    """
    anns = list(announcements)
    rows = [
        (
            round_id,
            a.project_no,
            a.notice_no,
            a.notice_type,
            a.publish_time,
            a.deadline,
            a.target_notice_no,
            a.title,
            a.url,
            a.body,
        )
        for a in anns
    ]
    with _connect() as conn:
        with conn:  # 事务：要么全部写入，要么全部回滚
            conn.executemany(
                "INSERT INTO announcements (round_id, project_no, notice_no, notice_type, publish_time, "
                "deadline, target_notice_no, title, url, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            prev = _previous_success_round(conn, round_id, conn.execute(
                "SELECT source_key FROM rounds WHERE id=?", (round_id,)
            ).fetchone()["source_key"])
            new_changes = 0
            if prev is not None:
                old_map = _bodies(conn, prev["id"])
                seen: set[str] = set()
                for a in anns:
                    if a.notice_no in seen or a.notice_no not in old_map:
                        continue
                    seen.add(a.notice_no)
                    old = old_map[a.notice_no]
                    # 旧库历史快照正文为 NULL（基线未知）：不比较，避免误报
                    if old["body"] is None or old["body"] == a.body:
                        continue
                    conn.execute(
                        "INSERT INTO body_changes (notice_no, project_no, title, old_body, new_body, "
                        "old_round_id, new_round_id, old_url, new_url, detected_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            a.notice_no,
                            a.project_no,
                            a.title,
                            old["body"],
                            a.body,
                            prev["id"],
                            round_id,
                            old["url"],
                            a.url,
                            _now(),
                        ),
                    )
                    new_changes += 1
            conn.execute(
                "UPDATE rounds SET status='success', finished_at=?, list_pages=?, detail_pages=? WHERE id=?",
                (_now(), list_pages, detail_pages, round_id),
            )
    return new_changes


def finish_round_failed(round_id: int, error_url: str, error_message: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE rounds SET status='failed', finished_at=?, error_url=?, error_message=? WHERE id=?",
            (_now(), error_url, error_message, round_id),
        )


def get_latest_success_round() -> sqlite3.Row | None:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM rounds WHERE status='success' ORDER BY id DESC LIMIT 1"
        ).fetchone()


def get_rounds(limit: int = 20) -> list[sqlite3.Row]:
    with _connect() as conn:
        return conn.execute("SELECT * FROM rounds ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def get_announcements(round_id: int) -> list[Announcement]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM announcements WHERE round_id=? ORDER BY publish_time, notice_no",
            (round_id,),
        ).fetchall()
    return [
        Announcement(
            project_no=r["project_no"],
            notice_no=r["notice_no"],
            notice_type=r["notice_type"],
            publish_time=r["publish_time"],
            deadline=r["deadline"],
            target_notice_no=r["target_notice_no"],
            title=r["title"] or r["notice_no"],
            url=r["url"],
            body=r["body"] or "",
        )
        for r in rows
    ]


def get_body_changes() -> list[sqlite3.Row]:
    """全部正文变更记录：待核对在前，同状态按检出时间倒序。"""
    with _connect() as conn:
        return conn.execute(
            "SELECT c.*, ro.finished_at AS old_round_time, rn.finished_at AS new_round_time "
            "FROM body_changes c "
            "LEFT JOIN rounds ro ON ro.id=c.old_round_id "
            "LEFT JOIN rounds rn ON rn.id=c.new_round_id "
            "ORDER BY c.acknowledged_at IS NOT NULL, c.detected_at DESC, c.id DESC"
        ).fetchall()


def acknowledge_body_change(change_id: int) -> bool:
    """确认单条正文变更；只影响该记录，已确认的不会被重复确认。"""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE body_changes SET acknowledged_at=? WHERE id=? AND acknowledged_at IS NULL",
            (_now(), change_id),
        )
        return cur.rowcount > 0
