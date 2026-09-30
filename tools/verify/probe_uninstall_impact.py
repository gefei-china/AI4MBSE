# -*- coding: utf-8 -*-
"""探测：今日 02:01-02:04 批量卸载 18 个 legacy tool 插件后，运行时还剩多少工具候选。

手法：调用**生产函数** AgentPipeline._build_tools_def 取真实候选，
      再用「放行 consumable_filter」做对照组 —— 两组之差 = 被卸载动作剔掉的工具。
"""
import sys, json, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 仓库根（tools/verify/x.py → parents[2]）

from database import get_db
from agent import AgentPipeline

INTENTS = ["design", "review", "requirement_analysis", "knowledge_qa", "impact", "chat"]
UNINSTALLED = [
    r["plugin_id"] for r in get_db().execute(
        "SELECT DISTINCT plugin_id FROM plugin_audit_logs "
        "WHERE action='uninstall' AND created_at LIKE '2026-09-30 02:0%'").fetchall()
]
BUILTIN_NAMES = [
    r["name"] for r in get_db().execute(
        "SELECT name FROM tools WHERE source='builtin'").fetchall()
]

pipe = AgentPipeline()
pipe._load_db_agents()


def names_of(defs):
    """_build_tools_def 返回的是 OpenAI-compatible dict：
        {"type":"function","function":{"name":..., "description":...}}"""
    out = []
    for d in defs:
        if isinstance(d, dict):
            out.append(((d.get("function") or {}).get("name") or d.get("name"), "openai"))
        else:  # 元组兜底
            out.append((d[0], d[2] if len(d) > 2 else "?"))
    return out


def run_all(tag, patch=None):
    saved0 = None
    if patch is not None:
        from plugin_system import store as _ps
        saved0 = _ps.consumable_filter
        _ps.consumable_filter = patch
    out = {}
    try:
        for it in INTENTS:
            try:
                defs = pipe._build_tools_def(it, "", None)
            except Exception as e:
                out[it] = ("ERR", str(e)[:80])
                continue
            out[it] = names_of(defs)
    finally:
        if saved0 is not None:
            from plugin_system import store as _ps
            _ps.consumable_filter = saved0
    return out


real = run_all("real")
# 对照组：放行一切插件过滤（等价于「那 18 个插件没被卸载」）
keep_all = run_all("keepall", patch=lambda conn, user=None, any_user=False: (lambda tbl, lid: True))

print("=" * 78)
print("被卸载插件数：%d    内置工具数：%d" % (len(UNINSTALLED), len(BUILTIN_NAMES)))
print("=" * 78)
total_real, total_full = 0, 0
for it in INTENTS:
    r = real.get(it) or []
    k = keep_all.get(it) or []
    if isinstance(r, tuple):
        print("%-22s 调用失败 %s" % (it, r[1])); continue
    rn = [x[0] for x in r]; kn = [x[0] for x in k]
    dropped = [n for n in kn if n not in rn]
    total_real += len(rn); total_full += len(kn)
    print("-" * 78)
    print("intent=%-20s 真实候选=%d  若未卸载=%d" % (it, len(rn), len(kn)))
    print("   真实候选 : %s" % rn)
    if dropped:
        print("   ✗ 被卸载剔除: %s" % dropped)
    else:
        print("   ✓ 无差异")
print("=" * 78)
print("合计：真实注入 %d 条工具定义；若那 18 个插件未卸载则应为 %d 条。" % (total_real, total_full))
print()

# 直接看 builtin 共同候选中，哪些彻底出局
all_real = set()
for it in INTENTS:
    if isinstance(real.get(it), list):
        all_real |= {x[0] for x in real[it]}
gone = [n for n in BUILTIN_NAMES if n not in all_real]
alive = [n for n in BUILTIN_NAMES if n in all_real]
print("内置工具现状：")
print("   仍在运行时候选 : %s" % (alive or "（无）"))
print("   已彻底出局     : %s" % (gone or "（无）"))
