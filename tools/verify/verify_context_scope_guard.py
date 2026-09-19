# -*- coding: utf-8 -*-
"""上下文防腐 / 范围自愈 / 质量门禁 的自检脚本（2026-09-19 P0 改动）。

背景（会话 351 取证：「帮我生成电动汽车热管理系统的 sysml V2 代码并进行校验」全链失败）：
  ① 建模上下文按 branch **无条件列名**注入既有实体 → 把上一任务领域素材（巡飞弹/动力分系统）
     塞进本轮，子 Agent 判定「素材与标题不匹配」**拒绝产出代码** → 下游校验无输入 → 链条断裂；
  ② design agent 的 kb_scope.docs 白名单 2 条全不存在 → 实体/分块/文档粗匹配**三路同时归零**
     且完全静默（4887 块 SysML 规范恒不可见）；
  ③ 反思闭环 reflection 判 passed=false/score=62，却不参与 orchestrated_status 聚合 →
     卡片仍显示「正常完成」，**失败被记录成成功**。

本脚本**不依赖 git ref、不依赖服务**（直接 import 模块 + 只读库），随时可跑：
    .venv/Scripts/python.exe tools/verify/verify_context_scope_guard.py
口径提示：通过数随【配置真实值】而定（例如 config 里 model_context_entities 被改成 names 时，
[2] 的 count 断言会失败 —— 那是配置不符预期，不是脚本回归）。
"""
import ast
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_n_pass = 0
_n_fail = 0


def check(name, ok, detail=""):
    global _n_pass, _n_fail
    if ok:
        _n_pass += 1
        print(f"  PASS  {name}" + (f"  | {detail}" if detail else ""))
    else:
        _n_fail += 1
        print(f"  FAIL  {name}" + (f"  | {detail}" if detail else ""))


def hr(t):
    print()
    print("=" * 90)
    print(t)
    print("=" * 90)


def ro_conn():
    db = os.path.join(ROOT, "mbse.db")
    c = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


# ─────────────────────────────────────────────────────────────────────────────
hr("[1] KB-S 文档白名单自愈：rag.GraphRAG._resolve_scope_docs")

from agent.rag import GraphRAG

conn = ro_conn()
try:
    have = [r["filename"] for r in conn.execute(
        "SELECT filename FROM documents ORDER BY id DESC LIMIT 2")]
    missing = "__definitely_not_exist__.md"

    ok1, w1 = GraphRAG._resolve_scope_docs(conn, have)
    check("全有效白名单 → 原样返回、无告警", ok1 == have and w1 is None, f"got={ok1}, warn={w1}")

    ok2, w2 = GraphRAG._resolve_scope_docs(conn, have + [missing])
    check("部分失效 → 剔除失效项并给出告警", ok2 == have and w2 and missing in w2["missing"],
          f"effective={ok2}, missing={w2 and w2['missing']}")
    check("部分失效 → unfiltered=False（未放宽）", w2 and w2["unfiltered"] is False,
          f"unfiltered={w2 and w2['unfiltered']}")

    ok3, w3 = GraphRAG._resolve_scope_docs(conn, [missing])
    check("★ 全失效 → 放宽为不限文档（绝不静默 0 命中）", ok3 == [] and w3 and w3["unfiltered"] is True,
          f"effective={ok3}, unfiltered={w3 and w3['unfiltered']}")

    ok4, w4 = GraphRAG._resolve_scope_docs(conn, [missing, missing])
    check("重复项去重（脏数据不再重复计入）", w4 and w4["requested"] == [missing],
          f"requested={w4 and w4['requested']}")

    ok5, w5 = GraphRAG._resolve_scope_docs(conn, [])
    check("空白名单 → 不做过滤（空列表, None）", ok5 == [] and w5 is None, f"got={ok5}, warn={w5}")

    # 关掉 fallback：失效项不该被剔除（回到改动前行为，仅告警）
    from core import config as _cfg
    _orig_get = _cfg.get

    def _fake_no_fb(sec, key, default=None):
        if (sec, key) == ("kb_scope", "docs_missing_fallback"):
            return False
        return _orig_get(sec, key, default)

    _cfg.get = _fake_no_fb
    try:
        ok6, w6 = GraphRAG._resolve_scope_docs(conn, [missing])
    finally:
        _cfg.get = _orig_get
    check("docs_missing_fallback=False → 保留原白名单（可回退到改动前行为）",
          ok6 == [missing] and w6 and w6["unfiltered"] is False, f"effective={ok6}")
finally:
    conn.close()

# ─────────────────────────────────────────────────────────────────────────────
hr("[2] 建模上下文：既有实体注入形态（memory.MemoryMixin._build_model_context）")

from agent.pipeline_parts.memory import MemoryMixin
from core import config as _cfg2

m = MemoryMixin()
_O = _cfg2.get


def _with_mode(mode, fn):
    def _fake(sec, key, default=None):
        if (sec, key) == ("context", "model_context_entities"):
            return mode
        return _O(sec, key, default)
    _cfg2.get = _fake
    try:
        return fn()
    finally:
        _cfg2.get = _O


_out_count = _with_mode("count", lambda: m._build_model_context("dev", 0, "帮我生成电动汽车热管理系统代码"))
_out_names = _with_mode("names", lambda: m._build_model_context("dev", 0, "帮我生成电动汽车热管理系统代码"))
_out_none = _with_mode("none", lambda: m._build_model_context("dev", 0, "帮我生成电动汽车热管理系统代码"))

check("count（默认）只报数量、不列具体实体名", "既有建模实体：本分支共" in _out_count and "活跃实体：" not in _out_count,
      repr(_out_count[:120]))
