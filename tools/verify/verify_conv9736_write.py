"""门禁：conv 9736「确认后仍写不进去 / 卡片位置 / 多份重复内容」四项（2026-10-28 第三轮）。

## 被验的真实故障（数据全部取自 mbse.db，不造样本）

用户在 conv 9736 确认写入后，仍然**一次写操作都没发生**，且报了三件事。
逐项真机定位：

| # | 用户看到 | 真实原因（数据） | 判据组 |
|---|---|---|---|
| 1 | 确认后还是没写入 | 模型走 `create_model_elements` 逐元素建，需要 `get_modeling_capabilities` 的合法类型清单；该回执 **47,229 字符**、`hasMore=true`、`creatable=50`，被 offload+截断到 8000 ⇒ 看不到清单 ⇒ 3 次猜 `SEQUENCE_VIEW`（清单里**不存在**此类型）⇒ `INVALID_ARGUMENTS` ×3 被计入失败轮 ⇒ 达上限强制收尾。而 `zhiyuan_sysmlv2_import`（一次调用写完整包）**压根没绑给该 Agent** | A/B/C |
| 2 | 已完成环节卡片被移到代码块上方 | 历史渲染顺序是「正文先、proc 后」，直播是「proc 先、正文后」⇒ 收口时跳位；用户要的最上方 | D |
| 3 | 生成代码仍有**多份相同内容** | 消息 5135：37,620 字符、**7 个代码块 md5 全同（af683702）**，是 7 轮"完整报告+同一份代码"被 `\\n\\n` 无脑累加 | E |

## 运行

    python tools/verify/verify_conv9736_write.py

退出码 0 = 全绿；1 = 有红项。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_PASS = 0
_FAIL = 0
_FAILED = []
CONV = 9736


def check(group, name, ok, detail=""):
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"  [PASS] {group} {name}")
    else:
        _FAIL += 1
        _FAILED.append(f"{group} {name} :: {detail}")
        print(f"  [FAIL] {group} {name} :: {detail}")


def _db():
    p = os.path.join(_ROOT, "mbse.db")
    return sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True)


def _row(sql, args=()):
    with _db() as c:
        return c.execute(sql, args).fetchone()


# ══════════════════════════════════════════════════════════════════════════
# A. 最短路径必须在工具清单里（导入通道）
# ══════════════════════════════════════════════════════════════════════════
def group_a():
    names = ("zhiyuan_sysmlv2_gen", "zhiyuan_sysmlv2_check", "zhiyuan_sysmlv2_import")
    have = {r[0] for r in _db().execute(
        "select t.tool_name from agent_tools t join agents a on a.id=t.agent_id "
        "where a.name='智源对接agent' and t.tool_type='tool'")}
    check("A1", "智源对接agent 已绑定 gen/check/import（一次调用写完整个包）",
          all(n in have for n in names), f"缺 {[n for n in names if n not in have]}")
    # A2：必须在 JIT core_keep 里（否则语义预筛会把它裁掉 —— autofix 已栽过一次）
    from core import config as _cfg
    keep = str(_cfg.get("tool_jit", "core_keep_extra", "") or "")
    check("A2", "三者都在 tool_jit.core_keep_extra（不被语义预筛裁掉）",
          all(n in keep for n in names), keep[:120])
    # A3：导入工具是写类 ⇒ 必须走人工确认（不能变成裸写通道）
    row = _row("select side_effect from tools where name='zhiyuan_sysmlv2_import'")
    hook = _row("select count(*) from tool_hooks where tool_pattern='zhiyuan_sysmlv2_import' "
                "and action='require_confirm' and enabled=1")
    check("A3", "import 仍受「覆盖导入需确认」闸约束（不是裸写）",
          (row and row[0] == "write") and (hook and hook[0] >= 1),
          f"side_effect={row and row[0]} hooks={hook and hook[0]}")
    # A4：真机证据 —— 9736 的工具序列里**没有** import/create
    with _db() as c:
        used = [r[0] for r in c.execute(
            "select tool_name from tool_call_logs where conversation_id=?", (CONV,))]
    check("A4", "真机症状确认：9736 从未调用 import/create（= 最短路径不在眼前）",
          (not any(n in used for n in names)) and ("create_model_elements" not in used),
          f"used={sorted(set(used))[:8]}")

    # A5：★ 真根因 —— 三个插件 author_name='平台内置' 且无安装记录 ⇒ 被可消费过滤剔除
    #   （"从未装过"与"被全局卸载"在该判据下不可区分）
    import sys as _s
    if _ROOT not in _s.path:
        _s.path.insert(0, _ROOT)
    from database import get_db as _gdb
    from plugin_system.store.visibility import consumable_filter as _cf
    _c = _gdb()
    try:
        _keep = _cf(_c, {"id": 1, "username": "gefei", "role_name": "admin"})
        _ids = {r["name"]: r["id"] for r in _c.execute("select id,name from tools")}
        bad = [n for n in names if not _keep("tools", _ids.get(n, -1))]
    finally:
        _c.close()
    check("A5", "三个工具在可消费集合内（系统级可用标记已补齐）",
          not bad, f"仍被剔除 {bad}")
    # A6：注入清单里真的出现（不只"绑了"）
    try:
        import os as _os
        _os.chdir(_ROOT)
        from agent.pipeline import AgentPipeline
        _p = AgentPipeline()
        # ★ 2026-10-28：必须加载 DB 绑定，否则 `get_bound_mcp()` 返回空 ⇒
        #   71 个 MCP 工具一个都不会进候选，判据就成了"验了个空环境"
        #   （本门禁第一版正是如此：只注入 11 个、还全是内置/DB 工具，
        #     于是"import 在不在清单里"这条断言**验的根本不是生产形态**）。
        from database import get_db as _gdb0
        _p.registry.load_from_db(_gdb0())
        _td = _p._build_tools_def(
            "智源对接agent",
            "获取工程数据，设计通信子系统时序图，校验后写入包 SD_通信子系统时序图",
            {"id": 1, "username": "gefei", "role_name": "admin"})
        _names = [t.get("function", {}).get("name") for t in _td]
    except Exception as e:
        _names = []
        print("   （实调 _build_tools_def 失败：%r，A6 按失败处理）" % (e,))
    # A6：注入清单里真的出现**可用的**写入通道（不只"绑了"）
    #   ★ 2026-10-28 语义更新（**不是放宽**）：本判据原先断言
    #     `zhiyuan_sysmlv2_import` 必须在清单里。后来查出该工具是死工具 ——
    #     它的 url 依赖 `{cfg:zhiyuan.base_url}`，而运行配置该值为空，
    #     历史调用 **16 次成功 0 次**；恰恰因为它总在清单里，模型优先用了它
    #     而不是真正能落库的 MCP `import_sysml_text`。
    #   ⇒ 正确的要求变成两条，缺一不可：
    #     ① 能落库的写入通道必须在清单里（MCP `import_sysml_text`）；
    #     ② 未就绪的死工具**必须不在**清单里，且跳过原因要留痕（不得静默）。
    #   只把断言语义换掉，判据强度不变（原来查 1 个名字，现在查 2 个条件）。
    check("A6", "★ 实调 _build_tools_def：可落库的写入通道在清单（MCP import_sysml_text）",
          "import_sysml_text" in _names,
          f"注入 {len(_names)} 个")
    check("A6b", "未就绪的死工具不得注入（zhiyuan_sysmlv2_* 缺 base_url）",
          not any(n.startswith("zhiyuan_sysmlv2_") for n in _names),
          [n for n in _names if n.startswith("zhiyuan_sysmlv2_")])
    check("A6c", "跳过未就绪工具必须留痕（_dropped_unready，不得静默）",
          bool(getattr(_p, "_dropped_unready", None)),
          getattr(_p, "_dropped_unready", None))
    # A7：剔除必须可见（不得再静默）
    _t2 = open(os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py"),
               encoding="utf-8").read()
    check("A7", "可消费过滤的剔除已留痕（logger + 实例属性，不再静默）",
          "_dropped_by_visibility" in _t2 and "被「可消费过滤」剔除" in _t2, "")

    # A8：★ 收敛闸不得直接终止 —— 应注入「停止勘查、立即执行」强指令
    #   真机 9738：模型已定位到目标包、也提高了 import，却把同一 parentId 查 3 次
    #   ⇒ 旧逻辑 break ⇒ 交付里写"下一步我会校验+导入"，本轮一次没做。
    _s3 = open(os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py"),
               encoding="utf-8").read()
    check("A8", "重复调用收敛：先注入强指令并 continue（不再直接 break）",
          "【禁止重复勘查】" in _s3 and "_dup_redirect < 2" in _s3, "仍是直接 break")
    check("A9", "强指令明确要求「import 一次写完 + 回读确认」",
          "zhiyuan_sysmlv2_import" in _s3.split("【禁止重复勘查】")[1][:900]
          if "【禁止重复勘查】" in _s3 else False, "")
    # A10：强指令计数必须请求级清零（单例残留会让新会话直接被终止）
    _c3 = open(os.path.join(_ROOT, "agent", "pipeline_parts", "common.py"),
               encoding="utf-8").read()
    check("A10", "_dup_redirect_n 已登记请求级清零（防跨会话连坐）",
          "pipe._dup_redirect_n = 0" in _c3, "")
    check("A11", "_dropped_by_visibility 已登记请求级清零（防排障跑偏）",
          "pipe._dropped_by_visibility = []" in _c3, "")

    # A12：★ 生成前收口（确认/澄清）**不得引用 llm_info** —— 它那时还没赋值。
    #   真机 9739：第一版在两处各写一份卡片字典、都读 llm_info["used_mock"]
    #   ⇒ `cannot access local variable 'llm_info'` ⇒ 整轮流中断，连澄清卡都发不出。
    _seg = _s3[_s3.find("def _interim_exec_card():"):]
    _seg = _seg[:_seg.find("if _hil_confirm_req:")] if "if _hil_confirm_req:" in _seg else _seg[:2000]
    # 只看**代码行**（注释里本就会复述旧写法，那是解释不是缺陷）
    _code_lines = [ln for ln in _seg.split("\n")
                   if ln.strip() and not ln.strip().startswith("#")]
    _code = "\n".join(_code_lines)
    check("A12", "生成前收口卡片不直接索引 llm_info（用 NameError 兜底）",
          '_li = llm_info if isinstance(llm_info, dict) else {}' in _code
          and 'llm_info["used_mock"]' not in _code,
          "仍直接索引 llm_info")
    check("A13", "确认/澄清三处共用同一份卡片装配（不再各写一份）",
          _s3.count("_interim_exec_card()") >= 4,     # 1 处定义 + 3 处调用
          f"n={_s3.count('_interim_exec_card()')}")

    # ── A14~A17：覆盖导入的目标包一致性闸（防"点了确认就冲掉父包"） ──
    from agent import import_target_guard as _itg
    _q = ("基于工程“”广汽建模方法论1022“获取工程数据，分析并帮我设计输出通信子系统的时序图，"
          "并完成校验后，写入对应的包”SD_通信子系统时序图“，写入前需要我确认")
    _c2 = _db()
    try:
        par, child = _itg.is_parent_of_target(_c2, CONV, "2114902516325482496", _q)
        okp, _ = _itg.is_parent_of_target(_c2, CONV, "2115359040604536832", _q)
    finally:
        _c2.close()
    check("A14", "真机：父包 2114902516325482496 被判为「父包」（会冲掉子树）",
          par and child, f"par={par} child={child}")
    check("A15", "真机：目标包 2115359040604536832 不被误拦（放行）", not okp, "")
    check("A16", "包名候选只取真名（整句不算候选，避免误判 block）",
          _itg.extract_package_names(_q) == ["SD_通信子系统时序图"],
          str(_itg.extract_package_names(_q)))
    check("A17", "import 分支已接闸（父包 ⇒ 拒绝并给出正确取 id 的方法）",
          "WRONG_TARGET_PACKAGE" in _t2 and "is_parent_of_target" in _t2, "未接线")


# ══════════════════════════════════════════════════════════════════════════
# B. 参数类错误不得扣失败轮
# ══════════════════════════════════════════════════════════════════════════
def group_b():
    from agent.loop_guard import LoopGuard, is_correctable_failure

    # B1：用真机失败回执（5767/5770/5773）验证判据
    ok_all = True
    detail = []
    for tid in (5767, 5770, 5773):
        r = _row("select result from tool_call_logs where id=?", (tid,))
        if not r:
            continue
        if not is_correctable_failure({"ok": False, "result": r[0]}):
            ok_all = False
            detail.append(tid)
    check("B1", "真机 INVALID_ARGUMENTS 回执被判为「可修正」（不扣失败轮）",
          ok_all, f"未判中 {detail}")
    # B2：真·工具坏了（超时/异常）不得被误判为可修正
    check("B2", "真故障（超时/异常）不被误判为可修正",
          not is_correctable_failure({"ok": False, "error": "tool_timeout",
                                      "result": "工具执行超过 120s 仍未返回"}),
          "")
    check("B3", "参数截断（args_truncated）算可修正",
          is_correctable_failure({"ok": False, "error": "args_truncated"}),
          "")
    # B4：护栏行为 —— 3 次参数错误不把失败轮打满
    lg = LoopGuard(max_failed_rounds=3)
    lg.charge_failed_round(True)      # 修复后：可修正错误按"本轮有产出"计
    lg.charge_failed_round(True)
    lg.charge_failed_round(True)
    check("B4", "可修正错误不计失败轮 ⇒ 不会 3 次就判死",
          lg.failed_rounds == 0 and lg.failed_rounds_left(), f"failed={lg.failed_rounds}")
    # B5：两路径都已接线（stream / execute）
    for f in ("agent/pipeline_parts/stream.py", "agent/pipeline_parts/execute.py"):
        s = open(os.path.join(_ROOT, f), encoding="utf-8").read()
        check("B5", f"{os.path.basename(f)} 已接 is_correctable_failure",
              "is_correctable_failure" in s, "未接线")
    # B6 变异自证：判据改成恒 False ⇒ B1 必须红
    ok_mut = not is_correctable_failure({"ok": False, "result": "{}"})
    check("B6", "变异自证：空回执不得被判为可修正（判据有区分度）", ok_mut, "")


# ══════════════════════════════════════════════════════════════════════════
# C. 47KB 能力清单必须被压成「合法取值索引」
# ══════════════════════════════════════════════════════════════════════════
def group_c():
    from agent import tool_result_notes as N

    raw = _row("select content from tool_result_offloads where id=2584")
    check("C0", "真机样本存在（47KB 能力清单）", bool(raw), "缺 offload 2584")
    if not raw:
        return
    note = N.build_note("get_modeling_capabilities",
                        {"parentId": "2114902516325482496"}, raw[0])
    # C1：必须远小于原文（原文 5 万字符级）
    check("C1", "判读句 ≤1600 字符（原文 %d）" % len(raw[0]),
          bool(note) and len(note) <= 1600, f"len={len(note)}")
    # C2：必须点名可建类型（含 PACKAGE / ITEM_DEFINITION / MESSAGE_DEFINITION）
    check("C2", "判读句列出可建类型（照抄即可）",
          all(t in note for t in ("PACKAGE", "ITEM_DEFINITION", "MESSAGE_DEFINITION")),
          note[:120])
    # C3：必须**明确反向提示**：不可建的类型别试（RELATION_MAP_VIEW 等）
    check("C3", "判读句标注「当前不支持创建」的类型",
          ("不支持创建" in note) and ("PANEL_VIEW" in note or "RELATION_MAP_VIEW" in note),
          note[300:420])
    # C4：必须给出写入正路（import 一次调用）
    check("C4", "判读句给出最短路径：zhiyuan_sysmlv2_import 一次写完",
          "zhiyuan_sysmlv2_import" in note, "")
    # C5：真机里模型猜的 SEQUENCE_VIEW 不在可建清单（证明"猜必然失败"）
    d = json.loads(raw[0])
    types = {x.get("type") for x in (d.get("creatable") or []) if isinstance(x, dict)}
    check("C5", "真机清单里确实没有 SEQUENCE_VIEW（模型只能靠清单，不能猜）",
          "SEQUENCE_VIEW" not in types, str(sorted(t for t in types if t and "VIEW" in t)))
    # C6 变异自证：把判读关掉 ⇒ C2 必须红
    _saved = N._NOTES.get("get_modeling_capabilities")
    N._NOTES["get_modeling_capabilities"] = lambda a, x: ""
    try:
        mut = N.build_note("get_modeling_capabilities", {}, raw[0])
    finally:
        N._NOTES["get_modeling_capabilities"] = _saved
    check("C6", "变异自证：关掉判读后 C2 判据必须为空", mut == "", repr(mut[:60]))


# ══════════════════════════════════════════════════════════════════════════
# D. 执行过程卡片回到「正文之前」（与直播同序）
# ══════════════════════════════════════════════════════════════════════════
def group_d():
    p = os.path.join(_ROOT, "static", "js", "mods", "05-markdown.js")
    s = open(p, encoding="utf-8").read()
    i_proc = s.find("html += _procHtmlNow;")
    i_body = s.find("html += `<div class=\"body\"")
    check("D1", "历史渲染：proc 先于 body（与直播同序，卡片回到最上方）",
          0 <= i_proc < i_body, f"proc@{i_proc} body@{i_body}")
    # D2：正文之后不得再渲染 proc（第一版改顺序时漏删 ⇒ 执行过程渲染两遍）。
    #   判据不能数 `<div class="proc">`（三元分支里本就有两处字面量），
    #   而要数「往 html 里追加 proc」的动作次数 == 1。
    n_append = s.count("html += _procHtmlNow;")
    n_after_body = s.count("html += `<div class=\"proc\">")
    check("D2", "proc 只被追加一次（正文之后无重复渲染）",
          n_append == 1 and n_after_body == 0,
          f"append={n_append} after_body={n_after_body}")
    # D3：直播路径确实是 proc 在前（对照，防止我们改反了方向）
    live = open(os.path.join(_ROOT, "static", "js", "mods", "12-chatsend.js"),
                encoding="utf-8").read()
    li_proc = live.find('<div class="proc" id="proc-box">')
    li_body = live.find('<div class="body streaming">', li_proc if li_proc > 0 else 0)
    check("D3", "直播路径 proc 在 body 之前（历史改后与之一致）",
          li_proc >= 0 and li_body > li_proc, f"proc@{li_proc} body@{li_body}")


# ══════════════════════════════════════════════════════════════════════════
# E. 多份相同内容不得再堆叠
# ══════════════════════════════════════════════════════════════════════════
def group_e():
    from agent.auto_continue import merge_round_content, _code_blocks

    txt = _row("select content from messages where id=5135")
    check("E0", "真机样本存在（message 5135）", bool(txt), "")
    if not txt:
        return
    blocks = _code_blocks(txt[0])
    import hashlib
    uniq = {hashlib.md5(b.strip().encode()).hexdigest() for b in blocks}
    check("E1", "真机症状确认：5135 有 7 个代码块且 md5 全同（= 用户报的重复）",
          len(blocks) == 7 and len(uniq) == 1, f"blocks={len(blocks)} uniq={len(uniq)}")

    # E2：把真机那 7 段按旧策略顺序喂进合并函数 ⇒ 输出只剩 1 份代码
    parts = txt[0].split("`" * 3)
    rounds = []
    for i in range(0, len(parts), 2):
        seg = parts[i].strip()
        if seg:
            rounds.append(seg + ("\n" + "`" * 3 + parts[i + 1] + "`" * 3
                                 if i + 1 < len(parts) else ""))
    merged = ""
    for r in rounds:
        merged = merge_round_content(merged, r)
    mb = _code_blocks(merged)
    mu = {hashlib.md5(b.strip().encode()).hexdigest() for b in mb}
    check("E2", "真机 7 轮合并后：代码块不再重复（≤2 份、且唯一）",
          len(mb) <= 2 and len(mu) == len(mb),
          f"merged_blocks={len(mb)} uniq={len(mu)} len={len(merged)}")
    check("E3", "合并后长度显著下降（37,620 → 更短）",
          len(merged) < len(txt[0]) * 0.6, f"{len(merged)} vs {len(txt[0])}")

    # E4：不同内容不得被吞（信息保全）
    a = "第一轮：只做了定位"
    b = "第二轮：新增了时序图分析（不同内容）"
    check("E4", "内容确实不同的两轮仍会追加（不丢信息）",
          merge_round_content(a, b) == a + "\n\n" + b, merge_round_content(a, b)[:60])

    # E5：接线（stream 的续跑累加点必须走 merge）
    s = open(os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py"),
             encoding="utf-8").read()
    check("E5", "stream 续跑/强制阻断两处累加都改走 merge_round_content",
          s.count("_ac.merge_round_content(") >= 2, f"n={s.count('_ac.merge_round_content(')}")
    # E6：告知类文本（待续清单）仍为追加（不能被去重吞掉）
    check("E6", "告知类文本仍是追加语义（待续清单不会被去重吞掉）",
          "_full_content + _nt" in s, "")


def main():
    print("=" * 78)
    print("门禁：conv 9736 确认后仍写不进去 / 卡片位置 / 多份重复内容")
    print("=" * 78)
    for fn in (group_a, group_b, group_c, group_d, group_e):
        print(f"\n--- {fn.__name__.upper()} ---")
        fn()
    print("\n" + "=" * 78)
    print(f"结果：PASS {_PASS} / FAIL {_FAIL}")
    if _FAILED:
        print("红项：")
        for x in _FAILED:
            print("  -", x)
    print("=" * 78)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
