"""正文提取与空白合并测试。"""

from scraper import extract_body, normalize_ws, parse_detail

DETAIL_HTML = """<!DOCTYPE html>
<html><body>
<div class="notice" data-notice-type="tender">
  <h2>某项目招标公告</h2>
  <dl class="notice-fields">
    <dt>项目编号</dt><dd id="project-no">PRJ-2026-001</dd>
    <dt>公告编号</dt><dd id="notice-no">NT-2026-0001</dd>
    <dt>发布时间</dt><dd id="publish-time">2026-11-01 09:00</dd>
    <dt>截标时间</dt><dd id="deadline">2026-11-20 17:00</dd>
  </dl>
  <div class="notice-body">
    <p>现就 <strong>市政道路</strong> 改造工程进行公开招标，</p>
    <p>欢迎符合条件的投标人参加投标。<script>var x = 1;</script></p>
    <p style="display:none">这段不可见，不应采集</p>
    <p hidden>这段也隐藏</p>
  </div>
</div>
</body></html>"""

URL = "http://example.com/detail/NT-2026-0001.html"


def test_normalize_ws_collapses_all_whitespace():
    assert normalize_ws("  a\n\tb   c  ") == "a b c"
    assert normalize_ws("") == ""


def test_extract_body_merges_whitespace_and_skips_hidden():
    soup_body = DETAIL_HTML
    a = parse_detail(soup_body, URL)
    assert a.body == "现就 市政道路 改造工程进行公开招标， 欢迎符合条件的投标人参加投标。"


def test_extract_body_is_whitespace_insensitive():
    from bs4 import BeautifulSoup

    root = BeautifulSoup(
        '<div class="notice"><div class="notice-body">甲  <span>乙</span>\n\n丙</div></div>',
        "html.parser",
    )
    other = BeautifulSoup(
        '<div class="notice"><div class="notice-body">甲 <span>乙</span> 丙</div></div>',
        "html.parser",
    )
    assert extract_body(root) == extract_body(other) == "甲 乙 丙"


def test_extract_body_without_body_element_is_empty():
    from bs4 import BeautifulSoup

    root = BeautifulSoup('<div class="notice"><h1>无正文</h1></div>', "html.parser")
    assert extract_body(root) == ""


def test_block_elements_do_not_glue_paragraphs():
    from bs4 import BeautifulSoup

    root = BeautifulSoup(
        '<div class="notice"><div class="notice-body"><p>第一段</p><p>第二段</p></div></div>',
        "html.parser",
    )
    assert extract_body(root) == "第一段 第二段"


def test_hidden_body_container_is_empty():
    from bs4 import BeautifulSoup

    root = BeautifulSoup(
        '<div class="notice"><div class="notice-body" style="display: none">不应采集</div></div>',
        "html.parser",
    )
    assert extract_body(root) == ""
