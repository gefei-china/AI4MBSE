# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真库（tools 表 side_effect 与 hil_policies 是判据输入），CI 干净库上 side_effect 为空 ⇒ 判据输入不存在；本机已实测 16/16 通过。改法须同步改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""真机验证：编排子任务里的写操作是否**暂存**而非**中断**（A 项修复）。

## 验证对象（真机 conv 990727 / run 990225 那个缺陷）
修前：`_orch_subtask=True` 时用 `HILService.is_write_tool(name)` 判定，
      该函数读硬编码 `{entity_create, relation_create, ...}`，
      对 `save_artifact` 返回 False ⇒ **跳过暂存** ⇒ 继续落到统一确认闸
      ⇒ 每个子任务各自被中断（`hil_confirm`）⇒ 设计上的"暂存→汇总统一确认"从未生效
      （实测 `subtask_artifacts` 为空）。
修后：判定改用 `_classify_write`（与统一闸同源）⇒ `save_artifact` kind=write ⇒ 暂存。

## 判据（全部落在**可复算的事实**上，不看代码自述）
  ① `save_artifact` 在编排上下文返回「已暂存」，**不是**「已进入人工确认队列」
  ② 返回值里**没有** `confirm_required`（= 没有中断信号）
  ③ `subtask_artifacts` 真的多出一条 `kind='write_request'`（DB 回读，不看返回值）
  ④ 非编排上下文（会话直答）**仍然**中断 —— 证明修复没有把闸整个拆掉
  ⑤ 修复类工具 `sysml_v2_autofix` 在编排上下文**不暂存**（豁免生效，否则修复闭环被卡死）
  ⑥ 只读工具 `graph_retrieve` 不暂存

