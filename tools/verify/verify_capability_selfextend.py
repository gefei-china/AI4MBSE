# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""端到端验证：能力自注册闭环（提案 → 批准 → 真能调用 → 能改能卸）。

## 为什么必须端到端

本仓已经踩过两次"注册了但调不到"：
  · `code_exec` 注册进表、模型看不见（漏 BUILTIN_DESC）
  · HTTP 工具 `kind` 没写 ⇒ 执行器认不出 ⇒ 保存成功但永远调不动
P2 的链路更长（提案 → 队列 → 批准 → 落盘 → 注册 → 绑定 → 运行时路由），
**任何一环断开都表现为"看似成功、实际没用"**。所以判据必须是
"最后一步真的跑出结果"，而不是"中间某一步返回 ok"。

## 判据

| # | 断言 | 防的失效 |
|---|---|---|
| A1 | 提案不落库（tools 表无新行） | 模型绕过确认直接注册 |
| A2 | 提案写进了 hil_confirmations，action=cap_capability_create | 卡不出现，用户无从批准 |
| A3 | 静态审查拦截危险脚本（subprocess / 越界写） | 后门脚本进注册流程 |
| A4 | 重名被拒（tools.name UNIQUE） | 覆盖既有工具 |
| B1 | 批准后 tools 表出现该行，source='capability' | 批准了没注册 |
| B2 | **运行时真能调用并返回正确结果** | 注册了调不到（历史坑） |
| B3 | 副作用按代码派生（写文件的脚本 ⇒ write） | 标 read 绕过写前闸 |
| B4 | 脚本落在 data/workspace 内，且不在平台源码目录 | 自注册代码污染源码树 |
| C1 | bind / unbind 真的改了 agent_tools | "绑了但没生效" |
| C2 | disable 后运行时拒绝执行 | 停用只是标记，运行时照跑 |
| C3 | delete 同时清理绑定与沙箱目录 | 删了留残骸 |
| D1 | 越界写脚本被 code_guard 拦 | 静态审查是空转 |

⚠️ 全程在真库上跑，`finally` 里清理探针数据。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)

PASS, FAIL = "PASS", "FAIL"
_res = []
CAP = "gate_calc_cap"


def _rec(name, ok, detail=""):
    _res.append(bool(ok))
    print("  [%s] %s%s" % (PASS if ok else FAIL, name,
                           ("  | " + str(detail)[:230]) if detail else ""))
    return bool(ok)


GOOD_SCRIPT = '''# -*- coding: utf-8 -*-
"""门禁用的能力脚本：算百分比。"""


def run(**kw):
    a = float(kw.get("a") or 0)
    b = float(kw.get("b") or 1)
    return {"ratio": round(a / b * 100, 2), "a": a, "b": b}
'''

BAD_SUBPROCESS = "import subprocess\nsubprocess.run(['dir'])\ndef run(**kw): return 'x'\n"
BAD_ABS_WRITE = 'def run(**kw):\n    open("D:/evil_probe.txt", "w").write("x")\n    return "x"\n'
WRITE_SCRIPT = '''import os


def run(**kw):
    p = os.path.join(os.environ["MBSE_WORKSPACE"], "capabilities", "%s", "out.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("hi")
    return {"wrote": p}
''' % CAP


def _db():
    from database import get_db
    return get_db()


def _cleanup(conn):
    try:
        conn.execute("DELETE FROM agent_tools WHERE tool_name LIKE ?", ("gate_calc%",))
        conn.execute("DELETE FROM tools WHERE name LIKE ?", ("gate_calc%",))
        conn.execute("DELETE FROM hil_confirmations WHERE action LIKE 'cap_%' "
                     "AND payload LIKE '%gate_calc%'")
        conn.commit()
    except Exception:
        pass
    try:
        from core import workspace
        d = os.path.join(workspace.root(), "capabilities", CAP)
        if workspace.is_in_root(d) and os.path.isdir(d):
            import shutil
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


