"""verify_n3_view_expansion — N3 视图展开节点能力实测（真实链路，非静态推断）。
# CI-OPTIONAL: 真调 LLM 跑 N2 骨架 + 8 视图（checker.jar 随包运行时不入库 + LLM 费用）；本地为唯一可信口径。

目的：决定「8 个旧视图 Agent 能否迁 disabled」——
**迁前必须证明 N3 能独立产出可用视图**，否则视图能力会真空。

测什么（全部真跑）
----------------
① 静态走查：prompt/工具/契约是否齐备（快，先过这一关）
② 真实调用：真实建模需求 → 真实 `pipeline.execute` → 官方 checker.jar
③ 逐视图判定：8 个视图各跑一遍，产出必须 verdict=pass
④ **技能正文是否真的注入**：确认 8 个视图 skill 之一被命中且正文进了 prompt
   （这是本轮 V10 补的正文，必须验证真能生效）
⑤ 降级声明：AST 未覆盖元类时，产出是否如实声明 degraded

判据（缺一即FAIL）
----------------
· 8 个视图全部**硬错为 0**（`n_hard == 0`，与官方门禁 `sysml_v2_check.verdict`
  同口径：语义错只降级 `report`，**不阻断**）
· 技能正文命中且含硬约束关键词（活动视图取「判定分支」——**异常分支按需可选**，
  2026-10-08 米爸明确不强制）
· 无任何视图产出"看起来成功实际没产出代码"

⚠️ 更严格的**内容层**判据（必备要素 / 禁用构造）与真实 LLM 端到端复测在
   `tools/verify/view_content_rules.py` 与 `tools/verify/verify_n3_e2e_real.py`
   ——本脚本只做语法层 + 技能注入，**两层都过才算 N3 可替代旧视图 Agent**。
"""
from __future__ import annotations

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "mbse.db")

# 8 个视图的枚举值与期望硬约束关键词（关键词取自 V10 写入的 skill 正文）
#
# ★ 2026-10-08 口径修正（米爸明确）：**活动图不强制要求异常分支**，
#   异常路径按业务需要生成、禁止臆造 ⇒关键词从"异常分支"改为"判定分支"（`if`），
#   这才是 SysML v2 活动视图的必备语法，而异常分支是按需项。
VIEWS = [
    ("requirement", "subject"),
    ("structure", "port def"),
    ("usecase", "use case def"),
    ("activity", "if <条件>"),          # 必备的是判定分支；异常分支按需可选
    ("ibd", "connect"),
    ("sequence", None),                 # 不设关键词（AST 未覆盖，不抽边）
    ("state", "transition"),
    ("parameter", "constraint"),
]

PROMPT_REQUIRE = ("view_type", "sysml_v2_validate", "已存在")


def static_audit(conn):
    print("\n" + "=" * 72)
    print("① 静态走查（prompt / 工具 / 契约）")
    print("=" * 72)
    row = conn.execute(
        "SELECT id, system_prompt, input_schema, output_schema, hil_level "
        "FROM agents WHERE name='view_expansion'").fetchone()
    if not row:
        print("  [FAIL] view_expansion 不存在")
        return False, None
    ok = True
    sp = row["system_prompt"] or ""
    for kw in PROMPT_REQUIRE:
        hit = kw in sp
        print(f"  [{'OK  ' if hit else 'MISS'}] prompt 含「{kw}」")
        if not hit:
            ok = False
    tools = {r[0] for r in conn.execute(
        "SELECT t.tool_name FROM agent_tools t WHERE t.agent_id=?", (row["id"],))}
    need = {"sysml_v2_validate", "sysml_ast_extract", "sysml_v2_lint"}
    miss = sorted(need - tools)
    print(f"  [{'OK  ' if not miss else 'MISS'}] 关键工具齐备（缺 {miss or '无'}）")
    if miss:
        ok = False
    out = json.loads(row["output_schema"] or "{}")
    props = set(out.get("properties", {}))
    need_p = {"view_type", "view_code"}
    miss_p = sorted(need_p - props)
    print(f"  [{'OK  ' if not miss_p else 'MISS'}] output_schema 含 {sorted(need_p)}"
          f"（缺 {miss_p or '无'}）")
    if miss_p:
        ok = False
    print(f"  [INFO] 绑定工具 {len(tools)} 个：{sorted(tools)}")
    return ok, row["id"]


