"""端到端联调测试：真实示例站 + 采集端网页流程。

起两个真实的 uvicorn 服务（示例站、核对工具），通过 HTTP 驱动页面操作，
验证正文切换、待核对保留、已核对确认与失败场景。
"""

import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_DIR = os.path.join(ROOT, "sample_site")
APP_DIR = os.path.join(ROOT, "app")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.15)
    raise RuntimeError(f"服务未启动：{url}")


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    site_port, app_port = _free_port(), _free_port()
    db_path = str(tmp_path_factory.mktemp("db") / "app.db")
    env_common = {**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "")}
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(site_port)],
            cwd=SITE_DIR,
            env=env_common,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ),
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(app_port)],
            cwd=APP_DIR,
            env={
                **env_common,
                "DB_PATH": db_path,
                "DEFAULT_START_URL": f"http://127.0.0.1:{site_port}/list.html?page=1",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ),
    ]
    try:
        _wait_http(f"http://127.0.0.1:{site_port}/list.html?page=1")
        _wait_http(f"http://127.0.0.1:{app_port}/")
        yield f"http://127.0.0.1:{site_port}", f"http://127.0.0.1:{app_port}"
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


def scrape(app_url: str, site_url: str) -> httpx.Response:
    with httpx.Client(follow_redirects=False, timeout=30) as c:
        return c.post(f"{app_url}/scrape", data={"start_url": f"{site_url}/list.html?page=1"})


def get_html(url: str) -> str:
    with httpx.Client(follow_redirects=True, timeout=15) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text


def test_body_change_flow_end_to_end(servers):
    site, app = servers
    list_url = f"{site}/list.html?page=1"

    # 第 1 轮：原始正文，无变更
    r = scrape(app, site)
    assert r.status_code == 303
    assert "正文变更" not in urllib.parse.unquote(r.headers["location"])
    html = get_html(app)
    assert "尚未发现正文变更" in html

    # 切换 NT-2026-0001 的正文（不新发更正公告，字段与发布时间不变）
    get_html(f"{site}/admin/body?notice=NT-2026-0001&v=1")
    detail = get_html(f"{site}/detail/NT-2026-0001.html")
    assert "投标保证金金额调整为人民币伍万元整" in detail
    assert "2026-11-01 09:00" in detail  # 发布时间未变

    # 第 2 轮：检出 1 条变更，页面列出旧文/新文/两次轮次/来源链接
    r = scrape(app, site)
    assert "发现 1 条正文变更" in urllib.parse.unquote(r.headers["location"])
    html = get_html(app)
    assert "NT-2026-0001" in html
    assert "现就市政道路改造工程进行公开招标，欢迎符合条件的投标人参加投标。" in html
    assert "投标保证金金额调整为人民币伍万元整" in html
    assert "旧文来源" in html and "新文来源" in html
    assert "待核对" in html

    # 第 3 轮：正文不变，待核对记录仍在且不新增
    r = scrape(app, site)
    assert "正文变更" not in urllib.parse.unquote(r.headers["location"])
    html = get_html(app)
    assert html.count("待核对") >= 1
    assert "投标保证金金额调整为人民币捌万元整" not in html

    # 点击“已核对”仅确认该条
    with httpx.Client(follow_redirects=False, timeout=15) as c:
        change_ids = [line.strip() for line in html.splitlines() if "/changes/" in line]
        change_id = change_ids[0].split("/changes/")[1].split("/")[0]
        r = c.post(f"{app}/changes/{change_id}/ack")
    assert r.status_code == 303
    html = get_html(app)
    assert "已核对" in html
    assert "待核对 1 条" not in html

    # 已核对后再改正文：产生一条新的待核对记录
    get_html(f"{site}/admin/body?notice=NT-2026-0001&v=2")
    r = scrape(app, site)
    assert "发现 1 条正文变更" in urllib.parse.unquote(r.headers["location"])
    html = get_html(app)
    assert "投标保证金金额调整为人民币捌万元整" in html
    assert "待核对 1 条" in html


def pending_count(html: str) -> int:
    marker = "待核对 "
    if marker not in html:
        return 0
    tail = html.split(marker, 1)[1]
    return int(tail.split(" 条", 1)[0])


def test_failed_scrape_keeps_baseline_and_pending(servers):
    site, app = servers
    # 先建立基线并制造一条待核对变更（不依赖前一个测试的执行顺序）
    scrape(app, site)
    get_html(f"{site}/admin/body?notice=NT-2026-0003&v=1")
    scrape(app, site)
    html_before = get_html(app)
    assert "现就办公楼物业服务进行公开招标，服务期调整为两年。" in html_before
    before = pending_count(html_before)
    assert before >= 1

    # 让 NT-2026-0001 详情页失败（该公告也出现在列表里）
    get_html(f"{site}/admin/fail?on=1&notice=NT-2026-0001")
    r = scrape(app, site)
    loc = urllib.parse.unquote(r.headers["location"])
    assert "抓取失败" in loc and "NT-2026-0001" in loc

    html_after = get_html(app)
    # 失败轮次不改变基线、不清空待核对记录
    assert "现就办公楼物业服务进行公开招标，服务期调整为两年。" in html_after
    assert pending_count(html_after) == before
    # 轮次表里失败轮次可见
    assert "失败" in html_after

    # 关闭故障后重抓：正文未再变化，不新增同一变化
    get_html(f"{site}/admin/fail?on=0")
    r = scrape(app, site)
    assert "正文变更" not in urllib.parse.unquote(r.headers["location"])
    assert pending_count(get_html(app)) == before
