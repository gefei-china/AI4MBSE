"""门禁：会话 9730「还是没有持续走下去」四项根因（2026-10-28 第二轮）。

## 被验的真实故障（数据全部取自 mbse.db，不造样本）

同一条用户输入、开关已开（卡片 `exec.gates` 两项皆 true），
仍然以「工具轮次已用尽 / 未校验 / 未写入」收尾。四项根因：

| # | 根因 | 真机证据 | 判据组 |
|---|---|---|---|
| 1 | 预算按**原始 token** 记账，92% 是缓存命中却按全价累计 | `llm_usage_stats`：270,433 prompt 中 249,344 是 cache hit；真实新增仅 21,089 miss + 10,600 输出 ⇒ 判「已用 86%」 | A |
| 2 | 降级闸在 70% 处**直接停**，而当时剩 14%≈2~3 轮 | stage：`token 预算已用 86%（阈值 70%）⇒ 降级收尾` | B |
| 3 | 待续清单生成了但**被 `_full_content` 覆盖**，用户一字未见 | 答复里无「分段收尾」、无「待续」 | C |
| 4 | `todo_write` 有工具、**提示词从未提过** ⇒ 模型不建清单 ⇒ 待办闸恒 fallback | 9726/9730 的 `tool_call_logs` 中 `todo_write` **0 次**，`agent_todos` 0 行 | D |

## 运行

    python tools/verify/verify_conv9730_continue.py

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
CONV = 9730


def check(group: str, name: str, ok: bool, detail: str = "") -> None:
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


def _real_usage():
    """取真机一轮的真实 usage 形态（prompt / cache hit / miss / completion）。"""
    with _db() as c:
        row = c.execute("select prompt_tokens, prompt_cache_hit_tokens, "
                        "prompt_cache_miss_tokens, completion_tokens, total_tokens "
                        "from llm_usage_stats where conversation_id=? "
                        "and prompt_cache_hit_tokens>0 order by id limit 1", (CONV,)).fetchone()
    if not row:
        raise SystemExit("缺少真机样本：llm_usage_stats 无缓存字段")
    return row


# ══════════════════════════════════════════════════════════════════════════
# A. 预算记账口径（缓存命中不得按全价累计）
# ══════════════════════════════════════════════════════════════════════════
def group_a():
    from agent.loop_guard import (LoopGuard, charged_tokens, tokens_of,
                                  tokens_total_of, CACHE_HIT_WEIGHT)

    pt, hit, miss, ct, total = _real_usage()
    resp = {"_meta": {"token_count": total, "cache_hit_tokens": hit,
                      "cache_miss_tokens": miss, "completion_tokens": ct}}
    ch = charged_tokens(resp)
    # A1：真机一帧 —— 计费等效必须远小于原始吞吐（该帧 92% 是缓存命中）
    check("A1", "真机一帧：计费等效 << 原始吞吐",
          ch < total * 0.35,
          f"charged={ch} raw={total} hit={hit} miss={miss} ct={ct}")
    # A2：等价性 —— charged ≈ miss + ct + hit×0.1（容差 2）
    expect = miss + ct + hit * CACHE_HIT_WEIGHT
    check("A2", "计费等效 = miss + completion + hit×权重",
          abs(ch - int(expect)) <= 2, f"charged={ch} expect={int(expect)}")
    # A3：`tokens_of`（预算唯一入口）必须等于 charged，而不是原始总量
    check("A3", "tokens_of 走计费口径（非原始总量）",
          tokens_of(resp) == ch and tokens_total_of(resp) == total,
          f"tokens_of={tokens_of(resp)} ch={ch} raw={tokens_total_of(resp)}")
    # A4：无缓存字段（Mock/老 provider）⇒ 退回原始值，零行为漂移
    plain = {"_meta": {"token_count": 5000}}
    check("A4", "无缓存字段 ⇒ 退回原始总量（零漂移）",
          charged_tokens(plain) == 5000 and tokens_total_of(plain) == 5000, "")
    # A5：真机 14 轮复算 —— 旧口径越线、新口径不越线（这是"为什么停"的直接对照）
    lg = LoopGuard(token_budget=300000)
    for _ in range(14):
        lg.add_llm(resp)
    lg_old = LoopGuard(token_budget=300000)
    for _ in range(14):
        lg_old.add_tokens(total)
    check("A5", "真机 14 轮：旧口径已过降级线、新口径未过",
          lg_old.budget_ratio() >= 0.7 and lg.budget_ratio() < 0.7,
          f"旧 {lg_old.budget_ratio():.1%} / 新 {lg.budget_ratio():.1%}")
    # A6：原始吞吐仍被完整记录（两条口径都要能回答）
    check("A6", "原始吞吐单独记账（可对拍）",
          lg.tokens_total_raw == total * 14, f"{lg.tokens_total_raw}")
    # A7 接线：候选调用点必须都走 add_llm（不再手工拼 tokens_of）
    bad = []
    for f in ("agent/pipeline_parts/stream.py", "agent/pipeline_parts/execute.py",
              "agent/pipeline_parts/autopilot.py"):
        s = open(os.path.join(_ROOT, f), encoding="utf-8").read()
        if "add_tokens(" in s:
            bad.append(f)
    check("A7", "预算记账统一走 add_llm（无手工 add_tokens 残留）",
          not bad, str(bad))
    # A8：config 键存在（可回退到旧口径）
    src = open(os.path.join(_ROOT, "core", "config.py"), encoding="utf-8").read()
    check("A8", "cache_hit_weight 配置项存在（置 1.0 = 旧口径）",
          '"cache_hit_weight"' in src, "")


# ══════════════════════════════════════════════════════════════════════════
# B. 预算吃紧 ⇒ 收紧续跑（不是直接停）
# ══════════════════════════════════════════════════════════════════════════
def group_b():
    from agent.auto_continue import decide

    v = {"done": False, "next_step": "调用 sysml_v2_validate 校验"}
    # B1：真机现场复现 —— 86% 且预算未耗尽 ⇒ 必须**继续**（这正是 9730 停掉的位置）
    cont, why = decide(v, used=1, max_used=8, seen_steps=[],
                       budget_left=True, budget_ratio=0.86)
    check("B1", "真机 86% 现场：应续跑而非收尾",
          cont and ("收紧续跑" in why), f"cont={cont} why={why}")
    # B2：收紧语义必须写进理由（供消息侧追加"只做剩余关键步骤"）
    check("B2", "理由串含「收紧续跑」标记（消息侧据此追加收紧指令）",
          "收紧续跑" in why, why)
    # B3：到硬停线 ⇒ 停，且理由含「降级收尾」（用户可见待续清单）
    cont3, why3 = decide(v, used=1, max_used=8, seen_steps=[],
                         budget_left=True, budget_ratio=0.96)
    check("B3", "硬停线（0.96）⇒ 降级收尾", (not cont3) and ("降级收尾" in why3), why3)
    # B4：预算真耗尽 ⇒ 停（无论比例）
    cont4, why4 = decide(v, used=1, max_used=8, seen_steps=[],
                         budget_left=False, budget_ratio=1.0)
    check("B4", "预算真耗尽 ⇒ 降级收尾", (not cont4) and ("降级收尾" in why4), why4)
    # B5：已完成优先（已做完不必提预算）
    cont5, why5 = decide({"done": True}, used=1, max_used=8, seen_steps=[],
                         budget_left=False, budget_ratio=1.2)
    check("B5", "已完成优先于预算", (not cont5) and ("已完成" in why5), why5)
    # B6：低占用仍普通续跑（未被收紧语义污染）
    cont6, why6 = decide(v, used=1, max_used=8, seen_steps=[],
                         budget_left=True, budget_ratio=0.3)
    check("B6", "低占用 ⇒ 普通续跑（措辞不变）",
          cont6 and ("收紧" not in why6) and ("自动继续" in why6), why6)
    # B7：两路径都必须传 hard_stop_ratio（同构接线，缺一处即错位）
    for f in ("agent/pipeline_parts/stream.py", "agent/pipeline_parts/execute.py"):
        s = open(os.path.join(_ROOT, f), encoding="utf-8").read()
        check("B7", f"{os.path.basename(f)} 已传 hard_stop_ratio",
              "hard_stop_ratio=_hard_stop_ratio" in s, "未接线")
    # B8：续跑时两路径都追加「预算吃紧」收紧指令
    for f in ("agent/pipeline_parts/stream.py", "agent/pipeline_parts/execute.py"):
        s = open(os.path.join(_ROOT, f), encoding="utf-8").read()
        check("B8", f"{os.path.basename(f)} 收紧续跑时追加禁用新检索的指令",
              "【预算吃紧】" in s, "未追加收紧指令")
    # B9 变异自证：把阈值语义退回"到 degrade 就停" ⇒ B1 必须翻红
    def _old(vd, **kw):
        if kw.get("budget_ratio", 0) >= 0.7:
            return False, "token 预算已用 70% 以上 ⇒ 降级收尾"
        return decide(vd, **kw)

    c_old, w_old = _old(v, used=1, max_used=8, seen_steps=[],
                        budget_left=True, budget_ratio=0.86)
    check("B9", "变异自证：退回旧阈值语义后 B1 必须翻红",
          (not c_old) and ("收紧续跑" not in w_old), w_old)


# ══════════════════════════════════════════════════════════════════════════
# C. 收尾告知不得被 `_full_content` 覆盖
# ══════════════════════════════════════════════════════════════════════════
def group_c():
    src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py"),
               encoding="utf-8").read()
    # C1：真机症状 —— 9730 的答复里没有清单（证明"生成了但没落地"）
    with _db() as c:
        row = c.execute("select content from messages where conversation_id=? "
                        "and role='assistant' order by id desc limit 1", (CONV,)).fetchone()
    txt = (row[0] if row else "") or ""
    check("C1", "真机症状确认：9730 答复中无「分段收尾/待续」（= 被覆盖）",
          ("分段收尾" not in txt) and ("待续" not in txt), txt[-80:])
    # C2：降级清单必须同时写入 `_full_content`
    seg = src[src.find('if (not _cont) and ("降级收尾"'):]
    seg = seg[:seg.find("# 评估发生过就透传") if "# 评估发生过就透传" in seg else 1200]
    check("C2", "降级清单同写 llm_content 与 _full_content",
          "_full_content = ((_full_content + _nt)" in seg, seg[:120])
    # C3：harness 强制放行的"未完成"告知同样两处都写
    check("C3", "强制放行告知同样两处都写（不得被覆盖）",
          "_full_content = ((_full_content + \"\\n\\n\" + _notice)" in src, "")
    # C4：覆盖点存在（说明这个坑是真实机制，不是猜测）
    check("C4", "确认存在覆盖点 `llm_content = _full_content`（坑的机制）",
          "llm_content = _full_content" in src, "")
    # C5：清单文案含三要素（为什么停/还差什么/怎么继续）
    from agent.auto_continue import build_degrade_notice
    nt = build_degrade_notice(["跑 sysml_v2_validate"], 0.96, 0.95)
    check("C5", "待续清单三要素齐全",
          ("分段收尾" in nt) and ("待续事项" in nt) and ("继续" in nt), nt[:80])


# ══════════════════════════════════════════════════════════════════════════
# D. 待办清单：工具在，提示词必须也教它用
# ══════════════════════════════════════════════════════════════════════════
def group_d():
    from agent.pipeline_parts.common import _todo_rules_suffix, TODO_RULES_TEXT

    # D1：真机症状 —— 两次同题会话都没有 todo_write（工具可用却没人用）
    with _db() as c:
        n = c.execute("select count(*) from tool_call_logs where conversation_id in (9726,9730) "
                      "and tool_name='todo_write'").fetchone()[0]
        rows = c.execute("select count(*) from agent_todos where conversation_id in (9726,9730)"
                         ).fetchone()[0]
    check("D1", "真机症状确认：9726/9730 的 todo_write 调用 0 次、待办 0 行",
          n == 0 and rows == 0, f"calls={n} todos={rows}")
    # D2：开启时提示词必须有指引
    from core import config as _cfg
    old = bool(_cfg.as_bool("agent", "todo_enabled", False))
    try:
        _cfg.save_override({"agent.todo_enabled": True})
        on = _todo_rules_suffix()
        check("D2", "开启后提示词含 todo_write 指引",
              ("todo_write" in on) and ("验收回执" in on), on[:80])
        check("D3", "指引覆盖四要素（什么时候建/verify/回执/blocked）",
              all(k in on for k in ("3 步以上", "verify", "回执", "blocked")), "")
        _cfg.save_override({"agent.todo_enabled": False})
        off = _todo_rules_suffix()
        check("D4", "关闭后逐字节零回归（空串）", off == "", repr(off[:40]))
    finally:
        _cfg.save_override({"agent.todo_enabled": old})
    # D5：接线 —— tool_rules 块确实带上了这段
    src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "common.py"),
               encoding="utf-8").read()
    check("D5", "tool_rules 拼装已接 _todo_rules_suffix",
          "+ _todo_rules_suffix())" in src, "未接线")
    check("D6", "TODO_RULES_TEXT 已进 __all__（否则 import * 取不到）",
          "'TODO_RULES_TEXT'" in src.split("__all__ = [")[1][:4000], "")
    # D7 变异自证：判据抽成 lambda，真实现为真、变异实现必须为假
    #   ⚠️ 不能写成「变异后仍通过」那种同义反复（本仓已栽过：那种断言永远为真，
    #      "自证"等于没有）。
    _judge = lambda s: "+ _todo_rules_suffix())" in s
    _mutated = src.replace("+ _todo_rules_suffix())", ")")
    check("D7", "变异自证：接线被移除后 D5 判据必须为假",
          _judge(src) and (not _judge(_mutated)) and _mutated != src,
          f"real={_judge(src)} mutant={_judge(_mutated)}")

    # ── D8~D11：conv 9732 真机暴露的三处（只建 1 条 / 先问人后校验 / 无批量） ──
    import sqlite3 as _sq
    # D8：真机症状 —— 9732 只建了 1 条待办（清单里没有"校验""写入"）
    with _db() as c:
        n9732 = c.execute("select count(*) from agent_todos where conversation_id=9732"
                          ).fetchone()[0]
    check("D8", "真机症状确认：9732 只建了 1 条待办（= 清单没列全）",
          n9732 == 1, f"n={n9732}")

    # D9：批量 create 必须真能建多条（用内存库真跑，不靠读源码）
    from agent.todo_store import handle_todo_write
    conn = _sq.connect(":memory:")
    conn.execute("CREATE TABLE agent_todos(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "conversation_id INT, run_id INT, seq INT, subject TEXT, status TEXT, "
                 "verify TEXT, verify_result TEXT, blocked_by TEXT, note TEXT, "
                 "created_at TEXT, updated_at TEXT)")
    r = handle_todo_write({"action": "create", "items": [
        {"subject": "定位工程", "verify": "query_projects 返回 projectId"},
        {"subject": "读结构", "verify": "query_data_tree 返回 part def"},
        {"subject": "生成时序代码", "verify": "sysml_v2_validate 0 ERROR"},
        {"subject": "校验后写入", "verify": "用户确认后写入并回读"},
    ]}, 9, conn=conn)
    cnt = conn.execute("select count(*) from agent_todos").fetchone()[0]
    check("D9", "items 批量 create 真建 4 条（内存库实跑）",
          bool(r.get("ok")) and cnt == 4 and len(r.get("ids") or []) == 4,
          f"ok={r.get('ok')} cnt={cnt} res={str(r.get('result'))[:80]}")
    # D10：缺 verify 的项必须被拒（验收方式是硬要求，不能靠批量绕过）
    r2 = handle_todo_write({"action": "create", "items": [
        {"subject": "无验收项"}]}, 9, conn=conn)
    check("D10", "批量建同样强制 verify（缺则整批失败）",
          (not r2.get("ok")), str(r2.get("result"))[:80])

    # D11：只读步骤不得等确认（提示词必须写明）
    check("D11", "提示词写明「只读步骤不要等确认」",
          "只读步骤不要等确认" in TODO_RULES_TEXT, "")

    # D12：澄清延后的接线存在且有界（最多 2 次 + 要求剩余项减少）
    _s = open(os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py"),
              encoding="utf-8").read()
    check("D12", "待办未做完时延后澄清（有界：≤2 次且要求剩余项减少）",
          "_ac.defer_clarify(" in _s, "未见延后接线")
    # D12a：顺序不变式 —— 待办闸必须**恰好一次**且**早于**延后判断。
    #   真机 conv 9734 教训：待办闸原在澄清块之后 ⇒ 延后逻辑引用未赋值的 `_tg`
    #   ⇒ 抛 `cannot access local variable '_tg'`，整轮流中断（连评估都没跑到）。
    _n_gate = _s.count("_apply_todo_gate(_verdict")
    _i_gate = _s.find("_apply_todo_gate(_verdict")
    _i_defer = _s.find("_ac.defer_clarify(")
    check("D12a", "待办闸恰好应用一次，且早于 defer_clarify（不重复/不越序）",
          _n_gate == 1 and 0 <= _i_gate < _i_defer,
          f"n={_n_gate} gate@{_i_gate} defer@{_i_defer}")
    # D12b：语义唯一实现 —— 延后判定必须在 auto_continue（纯函数），
    #   stream 里不得再出现一份"怎么覆盖 verdict"的本地分支
    #   （否则就是本仓反复禁止的"同一语义两处各写一份"，门禁 verify_todo_gate_p01 已判红过一次）
    from agent.auto_continue import defer_clarify
    _g = {"done": False, "remaining": ["a", "b"], "next_step": "做 a", "fallback": False}
    _v, _nt, _n2, _r2 = defer_clarify({"done": False, "needs_user": True}, _g)
    check("D12b", "defer_clarify 纯函数：有可做项 ⇒ 延后并给出待办 next_step",
          bool(_nt) and _v.get("next_step") == "做 a" and _n2 == 1 and _r2 == 2,
          f"v={_v} note={_nt} n={_n2}")
    _v3, _nt3, _n3, _ = defer_clarify({"done": False, "needs_user": True}, _g,
                                      n=2, last_rem=2)
    check("D12c", "defer_clarify 有界：已达 2 次 ⇒ 不再延后（会照常转卡）",
          (not _nt3) and _n3 == 2, f"note={_nt3} n={_n3}")
    _v4, _nt4, _, _ = defer_clarify({"done": False, "needs_user": True}, _g,
                                    n=1, last_rem=2)
    check("D12d", "defer_clarify 要求推进：剩余项未减少 ⇒ 不延后",
          not _nt4, f"note={_nt4}")

    # ── D14~D17：conv 9733 真机暴露的三处（autofix 被确认闸卡 / 做了不标 / 确认轮丢内容） ──
    # D14：确定性本地修复工具必须豁免写前确认（否则校验-修复闭环卡在一次人工点击）
    _t = open(os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py"),
              encoding="utf-8").read()
    check("D14", "sysml_v2_autofix 豁免写前确认（闭环不再卡人工点击）",
          "if str(name) in _REPAIR_WRITE_EXEMPT:" in _t, "未见豁免")
    # D15：证据自证 —— 该工具实现里确实没有任何持久化语句（豁免才成立）
    _af = open(os.path.join(_ROOT, "sysml_autofix_tools.py"), encoding="utf-8").read()
    check("D15", "autofix 实现无 INSERT/UPDATE/commit（豁免的前提成立）",
          all(k not in _af for k in ("INSERT", "UPDATE ", "commit(")), "")
    # D16：续跑提示必须带"结项提醒"（做了不标 ⇒ 账实不符，闸永远判未完成）
    from agent.auto_continue import continuation_prompt
    _cp = continuation_prompt("原始请求", {"next_step": "跑校验", "remaining": ["跑校验"]}, 1)
    check("D16", "续跑提示含结项提醒（todo_write update + verify_result）",
          "结项提醒" in _cp and "verify_result" in _cp, _cp[-120:])
    # D17：确认轮不得把整轮内容压成占位语（真机：content 只有 16 字、exec 为空）
    _ss = open(os.path.join(_ROOT, "agent", "pipeline_parts", "session.py"),
               encoding="utf-8").read()
    check("D17", "确认轮落库支持 content/card_data（本轮执行过程不丢）",
          "def _persist_hil_confirm(self, conversation_id, intent, confirm_req," in _ss
          and "card_data=None)" in _ss and "_card.update(card_data)" in _ss, "")
    check("D18", "确认轮的 message 卡片带 exec（工具/思考/闸门可见）",
          '"gates": _gates_safe(),' in _s and "_hil_card" in _s, "")
    with _db() as c:
        row = c.execute("select length(content), card_data from messages "
                        "where conversation_id=9733 and msg_type='hil_confirm' "
                        "order by id desc limit 1").fetchone()
    check("D19", "真机症状确认：9733 的确认轮只剩占位语（= 修复前）",
          bool(row) and int(row[0]) <= 20 and (row[1] or "").find('"exec"') < 0,
          f"len={row[0] if row else None}")
    # D20：澄清轮同样不得只留占位语（真机 9735：12 字，却要用户盲选 4 个写入选项）
    _cnt = open(os.path.join(_ROOT, "agent", "pipeline_parts", "session.py"),
                encoding="utf-8").read()
    check("D20", "澄清落库支持 content/card_data（本轮代码与校验回执不丢）",
          "forced_intent, skill_name, team, questions,\n" in _cnt
          and "_card.update(card_data)" in _cnt, "")
    with _db() as c:
        _r35 = c.execute("select length(content), card_data from messages "
                         "where conversation_id=9735 and msg_type='clarify' "
                         "order by id desc limit 1").fetchone()
    check("D21", "真机症状确认：9735 的澄清轮只剩 12 字占位语（= 修复前）",
          bool(_r35) and int(_r35[0]) <= 20 and (_r35[1] or "").find('"exec"') < 0,
          f"len={_r35[0] if _r35 else None}")


def main():
    print("=" * 78)
    print("门禁：conv 9730「还是没有持续走下去」四项根因")
    print("=" * 78)
    for fn in (group_a, group_b, group_c, group_d):
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
