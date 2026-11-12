"""示例站管理端点测试：正文切换与故障注入。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "sample_site"))

import main as site  # noqa: E402
from data import BODY_VARIANTS, body_version  # noqa: E402

SITE_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_site")


@pytest.fixture(autouse=True)
def reset_state(monkeypatch):
    monkeypatch.chdir(SITE_DIR)  # 模板目录是相对路径
    site.BODY_STATE.clear()
    site.FAIL_STATE.update({"on": False, "notice": "NT-2026-0004"})
    yield


def test_body_version_normalizes_against_variants():
    assert body_version("NT-2026-0001", 0) == 0
    assert body_version("NT-2026-0001", 1) == 1
    assert body_version("NT-2026-0001", 3) == 0
    assert body_version("NT-2026-0002", 5) == 0  # 无可切换正文的公告


def test_body_switch_changes_only_body_text():
    from fastapi.testclient import TestClient

    client = TestClient(site.app)
    before = client.get("/detail/NT-2026-0001.html").text
    assert "投标保证金金额调整为人民币伍万元整" not in before
    assert "2026-11-01 09:00" in before  # 发布时间

    client.get("/admin/body?notice=NT-2026-0001&v=1")
    after = client.get("/detail/NT-2026-0001.html").text
    assert "投标保证金金额调整为人民币伍万元整" in after
    assert "2026-11-01 09:00" in after  # 发布时间不变
    assert "当前第 2 版" in after

    client.get("/admin/body?notice=NT-2026-0001&reset=1")
    assert "投标保证金金额调整为人民币伍万元整" not in client.get("/detail/NT-2026-0001.html").text


def test_body_switch_cycles_and_rejects_unknown_notice():
    from fastapi.testclient import TestClient

    client = TestClient(site.app)
    r = client.get("/admin/body?notice=NT-2026-0003")
    assert r.status_code == 200
    assert "服务期调整为两年" in client.get("/detail/NT-2026-0003.html").text
    r = client.get("/admin/body?notice=NT-2026-0003")
    assert "服务期调整为两年" not in client.get("/detail/NT-2026-0003.html").text
    assert client.get("/admin/body?notice=NT-2026-0002").status_code == 404
    assert set(BODY_VARIANTS) == {"NT-2026-0001", "NT-2026-0003", "NT-2026-0006"}


def test_fail_injection_can_target_any_notice():
    from fastapi.testclient import TestClient

    client = TestClient(site.app)
    client.get("/admin/fail?on=1&notice=NT-2026-0001")
    assert client.get("/detail/NT-2026-0001.html").status_code == 500
    assert client.get("/detail/NT-2026-0004.html").status_code == 200

    client.get("/admin/fail?on=1&notice=NT-2026-0004")
    assert client.get("/detail/NT-2026-0004.html").status_code == 500
    assert client.get("/detail/NT-2026-0001.html").status_code == 200

    client.get("/admin/fail?on=0")
    assert client.get("/detail/NT-2026-0001.html").status_code == 200
    assert client.get("/detail/NT-2026-0004.html").status_code == 200


def test_failed_detail_page_leaves_rest_of_site_intact():
    from fastapi.testclient import TestClient

    client = TestClient(site.app)
    client.get("/admin/fail?on=1&notice=NT-2026-0001")
    page1 = client.get("/list.html?page=1")
    assert page1.status_code == 200
    assert "【撤销公告】医疗设备采购项目撤销公告" in page1.text
    # NT-2026-0001 按发布时间倒序排在最后一页，列表链接不受详情故障影响
    page3 = client.get("/list.html?page=3")
    assert "【招标公告】市政道路改造工程施工招标" in page3.text
