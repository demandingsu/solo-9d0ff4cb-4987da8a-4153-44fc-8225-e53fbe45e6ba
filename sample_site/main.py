"""示例公告站：模拟公开招标公告网站。

页面结构（与采集端解析约定一致）：
- 列表页 /list.html?page=N
    - 公告链接：ul.notice-list a[href]
    - 分页导航：nav.pagination a[href]
- 详情页 /detail/<公告编号>.html
    - 容器 div.notice，data-notice-type = tender|correction|cancellation
    - #project-no 项目编号、#notice-no 公告编号、#publish-time 发布时间
    - #deadline 截标时间（如有）、#target-notice 更正/撤销所指公告编号（如有）
    - .notice-body 正文（可在此页切换，模拟“未另发更正却改动原公告正文”）

正文切换：GET /admin/body?notice=NT-2026-0001&v=2 把该公告正文换成另一版本
（v 省略时在版本间轮换），用于演示正文变更核查；字段与发布时间保持不变。

故障注入：GET /admin/fail?on=1&notice=NT-2026-0004 开启后，该公告详情页返回 500，
用于演示采集端“任一页面失败则保留上一轮结果与待核对变更”的行为；on=0 关闭。
"""

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from data import (
    ANNOUNCEMENTS,
    BODY_VARIANTS,
    PAGE_SIZE,
    TYPE_LABELS,
    body_version,
    get_by_notice_no,
    sorted_announcements,
)

app = FastAPI(title="示例公告站")
templates = Jinja2Templates(directory="templates")

# 故障注入状态：开启后 FAIL_NOTICE 对应的详情页返回 500
FAIL_STATE = {"on": False, "notice": "NT-2026-0004"}
# 正文版本状态：{公告编号: 版本号}，默认版本 1（原始正文）
BODY_STATE: dict[str, int] = {}


def current_version(notice_no: str) -> int:
    """当前正文版本下标（未切换时为 0）。"""
    return body_version(notice_no, BODY_STATE.get(notice_no, 0))


def current_body(a: dict) -> str:
    """当前正文：有切换版本时用切换后的，否则用数据里的原始正文。"""
    version = BODY_STATE.get(a["notice_no"])
    if version is None:
        return a["body"]
    variants = BODY_VARIANTS.get(a["notice_no"])
    if not variants:
        return a["body"]
    return variants[version % len(variants)]


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse("/list.html?page=1")


@app.get("/list.html", response_class=HTMLResponse)
def list_page(request: Request, page: int = 1):
    items_all = sorted_announcements()
    total_pages = max(1, (len(items_all) + PAGE_SIZE - 1) // PAGE_SIZE)
    if page < 1 or page > total_pages:
        return HTMLResponse("<h1>404 页码不存在</h1>", status_code=404)
    start = (page - 1) * PAGE_SIZE
    items = []
    for a in items_all[start : start + PAGE_SIZE]:
        items.append(
            {
                **a,
                "type_label": TYPE_LABELS[a["type"]],
                "body_version": current_version(a["notice_no"]),
                "changed": current_version(a["notice_no"]) != 0,
            }
        )
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "items": items,
            "page": page,
            "total_pages": total_pages,
            "pages": list(range(1, total_pages + 1)),
            "fail_on": FAIL_STATE["on"],
            "fail_notice": FAIL_STATE["notice"],
        },
    )


@app.get("/detail/{notice_no}.html", response_class=HTMLResponse)
def detail_page(request: Request, notice_no: str):
    if FAIL_STATE["on"] and notice_no == FAIL_STATE["notice"]:
        return HTMLResponse(
            f"<h1>500 Internal Server Error</h1><p>故障注入已开启：{notice_no} 暂时不可用。</p>",
            status_code=500,
        )
    a = get_by_notice_no(notice_no)
    if a is None:
        return HTMLResponse(f"<h1>404 公告不存在：{notice_no}</h1>", status_code=404)
    variants = BODY_VARIANTS.get(notice_no, [])
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "a": {**a, "body": current_body(a), "type_label": TYPE_LABELS[a["type"]]},
            "body_version": current_version(notice_no),
            "has_variants": bool(variants),
        },
    )


@app.get("/admin/fail", response_class=HTMLResponse)
def admin_fail(on: int = 0, notice: str = ""):
    FAIL_STATE["on"] = bool(on)
    if notice.strip():
        FAIL_STATE["notice"] = notice.strip()
    state = "已开启" if FAIL_STATE["on"] else "已关闭"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>故障注入</title></head>
<body>
<h1>故障注入{state}</h1>
<p>当前故障公告：<code>{FAIL_STATE["notice"]}</code>（开启时其详情页返回 500，用于验证采集端失败处理）。</p>
<p><a href="/admin/fail?on=1&notice={FAIL_STATE["notice"]}">开启</a> · <a href="/admin/fail?on=0">关闭</a> · <a href="/list.html?page=1">返回列表</a></p>
<p>可指定公告编号，例如 <a href="/admin/fail?on=1&notice=NT-2026-0001">让 NT-2026-0001 失败</a>。</p>
</body></html>"""


@app.get("/admin/body", response_class=HTMLResponse)
def admin_body(notice: str = "", v: int | None = None, reset: int = 0):
    notice = notice.strip()
    if not notice:
        rows = "".join(f"<li><code>{n}</code>（{len(BODY_VARIANTS[n])} 个版本）</li>" for n in BODY_VARIANTS)
        return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>正文切换</title></head>
<body>
<h1>正文切换</h1>
<p>可切换正文的公告：</p><ul>{rows}</ul>
<p>用法：<code>/admin/body?notice=NT-2026-0001&amp;v=2</code> 切到第 2 版；
<code>?notice=NT-2026-0001</code> 在版本间轮换；<code>?notice=NT-2026-0001&amp;reset=1</code> 恢复原始正文。</p>
<p><a href="/admin/status">查看当前状态</a> · <a href="/list.html?page=1">返回列表</a></p>
</body></html>"""

    a = get_by_notice_no(notice)
    if a is None or not BODY_VARIANTS.get(notice):
        return HTMLResponse(f"<h1>404 该公告没有可切换的正文：{notice}</h1>", status_code=404)
    if reset:
        BODY_STATE.pop(notice, None)
    elif v is not None and v >= 0:
        BODY_STATE[notice] = v
    else:
        BODY_STATE[notice] = (BODY_STATE.get(notice, 0) + 1) % len(BODY_VARIANTS[notice])
    version = current_version(notice)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>正文切换</title></head>
<body>
<h1>正文已切换</h1>
<p><code>{notice}</code> 当前为第 {version + 1} 版正文。</p>
<p><a href="/detail/{notice}.html">查看详情</a> · <a href="/admin/body?notice={notice}&amp;v={(version + 1) % len(BODY_VARIANTS[notice])}">切到下一版</a>
 · <a href="/admin/body?notice={notice}&amp;reset=1">恢复原始正文</a> · <a href="/list.html?page=1">返回列表</a></p>
</body></html>"""


@app.get("/admin/status")
def admin_status():
    return {
        "fail_on": FAIL_STATE["on"],
        "fail_notice": FAIL_STATE["notice"],
        "announcements": len(ANNOUNCEMENTS),
        "body_versions": {
            n: current_version(n) for n in BODY_VARIANTS
        },
    }