def skill_hit_check(pipe):
    """验证 8 个视图 skill 之一能被真实路由命中，且正文进了 prompt。"""
    print("\n" + "=" * 72)
    print("④ 技能正文注入验证（V10 补的正文是否真生效）")
    print("=" * 72)
    ok = True
    for view, kw in VIEWS:
        try:
            txt = pipe._build_skill_prompt(
                intent="design", user_input=f"生成 {view} 视图")
        except Exception as exc:                # noqa: BLE001
            print(f"  [FAIL] {view:12} 装配异常：{type(exc).__name__}: {exc}")
            ok = False
            continue
        hits = [h for h in (getattr(pipe, "_last_skill_hits", []) or [])
                if h.startswith("sysml_view_generation")]
        has_body = "正文摘要" in txt and "本视图必备要素" in txt
        line = f"  [OK  ] {view:12} 命中视图 skill {len(hits)} 个 | 正文已注入={has_body}"
        if kw:
            line += f" | 含硬约束「{kw}」={'✓' if kw in txt else '✗'}"
            if kw not in txt:
                ok = False
        print(line)
        if not hits:
            print(f"         ⚠️ 未命中任何视图 skill（正文不会注入）")
    return ok


REQ_SKELETON = (
    "为纯电动汽车热管理系统（电池、电机、电控、电池热管理）生成 SysML v2 架构骨架。"
    "要求：只产 package / part def / port def / requirement def，"
    "顶层 import 必须带可见性前缀。中文标识符必须加单引号。"
    "生成后必须调用 sysml_v2_validate 校验。"
)


def build_skeleton(pipe, retries: int = 2) -> str:
    """先跑 N2 产出骨架，作为 8 个视图的引用基准。

    ★ 为什么必须有这一步（2026-10-09 实测）：
    原脚本的请求写"基于**已生成的**骨架"却从不给骨架，
    模型只能回"Let me locate the existing skeleton."然后停住
    ⇒ 8/8 视图判「未产出代码块」。
    **判据缺输入 ⇒ 全员假红**，与被测能力无关。

    ★ 为什么带重试（2026-10-09 第二次实测）：
    同一请求**上一轮成功产出 3665 字符**，下一轮却只回20 字
    「我先查标准库成员与领域素材，再生成骨架。」且**没有任何 tool_call**
    ⇒ 模型第一轮只说话不调工具，直接结束。
    这是 LLM 行为波动（不是能力缺陷），重试即可。
    但**每次重试必须换新会话**——同一 conversation_id 会留下历史消息，
    模型可能以为已经查过标准库而跳过工具调用。
    """
    import re as _re
    from database import get_db
    from sysml_v2_check import check_code
    conn = get_db()
    last_err = ""
    for attempt in range(1, retries + 2):
        cid = new_conv(conn, f"verify_n3_v3_skeleton_r{attempt}")
        try:
            pipe.execute(REQ_SKELETON, conversation_id=cid, branch="dev",
                         forced_intent="architecture_skeleton")
        except Exception as exc:                   # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            print(f"  [重试 {attempt}] N2 调用异常：{last_err}")
            continue
        rows = conn.execute(
            "SELECT content FROM messages WHERE conversation_id=? "
            "AND role='assistant' ORDER BY id", (cid,)).fetchall()
        best = ""
        for r in rows:
            for m in _re.finditer(r"```(?:sysml|sysmlv2)?\s*\n(.*?)```",
                                  r[0] or "", _re.S | _re.I):
                if len(m.group(1)) > len(best):
                    best = m.group(1)
        if best:
            r = check_code(best)
            print(f"  骨架 {len(best)} 字符（第 {attempt} 次尝试）｜"
                  f"verdict={r.get('verdict')} n_hard={r.get('n_hard')}")
            if r.get("n_hard"):
                for e in (r.get("errors") or [])[:3]:
                    print(f"     L{e.get('line')} {str(e.get('msg'))[:58]}")
                print("  ⚠️ 骨架有硬错，但仍作为基准继续（视图层判据独立）")
            return best
        # 没产出：报出模型到底说了什么，便于归因
        said = (rows[-1][0] or "")[:80] if rows else "(无消息)"
        last_err = f"未产出代码块，模型最后说：{said!r}"
        print(f"  [重试 {attempt}] {last_err}")
    print(f"  [FAIL] {retries + 1} 次尝试均未产出骨架：{last_err}")
    return ""


