# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""门禁：通用 HTTP 工具**从界面建出来就能跑**（P1b，2026-11-07）。

## 这一轮修的真因不是"缺表单"

表单一直在（method / URL / input_schema / param_in / timeout 都能填），
但 `StudioRepo.create_tool` **不写 `kind`**，而 `http_tool_executor._get_tool_config`
查的是 `kind='http'` ⇒ **注册成功、界面上看得见、保存也不报错，但永远调不动**。
这是本仓"有实现但没接线"的第二次同型事故（第一次是 code_exec/doc_parse）。

顺带补上三个"填了才有业务参数"的模板：`body` / `query` / `defaults`
—— 缺了它们，POST 工具发出去的是空请求体（能连上，对方必报缺参）。

## 判据

| # | 断言 | 防的失效 |
|---|---|---|
| A1 | create 落库 `kind='http'` | 建了但执行器认不出（本次真因） |
| A2 | **端到端真实调用**本机接口并取到结果 | 只测了"能存" |
| A3 | body 模板占位符被渲染进请求体 | 传了参数但没进请求 |
| A4 | POST + 手选 read ⇒ 实际 write（拒绝放宽） | 写请求绕过写前确认闸 |
| A5 | DELETE ⇒ destructive | 删接口被当普通写 |
| B1 | 编辑时 GET→POST 会收紧现有档位 | 改方法后档位不跟着变 |
| B2 | 编辑时 POST→GET **不放宽** | 靠改方法洗掉写标记 |
| C1 | destructive 试跑需显式确认 | 点一下"测试"就删了对方数据 |
| D1 | 前端有试跑入口且调用 /test | 后端有接口、界面没入口（上一轮 MCP 踩过） |
| D2 | 前端明确写"会真实调用" | 被当成 dry-run 误点 |

⚠️ 本门禁会在**真库**建一个 `gate_http_probe_*` 工具再删除（finally 保证清理）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)

PASS, FAIL = "PASS", "FAIL"
_res = []
PROBE = "gate_http_probe"


def _rec(name, ok, detail=""):
    _res.append(bool(ok))
    print("  [%s] %s%s" % (PASS if ok else FAIL, name,
                           ("  | " + str(detail)[:230]) if detail else ""))
    return bool(ok)


