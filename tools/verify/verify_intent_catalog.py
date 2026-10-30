# -*- coding: utf-8 -*-
"""门禁：**意图目录完整性** + 「保存动作不得静默清空关键词」（2026-10-30）。

## 为什么需要（这是今天"老是走不通 / MCP 工具不全"的真因，不是网络也不是模型）
本工程里 **意图名 == Agent 标识名**（`agent/intent.py` 的 `_db_intents` 按 `agents.name` 建）。
于是两个看似无害的操作会让路由悄悄失效：

1. **在工作室改 Agent 标识名**（`design` → `工程建模方案设计Agent`）
   ⇒ 意图 `design` 从此**没有 Agent**；路由器照样判出 `design`，编排却找不到执行主体。
   实测：改完后 `_db_intents` 里 design 消失，全部"生成视图"类请求退化成 chat。
2. **保存 Agent 时 form 未带 `intent_keywords`**
   ⇒ `update_agent` 原实现 `data.get("intent_keywords", []) or []` 把它写成 `[]`
   ⇒ 该 Agent 的关键词层失效 ⇒ 请求落到 chat ⇒ **chat 没有绑定 MCP/技能工具**
   ⇒ 模型如实回答"工具不全"，用户看到的就是"绑了 MCP 也用不上"。
   实测今天被清空 7 个：design / report_generation / knowledge_qa / chat /
   requirement_quality / view_expansion / 智源对接agent（`asset_change_logs` 可查）。

## 判据
| # | 断言 | 针对的失效 |
|---|---|---|
| C1 | 每个**核心意图**都有同名 active Agent | 改名抹掉意图 |
| C2 | 核心意图 Agent 的 `intent_keywords` 非空 | 关键词被清空 |
| C3 | `update_agent` 源码里 `intent_keywords` **不是**无条件赋值 | 代码层回退 |
| C4 | `AgentIn.intent_keywords` 缺省为 None（None=保留） | 模型层回退 |
| C5 | 变异：把 C3 改回旧写法 ⇒ C3 必须判红 | 防断言空转 |

## 为什么 C3/C4 是**静态**断言
这两条描述的是"代码形态"，而缺陷的形态恰恰是"运行期静默"——
没有任何异常、没有任何日志，只有下一次路由变差。文档级约定挡不住它，只有源码断言能挡。

## 用法（不需要 mbse.db 也能跑 C3/C4/C5；C1/C2 需真库，缺库时 SKIP）
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_intent_catalog.py
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results = []

#: 核心意图：路由/编排的硬契约（`_db_intents` 的键就来自这些 Agent 的 name）。
CORE_INTENTS = ("design", "review", "impact", "chat", "knowledge_qa",
                "report_generation", "requirement_analysis", "requirement_quality")

REPO = os.path.join(_ROOT, "repositories", "agent_repo.py")
MODELS = os.path.join(_ROOT, "models", "studio.py")


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((PASS if ok else kind, name, detail))
    print("  [%s] %s%s" % (_results[-1][0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _find_update_kw_assign(src):
    """在 `update_agent` 里找 intent_keywords 的赋值形态。返回 (有无条件守卫, 命中行)。

    判据不是"文件里出现过这个词"，而是"**赋值是否被条件包住**"：
    无条件的 `new_values["intent_keywords"] = ...` 就是缺陷形态。
    """
    # 取 update_agent 方法体（到下一个同缩进 def 为止）
    m = re.search(r"\n    def update_agent\(self[\s\S]*?(?=\n    def |\Z)", src)
    if not m:
        return False, "未找到 update_agent 方法"
    body = m.group(0)
    lines = body.splitlines()
    hits = []
    for i, ln in enumerate(lines):
        if "intent_keywords" in ln and "=" in ln and "new_values" in ln:
            hits.append((i, ln.strip()))
    if not hits:
        return True, ""          # 不再直接赋值（例如抽成 helper）⇒ 视为已加固
    for i, ln in hits:
        # 往回看 3 行是否有 `if ... is not None:` 这类守卫
        guard = False
        for j in range(max(0, i - 3), i):
            if re.match(r"\s*if\s+.*is not None", lines[j]):
                guard = True
                break
        if not guard:
            return False, ln
    return True, hits[0][1]


def t_static():
    print("\n=== C3/C4 代码形态：保存动作不得无条件覆盖 intent_keywords ===")
    src = _read(REPO)
    ok, detail = _find_update_kw_assign(src)
    ok = _rec("C3 update_agent 的 intent_keywords 赋值带 `is not None` 守卫"
              "（None=保留，[]=清空）", ok, detail)

    # C3b：老写法必须绝迹（这是缺陷的原文）
    legacy = 'json.dumps(data.get("intent_keywords", []) or [], ensure_ascii=False)'
    ok &= _rec("C3b 旧写法（缺省即清空）已不存在", legacy not in src,
               "命中老写法 ⇒ 缺陷会复发")

    msrc = _read(MODELS)
    m = re.search(r"intent_keywords:\s*Optional\[List\[str\]\]\s*=\s*(\S+)", msrc)
    ok &= _rec("C4 AgentIn.intent_keywords 缺省为 None（None=保留既有值）",
               bool(m) and m.group(1).startswith("None"), m.group(1) if m else "未找到字段")

    # 创建路径不能被 None 落库成字符串 "null"
    ok &= _rec("C4b create_agent 用 `or []` 兜底（防 json.dumps(None) → 'null'）",
               'json.dumps(data.get("intent_keywords") or []' in src)
    return ok


def t_data():
    print("\n=== C1/C2 真库：核心意图目录完整 + 关键词非空 ===")
    db = os.path.join(_ROOT, "mbse.db")
    if not os.path.exists(db):
        return _rec("C1/C2 需要真库（CI 无 mbse.db）⇒ SKIP", True, "本机跑才有判定", SKIP)
    import json as _json
    import sqlite3
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    ok = True
    for it in CORE_INTENTS:
        row = c.execute("SELECT id,intent_keywords FROM agents WHERE name=? AND status='active'",
                        (it,)).fetchone()
        ok &= _rec("C1 %-22s 有同名 active Agent" % it, bool(row),
                   "缺失 ⇒ 路由判得出、编排找不到执行主体")
        if row:
            try:
                kws = [x for x in (_json.loads(row["intent_keywords"] or "[]") or []) if str(x).strip()]
            except Exception:
                kws = []
            ok &= _rec("C2 %-22s intent_keywords 非空（%d 个）" % (it, len(kws)),
                       len(kws) > 0, "关键词层对该 Agent 失效 ⇒ 请求落到 chat")
    c.close()
    return ok


def m_mutation():
    """变异：把 C3 的守卫去掉 ⇒ C3 必须判红（证明它不是恒真）。"""
    print("\n=== 变异 M：还原无条件赋值 ⇒ C3 必须判红 ===")
    src = _read(REPO)
    # 把带守卫的写法还原成老写法（只在内存里，不落盘）
    import re as _re
    mut = _re.sub(
        r'\n        _ikw = data\.get\("intent_keywords"\)\n'
        r'\s*if _ikw is not None:\n'
        r'\s*new_values\["intent_keywords"\] = json\.dumps\(list\(_ikw or \[\]\), ensure_ascii=False\)',
        '\n        new_values["intent_keywords"] = json.dumps(data.get("intent_keywords", []) or [], ensure_ascii=False)',
        src)
    changed = mut != src
    if not changed:
        # 兜底：直接拼一行无条件赋值塞进方法体
        mut = _re.sub(r'(\n    def update_agent\(self[\s\S]{0,4000}?)(\n        if data\.get\("version"\))',
                      r'\1\n        new_values["intent_keywords"] = json.dumps(data.get("intent_keywords", []) or [], ensure_ascii=False)\2',
                      src)
        changed = mut != src
    ok_flag, detail = _find_update_kw_assign(mut)
    return _rec("M 复现缺陷形态（无条件赋值被 C3 抓红）⇒ 证明 C3 非空转",
                changed and not ok_flag, detail)


def main():
    print("=" * 78)
    print("意图目录完整性 / 保存动作不得静默清空关键词")
    print("=" * 78)
    t_static()
    t_data()
    m_mutation()
    bad = [r for r in _results if r[0] == FAIL]
    n_pass = sum(1 for r in _results if r[0] == PASS)
    n_skip = sum(1 for r in _results if r[0] == SKIP)
    print("\n" + "=" * 78)
    print("断言 %d  PASS %d  SKIP %d  FAIL %d" % (len(_results), n_pass, n_skip, len(bad)))
    for k, n, d in bad:
        print("  [%s] %s  %s" % (k, n, d))
    print("结论：%s" % ("全部通过" if not bad else "有断言未通过"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
