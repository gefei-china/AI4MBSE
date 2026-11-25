# -*- coding: utf-8 -*-
"""未闭环项门禁（2026-11-19 · 第二轮通用化改造后）。

## 这个门禁守什么

**A 写入范围三态**（`core/write_scope.py`）
  事故：conv 999999905 msg#7013「帮我优化并验证提示词效果」→ planner 把 t4 派给
  「智源数据读写agent」（id=1032）→ 它先查工程三元组（MCP 502）→ 另一个子任务
  `ask_user` 打断整轮 → 用户要的提示词全文始终没拿到（msg#7016 仅 318 字）。
  真库量化（697 条用户消息）：
    · unknown 519（74%）/ yes 166（24%）/ no 12（1.7%）；
    · **判 no 之后平台写入 = 0 条** ⇒ `no` 可硬拦，零误伤；
    · 判 unknown 之后真写入 6 条（1.2%，如「把那个包删掉」不含「删除」二字）
      ⇒ `unknown` **不能**硬拦，只做「默认只读」措辞。
  ⇒ 三态各自处置，而不是「要/不要写入」二选一。

**B 交付体裁分层**（`workflows/planner.py`）
  事故：msg#7014（10,304 字报告）里「## 二、优化后提示词模板」只有结构说明，
  成品全文 0 字 ⇒ 用户下一句就是「我的提示词呢」（msg#7015）。
  真库量化（170 条样本 + 事故轮 5 个子任务）：
    · 旧词表命中 11.8% ⇒ 漏 100 条；
    · 只按代码占比 ⇒ 命中 67~76%，过宽（本系统天然产出 SysML 代码）；
    · 只按覆盖率 ⇒ 会选 t2（7945 字，cov=0.000）而不是用户要的 t3（5556 字）；
  ⇒ 必须**分层**：L1 可交付措辞 > L2 完整性措辞 > L3 大体量代码，报告体一律否决。

**C 路径纠偏** / **D 执行器注册表**（后者见 verify_tool_executor_registry.py）

**S 变异自证**：拿掉关键因子，结论必须翻转（否则门禁只是在复述实现）。

用法：.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_unclosed_round3.py
退出码：全绿 0 / 有失败 1。
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
DB = os.path.join(ROOT, "mbse.db")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results = []


def ck(tag, ok, detail=""):
    _results.append((PASS if ok else FAIL, tag))
    print("  %-5s %s%s" % (PASS if ok else FAIL, tag, ("  | " + str(detail)) if detail else ""))


def skip(tag, why=""):
    _results.append((SKIP, tag))
    print("  SKIP  %s%s" % (tag, ("  | " + why) if why else ""))


def _ro():
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    return con


# ══════════════════════════════════════════════════════════════════════════
def group_a():
    print("\n[A] 写入范围三态：unknown 不惩罚 / no 才硬拦 / yes 放行")
    from core.write_scope import (scan_write_intent, split_pool, is_write_agent,
                                  readonly_task_note)

    REAL_GOAL = ("生成过程中经历四次校验与修正，是什么原因，我的提示词给的不清晰嘛，"
                 "帮我优化并验证提示词效果，提升AI代码生成的准确性")
    ck("A1 事故目标 ⇒ unknown（既没说写也没说不写）",
       scan_write_intent(REAL_GOAL)["verdict"] == "unknown",
       scan_write_intent(REAL_GOAL)["reason"])
    ck("A2 明确写入 ⇒ yes", scan_write_intent("把生成的模型导入智源工程")["verdict"] == "yes")
    ck("A3 否定优先 ⇒ no",
       scan_write_intent("先不要写入智源，只输出文本给我")["verdict"] == "no")
    ck("A4 「只输出文本」也是收窄 ⇒ no",
       scan_write_intent("只输出文本，不要动平台")["verdict"] == "no")
    ck("A5 弱动词+平台宾语 ⇒ yes（真库 msg#4899「新增一个测试包」原先被判 no）",
       scan_write_intent("在工程里的通信子系统用例包中新增一个测试包")["verdict"] == "yes",
       scan_write_intent("在工程中的用例包中新增一个测试包")["reason"])
    ck("A6 「新增一条待办」不误判为写平台（否则 todo_write 每轮被拦）",
       scan_write_intent("帮我新增一条待办")["verdict"] == "unknown")
    ck("A6b 「删除临时文件」不误判为写平台",
       scan_write_intent("删除临时文件")["verdict"] == "unknown")
    # ★ 2026-11-20（真机 conv 999999952 误判）：弱动词 + **仅建模对象** ⇒ 不是写平台。
    #   原文判 yes ⇒ 规划器派出「定位智源平台坐标」「向草稿版本写入」两个子任务，
    #   白跑 17 次 query_projects + 11 次 query_data_tree（≈8 分钟），
    #   而用户只要一段 SysML 代码。判据：写入需要**位置锚点**（工程里/包中/平台/分支…），
    #   建模对象（模型/包/元素/视图/需求）本身不构成锚点。
    _BUILD_GOAL = ("为 A8E 串并联混动整车建立顶层运行需求集 '热管理相关需求集'，"
                   "生成符合 SysML v2 规范的需求视图代码")
    ck("A6c 「建立…需求集 + 生成视图代码」⇒ unknown（不派平台写入子任务）",
       scan_write_intent(_BUILD_GOAL)["verdict"] == "unknown",
       scan_write_intent(_BUILD_GOAL)["reason"])
    ck("A6d 同一句话只要加上位置锚点就仍是写入 ⇒ yes（判据没被放宽）",
       scan_write_intent(_BUILD_GOAL + "，并写入工程里的整车IR需求包中")["verdict"] == "yes",
       scan_write_intent(_BUILD_GOAL + "，并写入工程里的整车IR需求包中")["reason"])
    ck("A7 写类 Agent 识别（真机 id=1032 名字带读写）",
       is_write_agent("智源数据读写agent") is True)
    ck("A7b 普通 Agent 不误判",
       is_write_agent("design") is False and is_write_agent("report_generation") is False)

    ck("A8 只读约束文本：unknown 有、yes 无",
       bool(readonly_task_note("unknown")) and readonly_task_note("yes") == "")
    ck("A8b 只读约束文本：no 是硬约束措辞", "硬约束" in readonly_task_note("no"))
    pool = ["requirement_analysis", "design", "智源数据读写agent", "report_generation"]
    keep_no, drop_no = split_pool(pool, False)
    ck("A9 split_pool(False) 剔写类 Agent", drop_no == ["智源数据读写agent"], drop_no)
    keep_yes, drop_yes = split_pool(pool, True)
    ck("A9b split_pool(True) 原样保留", keep_yes == pool and drop_yes == [])
    keep_n, _dn = split_pool(["智源数据读写agent"], False)
    ck("A9c 安全阀：池子会空 ⇒ 退回原池", keep_n == ["智源数据读写agent"])

    src = open(os.path.join(ROOT, "workflows", "planner.py"), encoding="utf-8").read()
    ssrc = open(os.path.join(ROOT, "agent", "pipeline_parts", "stream.py"), encoding="utf-8").read()
    tsrc = open(os.path.join(ROOT, "agent", "pipeline_parts", "tools.py"), encoding="utf-8").read()

    ck("A10 非流式：只有 verdict==no 才剔池",
       'if _wr_verdict == "no":' in src and "_split_pool(agents, False)" in src)
    ck("A10b 非流式：三个提示分支齐备",
       all(k in src for k in ("明确要求写入平台", "明确不需要写入平台", "没有说明")))
    ck("A10c 流式：同一判定器（不另写一份）",
       "scan_write_intent" in ssrc and "self._wr_verdict" in ssrc)
    ck("A10d 流式：子任务透传只读标记", "_orch_readonly" in ssrc)
    i_ws = src.find("把「本轮要不要写入平台」明说给规划器")
    i_dg = src.find("        if artifact_digest:", src.find("def _planner_core"))
    ck("A11 写入范围段在摘要注入块之前（保多轮门禁 R3/R4）",
       0 < i_ws < i_dg, "ws@%d digest@%d" % (i_ws, i_dg))
    ck("A12 只读约束注入到**每个**任务（不只写类 Agent 的任务）",
       "_ro_note)" in src and "_rtn(_wr_verdict)" in src)
    ck("A12b 守卫读三态而非布尔", "_ro_note = _rtn(_wr_verdict)" in src)

    i_ro = tsrc.find('if getattr(self, "_orch_readonly", False):')
    i_store = tsrc.find('if getattr(self, "_orch_subtask", False):')
    ck("A13 执行侧闸在「暂存写请求」之前（明确不做的事不必生成确认单）",
       0 < i_ro < i_store, "readonly@%d store@%d" % (i_ro, i_store))
    ck("A13b 只读拒绝带 error_class（可统计）", 'error_class": "readonly_scope"' in tsrc)
    ck("A13c 豁免集与暂存分支共用同一对常量",
       tsrc.count("_REPAIR_WRITE_EXEMPT") >= 4 and tsrc.count("_PROPOSE_WRITE_EXEMPT") >= 4)

    if os.path.exists(DB):
        con = _ro()
        try:
            row = con.execute("SELECT agent_id FROM agent_tasks WHERE conversation_id=999999905 "
                              "AND task_key='t4' ORDER BY id DESC LIMIT 1").fetchone()
            g = con.execute("SELECT content FROM messages WHERE conversation_id=999999905 "
                            "AND id=7013").fetchone()
        finally:
            con.close()
        if row and g:
            ck("A14 真库复现：t4 确被派给写类 Agent", is_write_agent(row["agent_id"]),
               row["agent_id"])
            ck("A14b 真库复现：当轮目标 verdict=unknown（不是「明确不要写入」）",
               scan_write_intent(g["content"])["verdict"] == "unknown")
        else:
            skip("A14 真库复现", "找不到 conv 999999905 的 t4/7013")


def group_a2():
    print("\n[A2] 执行侧闸的功能自证（真跑 _exec_tool_call）")
    try:
        from agent import AgentPipeline
        p = AgentPipeline()
        p._load_db_agents()
        p._orch_subtask = True
        p._orch_readonly = True
        p._tool_run_ctx = 0
        p._tool_task_key = "t1"
        r1 = p._exec_tool_call("save_artifact", {"title": "t", "content": "c"})
        ck("A15 只读时 save_artifact 被拒", r1.get("ok") is False
           and r1.get("error_class") == "readonly_scope", r1.get("error_class"))
        r2 = p._exec_tool_call("create_model_elements", {"elements": []})
        ck("A15b 只读时 MCP 写工具被拒（动词族判定生效）",
           r2.get("ok") is False and r2.get("error_class") == "readonly_scope",
           r2.get("error_class"))
        r3 = p._exec_tool_call("sysml_v2_autofix", {})
        ck("A15c 只读时 autofix **不被拦**（豁免集生效，校验-修复闭环不卡死）",
           r3.get("error_class") != "readonly_scope", r3.get("error_class") or "not_blocked")
        r4 = p._exec_tool_call("graph_retrieve", {"query": "sysml"})
        ck("A15d 只读时读工具放行", r4.get("ok") is True)
        p._orch_readonly = False
        r5 = p._exec_tool_call("save_artifact", {"title": "t", "content": "c"})
        ck("A15e 非只读时 save_artifact 不被只读闸拦（走暂存）",
           r5.get("error_class") != "readonly_scope", str(r5.get("result"))[:40])
    except Exception as e:                                   # noqa: BLE001
        ck("A15 执行侧闸功能自证", False, "异常：%s" % str(e)[:120])


# ══════════════════════════════════════════════════════════════════════════
def group_b():
    print("\n[B] 交付体裁分层：L1 可交付 > L2 完整性 > L3 大体量代码；报告体否决")
    from workflows.planner import (pick_direct_deliverable, ensure_direct_delivery,
                                   deliverable_coverage, _deliver_body, _form_profile)

    def mk(tk, title, body, eo):
        return ({"task_key": tk, "title": title, "result": body},
                {"key": tk, "title": title, "config": {"expected_output": eo}})

    prompt_body = "\n".join(["## 角色与范围\n你是建模助手，只输出 SysML v2 文本。",
                             "## 变体约束\n三个方案必须用变体基型表达。",
                             "## 命名规范\n元素名用单引号英文标识。",
                             "## 输出格式\n每张视图先写 view 声明。",
                             "## 自检条款\n输出前逐条核对 5 项。"] * 5)
    i1, p1 = mk("t3", "产出提示词模板", prompt_body, "优化版提示词模板全文，可直接投喂使用")
    q = pick_direct_deliverable([i1], [p1])
    ck("B1 L1（可直接投喂）命中", bool(q) and q["level"] == 1 and q["task_key"] == "t3",
       q and (q["level"], q["marks"]))

    i2, p2 = mk("t2", "问题清单", prompt_body, "提示词问题清单：逐条可追溯")
    ck("B2 无 L1/L2/L3 信号 ⇒ 不选", pick_direct_deliverable([i2], [p2]) is None)

    i3, p3 = mk("t1", "复盘", prompt_body, "逐轮复盘表，含完整清单")
    q3 = pick_direct_deliverable([i3], [p3])
    ck("B3 L2（完整清单）命中", bool(q3) and q3["level"] == 2, q3 and q3["level"])

    code_body = "## 代码\n```sysml\n" + "\n".join("part def P%d;" % i for i in range(160)) + "\n```"
    i4, p4 = mk("t1", "补齐元素", code_body, "产出补齐 45 个缺失元素的 SysML v2 代码文件")
    q4 = pick_direct_deliverable([i4], [p4])
    ck("B4 L3（大体量代码）命中", bool(q4) and q4["level"] == 3,
       q4 and (q4["level"], q4["code_ratio"]))

    rep_body = ("# 校验报告\n\n## 摘要\n本报告…\n\n## 结论\n通过。\n\n## 建议\n继续。\n\n"
                "## 风险\n无。\n" + "分析正文。" * 150)
    i5, p5 = mk("t2", "校验报告", rep_body, "校验报告：verdict=pass，含结论与建议")
    ck("B5 报告体否决（小节大半是结论/建议）",
       pick_direct_deliverable([i5], [p5]) is None, _form_profile(rep_body))

    if os.path.exists(DB):
        con = _ro()
        try:
            rows = con.execute("SELECT task_key,title,result,expected_output FROM agent_tasks "
                               "WHERE conversation_id=999999905 AND id BETWEEN 3060 AND 3064 "
                               "ORDER BY id").fetchall()
            rep = con.execute("SELECT content FROM messages WHERE id=7014").fetchone()
        finally:
            con.close()
        if rows:
            items = [{"task_key": r["task_key"], "title": r["title"], "result": r["result"]}
                     for r in rows]
            plan = [{"key": r["task_key"], "title": r["title"],
                     "config": {"expected_output": r["expected_output"]}} for r in rows]
            qq = pick_direct_deliverable(items, plan)
            ck("B6 真库事故轮：选中 t3（用户追问的提示词模板）",
               bool(qq) and qq["task_key"] == "t3", qq and (qq["task_key"], qq["level"]))
            t2 = next((r for r in rows if r["task_key"] == "t2"), None)
            t3 = next((r for r in rows if r["task_key"] == "t3"), None)
            ck("B6b 反例成立：t2 比 t3 更长（证明不是按长度选）",
               bool(t2 and t3) and len(t2["result"] or "") > len(t3["result"] or ""),
               "t2=%d t3=%d" % (len(t2["result"] or ""), len(t3["result"] or "")))
            if rep and t2 and t3:
                cov2 = deliverable_coverage(t2["result"] or "", rep["content"])
                cov3 = deliverable_coverage(t3["result"] or "", rep["content"])
                ck("B6c 反例成立：t2 覆盖率不高于 t3（证明不是按覆盖率选）",
                   cov2 <= cov3 + 0.01, "cov t2=%.3f t3=%.3f" % (cov2, cov3))

    low = "# 报告\n\n## 一、背景\n用户希望优化提示词。\n\n## 二、模板结构\n### 2.1 结构\n共五节。\n"
    out_low, info_low = ensure_direct_delivery(low, q)
    out_hi, info_hi = ensure_direct_delivery("# R\n\n" + prompt_body, q)
    ck("B7 低覆盖 ⇒ 前置直答区", info_low["applied"] is True and info_low["coverage"] < 0.6,
       info_low)
    ck("B7b 前置后正文以「## 直接交付物：」开头", out_low.startswith("## 直接交付物："))
    ck("B7c 前置内容无损（原文在）", prompt_body.strip() in out_low)
    ck("B8 高覆盖 ⇒ 不动正文", info_hi["applied"] is False and out_hi == "# R\n\n" + prompt_body)
    ck("B8b 无成品承诺 ⇒ 不改写", ensure_direct_delivery(low, None)[1]["applied"] is False)

    streamish = "【结构化摘要】状态=done；风险=无\n【完整交付物】\n" + prompt_body
    ck("B9 流式前缀被剥离", _deliver_body(streamish).startswith("## 角色与范围"))
    ck("B9b 摘要行不计入覆盖率",
       deliverable_coverage(_deliver_body(streamish), "状态=done；风险=无") < 0.1)

    src = open(os.path.join(ROOT, "workflows", "planner.py"), encoding="utf-8").read()
    ck("B10 prompt 硬要求按层级措辞",
       "可直接使用的成品" in src and "完整本体（全文）" in src and "大体量代码交付" in src)
    ck("B11 出口兜底在 dedupe/ordfix 之后",
       0 < src.find("content, _no = _ordfix(content)")
       < src.find("ensure_direct_delivery(content, _primary)"))
    ck("B11b 前置后再收敛代码块（避免同一模型两份）",
       'if _dd.get("applied"):' in src and "_n2 = _dedupe(content)" in src)

    if os.path.exists(DB):
        con = _ro()
        try:
            rows = con.execute("SELECT conversation_id,run_id,task_key,title,result,"
                               "expected_output FROM agent_tasks WHERE length(result)>400 "
                               "ORDER BY id").fetchall()
        finally:
            con.close()
        groups = {}
        for r in rows:
            groups.setdefault((r["conversation_id"], r["run_id"]), []).append(dict(r))
        tot = hit = 0
        lv = {}
        for _k, g in groups.items():
            if len(g) < 2:
                continue
            tot += 1
            pp = pick_direct_deliverable(
                [{"task_key": x["task_key"], "title": x["title"], "result": x["result"]} for x in g],
                [{"key": x["task_key"], "title": x["title"],
                  "config": {"expected_output": x["expected_output"]}} for x in g])
            if pp:
                hit += 1
                lv[pp["level"]] = lv.get(pp["level"], 0) + 1
        rate = hit / float(tot or 1)
        # 上限 0.45 不是拍的：曾有一版允许「eo 为空只凭形态判」，前置率飙到 69.4%
        # （L3 命中 78/121），实测过宽 —— 本系统子任务天然产代码，"代码占比高"没区分度。
        ck("B12 全库前置率在合理区间（5%~45%，过高=判据过宽、过低=失效）",
           0.05 <= rate <= 0.45, "%d/%d = %.1f%%" % (hit, tot, 100 * rate))
        ck("B12c L3 不得吃掉大半批次（>60% 说明形态判据失去区分度）",
           lv.get(3, 0) / float(tot or 1) <= 0.60,
           "L3=%d/%d" % (lv.get(3, 0), tot))
        ck("B12b 层级分布非单一（分层真的在分流）", len(lv) >= 2, lv)


# ══════════════════════════════════════════════════════════════════════════
def group_c():
    print("\n[C] 路径纠偏：不回列「不存在的路径」")
    import file_tools as ft

    r1 = ft.exec_file_tool("file_read", {"path": "workflows/planer.py"})
    ck("C1 拼错文件名 ⇒ 给出相似项", r1["ok"] is False and "同目录相似项" in r1["result"])
    ck("C1b 相似项里含正确文件", "planner.py" in r1["result"])
    r2 = ft.exec_file_tool("file_read", {"path": "no_such_dir_xyz/a/b/foo.sysml"})
    ck("C2 目录不存在 ⇒ 上溯后列现有", r2["ok"] is False and "该目录下现有" in r2["result"])
    r3 = ft.exec_file_tool("file_list", {"path": "no_such_dir_xyz"})
    ck("C2b file_list 同样给候选", r3["ok"] is False and "可改用" in r3["result"])
    ck("C2c 隐藏文件不优先列出", ".git、" not in r2["result"])

    src = open(os.path.join(ROOT, "services/tools/file_tools.py"), encoding="utf-8").read()
    ck("C3 三个入口都走 _miss", src.count("_miss(") >= 4, src.count("_miss("))
    orig = ft._path_suggest_on
    try:
        ft._path_suggest_on = lambda: False
        r4 = ft.exec_file_tool("file_read", {"path": "workflows/planer.py"})
        ck("C4 开关关闭时不追加建议", "可改用" not in r4["result"])
    finally:
        ft._path_suggest_on = orig


# ══════════════════════════════════════════════════════════════════════════
def group_s():
    print("\n[S] 变异自证：拿掉关键因子，结论必须翻转")
    import core.write_scope as ws
    import workflows.planner as WP
    from workflows.planner import ensure_direct_delivery, pick_direct_deliverable

    body = "\n".join(["## 角色与范围\n你是建模助手，只输出 SysML v2 文本。",
                      "## 变体约束\n三个方案必须用变体基型表达。",
                      "## 命名规范\n元素名用单引号英文标识。",
                      "## 输出格式\n每张视图先写 view 声明。",
                      "## 自检条款\n输出前逐条核对 5 项。"] * 6)
    q = {"task_key": "t3", "title": "模板", "text": body, "level": 1}
    low = "# 报告\n\n## 一、背景\n优化提示词。\n"
    ck("S1 基线：min_cov 默认 ⇒ 前置", ensure_direct_delivery(low, q)[1]["applied"] is True)
    ck("S2 变异：min_cov=0 ⇒ 不前置（阈值承重）",
       ensure_direct_delivery(low, q, min_cov=0.0)[1]["applied"] is False)

    _neg = ws.NEGATORS
    try:
        ws.NEGATORS = ()
        ck("S3 变异：清空否定词 ⇒「不要写入智源」被误判为 yes",
           ws.scan_write_intent("不要写入智源")["verdict"] == "yes")
    finally:
        ws.NEGATORS = _neg
    ck("S3b 恢复后否定重新生效",
       ws.scan_write_intent("不要写入智源")["verdict"] == "no")

    _wk = ws.WEAK_WRITE_VERBS
    try:
        ws.WEAK_WRITE_VERBS = ()
        ck("S4 变异：清空弱动词 ⇒ 真库 msg#4899「新增测试包」漏判",
           ws.scan_write_intent("在工程里的用例包中新增一个测试包")["verdict"] != "yes")
    finally:
        ws.WEAK_WRITE_VERBS = _wk
    ck("S4b 恢复后重新命中",
       ws.scan_write_intent("在工程里的用例包中新增一个测试包")["verdict"] == "yes")

    _o = ws.PLATFORM_OBJECTS
    try:
        ws.PLATFORM_OBJECTS = ()
        ck("S5 变异：清空平台宾语 ⇒ 弱动词不判写（宾语是必要条件）",
           ws.scan_write_intent("在工程里的用例包中新增一个测试包")["verdict"] == "unknown")
    finally:
        ws.PLATFORM_OBJECTS = _o

    _m = ws.WRITE_AGENT_MARKS
    try:
        ws.WRITE_AGENT_MARKS = ()
        ck("S6 变异：清空 Agent 标记 ⇒ 写类 Agent 不再被剔",
           ws.split_pool(["智源数据读写agent", "design"], False)[1] == [])
    finally:
        ws.WRITE_AGENT_MARKS = _m
    ck("S6b 恢复后重新剔除",
       ws.split_pool(["智源数据读写agent", "design"], False)[1] == ["智源数据读写agent"])

    rep_body = ("# 校验报告\n\n## 摘要\n本报告…\n\n## 结论\n通过。\n\n## 建议\n继续。\n"
                + "分析正文。" * 250)
    it = [{"task_key": "t2", "title": "校验报告", "result": rep_body}]
    pl = [{"key": "t2", "title": "校验报告",
           "config": {"expected_output": "校验报告全文，可直接使用"}}]
    ck("S7 基线：报告体被否决（即使判据写了「可直接使用」）",
       pick_direct_deliverable(it, pl) is None)
    _orig = WP._is_report_body
    try:
        WP._is_report_body = lambda prof: False
        ck("S7b 变异：拿掉报告体否决 ⇒ 报告被选中（否决承重）",
           pick_direct_deliverable(it, pl) is not None)
    finally:
        WP._is_report_body = _orig

    two = ([{"task_key": "t2", "title": "清单", "result": body * 2},
            {"task_key": "t3", "title": "模板", "result": body}],
           [{"key": "t2", "title": "清单", "config": {"expected_output": "问题清单：逐条可追溯"}},
            {"key": "t3", "title": "模板", "config": {"expected_output": "模板全文，可直接投喂"}}])
    _l1, _l2 = WP._DIRECT_READY_MARKS, WP._DIRECT_FULL_MARKS
    try:
        WP._DIRECT_READY_MARKS = ()
        WP._DIRECT_FULL_MARKS = ()
        a = pick_direct_deliverable(*two)
        ck("S8 变异：清空 L1/L2 措辞 ⇒ 只剩形态（选更长的 t2，证明措辞是主判据）",
           a is None or a["task_key"] == "t2", a and a["task_key"])
    finally:
        WP._DIRECT_READY_MARKS, WP._DIRECT_FULL_MARKS = _l1, _l2
    b = pick_direct_deliverable(*two)
    ck("S8b 恢复后选 t3（L1 压过长度）", bool(b) and b["task_key"] == "t3", b and b["task_key"])


def main():
    print("=" * 84)
    print("未闭环项门禁（2026-11-19 · 通用化后）")
    print("  repo=%s" % ROOT)
    print("=" * 84)
    group_a()
    group_a2()
    group_b()
    group_c()
    group_s()

    fails = [t for s, t in _results if s == FAIL]
    skips = [t for s, t in _results if s == SKIP]
    print("\n" + "-" * 84)
    print("合计 %d 项（PASS %d / FAIL %d / SKIP %d）"
          % (len(_results), len(_results) - len(fails) - len(skips), len(fails), len(skips)))
    if fails:
        for t in fails:
            print("  · FAIL " + t)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
