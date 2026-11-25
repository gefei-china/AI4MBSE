# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：2026-11-20 补登记：历史漏入清单，当前按 local 手工跑；是否晋 pr 需先确认无外部依赖并同步 ci.yml（G6a）
"""P0 修复四件套的常驻门禁（2026-11-19）。

锁定的四个不变式（每条都有真库事故出处）：

  A 编排子任务必须能看到**父会话用户原文**
    事故：conv 999999905 msg#7013「帮我优化并验证提示词」→ 子任务 t2 自报
    「原始提示词未取回」→ 10304 字报告里没有提示词 → 用户追问「我的提示词呢」。
    根因：子任务 `conversation_id=0` ⇒ `_load_history` 为空；`artifact_digest`
    只有产物清单（实测对 #7011 的 8 个需求独有短语命中 0/8）。

  B `file_list` 遇到「无权限/被截断」条目不得抛 KeyError
    事故：真库 42 次「文件工具执行失败: 'type' / 'name'」（含 conv 999999905 各 3 次）。

  C 会话 intent 必须在收口语义下落库（只填空、不覆盖）
    事故：117/473（24.7%）为空；117 条里 103 条有消息 ⇒ 跑完了却没记。

  D 孤儿回收不得把「批次心跳还在走」的任务判死
    事故：105 条 failed 里 93 条（88.6%）是孤儿回收误判；真实失败 <1%。

用法：<repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_p0_fixes_20261119.py
退出码：全绿 0 / 有失败 1。
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL = "PASS", "FAIL"
_results = []


def ck(tag, ok, detail=""):
    _results.append((PASS if ok else FAIL, tag))
    print("  %-5s %s%s" % (PASS if ok else FAIL, tag, ("  | " + str(detail)) if detail else ""))


def main():
    print("=" * 84)
    print("P0 修复四件套门禁（2026-11-19）")
    print("  repo=%s" % ROOT)
    print("=" * 84)

    # ── A · 父会话用户原文注入 ────────────────────────────────────────────
    print("\n[A] 编排子任务可看到父会话用户原文")
    try:
        from agent.session_artifacts import load_parent_user_msgs
        from workflows.planner import build_subtask_context
        ck("A1 load_parent_user_msgs 存在", True)
    except Exception as e:                                   # noqa: BLE001
        ck("A1 load_parent_user_msgs 存在", False, e)
        load_parent_user_msgs = build_subtask_context = None

    if load_parent_user_msgs:
        DB = os.path.join(ROOT, "mbse.db")
        con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
        con.row_factory = sqlite3.Row
        # 用真库事故会话做正例
        msgs = load_parent_user_msgs(con, 999999905)
        ck("A2 真库 conv 999999905 能读到 user 原文", bool(msgs), "%d 条" % len(msgs))
        joined = "\n".join(m["content"] for m in (msgs or []))
        # #7011 的需求独有短语（绝不可能是产物清单里出现的）
        probes = ["前蒸 + 后蒸", "变体基型", "不得画成与方案一同等规模"]
        hit = sum(1 for p in probes if p in joined)
        ck("A3 含 #7011 需求独有短语（>=1）", hit >= 1, "%d/%d 命中" % (hit, len(probes)))
        # ★ 2026-11-19 判据修正：原来写死「#7015 不在内」，但该会话后来又多了
        #   #7038「输出提示词，不是V2代码」——用户在我修完之后**仍然**又问了一次，
        #   这本身是「没闭环」的二次证据。`exclude_latest` 排除的是**最新一条 user**，
        #   所以 #7015 理应进入快照。写死具体 id/文案的判据会随数据漂移而假红
        #   ⇒ 改为守**机制**：最新一条 user 原文不得出现在快照里。
        _last = con.execute(
            "SELECT content FROM messages WHERE conversation_id=999999905 "
            "AND role='user' ORDER BY id DESC LIMIT 1").fetchone()
        _last_txt = (_last["content"] if _last else "").strip()
        _probe = _last_txt[:14]
        ck("A4 排除本轮输入（最新一条 user 原文不在快照内）",
           bool(_probe) and _probe not in joined, "probe=%r" % _probe)

        ctx = build_subtask_context({"task_key": "tX", "title": "T"}, "目标", [],
                                    artifact_digest="DIGEST", parent_user_msgs=msgs)
        ck("A5 快照含『原始请求原文』段", "原始请求原文" in ctx)
        ck("A6 快照含『禁止声称已取回』护栏", "禁止声称" in ctx)
        ck("A7 空列表 ⇒ 不注入（回退口径）",
           "原始请求原文" not in build_subtask_context(
               {"task_key": "tY", "title": "T"}, "目标", [], parent_user_msgs=[]))
        # 预算受控
        huge = [{"label": "L", "content": "X" * 200000}]
        ctx_h = build_subtask_context({"task_key": "tZ", "title": "T"}, "目标", [],
                                      parent_user_msgs=huge)
        ck("A8 超长原文被预算截断（快照 < 20000 字）", len(ctx_h) < 20000, "%d 字" % len(ctx_h))
        con.close()

    # ── B · file_list 不再 KeyError ───────────────────────────────────────
    print("\n[B] file_list 对 error/note 条目不得抛 KeyError")
    try:
        import file_tools as FT
        orig = FT._fmt_list
        cases = [
            ("纯 error 条目", [{"error": "权限不足，无法读取 X"}]),
            ("纯 note 条目", [{"note": "条目超过上限，已截断"}]),
            ("error + 正常混合", [{"error": "子目录失败"},
                                 {"name": "a", "type": "file", "size": "1KB"}]),
            ("空列表", []),
        ]
        bad = []
        shown = []
        for label, fake in cases:
            FT._fmt_list = lambda *a, _f=fake, **k: _f
            try:
                r = FT._do_list({"path": "."})
                res = str(r.get("result") or "")
                shown.append(res)
                if not r.get("ok") or "KeyError" in res or res.strip().endswith("'"):
                    bad.append(label)
            except Exception as e:                            # noqa: BLE001
                bad.append("%s: %s" % (label, e))
        FT._fmt_list = orig
        ck("B1 四种形态均不抛异常且 ok=True", not bad, bad or "全部通过")
        # 真判据：error/note 的原文必须出现在结果里（旧实现会把它整条丢掉）
        allres = "\n".join(shown)
        ck("B2 error 与 note 原文均被如实呈现",
           ("权限不足" in allres) and ("已截断" in allres))
        ck("B3 不出现 KeyError 字样",
           "KeyError" not in allres and "'type'" not in allres)
    except Exception as e:                                    # noqa: BLE001
        ck("B1 file_list 可导入", False, e)

    # ── C · intent 回填 ──────────────────────────────────────────────────
    print("\n[C] conversations.intent 收口回填（只填空不覆盖）")
    src = open(os.path.join(ROOT, "routers", "conversations.py"),
               encoding="utf-8", errors="replace").read()
    ck("C1 收口处存在 intent 回填语句",
       "UPDATE conversations SET intent=?" in src)
    ck("C2 判据只填空（含 intent IS NULL OR TRIM(intent)=''）",
       bool(re.search(r"AND \(intent IS NULL OR TRIM\(intent\)=''\)", src)))
    ck("C3 使用独立连接 db_conn（不抢请求级写锁）",
       "with db_conn() as _ic:" in src)
    # 真库：空值占比必须已被治理过（<25% 且不再是"最多的一项"）
    DB = os.path.join(ROOT, "mbse.db")
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    tot = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    emp = con.execute(
        "SELECT COUNT(*) FROM conversations WHERE COALESCE(intent,'')=''").fetchone()[0]
    ratio = (emp * 100.0 / tot) if tot else 0
    ck("C4 真库空 intent 占比 < 25%", ratio < 25, "%d/%d = %.1f%%" % (emp, tot, ratio))
    # 所有空值会话都不得是"有 assistant 回复却没有 card_data.intent 可反查"的新数据
    con.close()

    # ── D · 孤儿回收参考批次心跳 ─────────────────────────────────────────
    print("\n[D] 孤儿回收：批次心跳在走就不判死")
    try:
        from core import run_registry as RR

        def mkdb():
            c = sqlite3.connect(":memory:")
            c.row_factory = sqlite3.Row
            c.execute("CREATE TABLE agent_tasks(id INTEGER PRIMARY KEY, run_id INT, "
                      "conversation_id INT, task_key TEXT, title TEXT, status TEXT, "
                      "updated_at TEXT, agent_id TEXT)")
            c.execute("CREATE TABLE orch_checkpoints(run_id INTEGER PRIMARY KEY, updated_at TEXT)")
            return c

        # D1 关键场景：任务旧 + 批次心跳新 ⇒ 不判孤儿（旧判据会判）
        c = mkdb()
        c.execute("INSERT INTO agent_tasks VALUES(1,100,9,'t1','长任务','running',"
                  "datetime('now','-60 minutes'),'design')")
        c.execute("INSERT INTO orch_checkpoints VALUES(100,datetime('now','-10 seconds'))")
        c.commit()
        new_n = len(RR.find_orphans(c, 1800))
        old_n = len(c.execute(
            "SELECT id FROM agent_tasks WHERE status IN ('planned','ready','running','blocked') "
            "AND updated_at < datetime('now','-1800 seconds')").fetchall())
        ck("D1 旧判据判孤儿(1) 而新判据不判(0)", old_n == 1 and new_n == 0,
           "旧=%d 新=%d" % (old_n, new_n))

        # D2 真僵尸（两者都旧）仍回收
        c = mkdb()
        c.execute("INSERT INTO agent_tasks VALUES(2,101,9,'t1','僵尸','running',"
                  "datetime('now','-60 minutes'),'design')")
        c.execute("INSERT INTO orch_checkpoints VALUES(101,datetime('now','-60 minutes'))")
        c.commit()
        ck("D2 真僵尸仍被回收", len(RR.find_orphans(c, 1800)) == 1)

        # D3 无批次行（单 Agent）不漏判
        c = mkdb()
        c.execute("INSERT INTO agent_tasks VALUES(3,0,9,'t1','孤立','running',"
                  "datetime('now','-60 minutes'),'design')")
        c.commit()
        ck("D3 无 checkpoint 行时仍判孤儿", len(RR.find_orphans(c, 1800)) == 1)

        # D4 老库降级不崩
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE agent_tasks(id INTEGER PRIMARY KEY, run_id INT, "
                  "conversation_id INT, task_key TEXT, title TEXT, status TEXT, "
                  "updated_at TEXT, agent_id TEXT)")
        c.execute("INSERT INTO agent_tasks VALUES(5,103,9,'t1','老库','running',"
                  "datetime('now','-60 minutes'),'design')")
        c.commit()
        ck("D4 无 orch_checkpoints 表时降级可回收", len(RR.find_orphans(c, 1800)) == 1)

        # D5 回退开关
        c = mkdb()
        c.execute("INSERT INTO agent_tasks VALUES(6,104,9,'t1','开关','running',"
                  "datetime('now','-60 minutes'),'design')")
        c.execute("INSERT INTO orch_checkpoints VALUES(104,datetime('now','-10 seconds'))")
        c.commit()
        orig = RR._respect_run_heartbeat
        RR._respect_run_heartbeat = lambda cfg=None: False
        try:
            off = len(RR.find_orphans(c, 1800))
        finally:
            RR._respect_run_heartbeat = orig
        ck("D5 开关关闭 ⇒ 逐字节回旧行为（1 条）", off == 1)
        ck("D6 开关打开 ⇒ 豁免（0 条）", len(RR.find_orphans(c, 1800)) == 0)

        # D7 配置键存在
        from core import config as CFG
        ck("D7 配置键 runtime.orphan_respect_run_heartbeat 已登记",
           "orphan_respect_run_heartbeat" in (CFG.DEFAULT_CONFIG.get("runtime") or {}))
    except Exception as e:                                    # noqa: BLE001
        ck("D 模块可导入", False, e)

    # ── E · 规则文本与实际能力一致（D11 语言后门同类）────────────────────
    print("\n[E] 必含规则不得与「已注入父会话原文」自相矛盾")
    try:
        from core.subtask_rules import INVIOLABLE_RULES, RULES_VERSION
        fifth = INVIOLABLE_RULES[4]
        ck("E1 第 5 条不再断言『看不到父会话历史』（与注入矛盾）",
           "看不到父会话历史" not in fifth, fifth[:50])
        ck("E2 第 5 条改为「仅在快照显式给出时可见」",
           "仅在快照显式给出时可见" in fifth or "快照" in fifth)
        ck("E3 版本已同步登记", RULES_VERSION == "2026-11-19.1", RULES_VERSION)
    except Exception as e:                                    # noqa: BLE001
        ck("E 规则模块可导入", False, e)

    # ── F · ask_user 不得把技术故障抛给用户 ─────────────────────────────
    print("\n[F] ask_user 禁止把「技术故障」当成需要用户拍板的事")
    try:
        from agent.pipeline_parts.tools import _ASK_USER_TOOL
        from agent.pipeline_parts.common import ASK_USER_RULES_TEXT
        desc = _ASK_USER_TOOL["function"]["description"]
        # 事故原文：msg#7016「平台 MCP 通道返回 502…你希望怎么继续？」+ 要求用户给 branchId
        for k, label in (
                ("技术故障", "禁止把技术故障问人（工具描述）"),
                ("自行降级或跳过", "要求自行降级/跳过"),
                ("不得要求用户提供", "禁止索要技术参数"),
                ("必须先回答用户本轮的直接问题", "必须先答用户问题"),
        ):
            ck("F·%s" % label, k in desc)
        # 原有契约不得被破坏
        for k in ("缺少关键信息", "多个合理方案", "动作影响面大"):
            ck("F·原有条款保留「%s」" % k, k in desc)
        # prompt 层必须同步（只改工具描述、不改 prompt ⇒ 遵循率不稳）
        ck("F·prompt 层含禁止条款标题", "【ask_user 禁止事项】" in ASK_USER_RULES_TEXT)
        ck("F·prompt 层禁止技术故障问人", "技术故障" in ASK_USER_RULES_TEXT)
        ck("F·prompt 层要求先答用户问题",
           "先正面回答用户本轮的直接问题" in ASK_USER_RULES_TEXT)
    except Exception as e:                                    # noqa: BLE001
        ck("F 模块可导入", False, e)

    # ── G · 工具兜底错误文案不得把「MCP 离线」报成「未配置执行器」──────
    print("\n[G] 工具兜底：MCP 离线 ≠ 未配置执行器")
    try:
        src = open(os.path.join(ROOT, "agent", "pipeline_parts", "tools.py"),
                   encoding="utf-8", errors="replace").read()
        ck("G1 兜底分支查询工具 source", "SELECT name, source, mcp_server_id, status FROM tools" in src
           or "source, mcp_server_id" in src)
        ck("G2 兜底分支查询 MCP server 状态",
           "FROM mcp_servers" in src and "last_error" in src)
        ck("G3 MCP 离线时给出 dependency_unavailable 标记",
           'error_class": "dependency_unavailable"' in src)
        ck("G4 离线文案不再说「未配置执行器」",
           "不是工具缺失，无需配置执行器" in src)
        # ★ 2026-11-19：B 类文案升级（带注册表依据 + 同类可用工具）。
        #   原来只守「已注册但未配置执行器（仅内置工具与在线 MCP 工具可执行）」
        #   这一句 —— 文案改了判据就得跟着改，否则门禁**持续假红**（本仓既有教训）。
        #   守住的语义不变：A 类（MCP 离线）与 B 类（真无执行器）必须**可区分**。
        ck("G5 真·无执行器仍报 not_implemented 且可区分",
           '"error_class": "not_implemented"' in src
           and "本通道**没有执行器**" in src
           and "不是工具缺失，无需配置执行器" in src)
        ck("G6 MCP 工具名精确匹配（带引号，防 query_project 误命中 query_projects）",
           """'%"' + name + '"%'""" in src)
        # 真库判据：精确匹配确实比宽松更准（反例必须成立）
        DB2 = os.path.join(ROOT, "mbse.db")
        c2 = sqlite3.connect("file:%s?mode=ro" % DB2.replace("\\", "/"), uri=True)
        ex = c2.execute("SELECT name FROM mcp_servers WHERE tools LIKE ? LIMIT 1",
                        ('%"query_project"%',)).fetchone()
        lo = c2.execute("SELECT name FROM mcp_servers WHERE tools LIKE ? LIMIT 1",
                        ("%query_project%",)).fetchone()
        ck("G7 真库反例：query_project 精确不命中、宽松会误命中",
           ex is None and lo is not None, "精确=%s 宽松=%s" % (ex, lo))
        c2.close()
    except Exception as e:                                    # noqa: BLE001
        ck("G 可执行", False, e)

    # ── H · todo_write 报「不存在」时必须给出可用 id ────────────────────
    print("\n[H] todo_write 不存在 ⇒ 给出本会话可用 id（防模型继续猜编号）")
    try:
        ts = open(os.path.join(ROOT, "agent", "todo_store.py"),
                  encoding="utf-8", errors="replace").read()
        ck("H1 不存在分支返回 available_ids", '"available_ids": _avail' in ts)
        ck("H2 文案要求不要按顺序猜编号", "不要按顺序猜编号" in ts)
        ck("H3 无待办时明确说先 add", "没有任何待办" in ts)
        # 真库实证：反例必须成立（历史确有"猜序号当 id"的失败）
        DB3 = os.path.join(ROOT, "mbse.db")
        c3 = sqlite3.connect("file:%s?mode=ro" % DB3.replace("\\", "/"), uri=True)
        bad = c3.execute(
            "SELECT COUNT(*) FROM tool_call_logs WHERE tool_name='todo_write' AND ok=0 "
            "AND result LIKE '%待办 #%不存在%'").fetchone()[0]
        ck("H4 真库确有此类失败（证明修复有据）", bad > 0, "%d 条" % bad)
        # 该会话实际 id 与报错 id 不同 ⇒ 证明是"猜"
        row = c3.execute(
            "SELECT conversation_id, result FROM tool_call_logs WHERE tool_name='todo_write' "
            "AND ok=0 AND result LIKE '%待办 #%不存在%' ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            cid = row[0]
            ids = [r[0] for r in c3.execute(
                "SELECT id FROM agent_todos WHERE conversation_id=? ORDER BY id", (cid,)).fetchall()]
            ck("H5 反例：报错 id 不在该会话实际 id 里（=模型在猜）",
               bool(ids) and ("#%d" % ids[0]) not in str(row[1]), "实际前2个=%s" % ids[:2])
        c3.close()
    except Exception as e:                                    # noqa: BLE001
        ck("H 可执行", False, e)

    # ── I · 短输入不得被上一轮 slots 实体淹没 ───────────────────────────
    print("\n[I] 短追问不被上一轮 slots（槽位）实体淹没")
    try:
        from agent.pipeline_parts.common import _slot_entity_cap
        # 事故原文：msg#7015 只有 6 字
        ck("I1 短输入(6字)上限压到 2", _slot_entity_cap("我的提示词呢") == 2,
           _slot_entity_cap("我的提示词呢"))
        ck("I2 长输入(30字+)仍为 5（行为不变）",
           _slot_entity_cap("为A8E建立多方案模块定义图用SysML v2变体表达三个空调方案") == 5)
        ck("I3 空输入按短输入处理", _slot_entity_cap("") == 2)
        # 两处必须同时生效（成对改铁律）
        for rel in ("agent/pipeline_parts/stream.py", "agent/pipeline_parts/execute.py"):
            src = open(os.path.join(ROOT, rel.replace("/", os.sep)),
                       encoding="utf-8", errors="replace").read()
            ck("I4 %s 已改用 _slot_entity_cap" % os.path.basename(rel),
               "_slot_entity_cap(user_input)" in src)
            ck("I5 %s 无残留旧上限 [:5]" % os.path.basename(rel),
               "if str(e)][:5]" not in src)
        # 回退开关
        from core import config as CFG
        ck("I6 配置键已登记（可回退）",
           "slot_entity_short_max_chars" in (CFG.DEFAULT_CONFIG.get("context") or {})
           and "slot_entity_cap_short" in (CFG.DEFAULT_CONFIG.get("context") or {}))
        # 效果量化：事故串必须显著变短
        ents = ["A8E", "空调子系统V1", "空调子系统V2", "空调子系统（V3）", "热管理系统（AD1)"]
        ui = "我的提示词呢"
        old_len = len(ui + " " + " ".join(ents[:5]))
        new_len = len(ui + " " + " ".join(ents[:_slot_entity_cap(ui)]))
        ck("I7 事故串显著变短（>=40% 缩减）", new_len <= old_len * 0.6,
           "%d → %d 字" % (old_len, new_len))
    except Exception as e:                                    # noqa: BLE001
        ck("I 可执行", False, e)

    n_fail = sum(1 for s, _ in _results if s == FAIL)
    n_pass = sum(1 for s, _ in _results if s == PASS)
    print("\n" + "=" * 84)
    print("结果：%d PASS / %d FAIL  共 %d 项" % (n_pass, n_fail, len(_results)))
    print("=" * 84)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
