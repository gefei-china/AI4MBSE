"""verify_n3_e2e_real — N3 视图展开**真实链路**端到端复测（双层校验）。
# CI-OPTIONAL: 真调 LLM 跑 8 视图双层校验（约 20 分钟 + 产生费用），CI 无 LLM secret；本地已跑通，见 docs/N3 视图展开实测。

## 为什么必须重测（2026-10-08 关键发现）

上一轮「内容层 4/8 合格」的实测结论**无效**—— 那次跑的解释器里`httpx` 缺失，
`llm_client` 每次都「静默回落 Mock」，模型产出的是
`（Mock 回答）已收到你的消息…` 这类占位文本。
⇒ 拿 Mock 产出判"视图质量"，等于**拿假数据评估真能力**。

项目 `.venv` 里 httpx 是齐的，本脚本强制用它跑（并在开头断言 LLM 非 Mock）。

## 判据

① LLM 真实可用（响应里不得出现 Mock 标记）—— 前提，不成立直接退出非 0
② N2 先产出一个能过 checker.jar 的骨架，作为 N3 的引用基准
③ N3 逐视图真实生成 8 个视图：
   · **语法层**：`n_hard == 0`（词法 + 语法硬错）
     —— 与官方门禁 `sysml_v2_check.verdict` **同口径**：语义错只降级为 `report`，
     **不阻断**。第一版误用 `verdict == "pass"`，等于另立一套更严的门禁。
   · **内容层**：`view_content_rules.check_view` 不判 FAIL
     （活动图**不要求**异常分支，按米爸 2026-10-08 口径）
④ 两层都过才算合格；任一层不过都打印失败原因

## 用法

    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_n3_e2e_real.py
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_n3_e2e_real.py --views activity state

退出码 0 = 8 视图两层全过。
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

VIEWS = ["requirement", "structure", "usecase", "activity",
         "ibd", "sequence", "state", "parameter"]

REQ = ("为纯电动汽车热管理系统（EV TMS）建立 SysML v2 模型："
       "系统 EVThermalManagementSystem 下含 BatteryThermalManagement（电池热管理）、"
       "CabinThermalManagement（座舱热管理）、ThermalLoop（热回路）、"
       "HeatPumpAssembly（热泵组件）四个子系统；"
       "含 ReqEndurance（续航≥500km）、ReqColdStart（低温启动）两条需求。")

CODE_RE = re.compile(r"```(?:sysml|sysmlv2)?\s*\n(.*?)```", re.S | re.I)


def new_conv(conn, title: str, intent: str) -> int:
    """建一个真实会话并返回 id。

    ⚠️ 不能硬编码 conversation_id —— `messages.conversation_id` 上有**外键**，
    用不存在的 id 会在 `execute` 落用户消息时直接
    `sqlite3.IntegrityError: FOREIGN KEY constraint failed`（2026-10-08 实测踩到）。
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 占位符与绑定参数**一一对应**：title / intent / created_at / updated_at /
    # current_intent（current_intent 与 intent 同值，故两处各传一次）
    cur = conn.execute(
        "INSERT INTO conversations (title, intent, status, user_id, phase, "
        "created_at, updated_at, project_id, current_intent, last_slots, pending_clarify) "
        "VALUES (?,?,'active',NULL,'requirement',?,?,'',?,'{}','')",
        (title, intent, ts, ts, intent))
    conn.commit()
    return int(cur.lastrowid)


def _clear(conn, conv):
    try:
        conn.execute("DELETE FROM messages WHERE conversation_id=?", (conv,))
        conn.commit()
    except Exception:
        pass


def _codes(conn, conv):
    out = []
    rows = conn.execute(
        "SELECT content FROM messages WHERE conversation_id=? AND role='assistant' "
        "ORDER BY id", (conv,)).fetchall()
    for r in rows:
        for m in CODE_RE.finditer(r[0] or ""):
            out.append(m.group(1))
    return out


def _last_real_code(conn, conv):
    """取最后一条**非空**代码块（Mock 期会产出占位文本，这里只做形状过滤）。"""
    cs = _codes(conn, conv)
    cs = [c for c in cs if c.strip() and len(c.strip()) > 30]
    return cs[-1] if cs else ""


