# 截标时间核对工具

面向采购专员的网页工具：从公开招标公告站抓取公告，按项目串起公告链，
自动应用更正与撤销，给出每个项目的**最终截标时间 / 停办状态**及**依据链接**；
同时核查公告站**未另发更正却改动原公告正文**的情况。

仓库包含两个服务：

| 服务 | 目录 | 说明 |
| --- | --- | --- |
| 核对工具 | `app/` | FastAPI 网页：输入列表网址 → 抓取同源分页与详情 → 分析展示，SQLite 保存采集轮次 |
| 示例公告站 | `sample_site/` | FastAPI 模拟公告站：3 页列表分页 + 11 条公告详情（含更正、撤销），支持正文切换与故障注入 |

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 核对工具：http://localhost:8000/ （打开后点击 **开始抓取**，默认已填入示例站列表网址）
- 示例公告站：http://localhost:8080/list.html?page=1

## 功能说明

1. **抓取**：输入与示例站同结构的列表网址，点击抓取。工具只跟随与起始网址
   **同源**的分页链接（`nav.pagination`）与详情链接（`ul.notice-list`），
   逐个读取详情页的：项目编号、公告编号、公告类型、发布时间、截标时间、
   更正/撤销所指公告编号。
2. **分析**：同一项目编号的公告按发布时间串链——
   - 招标公告确立截标时间；
   - 更正公告更新截标时间（链上记录 `旧值 → 新值`）；
   - 撤销公告将项目标记为 **停办**。
   页面展示每个项目的最终状态、当前截标时间，以及每条公告的来源链接（依据）。
3. **轮次持久化（SQLite）**：每次抓取是一个轮次，公告快照挂在轮次下，
   保存于 SQLite（容器卷 `app-data`，`/data/app.db`）。
   - **任一页面失败**：轮次标记失败并记录出错网址与原因，页面红色横幅提示，
     项目结果仍展示上一轮成功数据；
   - **重抓不叠加**：公告只在整轮全部成功后于单事务内写入，失败轮次不留
     半截数据；页面始终只展示最近一次成功轮次的结果。

## 正文变更核查

公告站有时不另发更正公告，而是直接改动原公告的正文。工具在每次成功抓取时，
按**公告编号**比较同一目录（同源同路径、忽略分页参数）**相邻两次成功轮次**的
`div.notice-body` 正文：

- 正文取可见文字（跳过 `script`、`hidden`、`display:none`、`visibility:hidden`），
  再把连续空白合并为单个空格；只有合并空白后仍不同才记录变更；
- **新公告**（上一成功轮次没有该编号）不算变更；
- 页面“正文变更核查”区列出**旧文、新文、比较的两个轮次（含完成时间）**，以及
  两轮各自的**来源链接**；
- 待核对记录**独立于快照保留**：后续抓取若正文不再变化，记录继续待核对；
  同一次变化不会因重复抓取而重复生成；
- 点击“**已核对**”只确认**该条**记录；若日后正文再次改动，会与最后核对的
  内容比较并生成**新记录**（仍为待核对）；
- **成功快照与正文变更记录在同一事务内提交**（`body_changes` 表）；
  任一页面失败的轮次不写入任何数据，因此**不改对照基线、也不影响待核对记录**。

## 演示正文变更与失败场景

```bash
# 1) 基线：在 http://localhost:8000/ 点击“开始抓取”（第 1 轮，无变更）
# 2) 切换示例站 NT-2026-0001 的正文（不新发更正，发布时间不变）
curl "http://localhost:8080/admin/body?notice=NT-2026-0001&v=1"
# 3) 再次“开始抓取” → 第 2 轮检出 1 条正文变更（旧文 / 新文 / 两轮 / 来源）
# 4) 连点两次“开始抓取” → 正文未变，不新增记录，待核对仍在
# 5) 点该条的“已核对” → 仅此条变为已核对
# 6) 再切下一版后抓取 → 生成新的待核对记录
curl "http://localhost:8080/admin/body?notice=NT-2026-0001&v=2"

# 失败场景：让 NT-2026-0001 详情页返回 500，再抓取
curl "http://localhost:8080/admin/fail?on=1&notice=NT-2026-0001"
#   → 轮次失败并报出该网址；上一轮结果与待核对变更原样保留，基线不变
curl "http://localhost:8080/admin/fail?on=0"
#   → 恢复后重抓：正文未再变化，不新增同一变化

# 其他正文切换入口：/admin/body（列出可切换公告）、
# ?notice=NT-2026-0001（版本轮换）、&reset=1（恢复原始正文）
# 故障注入也可只指定公告编号：/admin/fail?on=1&notice=NT-2026-0003
```

## 页面结构约定（解析契约）

输入网址须与示例站同结构：

- 列表页 `list.html?page=N`：公告链接在 `ul.notice-list a`，分页链接在 `nav.pagination a`；
- 详情页 `detail/<公告编号>.html`：容器 `div.notice[data-notice-type]`
  （`tender`/`correction`/`cancellation`），字段元素
  `#project-no`、`#notice-no`、`#publish-time`、`#deadline`（如有）、
  `#target-notice`（更正/撤销必填）；正文 `div.notice-body`（变更核查用）。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/app.db`（容器） | SQLite 文件路径 |
| `DEFAULT_START_URL` | `http://sample-site:8000/list.html?page=1` | 抓取表单预填的列表网址 |
| `URL_REWRITE_FROM` / `URL_REWRITE_TO` | 见 compose | 把结果页中的内部网址改写为浏览器可访问地址 |

## 本地开发与测试

```bash
pip install -r app/requirements.txt -r sample_site/requirements.txt pytest
python -m pytest tests/            # 单元测试：公告链分析 + 详情页/正文解析 + 变更语义 + 端到端网页流程
# 手动联调：
cd sample_site && uvicorn main:app --port 8080 &
cd app && DB_PATH=/tmp/app.db DEFAULT_START_URL=http://localhost:8080/list.html?page=1 \
  uvicorn main:app --port 8000
```