check("names 模式仍可列出具体名字（旧行为可回退）", ("活跃实体：" in _out_names) or ("既有建模实体" not in _out_names),
      repr(_out_names[:120]))
check("none 模式不含实体项", "实体" not in _out_none, repr(_out_none[:120]))
check("三种模式都带「适用范围」声明（第二层防御）",
      all("与本" in t and "不符" in t for t in (_out_count, _out_names, _out_none) if t),
      repr(_out_count[:60]))

# 声明必须**不参与预算裁剪**：把预算压到极小，声明仍应完整
def _fake_small(sec, key, default=None):
    if (sec, key) == ("context", "model_context_chars"):
        return 40
    if (sec, key) == ("context", "model_context_entities"):
        return "names"
    return _O(sec, key, default)


_cfg2.get = _fake_small
try:
    _out_small = m._build_model_context("dev", 0, "x")
finally:
    _cfg2.get = _O
check("预算极小（40 字符）时范围声明仍完整保留（不被截掉）",
      "不符" in _out_small and "必须忽略" in _out_small, repr(_out_small[:100]))

# ─────────────────────────────────────────────────────────────────────────────
hr("[3] 项目宪法：注入带项目名 + 适用范围声明（memory.MemoryMixin._build_project_memory）")

_out_pm = m._build_project_memory(user_input="帮我生成电动汽车热管理系统的 sysml V2 代码")
check("注入块含项目标识（pid）", "项目" in _out_pm and "Constitution" in _out_pm, repr(_out_pm[:80]))
check("注入块含「仅当属于该项目领域时适用」的范围声明", "仅当本次任务属于该项目领域时适用" in _out_pm,
      repr(_out_pm[:160]))
check("范围声明不参与预算裁剪（截掉了等于没有防御）",
      "仅当本次任务属于该项目领域时适用" in m._build_project_memory(user_input="x" * 100),
      "长 query 下仍完整")

# ─────────────────────────────────────────────────────────────────────────────
hr("[4] 质量门禁回接：services.subtask_protocol.apply_quality_gate")

from services.subtask_protocol import apply_quality_gate, summarize_status

_s, g1 = apply_quality_gate("full", {"passed": False, "score": 62, "issues": ["报告被截断", "引用不可核"]})
check("★ reflection 未通过 → full 降级为 partial（失败可见）", _s == "partial", f"status={_s}")
check("缺口说明带分数与 issues 摘要", g1 and "62" in g1[0] and "报告被截断" in g1[0], f"gaps={g1}")

_s2, g2 = apply_quality_gate("full", {"passed": True, "score": 90})
check("reflection 通过 → 状态不变、无缺口", _s2 == "full" and g2 == [], f"status={_s2}, gaps={g2}")

_s3, g3 = apply_quality_gate("failed", {"passed": False, "score": 10})
check("只降不升：failed 不被改回 partial", _s3 == "failed", f"status={_s3}")

_s4, g4 = apply_quality_gate("full", None)
check("无 reflection（未启用闭环）→ 状态不变", _s4 == "full" and g4 == [], f"status={_s4}")

check("summarize_status 既有口径未被改动（全 full → full）",
      summarize_status([{"status": "full"}, {"status": "full"}]) == "full")

# ─────────────────────────────────────────────────────────────────────────────
hr("[5] 源码级：两条编排路径都写 orchestrated_status（同一功能不留路径差异）")

_str_src = open(os.path.join(ROOT, "agent/pipeline_parts/stream.py"), encoding="utf-8").read()
_orc_src = open(os.path.join(ROOT, "agent/pipeline_parts/orchestration.py"), encoding="utf-8").read()
check("流式编排路径写 orchestrated_status", '"orchestrated_status": _agg_status' in _str_src)
check("非流式编排路径也写 orchestrated_status（此前缺失）",
      '"orchestrated_status": _agg_status' in _orc_src)
check("非流式路径并入质量门禁", "apply_quality_gate" in _orc_src)

# ─────────────────────────────────────────────────────────────────────────────
hr("[6] 反例守护：不得引入「语义相关性过滤」这类标定不通过的判据")

_mem_src = open(os.path.join(ROOT, "agent/pipeline_parts/memory.py"), encoding="utf-8").read()
check("memory.py 中不再存在 _filter_by_relevance 的**定义**（标定 gap<0，已放弃）",
      "def _filter_by_relevance" not in _mem_src)
check("memory.py 无对已删除符号的悬空调用（防 NameError）",
      "_filter_by_relevance(" not in _mem_src.replace("def _filter_by_relevance", ""))
check("memory.py 留有「标定不通过」的结论注释（防止后人重蹈）",
      "gap = -0.0954" in _mem_src or "不存在能分开二者的阈值" in _mem_src)

cfg_src = open(os.path.join(ROOT, "core/config.py"), encoding="utf-8").read()
check("config 中 model_context_entities 默认 count", '"model_context_entities": "count"' in cfg_src)
check("config 中无残留的 model_context_relevance_* 死配置",
      "model_context_relevance" not in cfg_src)

# ─────────────────────────────────────────────────────────────────────────────
print()
print("=" * 90)
print(f"断言汇总：{_n_pass}/{_n_pass + _n_fail} 通过")
if _n_fail:
    print(f"❌ 失败 {_n_fail} 项")
print("口径提示：本脚本不依赖 git ref 与服务；[2] 的 count 断言依赖 config 真实值为 count。")
print("=" * 90)
sys.exit(1 if _n_fail else 0)