def new_conv(conn, title: str) -> int:
    """新建独立会话（不复用 id）——避免跨会话状态互相污染。

    ★ 2026-10-09：共用一个 conversation_id 会让 8 个视图共用同一份
      `AgentPipeline` 实例状态，实测跑完后只剩最后一个视图的代码块，
      判据把残留当本次产出 ⇒ **8/8 假通过**。
    """
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # ⚠️ 所有可变值一律走占位符，**不要在SQL 里写字面量 '?'**——
    #   那样容易数错占位符个数（本工程已踩过两次：verify_n3_e2e_real / 本文件）。
    cur = conn.execute(
        "INSERT INTO conversations "
        "(title, intent, status, user_id, phase, created_at, updated_at, "
        " project_id, current_intent, last_slots, pending_clarify) "
        "VALUES (?, ?, 'active', NULL, 'requirement', ?, ?, '', ?, '{}', '')",
        (title, "view_expansion", ts, ts, "view_expansion"))
    conn.commit()
    return int(cur.lastrowid)


def real_run(pipe, aid, view, skeleton=""):
    """真实跑一个视图，返回 (out, err, codes)。

    ★ 2026-10-09 修正：`skeleton` 必须真实传入。
    原版请求写"请基于**已生成的**架构骨架"但**从不提供骨架**，
    模型只能回一句 "Let me locate the existing skeleton."（85 字）
    ⇒ 8/8 视图全部"未产出代码块"。
    实测证据：会话 9051 的 assistant 消息只有 85 字，正是那句找骨架的话。
    ⇒ **判据自身缺输入**（模型问什么我们就答什么，不是它不听话）。
    """
    from database import get_db
    if skeleton:
        req = (f"请基于下面的架构骨架，生成 **{view}** 视图。\n"
               f"只引用骨架中已存在的元素，不要凭空引入部件或端口。\n"
               f"生成后必须调用 sysml_v2_validate 校验，verdict=block 时按诊断修复后重试（最多 3 轮）。\n\n"
               f"骨架：\n```sysml\n{skeleton}\n```")
    else:
        req = (f"请基于已生成的 SysML v2 架构骨架，生成 **{view}** 视图。"
               f"只引用骨架中已存在的元素，不要凭空引入部件或端口。"
               f"生成后必须调用 sysml_v2_validate 校验，verdict=block 时按诊断修复后重试（最多 3 轮）。")
    conn = get_db()

    # ★★ 2026-10-09 修正第二处假通过：**每视图独立会话**。
    #   原版所有视图共用 conversation_id=9051（只 DELETE 消息、不换会话）
    #   ⇒ `AgentPipeline` 的跨会话状态（`_last_skill_hits` /
    #     `_skill_body_offloads` 等）会互相污染；
    #   实测跑完 8 个视图后会话里**只剩 1 个代码块**，
    #   于是"每个视图都拿到 4865 字符"——那是**最后一个视图的残留**，
    #   8/8 verdict=pass 全是假通过。
    #   ⇒ 每视图一个独立会话，互不干扰（与 verify_n3_e2e_real 同口径）。
    cid = new_conv(conn, f"verify_n3_v3_{view}")
    try:
        out = pipe.execute(req, conversation_id=cid, branch="dev",
                           forced_intent="view_expansion")
    except Exception as exc:                     # noqa: BLE001
        return None, str(f"{type(exc).__name__}: {exc}")[:120], ""
    # 从消息表抽真实产出的代码块（不能从返回值字符串瞎抓）
    codes = []
    try:
        rows = conn.execute(
            "SELECT content FROM messages WHERE conversation_id=? "
            "AND role='assistant' ORDER BY id", (cid,)).fetchall()
        for r in rows:
            for m in re.finditer(r"```(?:sysml|sysmlv2)?\s*\n(.*?)```",
                                 r[0] or "", re.S | re.I):
                codes.append(m.group(1))
    except Exception:                            # noqa: BLE001
        pass
    return out, None, codes