def assert_llm_real():
    """前提断言：LLM 必须真实可用。任何 Mock 降级都直接判失败退出。"""
    from llm import llm_client
    r = llm_client.chat([{"role": "user", "content": "只回复两个字：可用"}], _intent="probe")
    txt = (r.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    if "Mock" in txt:
        print(f"[FAIL] LLM 处于 Mock 降级模式（响应={txt[:60]!r}）")
        print("       ⇒ 此时的产出不能用于评估真实能力。请用 .venv 解释器并检查 provider 配置。")
        return False
    print(f"[OK  ] LLM 真实可用（探针响应={txt[:40]!r}）")
    return True


def build_skeleton(pipe, conn):
    """产出（或复用）一个能过 checker.jar 的骨架，作为 N3 的引用基准。

    ★ 复用逻辑（2026-10-08 实测）：N2 单次耗时可达数分钟，且**存在轮次耗尽**的
      不稳定（实测有一次 assistant 最后停在「现在生成骨架并自校验」就没再产出代码块——
      工具轮次用尽）。骨架内容对 N3 的验证**不敏感**（只要是合法且含四部件+两需求的模型），
      所以先查库里已有的合格骨架直接复用，避免每次都重跑 N2 拖慢验证。
    """
    from sysml_v2_check import check_code

    # ① 先找可复用的骨架
    #    （`conversations` 主键列名就是 `id`；`messages` 侧才是 `conversation_id`
    #     —— 2026-10-08 实测在这里写错过一次）
    for row in conn.execute(
            "SELECT id FROM conversations WHERE intent=? "
            "AND title LIKE 'verify_n3_e2e_skeleton%' ORDER BY id DESC LIMIT 5",
            ("architecture_skeleton",)).fetchall():
        code = _last_real_code(conn, row["id"])
        if not code:
            continue
        r = check_code(code)
        # 同官方门禁口径：硬错为 0 即可复用（语义提示不阻断）
        if (r.get("n_hard") or 0) == 0:
            print("\n" + "=" * 72)
            print("② N2 骨架（复用库中已通过校验的骨架，跳过重跑）")
            print("=" * 72)
            print(f"  复用 conv={row['id']} | {len(code)} 字符 | "
                  f"verdict={r.get('verdict')} n_hard={r.get('n_hard')}")
            return code

    print("\n" + "=" * 72)
    print("② N2 骨架（现产）")
    print("=" * 72)
    conv = new_conv(conn, "verify_n3_e2e_skeleton", "architecture_skeleton")
    pipe.execute(REQ, conversation_id=conv, branch="dev",
                 forced_intent="architecture_skeleton")
    code = _last_real_code(conn, conv)
    if not code:
        print("[FAIL] N2 未产出可用代码块（可能工具轮次耗尽；重跑本脚本会复用或重新生成）")
        return ""
    r = check_code(code)
    print(f"  骨架 {len(code)} 字符 | verdict={r.get('verdict')} n_hard={r.get('n_hard')}")
    for e in (r.get("errors") or [])[:4]:
        print(f"     L{e.get('line')} [{e.get('path')}] {str(e.get('msg'))[:60]}")
    if (r.get("n_hard") or 0) != 0:
        print("[WARN] 骨架存在硬错，仍作为基准继续（视图层判据独立）")
    return code


def run_views(pipe, conn, skeleton, views):
    from sysml_v2_check import check_code
    sys.path.insert(0, os.path.join(_ROOT, "tools", "verify"))
    from view_content_rules import check_view, render

    results = {}
    npass = 0
    for v in views:
        req = (f"请基于下面的架构骨架，生成 **{v}** 视图。\n"
               f"只引用骨架中已存在的元素，不要凭空引入部件或端口。\n"
               f"生成后必须调用 sysml_v2_validate 校验。\n\n骨架：\n```sysml\n{skeleton}\n```")
        conv = new_conv(conn, f"verify_n3_e2e_{v}", "view_expansion")
        try:
            pipe.execute(req, conversation_id=conv, branch="dev",
                         forced_intent="view_expansion")
        except Exception as exc:                          # noqa: BLE001
            print(f"  [FAIL] {v:12} 调用异常：{type(exc).__name__}: {exc}")
            results[v] = None
            continue
        code = _last_real_code(conn, conv)
        if not code:
            print(f"  [FAIL] {v:12} 未产出代码块")
            results[v] = None
            continue
        syn = check_code(code)
        con = check_view(v, code)
        # ★ 语法层口径与官方门禁一致（`sysml_v2_check.verdict`，2026-09-19 三路化）：
        #   **词法 + 语法 = 硬错（n_hard）**，任一 > 0 → block；
        #   语义错（"Couldn't resolve reference" / "Subject must be first parameter"）
        #   只降级为 `report`，**不阻断**。
        #   第一版把 `verdict != "pass"` 当失败 ⇒ 把语义警告误判成硬错，
        #   等于在本脚本里**另立一套比官方更严的门禁** —— 与「少加限制」相悖。
        s_ok = (syn.get("n_hard") or 0) == 0
        c_ok = con["level"] != "FAIL"
        good = s_ok and c_ok
        npass += good
        results[v] = {"verdict": syn.get("verdict"), "n_hard": syn.get("n_hard"),
                      "n_error": syn.get("n_error"), "content": con["level"],
                      "len": len(code), "ok": good, "conv": conv,
                      "md5": hashlib.md5(code.encode()).hexdigest()}
        note = "" if syn.get("verdict") == "pass" else \
            f"（语义级提示 {syn.get('n_error') or 0} 条，不阻断）"
        print(f"  [{'OK  ' if good else 'FAIL'}] {v:12} {len(code):5}字符 | "
              f"语法={syn.get('verdict')}/硬错{syn.get('n_hard')}{note} | 内容={con['level']}")
        if not s_ok:
            for e in (syn.get("errors") or [])[:3]:
                print(f"         硬错 L{e.get('line')} {str(e.get('msg'))[:58]}")
        elif syn.get("verdict") != "pass":
            for e in (syn.get("errors") or [])[:2]:
                print(f"         语义 L{e.get('line')} {str(e.get('msg'))[:58]}")
        if not c_ok:
            print("         " + render(con).replace("\n", "\n         "))
    return results, npass


def main():
    only = None
    if "--views" in sys.argv:
        only = sys.argv[sys.argv.index("--views") + 1].split(",")
        only = [x.strip() for x in only if x.strip()]
    views = only or VIEWS

    print("=" * 72)
    print("N3 视图展开 · 真实链路端到端复测（双层：语法 + 内容）")
    print("=" * 72)

    if not assert_llm_real():
        return 2

    from database import get_db
    from agent.pipeline import AgentPipeline
    pipe = AgentPipeline()
    pipe._load_db_agents()
    conn = get_db()

    skeleton = build_skeleton(pipe, conn)
    if not skeleton:
        return 1

    print("\n" + "=" * 72)
    print(f"③ N3 逐视图真实生成（{len(views)} 个）+ 双层校验")
    print("=" * 72)
    results, npass = run_views(pipe, conn, skeleton, views)

    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    print(f"  双层全过：{npass}/{len(views)}")
    for v in views:
        r = results.get(v)
        if r is None:
            print(f"    {v:12} 调用失败/无产出")
        else:
            print(f"    {v:12} 硬错={r['n_hard']} 语义提示={r['n_error']} "
                  f"内容={r['content']:5} {'✔' if r['ok'] else '✘'}")

    # ★ 串味检查（2026-10-08 实测踩坑）：曾出现 8 个视图里 5 个产出**完全相同**的内容
    #   （全是照抄骨架/上一个视图，视图部分压根没生成）。
    #   这种失效**通过率只会体现为"都 FAIL"**，看不到根因；
    #   ⇒ 单列一条判据：任意两个视图的产出不得相同。
    #   ⚠️ 直接用 run_views 返回的 md5，**不要回头二次查库**——
    #首版"二次查询 + try/except 兜底"把真实结果吞了，
    #   判据形同虚设（实际有雷同，却报"无雷同"）。
    _hs = {}
    for _v, _r in results.items():
        if _r and _r.get("md5"):
            _hs.setdefault(_r["md5"], []).append(_v)
    dups = {k: vs for k, vs in _hs.items() if len(vs) > 1}
    print()
    if dups:
        print(f"  ⚠️ 检出**产出雷同**：{list(dups.values())}")
        print("     （多个视图产出同一份内容⇒ 模型在照抄骨架/上一个视图，视图部分未生成；"
              "此时通过率不可信，须先修注入链路）")
    elif len(views) > 1:
        print(f"  [OK  ] 无产出雷同（{len(_hs)} 份产出各不相同）")
    bad = [v for v in views if not (results.get(v) or {}).get("ok")]
    print()
    if not bad:
        print("  ✅ N3 双层全过 ⇒ 具备替代旧视图 Agent 的能力")
        return 0
    print(f"  ⚠️ 未通过的视图：{', '.join(bad)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())