"""正文变更核查的持久化语义测试（直接驱动 db 层）。"""

import db
from scraper import Announcement

START = "http://site.example/list.html?page=1"


def ann(notice, body, project="P1", url=None):
    return Announcement(
        project_no=project,
        notice_no=notice,
        notice_type="tender",
        publish_time="2026-11-01 09:00",
        deadline="2026-11-20 17:00",
        target_notice_no=None,
        title=f"公告 {notice}",
        url=url or f"http://site.example/detail/{notice}.html",
        body=body,
    )


def run_round(anns, start_url=START):
    """模拟一轮完整成功采集，返回 (轮次 id, 新增变更条数)。"""
    rid = db.create_round(start_url)
    n = db.finish_round_success(rid, anns, 1, len(anns))
    return rid, n


def test_new_notice_is_not_a_change(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    db.init_db()
    run_round([ann("N1", "正文甲")])
    _rid, n = run_round([ann("N1", "正文甲"), ann("N2", "新公告正文")])
    assert n == 0
    assert db.get_body_changes() == []


def test_same_body_with_different_whitespace_is_not_a_change(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    db.init_db()
    run_round([ann("N1", "甲 乙 丙")])
    _rid, n = run_round([ann("N1", "甲 乙 丙")])
    assert n == 0
    assert db.get_body_changes() == []


def test_changed_body_records_old_new_and_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    db.init_db()
    r1, _ = run_round([ann("N1", "旧正文", url="http://site.example/detail/N1.html")])
    r2, n = run_round([ann("N1", "新正文", url="http://site.example/detail/N1.html")])
    assert n == 1
    (c,) = db.get_body_changes()
    assert (c["notice_no"], c["old_body"], c["new_body"]) == ("N1", "旧正文", "新正文")
    assert (c["old_round_id"], c["new_round_id"]) == (r1, r2)
    assert c["old_url"] == c["new_url"] == "http://site.example/detail/N1.html"
    assert c["acknowledged_at"] is None


def test_repeated_scrape_does_not_duplicate_same_change(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    db.init_db()
    run_round([ann("N1", "旧正文")])
    run_round([ann("N1", "新正文")])
    _r, n = run_round([ann("N1", "新正文")])  # 正文不再变化
    assert n == 0
    assert len(db.get_body_changes()) == 1


def test_pending_change_survives_later_unchanged_scrapes(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    db.init_db()
    run_round([ann("N1", "旧正文")])
    run_round([ann("N1", "新正文")])
    run_round([ann("N1", "新正文")])
    (c,) = db.get_body_changes()
    assert c["acknowledged_at"] is None


def test_acknowledge_only_that_change_and_later_change_is_new(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    db.init_db()
    run_round([ann("N1", "v1")])
    run_round([ann("N1", "v2")])
    (c,) = db.get_body_changes()
    assert db.acknowledge_body_change(c["id"]) is True
    assert db.acknowledge_body_change(c["id"]) is False  # 重复确认无效

    run_round([ann("N1", "v2")])  # 已核对后正文不变：不新增
    assert len(db.get_body_changes()) == 1

    _r, n = run_round([ann("N1", "v3")])  # 再改：生成新记录
    assert n == 1
    changes = db.get_body_changes()
    assert len(changes) == 2
    pending = [c for c in changes if c["acknowledged_at"] is None]
    assert len(pending) == 1
    assert (pending[0]["old_body"], pending[0]["new_body"]) == ("v2", "v3")


def test_different_source_directory_is_not_compared(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    db.init_db()
    run_round([ann("N1", "目录甲正文")], start_url="http://site.example/a/list.html?page=1")
    _r, n = run_round(
        [ann("N1", "目录乙正文")], start_url="http://other.example/list.html?page=1"
    )
    assert n == 0
    assert db.get_body_changes() == []


def test_failed_round_does_not_move_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    db.init_db()
    run_round([ann("N1", "旧正文")])
    # 失败轮次：没有任何数据写入，也不参与比较
    rid = db.create_round(START)
    db.finish_round_failed(rid, "http://site.example/detail/N1.html", "HTTP 500")
    # 恢复后新正文仍与最后一个成功轮次比较
    _r, n = run_round([ann("N1", "新正文")])
    assert n == 1
    (c,) = db.get_body_changes()
    assert c["old_body"] == "旧正文"
    assert c["new_round_id"] != rid


def test_page_numbers_of_same_directory_share_source(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "app.db"))
    db.init_db()
    r1, _ = run_round([ann("N1", "旧正文")], start_url="http://site.example/list.html?page=1")
    r2, n = run_round([ann("N1", "新正文")], start_url="http://site.example/list.html?page=2")
    assert n == 1
    (c,) = db.get_body_changes()
    assert (c["old_round_id"], c["new_round_id"]) == (r1, r2)


def test_legacy_snapshot_without_body_is_not_misreported(tmp_path, monkeypatch):
    """升级旧库：历史快照没有正文（NULL），升级后首轮不应把全部公告误报为变更。"""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
          finished_at TEXT, start_url TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
          list_pages INTEGER NOT NULL DEFAULT 0, detail_pages INTEGER NOT NULL DEFAULT 0,
          error_url TEXT, error_message TEXT);
        CREATE TABLE announcements (id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER NOT NULL,
          project_no TEXT NOT NULL, notice_no TEXT NOT NULL, notice_type TEXT NOT NULL,
          publish_time TEXT NOT NULL, deadline TEXT, target_notice_no TEXT, title TEXT,
          url TEXT NOT NULL);
        INSERT INTO rounds (started_at, start_url, status, finished_at)
          VALUES ('2026-11-01T09:00:00','http://site.example/list.html?page=1','success','2026-11-01T09:01:00');
        INSERT INTO announcements (round_id, project_no, notice_no, notice_type, publish_time,
          deadline, target_notice_no, title, url)
          VALUES (1,'P1','N1','tender','2026-11-01 09:00','2026-11-20 17:00',NULL,'旧公告',
                  'http://site.example/detail/N1.html');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    _r, n = run_round([ann("N1", "第一版正文")])
    assert n == 0
    assert db.get_body_changes() == []
    # 再改一次才产生变更，旧文即升级后第一轮的快照
    _r, n = run_round([ann("N1", "第二版正文")])
    assert n == 1
    (c,) = db.get_body_changes()
    assert (c["old_body"], c["new_body"]) == ("第一版正文", "第二版正文")