def main() -> int:
    print("=" * 76)
    print("端到端：能力自注册闭环（提案 → 批准 → 调用 → 改绑 → 停用 → 删除）")
    print("=" * 76)

    from capability_runtime import exec_capability, invalidate_cache, is_capability
    from services import capability_admin as CA

    conn = _db()
    _cleanup(conn)
    conn.close()

    # ══ A 提案侧 ══════════════════════════════════════════════════
    conn = _db()
    n0 = conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
    conn.close()
    r = CA.propose("capability", "create",
                   {"name": CAP, "description": "门禁用：算百分比（a/b*100）",
                    "script": GOOD_SCRIPT,
                    "input_schema": {"type": "object", "properties": {
                        "a": {"type": "number"}, "b": {"type": "number"}},
                        "required": ["a", "b"]}},
                   agent_id="gate", conversation_id=0)
    conn = _db()
    n1 = conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
    _conf = conn.execute("SELECT id, action, status, preview FROM hil_confirmations "
                         "WHERE id=?", (r.get("confirmation_id"),)).fetchone()
    conn.close()
    _rec("A1 提案本身不注册（tools 行数不变）", n0 == n1, "before=%s after=%s" % (n0, n1))
    _rec("A2 已写入确认队列且 action=cap_capability_create",
         bool(_conf) and _conf["action"] == "cap_capability_create"
         and _conf["status"] == "pending", dict(_conf) if _conf else None)
    _rec("A2b 预览是**人话**（不是裸 JSON）",
         bool(_conf) and "注册新能力" in (_conf["preview"] or ""),
         (_conf["preview"] if _conf else "")[:110])

    _bad1 = CA.propose("capability", "create",
                       {"name": CAP + "_bad1", "description": "x", "script": BAD_SUBPROCESS})
    _bad2 = CA.propose("capability", "create",
                       {"name": CAP + "_bad2", "description": "x", "script": BAD_ABS_WRITE})
    _rec("A3 静态审查拦截 subprocess 脚本", not _bad1.get("ok"), _bad1.get("result", "")[:100])
    _rec("A3b 静态审查拦截越界绝对路径写", not _bad2.get("ok"), _bad2.get("result", "")[:100])

    # 重名：能力名与既有工具同名 ⇒ 提案时**不做**这个检查（要等执行，见 _validate 说明），
    # 但执行必须拒。这里先批准一个重名的，验证执行侧兜住。
    conn = _db()
    _exist_tool = conn.execute("SELECT name FROM tools WHERE status='active' LIMIT 1").fetchone()[0]
    conn.close()
    _dup = CA.propose("capability", "create",
                      {"name": _exist_tool, "description": "x", "script": GOOD_SCRIPT})
    from hil_service import HILService
    from hil_service import execute_confirmation as _xconf
    conn = _db()
    _dconf = HILService.get(conn, _dup.get("confirmation_id")) or {}
    _dres = _xconf(conn, _dconf, user={"name": "gate"})
    conn.close()
    _rec("A4 重名在执行时被拒（不能覆盖既有工具）",
         (not _dres.get("ok")) and ("已存在" in str(_dres.get("result"))),
         str(_dres.get("result"))[:120])

    # ══ B 批准 → 注册 → 调用 ═══════════════════════════════════════
    conn = _db()
    conf = HILService.get(conn, r.get("confirmation_id")) or {}
    res = _xconf(conn, conf, user={"name": "gate"})
    row = conn.execute("SELECT id, source, side_effect, status, kind, config "
                       "FROM tools WHERE name=?", (CAP,)).fetchone()
    conn.close()
    _rec("B1 批准后注册成功", bool(res.get("ok")), str(res.get("result"))[:130])
    _rec("B1b tools 行 source='capability' 且 side_effect 派生为 read",
         bool(row) and row["source"] == "capability" and row["side_effect"] == "read",
         tuple(row)[:5] if row else None)

    invalidate_cache()
    _rec("B2a 运行时能识别它是能力", is_capability(CAP))
    call = exec_capability(CAP, {"a": 37, "b": 158})
    _txt = str(call.get("result") or "")
    _rec("B2b **真实调用并返回正确结果**（37/158=23.42）",
         bool(call.get("ok")) and "23.42" in _txt, _txt[:150])

    _cfg = {}
    try:
        _cfg = json.loads((row["config"] if row else "") or "{}")
    except Exception:
        pass
    _sp = str(_cfg.get("script_path") or "")
    from core import workspace as W
    _full = os.path.join(W.root(), _sp) if _sp else ""
    _rec("B4 脚本落在 data/workspace 内（不污染源码树）",
         bool(_sp) and W.is_in_root(_full) and "mbse_system/capability" not in _full.replace("\\", "/"),
         _sp)

    # B3：写型脚本 ⇒ 派生为 write
    _w = CA.propose("capability", "create",
                    {"name": CAP + "_w", "description": "门禁：写文件的能力",
                     "script": WRITE_SCRIPT})
    conn = _db()
    _wc = HILService.get(conn, _w.get("confirmation_id")) or {}
    _wr = _xconf(conn, _wc, user={"name": "gate"})
    _wrow = conn.execute("SELECT side_effect FROM tools WHERE name=?", (CAP + "_w",)).fetchone()
    conn.close()
    _rec("B3 会写文件的脚本 ⇒ 副作用自动派生为 write（不会被标 read）",
         bool(_wrow) and _wrow["side_effect"] == "write",
         "side_effect=%s" % (_wrow["side_effect"] if _wrow else None))

    # ══ C 绑定 / 停用 / 删除 ══════════════════════════════════════
    conn = _db()
    _main = conn.execute("SELECT name FROM agents WHERE status='active' "
                         "AND agent_role='main' LIMIT 1").fetchone()
    conn.close()
    if _main:
        _bn = CA.execute({"kind": "capability", "action": "bind",
                          "spec": {"target": CAP, "agents": [_main["name"]]}})
        conn = _db()
        _nb = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_name=?",
                           (CAP,)).fetchone()[0]
        conn.close()
        _rec("C1 bind 真的写进了 agent_tools", bool(_bn.get("ok")) and _nb >= 1,
             "binds=%d" % _nb)

        _ub = CA.execute({"kind": "capability", "action": "unbind",
                          "spec": {"target": CAP, "agents": [_main["name"]]}})
        conn = _db()
        _nb2 = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_name=?",
                            (CAP,)).fetchone()[0]
        conn.close()
        _rec("C1b unbind 真的删掉了绑定", bool(_ub.get("ok")) and _nb2 == 0,
             "binds=%d" % _nb2)
    else:
        _rec("C1 bind 真的写进了 agent_tools", False, "库里没有 main Agent，跳过")

    _dis = CA.execute({"kind": "capability", "action": "disable",
                       "spec": {"target": CAP}})
    invalidate_cache()
    _after = exec_capability(CAP, {"a": 1, "b": 2})
    _rec("C2 停用后运行时拒绝执行（不是只改标记）",
         bool(_dis.get("ok")) and not _after.get("ok"),
         str(_after.get("result"))[:110])

    # 重新启用再删（验证 delete 路径）
    CA.execute({"kind": "capability", "action": "enable", "spec": {"target": CAP}})
    invalidate_cache()
    _del = CA.execute({"kind": "capability", "action": "delete", "spec": {"target": CAP}})
    conn = _db()
    _gone = conn.execute("SELECT COUNT(*) FROM tools WHERE name=?", (CAP,)).fetchone()[0]
    _gb = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE tool_name=?",
                       (CAP,)).fetchone()[0]
    conn.close()
    invalidate_cache()
    _rec("C3 delete 清掉工具行与绑定", bool(_del.get("ok")) and _gone == 0 and _gb == 0,
         "tools=%d binds=%d" % (_gone, _gb))
    _rec("C3b delete 同时删掉沙箱目录",
         not os.path.isdir(os.path.join(W.root(), "capabilities", CAP)))

    # ══ D 其它 kind 的增删改（覆盖"是否可实现"的答案）══════════════
    conn = _db()
    _n_ag = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    conn.close()
    _ag = CA.propose("agent", "create",
                     {"name": CAP + "_agent", "display_name": "门禁探针Agent",
                      "description": "门禁用，稍后删除", "agent_role": "sub"})
    conn = _db()
    _ac = HILService.get(conn, _ag.get("confirmation_id")) or {}
    _ar = _xconf(conn, _ac, user={"name": "gate"})
    _n_ag2 = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    conn.close()
    _rec("D1 agent 可创建（并走确认）", bool(_ar.get("ok")) and _n_ag2 == _n_ag + 1,
         "before=%d after=%d" % (_n_ag, _n_ag2))

    _sk = CA.propose("skill", "create",
                     {"name": CAP + "_skill", "description": "门禁探针技能",
                      "content": "这是门禁用技能正文。\n当用户问「门禁探针」时回答 OK。",
                      "triggers": ["门禁探针"]})
    conn = _db()
    _sc = HILService.get(conn, _sk.get("confirmation_id")) or {}
    _sr = _xconf(conn, _sc, user={"name": "gate"})
    _srow = conn.execute("SELECT id, length(content) FROM skills WHERE name=?",
                         (CAP + "_skill",)).fetchone()
    conn.close()
    _rec("D2 skill 可创建（正文落库）", bool(_sr.get("ok")) and bool(_srow),
         "len=%s" % (_srow[1] if _srow else None))

    _up = CA.execute({"kind": "skill", "action": "update",
                      "spec": {"target": CAP + "_skill",
                               "fields": {"description": "门禁探针技能（已改）"}}})
    conn = _db()
    _sv = conn.execute("SELECT description FROM skills WHERE name=?",
                       (CAP + "_skill",)).fetchone()
    conn.close()
    _rec("D3 skill 可修改", bool(_up.get("ok")) and "已改" in (_sv[0] if _sv else ""),
         _sv[0] if _sv else None)

    _badf = CA.execute({"kind": "skill", "action": "update",
                        "spec": {"target": CAP + "_skill",
                                 "fields": {"builtin": 1}}})
    _rec("D3b 不允许改的字段被白名单拒绝（不许改 builtin/source）",
         not _badf.get("ok"), str(_badf.get("result"))[:100])

    CA.execute({"kind": "skill", "action": "delete", "spec": {"target": CAP + "_skill"}})
    CA.execute({"kind": "agent", "action": "delete", "spec": {"target": CAP + "_agent"}})
    conn = _db()
    _left = (conn.execute("SELECT COUNT(*) FROM skills WHERE name=?",
                          (CAP + "_skill",)).fetchone()[0]
             + conn.execute("SELECT COUNT(*) FROM agents WHERE name LIKE ?",
                            (CAP + "_agent",)).fetchone()[0])
    conn.close()
    _rec("D4 skill / agent 可删除（探针清理干净）", _left == 0, "残留=%d" % _left)

    # ══ E 注入侧：注册的能力要让**任意** Agent 都能在点名时用到 ═════════
    #   真机实证（990513）：能力只绑给「知识问答Agent」，而本轮路由到「通用问答Agent」
    #   ⇒ 模型如实说"我这边调不了"（没撒谎，但用户觉得"刚加的能力说了名字还用不了"）。
    #   断点在**可见性跟着 Agent 走** ⇒ 需要"点名即注入"。
    _c2 = CA.propose("capability", "create",
                     {"name": CAP + "_inj", "description": "门禁：注入测试用",
                      "script": GOOD_SCRIPT},
                     agent_id="gate")
    conn = _db()
    _ic = HILService.get(conn, _c2.get("confirmation_id")) or {}
    _ir = _xconf(conn, _ic, user={"name": "gate"})
    conn.close()
    invalidate_cache()
    _rec("E1 注入用能力已注册", bool(_ir.get("ok")), str(_ir.get("result"))[:100])

    from agent.pipeline import AgentPipeline
    _p = AgentPipeline()
    _p._load_db_agents()
    _p._tool_user = {"id": 1, "role_name": "系统管理员", "username": "gate"}
    _p._tool_agent_ctx = {"agent": "chat"}
    _p._tool_conv_ctx = 0
    _p._tool_whitelist = None
    _u = {"id": 1, "role_name": "系统管理员"}

    def _names(q):
        _d = _p._build_tools_def("chat", q, _u)
        return [(d.get("function") or {}).get("name") or d.get("name") for d in _d]

    _n_hit = _names("用 %s_inj 算一下 37/158" % CAP)
    _n_miss = _names("算一下 37/158 是多少")
    _n_fake = _names("用 %s_injx 算一下" % CAP)
    _rec("E2 点名即注入：提到能力名 ⇒ 进候选（跨 Agent，不依赖绑定）",
         (CAP + "_inj") in _n_hit, _n_hit)
    _rec("E3 没点名 ⇒ 不注入（不白占 token）", (CAP + "_inj") not in _n_miss, _n_miss)
    _rec("E4 假名字（前缀相似）⇒ 不误命中（token 边界判据）",
         (CAP + "_inj") not in _n_fake, _n_fake)

    conn = _db()
    _cleanup(conn)
    conn.close()

    ok = all(_res)
    print("\n" + "=" * 76 + "  ALL_OK=%s  (%d passed / %d failed)"
          % (ok, sum(1 for x in _res if x), sum(1 for x in _res if not x)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
