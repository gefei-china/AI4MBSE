# -*- coding: utf-8 -*-
"""能力中心 / 能力账本 的链路闭环（2026-10-29）。

## 背景

复盘发现"能力中心"与"能力账本"**不是两个页面**（同一页的两个 tab，
tabbar 里并排可见），所以此前"用户在能力中心看不到账本"的说法**不准确**，
已在结论里更正。真正的三处断点是：

| # | 断点 | 本质 |
|---|---|---|
| 1 | 探针污染无人过滤 | 见 `test_probe_filter_consumers.py` |
| 2 | 两个 tab 各用一套数据源、**彼此无连路** | 看到卡片无法知道"它在账本里被判成什么" |
| 3 | `/api/capabilities/registry` **前端 0 引用** | 后端支持搜索/分页，界面只能看 ledger 的 top500 |

本文件锁定 2 与 3 的修复，以及一个**语义一致性**断言：
前端下拉框里的 kind 取值不得超出后端 `_KINDS`（否则选了必空）。
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel: str) -> str:
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ══════════════════════════════════════════════════════════════════════
# 断点 3：孤儿端点必须被消费

def test_registry_endpoint_is_consumed_by_frontend():
    """`/api/capabilities/registry` 必须在**前端有真实调用点**。

    为什么单独一条：这个端点建好了、返回结构完整，但前端引用 0 处 ——
    属于"生产者做了、消费者没接"。判据落在"有没有 fetch 调用"，
    不是"文件里出现过这个名字"（注释里出现不算）。
    """
    src = _src("static/js/mods/50-capregistry.js")
    calls = re.findall(r"fetch\(\s*['\"]/api/capabilities/registry", src)
    assert calls, "★ /api/capabilities/registry 仍无前端 fetch 调用（孤儿端点）"


def test_registry_ui_supports_search_and_paging():
    """原始注册表视图必须支持 搜索 / 类型 / 状态 / 分页 —— 这四点正是它相对 ledger 的价值。"""
    src = _src("static/js/mods/50-capregistry.js")
    for token, why in (
        ("capabilities/registry", "端点"),
        ("qs.set('q'", "按名称搜索"),
        ("qs.set('kind'", "按类型筛选"),
        ("qs.set('status'", "按状态筛选"),
        ("offset", "分页参数"),
    ):
        assert token in src, f"★ 原始注册表视图缺少{why}（{token}）"


# ══════════════════════════════════════════════════════════════════════
# 断点 2：两个 tab 之间的连路

def test_capability_center_links_to_ledger():
    """能力中心各类型页必须有通往账本的入口（带 KPI），不是两个孤岛。

    ⚠️ 判据要分三层，别只查"字符串出现过"：
       ① `capJumpLedger` **函数定义**存在（`function capJumpLedger(`）；
       ② 有 `onclick="capJumpLedger()"` 的**绑定点**（否则函数是死的）；
       ③ KPI 真去读账本端点。
       第一版只做 `"capJumpLedger" in src` —— 把函数改名成
       `capJumpLedgerDisabled` 后，onclick 里的旧名字仍让断言通过（假绿一轮）。
    """
    src = _src("static/js/mods/36-capability.js")
    assert re.search(r"function capJumpLedger\s*\(", src), (
        "★ 能力中心缺少 capJumpLedger 的**函数定义**")
    assert 'onclick="capJumpLedger()"' in src, (
        "★ 有函数但没有绑定点（按钮点不动）")
    assert re.search(r"(async\s+)?function capLoadLedgerKpi\s*\(", src), (
        "★ 能力中心缺少账本 KPI 的**函数定义**")
    assert "capabilities/ledger" in src, (
        "★ 能力中心未读取账本 KPI（互链只是空按钮）")


def test_capability_center_kpi_shows_verdicts():
    """KPI 必须展示判定分布（在用/未用），而不是只显示一个总数。"""
    src = _src("static/js/mods/36-capability.js")
    assert "matrix" in src and "in_use" in src, "★ KPI 未使用账本矩阵（matrix.in_use）"
    assert "unused" in src, "★ KPI 未展示未使用数"


# ══════════════════════════════════════════════════════════════════════
# 顺手修的真 bug：搜索输入框失焦

def test_search_does_not_rebuild_whole_panel():
    """`caprSetQ` 不得再调 `loadCapRegistry()`。

    原实现：注释明写"只重画表格，不整页重载（重建整页会让输入框失焦）"，
    代码却直接调 `loadCapRegistry()` ⇒ 每按一个键失焦 + 并发两次 fetch。

    ⚠️ 断言前必须**剥离注释**：修复说明里天然会提到被删掉的那行
       （"原实现直接调了 `loadCapRegistry()`"），第一版没剥注释就被自己写的
       说明文字误判成"仍在调用"（假红一轮）。凡"不得出现某代码"的断言，
       都要先去掉注释，否则注释会变成检测器的靶子。
    """
    src = _src("static/js/mods/50-capregistry.js")
    m = re.search(r"function caprSetQ\(v\)\s*\{(.*?)\n\}", src, re.S)
    assert m, "找不到 caprSetQ（结构变了，需同步本用例）"
    body = m.group(1)
    # 去掉行注释与块注释后再判"是否真的在调用"
    code = re.sub(r"//[^\n]*", "", body)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    assert "loadCapRegistry(" not in code, (
        "★ caprSetQ 仍在整页重载（输入框会失焦、并发请求）")
    assert "capr-ledger-tbody" in code, "★ caprSetQ 未改为只重画 tbody"


# ══════════════════════════════════════════════════════════════════════
# 前后端 kind 取值一致性（选了必空的隐患）

def _kind_options_of_select(src: str, sel_id: str) -> set:
    """只取**指定 select 内**的 option 值。

    ⚠️ 不能用"全文件 option + 中文关键词"去匹配：页面里的 **tab 选择器**
       也长这样（`<option value="market">插件市场`、`<option value="mcp">MCP`），
       第一版就因此误判出 `market`/`mcp` 两个"越界 kind"（假红一轮）。
       跨语言一致性断言必须先把**作用域**限定到那个控件上。
    """
    m = re.search(r'<select id="%s"[^>]*>(.*?)</select>' % re.escape(sel_id),
                  src, re.S)
    if not m:
        return set()
    return set(re.findall(r'<option value="([a-z_]+)"', m.group(1)))


def test_frontend_kind_options_subset_of_backend():
    """前端下拉框的 kind 取值必须是后端 `_KINDS` 的子集。

    为什么必须有这条：kind 是**跨语言各写一份**的枚举 ——
    后端 `routers/capabilities.py::_KINDS`、前端两处 `<select>` 各一份。
    前端多写一个值 ⇒ 用户选了必然空表，而界面只会显示"无匹配行"，
    看起来像"这类能力真的没有"。
    """
    from routers.capabilities import _KINDS

    backend = set(_KINDS)
    pairs = [
        ("static/index.html", "capr-kind"),                       # 账本筛选
        ("static/js/mods/50-capregistry.js", "capr-raw-kind"),    # 原始注册表筛选
    ]
    checked = 0
    for rel, sel_id in pairs:
        opts = _kind_options_of_select(_src(rel), sel_id)
        if not opts:
            continue          # 该控件不在本文件（结构变动），不做假红
        checked += 1
        extra = opts - backend
        assert not extra, (
            f"★ {rel} 的 <select id={sel_id}> 选项超出后端 _KINDS：{sorted(extra)}")
    assert checked >= 1, "两处 kind 下拉都没找到 —— 结构变了，需同步本用例"


# ══════════════════════════════════════════════════════════════════════
# 真实 HTTP 端到端（不是只看源码）

def test_template_html_comments_have_no_backtick():
    """JS 模板字符串内部的 HTML 注释**不得含反引号**。

    这是本轮真实踩到的运行时崩溃（2026-10-29）：
      在 `body.innerHTML = \\`...\\`` 的模板里写了 HTML 注释，
      注释里给端点名加了反引号 ⇒ **反引号提前终止了模板字符串**，
      后面的裸词 `ledger` 变成标识符 ⇒ 页面报
      「加载失败：ledger is not defined」，账本整块渲染不出来。

    为什么单独立一条：`node --check` **通过**（语法凑巧成立 —— 字符串结束后
    `/ledger` 仍能被解析），只有真浏览器跑起来才炸。基线版本渲染正常、
    带我改动的版本崩，靠 A/B 才判定是"我引入的"。
    静态检查抓不住的这类问题，必须有一条能抓住它的哨兵。

    判据：函数体内的 HTML 注释（在 JS 里只可能出现在模板字符串中）
    不得含反引号、也不得含 `${`（后者会被求值）。
    """
    src = _src("static/js/mods/50-capregistry.js")
    for fname in ("loadCapRegistry", "caprRawLoad"):
        m = re.search(r"(async\s+)?function %s\s*\([^)]*\)\s*\{(.*?)\n\}"
                      % re.escape(fname), src, re.S)
        if not m:
            continue
        body = m.group(2)
        for cmt in re.findall(r"<!--.*?-->", body, re.S):
            assert "`" not in cmt, (
                f"★ {fname} 的模板内 HTML 注释含**反引号** —— "
                f"会提前终止模板字符串并造成运行时 ReferenceError：{cmt[:60]!r}")
            assert "${" not in cmt, (
                f"★ {fname} 的模板内 HTML 注释含 `${{` —— 会被当作插值求值：{cmt[:60]!r}")


def test_registry_endpoint_http_roundtrip(client):
    """走真实 HTTP：结构完整 + 分页/搜索参数被接受。"""
    r = client.get("/api/capabilities/registry?limit=5&offset=0",
                   headers={"X-User-Id": "tester"})
    assert r.status_code == 200, r.text
    d = r.json()
    for k in ("total", "limit", "offset", "rows", "by_kind", "hint"):
        assert k in d, f"返回缺少字段 {k}"
    assert isinstance(d["rows"], list)
    assert len(d["rows"]) <= 5, "limit 未生效"
    # 搜索参数被接受（不报错即可；库小时结果可能为空）
    r2 = client.get("/api/capabilities/registry?q=zzz_no_such_capability",
                    headers={"X-User-Id": "tester"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["total"] == 0, "搜索参数未生效（应无匹配）"


def test_ledger_endpoint_exposes_probe_caliber(client):
    """账本的 `stats.probe_filtered` 必须出现在**真实响应**里（口径可核对）。"""
    r = client.get("/api/capabilities/ledger?days=30", headers={"X-User-Id": "tester"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["stats"].get("probe_filtered") is True, (
        "★ 账本响应未声明探针已过滤 —— 读账本的人无法核对口径")
    assert "matrix" in d and "orphan_calls" in d