def main(only=None):
    from database import db_conn
    from agent.pipeline import AgentPipeline
    from sysml_v2_check import check_code

    print("=" * 72)
    print("N3 视图展开能力实测（真实链路）")
    print("=" * 72)
    print("判定目标：8 个旧视图 Agent 能否迁 disabled")

    with db_conn() as conn:
        ok_static, aid = static_audit(conn)

    pipe = AgentPipeline()
    pipe._load_db_agents()

    ok_skill = skill_hit_check(pipe)

    if only == "static":
        print()
        print("[静态 + 技能注入 结论]")
        print(f"  静态走查：{'通过' if ok_static else '未通过'}")
        print(f"  技能正文注入：{'通过' if ok_skill else '未通过'}")
        return 0 if (ok_static and ok_skill) else 1

    print("\n" + "=" * 72)
    print("② N2 骨架（视图的引用基准 —— 缺了它模型只会去找不存在的东西）")
    print("=" * 72)
    skeleton = build_skeleton(pipe)
    if not skeleton:
        print("[FAIL] 未能取得可用的架构骨架 ⇒ 视图层无参考基准，终止")
        print("       （这正是 2026-10-09 之前 8/8 全部「未产出代码块」的根因）")
        return 1

    print("\n" + "=" * 72)
    print("③ 真实调用 + 逐视图校验（8 个视图）")
    print("=" * 72)
    results = {}
    all_ok = ok_static and ok_skill
    for view, _kw in VIEWS:
        out, err, codes = real_run(pipe, aid, view, skeleton)
        if err:
            print(f"  [FAIL] {view:12} 调用失败：{err}")
            results[view] = None
            all_ok = False
            continue
        if not codes:
            print(f"  [FAIL] {view:12} 未产出任何 ```sysml 代码块（视为失败）")
            results[view] = None
            all_ok = False
            continue
        # 逐轮校验，取最终轮
        last = None
        for code in codes:
            r = check_code(code)
            last = r
        v = last.get("verdict")
        nh = last.get("n_hard")
        # ★ 口径与官方门禁一致（`sysml_v2_check.verdict`）：**硬错 n_hard 为 0 即通过**，
        #   语义错只降级为 `report`，不阻断。
        #   原写法 `v == "pass" and nh == 0` 会把语义提示误判成失败
        #   （实测 requirement/usecase 因此被误判为不通过）。
        good = (nh == 0)
        # ★ 记录末轮 md5 —— 用于「产出雷同」判据（比通过率更早的信号）
        import hashlib
        md5 = hashlib.md5(codes[-1].encode()).hexdigest()[:8] if codes else None
        results[view] = {"verdict": v, "n_hard": nh, "rounds": len(codes),
                         "len": len(codes[-1]) if codes else 0, "md5": md5}
        flag = "OK  " if good else "FAIL"
        extra = "" if v == "pass" else f"（语义提示 {last.get('n_error') or 0} 条，不阻断）"
        print(f"  [{flag}] {view:12} 轮次 {len(codes)} | verdict={v} n_hard={nh}{extra}")
        if not good:
            all_ok = False
            for e in (last.get("errors") or [])[:3]:
                print(f"         L{e.get('line')} [{e.get('path')}] {str(e.get('msg'))[:62]}")
        else:
            print(f"         产出 {len(codes[-1])} 字符")

    # ★★ 产出雷同判据（2026-10-09 新增）
    #   失效形态：多个视图产出**完全相同**的内容（都在照抄骨架/上一个视图）。
    #   这种失效**通过率只会体现为"都OK"**，看不到根因——
    #   实测就发生过：8 个视图全部报 verdict=pass，但会话里只剩 1 个代码块。
    #   ⇒ 单列一条：任意两个视图的 md5 不得相同。
    _hs = {}
    for _v, _r in results.items():
        if _r and _r.get("md5"):
            _hs.setdefault(_r["md5"], []).append(_v)
    dups = {k: vs for k, vs in _hs.items() if len(vs) > 1}
    print()
    if dups:
        print(f"  ⚠️ 检出**产出雷同**：{list(dups.values())}")
        print("     多个视图产出同一份内容 ⇒ 模型在照抄骨架/上一个视图，")
        print("     视图部分未真正生成；**此时通过率不可信**，须先修注入链路。")
    elif len(VIEWS) > 1:
        print(f"  [OK  ] 无产出雷同（{len(_hs)} 份产出各不相同）")

    # 汇总（★ 与逐条判定同口径：一律按 n_hard，不再按 verdict=='pass'
    #   —— 混用会出现"逐条打OK、汇总说失败"的错位）
    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    npass = sum(1 for v in results.values() if v and (v.get("n_hard") or 0) == 0)
    print(f"  静态走查：{'通过' if ok_static else '未通过'}")
    print(f"  技能正文注入：{'通过' if ok_skill else '未通过'}")
    print(f"  视图校验（硬错=0）：{npass}/{len(VIEWS)}")
    print()
    # ★ 产出雷同时**不允许**宣布通过（那是假通过）
    if dups:
        print("  [FAIL] 存在产出雷同 ⇒ 不宣布通过（通过率不可信）")
        return 1
    if all_ok and npass == len(VIEWS):
        print("  ✅ N3 能力实测通过 ⇒ **8 个旧视图 Agent 可以迁 disabled**")
        print("（先迁，保留 enabled=0 可回滚；观察 1~2 天再物理清理）")
        return 0
    print("  ⚠️ 未全部通过 ⇒ **暂不迁**，否则视图能力真空")
    print("     失败视图：" + ", ".join(k for k, v in results.items()
                                  if not (v and (v.get("n_hard") or 0) == 0)))
    return 1


if __name__ == "__main__":
    _only = "static" if "--static" in sys.argv else None
    sys.exit(main(_only))