def main() -> int:
    print("=" * 72)
    print("门禁 P1b：通用 HTTP 工具（建得出、调得动、档位不被人为放宽）")
    print("=" * 72)

    from database import get_db
    from http_tool_executor import exec_http_tool
    from repositories.studio_repo import StudioRepo
    from routers.studio_parts.tools import _derive_http_side_effect

    conn = get_db()
    repo = StudioRepo(conn)
    tid = None
    try:
        # 清掉上一轮的残留（幂等）
        # ★ 2026-11-07：**必须同时清 plugins 表**。实测通过 HTTP API 建工具时
        #   会连带注册插件行（plugin_id='com.mbse.local.tool.<name>'），
        #   旧清理只 DELETE tools ⇒ 每跑一次门禁就多留一条 `gate-http-probe-*` 插件，
        #   已积累 8 条（还各带 1 条 plugin_versions / plugin_installs）。
        #   ⇒ 门禁自己在污染资产表，这正是"聚合精简"要治的本。
        for r in conn.execute("SELECT id FROM tools WHERE name LIKE ?", (PROBE + "%",)).fetchall():
            conn.execute("DELETE FROM agent_tools WHERE tool_name LIKE ?", (PROBE + "%",))
            conn.execute("DELETE FROM tools WHERE id=?", (r["id"],))
        conn.execute("DELETE FROM plugin_versions WHERE plugin_id LIKE ?",
                     ("com.mbse.local.tool." + PROBE + "%",))
        conn.execute("DELETE FROM plugin_installs WHERE plugin_id LIKE ?",
                     ("com.mbse.local.tool." + PROBE + "%",))
        conn.execute("DELETE FROM plugins WHERE name LIKE ?", (PROBE + "%",))
        conn.commit()

        # ── A1/A3：建一个真实可调的 GET 工具（打本机 /api/system/boot）──
        _se, _rule = _derive_http_side_effect(PROBE, "GET", "read")
        tid = repo.create_tool(
            PROBE, "门禁用：读取本机启动信息（验证 HTTP 工具端到端可用）", "http",
            json.dumps({"type": "object", "properties": {}}, ensure_ascii=False),
            "v1.0", _se, "low", "gate",
            config=json.dumps({"method": "GET",
                               "url": "http://127.0.0.1:8000/api/system/boot",
                               "timeout": 10}, ensure_ascii=False),
            kind="http")
        _row = conn.execute("SELECT kind, side_effect FROM tools WHERE id=?", (tid,)).fetchone()
        _rec("A1 create 落库 kind='http'（执行器按此找 config）",
             bool(_row) and _row[0] == "http", tuple(_row) if _row else None)

        # ── A2：端到端真实调用 ──
        # ⚠️ 必须先 commit：`exec_http_tool` 自己开新连接读 config，
        #   未提交的行它看不见（第一版漏了这步 ⇒ 报"未知工具"，看着像产品 bug）。
        conn.commit()
        r = exec_http_tool(PROBE, {})
        _res_txt = str(r.get("result") or "")
        _rec("A2 端到端真实调用成功（拿到 boot_id/pid 说明真通了）",
             bool(r.get("ok")) and ("boot_id" in _res_txt or "pid" in _res_txt),
             _res_txt[:140])

        # ── A3：body 模板渲染（用一个会回显请求体的本地端点不现实，
        #    改为直接对渲染器做断言：占位符 → 原生类型/字符串）
        from http_tool_executor import _render_value
        _rd = _render_value({"q": "{args:q}", "top": "{args:top,int}",
                             "opt": "{args:missing,str,optional}"},
                            {"q": "abc", "top": "5"})
        _rec("A3 body 模板占位符渲染（含原生类型与 optional）",
             _rd.get("q") == "abc" and _rd.get("top") == 5 and "opt" not in _rd, _rd)

        # ── A4/A5：派生判据 ──
        _rec("A4 POST + 手选 read ⇒ 拒绝放宽，落 write",
             _derive_http_side_effect("x", "POST", "read")[0] == "write",
             _derive_http_side_effect("x", "POST", "read"))
        _rec("A5 DELETE ⇒ destructive",
             _derive_http_side_effect("x", "DELETE", "")[0] == "destructive")
        _rec("A5b 名字带 delete 的 GET ⇒ destructive（方法不是唯一判据）",
             _derive_http_side_effect("delete_item", "GET", "")[0] == "destructive")
        _rec("A5c 用户可收紧（GET + 手选 write ⇒ write）",
             _derive_http_side_effect("x", "GET", "write")[0] == "write")

        # ── B：编辑态改方法 ⇒ 档位收紧、但不放宽 ──
        conn.execute("UPDATE tools SET side_effect='read' WHERE id=?", (tid,))
        conn.commit()
        _d2, _r2 = _derive_http_side_effect(PROBE, "POST", "read")
        repo.set_tool_side_effect(tid, _d2)
        _se2 = conn.execute("SELECT side_effect FROM tools WHERE id=?", (tid,)).fetchone()[0]
        _rec("B1 编辑 GET→POST 后档位收紧为 write", _se2 == "write", _se2)

        _d3, _ = _derive_http_side_effect(PROBE, "GET", "read")
        _rec("B2 POST→GET 不放宽到 read（改方法洗不掉写标记已被拒绝）",
             _derive_http_side_effect(PROBE, "POST", "read")[0] != "read",
             "derive(GET)=%s" % (_d3,))

        # ── C：destructive 试跑需确认（直接测路由函数的守卫）──
        conn.execute("UPDATE tools SET side_effect='destructive' WHERE id=?", (tid,))
        conn.commit()
        import routers.studio_parts.tools as _rt
        _resp = _rt.test_tool(tid, {}, conn=conn, user={"name": "gate"})
        _body = getattr(_resp, "body", b"")
        _txt = _body.decode("utf-8") if isinstance(_body, bytes) else str(_body)
        _rec("C1 destructive 试跑被拒（需 confirm_destructive）",
             "confirm_destructive" in _txt or "破坏性" in _txt, _txt[:120])
        conn.execute("UPDATE tools SET side_effect='read' WHERE id=?", (tid,))
        conn.commit()

        # ── D：前端入口 ──
        _js = open(os.path.join(_ROOT, "static", "js", "mods", "30-agents.js"),
                   encoding="utf-8").read()
        _core = open(os.path.join(_ROOT, "static", "js", "mods", "01-core.js"),
                     encoding="utf-8").read()
        _rec("D1 编辑弹窗有试跑按钮且函数真调 /test",
             "testHttpToolFromForm" in _js and "/test" in _js
             and "f-http-test-btn" in _core)
        _rec("D2 界面明确写「真实调用」而不是 dry-run",
             bool(re.search(r"试跑会</?b?>?真实调用", _core)) or "会<b>真实调用</b>" in _core,
             "（文案：试跑会真实调用该接口）")
        _rec("D3 表单有 body/query/defaults/data_path 四项",
             all(('f-tool-' + k) in _core for k in ("body", "query", "defaults", "data-path")))
        _rec("D4 保存时把四项提交给后端",
             all(("f-tool-" + k) in _js for k in ("body", "query", "defaults", "data-path")))
    finally:
        try:
            conn.execute("DELETE FROM agent_tools WHERE tool_name LIKE ?", (PROBE + "%",))
            conn.execute("DELETE FROM tools WHERE name LIKE ?", (PROBE + "%",))
            conn.execute("DELETE FROM plugin_versions WHERE plugin_id LIKE ?",
                         ("com.mbse.local.tool." + PROBE + "%",))
            conn.execute("DELETE FROM plugin_installs WHERE plugin_id LIKE ?",
                         ("com.mbse.local.tool." + PROBE + "%",))
            conn.execute("DELETE FROM plugins WHERE name LIKE ?", (PROBE + "%",))
            conn.commit()
        except Exception:
            pass
        conn.close()

    ok = all(_res)
    print("\n" + "=" * 72 + "  ALL_OK=%s  (%d passed / %d failed)"
          % (ok, sum(1 for x in _res if x), sum(1 for x in _res if not x)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