## 为什么不用真 LLM 跑整轮
  本脚本验的是「工具执行层的判定与副作用」，与模型说什么无关；
  真 LLM 一轮 5~10 分钟且结果随模型波动 ⇒ 判据不稳定。
  LLM 端到端另有 `verify_orchestration_*` 覆盖，不在此重复。

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_orch_write_staging.py
"""
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
os.chdir(REPO)

PASS, FAIL = 0, 0
ROWS = []


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s | %s" % (label, str(detail)[:240]))
    ROWS.append({"label": label, "ok": bool(cond), "detail": str(detail)[:300]})


def new_pipeline():
    """构造一个只用于工具执行的最小 pipeline 实例（不加载 Agent / 不连 LLM）。"""
    from agent.pipeline_parts.tools import ToolMixin

    class _P(ToolMixin):
        def __init__(self):
            self._tool_se_cache = {}
            self._tool_whitelist = None      # 空 ⇒ 白名单闸放行
            # ★ RBAC 是**独立维度**（"这个人准不准用"），与 A 项要验的
            #   "编排子任务准不准直接执行"正交。探针用户不是真实登录用户，
            #   权限表里查不到 `agent_tool:write` ⇒ 会先被 RBAC 拦，
            #   使判据测到的是权限而不是暂存逻辑（第一次跑就是这么假红的）。
            #   ⇒ 显式关掉权限闸，只测目标维度；权限闸自身另有 verify_rbac_* 覆盖。
            self._tool_perm_gate_disabled = True
            self._tool_user = None
            self._tool_conv_ctx = 0
            self._tool_trace_id = ""
            self._tool_run_ctx = 0
            self._tool_task_key = ""
            self._orch_subtask = False
            self._orch_result_offloads = {}

    return _P()


def run_tool(pipe, name, args):
    """走唯一出口 `_exec_tool_call`（幂等闸在无关 run 下不生效，故等同于 _impl）。"""
    try:
        return pipe._exec_tool_call(name, args) or {}
    except Exception as e:
        return {"_exc": "%s: %s" % (type(e).__name__, str(e)[:200])}


def count_write_requests(run_id):
    from database import get_db
    c = get_db()
    try:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM subtask_artifacts WHERE run_id=? AND kind='write_request'",
            (int(run_id),)).fetchone()
        return int(row["n"] if row else 0)
    finally:
        c.close()


def main():
    run_id = int(time.time()) % 100000000 + 900000000     # 专用验证 run_id，不撞生产
    print("=" * 74)
    print("编排子任务写操作暂存验证（A 项）  run_id=%d" % run_id)
    print("=" * 74)

    # ── ① 编排上下文 + save_artifact ⇒ 必须暂存 ──
    print("\n=== ① 编排子任务 · save_artifact ⇒ 暂存（不中断）===")
    before = count_write_requests(run_id)
    p = new_pipeline()
    p._orch_subtask = True
    p._tool_run_ctx = run_id
    p._tool_task_key = "t1"
    r = run_tool(p, "save_artifact", {"title": "gate_probe.sysml",
                                      "content": "package P { part def A; }", "kind": "sysml"})
    txt = json.dumps(r, ensure_ascii=False)
    check("① 返回 ok=True", r.get("ok") is True, txt)
    check("② 文案为「已暂存」", "已暂存" in str(r.get("result") or ""), txt)
    check("③ 文案**不是**「人工确认队列」", "人工确认队列" not in str(r.get("result") or ""), txt)
    check("④ 返回里无 confirm_required（未中断）", not r.get("confirm_required"), txt)
    after = count_write_requests(run_id)
    check("⑤ DB 真的多了一条 write_request（回读，不信返回值）", after == before + 1,
          "before=%d after=%d" % (before, after))

    # ── ② 暂存内容完整（下游要靠它建确认单）──
    print("\n=== ② 暂存内容完整性（下游建确认单的依据）===")
    from database import get_db
    c = get_db()
    try:
        row = c.execute(
            "SELECT title, content_json FROM subtask_artifacts "
            "WHERE run_id=? AND kind='write_request' ORDER BY id DESC LIMIT 1",
            (run_id,)).fetchone()
    finally:
        c.close()
    cj = {}
    try:
        cj = json.loads((row["content_json"] if row else "") or "{}")
    except Exception:
        cj = {}
    check("⑥ 留档的 tool 名正确", cj.get("tool") == "save_artifact", str(cj)[:200])
    check("⑦ 留档带完整 arguments（含 content）",
          bool((cj.get("arguments") or {}).get("content")), str(cj)[:200])

    # ── ③ 非编排上下文仍然中断（证明闸没被拆掉）──
    # ★ 2026-11-26 修（探针失效，不是我改坏了闸）：
    #   本组原本用 `save_artifact` 当探针来证明"确认闸仍在"。而 2026-11-26 米爸批准
    #   把 `save_artifact` 按策略降为 **notify**（真库实证：81 张卡里过期 26%，
    #   其中 save_artifact 占 7 张，而不可逆的 delete_model_elements **过期 0** ⇒
    #   不是用户不看卡，是卡被低价值事项占满），它**不再中断** ⇒ ⑧ 判红。
    #   **语义未变**（"闸没被拆掉"仍然成立），故换一个仍为 confirm 的写工具做探针：
    #   `file_write` 走同一条 `write → confirm` 策略，且参数简单可构造。
    #   ⚠️ 同时补一条反向断言（⑧b），把"save_artifact 已按策略降级"**如实记录** ——
    #     否则将来有人只看本门禁会误以为 save_artifact 也该中断，
    #     又会把它改回 confirm（或误判成闸坏了）。
    print("\n=== ③ 会话直答路径 · file_write ⇒ **仍然**中断（闸未失效）===")
    p2 = new_pipeline()
    p2._orch_subtask = False          # 关键：非编排
    import tempfile as _tf
    _probe_path = os.path.join(_tf.gettempdir(), "gate_probe2.txt")
    r2 = run_tool(p2, "file_write", {"path": _probe_path, "content": "probe"})
    t2 = json.dumps(r2, ensure_ascii=False)
    check("⑧ 直答路径出现 confirm_required（确认闸仍在）",
          bool(r2.get("confirm_required")), t2)
    check("⑨ 直答路径**未**暂存", "已暂存" not in str(r2.get("result") or ""), t2)

    # ── ③b save_artifact 已按策略降级为 notify（如实记录，勿当缺陷）──
    print("\n=== ③b 会话直答路径 · save_artifact ⇒ 按策略 **不**中断（notify 档）===")
    _p3b = new_pipeline()
    _p3b._orch_subtask = False
    _r3b = run_tool(_p3b, "save_artifact", {"title": "gate_probe3.sysml",
                                            "content": "package Q { part def B; }",
                                            "kind": "sysml"})
    _t3b = json.dumps(_r3b, ensure_ascii=False)
    check("⑧b save_artifact 在直答路径不再要求确认（2026-11-26 策略：notify）",
          not _r3b.get("confirm_required"), _t3b)

    # ── ④ 修复类工具豁免（不得暂存，否则校验-修复闭环被卡死）──
    print("\n=== ④ 修复类工具豁免（sysml_v2_autofix 不得暂存）===")
    p3 = new_pipeline()
    p3._orch_subtask = True
    p3._tool_run_ctx = run_id
    p3._tool_task_key = "t1"
    n0 = count_write_requests(run_id)
    r3 = run_tool(p3, "sysml_v2_autofix", {"code": "package X { part def Z; }"})
    t3 = json.dumps(r3, ensure_ascii=False)
    check("⑩ autofix 未被暂存", "已暂存" not in str(r3.get("result") or ""), t3)
    check("⑪ autofix 未写 write_request", count_write_requests(run_id) == n0,
          "n0=%d now=%d" % (n0, count_write_requests(run_id)))

    # ── ⑤ 只读工具不受影响 ──
    print("\n=== ⑤ 只读工具（graph_retrieve）不暂存、不中断 ===")
    p4 = new_pipeline()
    p4._orch_subtask = True
    p4._tool_run_ctx = run_id
    p4._tool_task_key = "t1"
    n1 = count_write_requests(run_id)
    r4 = run_tool(p4, "graph_retrieve", {"query": "用例视图 规范"})
    t4 = json.dumps(r4, ensure_ascii=False)[:200]
    check("⑫ 只读工具未暂存", "已暂存" not in str(r4.get("result") or ""), t4)
    check("⑬ 只读工具未写 write_request", count_write_requests(run_id) == n1,
          "n1=%d now=%d" % (n1, count_write_requests(run_id)))

    # ── ⑥ 判定入口唯一性（静态，防回退）──
    print("\n=== ⑥ 判定入口唯一性（防再次分叉）===")
    src = open(os.path.join(REPO, "agent", "pipeline_parts", "tools.py"), encoding="utf-8").read()
    check("⑭ 暂存分支不再调用 HILService.is_write_tool",
          "if HILService.is_write_tool(name):" not in src, "旧调用仍在")
    check("⑮ 暂存分支改用 _classify_write",
          "_classify_write(name).get(\"kind\") != \"read\"" in src, "未接入唯一入口")
    check("⑯ 暂存分支同步应用两道豁免",
          "str(name) not in _REPAIR_WRITE_EXEMPT" in src and
          "str(name) not in _PROPOSE_WRITE_EXEMPT" in src, "豁免缺失")

    # ── 清理验证残留（只删本次 run_id 的暂存 + 本脚本产生的确认单夹具）──
    try:
        c = get_db()
        try:
            c.execute("DELETE FROM subtask_artifacts WHERE run_id=? AND run_id>=900000000",
                      (run_id,))
            # ★ 2026-11-13：③ 那条「直答路径仍会中断」的用例会**真的建一张确认单**
            #   （action=save_artifact, preview=执行「gate_probe2.sysml」）。
            #   它是本门禁的**测试夹具**，不清理就会挂在用户的待确认列表里
            #   （实测：跑完门禁后 pending 从 2 变 3）。按 preview 精确识别后删除，
            #   不动任何真实确认单。
            n_fx = c.execute(
                "DELETE FROM hil_confirmations WHERE status='pending' AND "
                "action='save_artifact' AND preview LIKE '%gate_probe2.sysml%'").rowcount
            c.commit()
        finally:
            c.close()
        print("\n（已清理本次验证：暂存 run_id=%d；确认单夹具 %d 张）" % (run_id, n_fx))
    except Exception as e:
        print("\n⚠ 清理失败（不影响判据）：%s" % e)

    print("\n" + "=" * 74)
    print("PASS %d / FAIL %d" % (PASS, FAIL))
    os.makedirs(os.path.join(REPO, "outputs"), exist_ok=True)
    with open(os.path.join(REPO, "outputs", "_verify_orch_write_staging.json"),
              "w", encoding="utf-8") as f:
        json.dump({"run_id": run_id, "pass": PASS, "fail": FAIL, "rows": ROWS},
                  f, ensure_ascii=False, indent=1)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
