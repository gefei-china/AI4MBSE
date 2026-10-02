# -*- coding: utf-8 -*-
"""意图级缓存开关自检（P1-31，2026-10-02）。

## 为什么要有它
`intent.cache_enabled` 默认**关**（依据：真库近 7 天仅 18 条用户消息、重复率 22% ⇒
潜在命中约 17 次/月；而 `intent_detect` 的 1605 次/7 天 >95% 是**评测脚本**刷的）。
但"关"必须是**真的不查库、不落库**，而不是"查了但不用" —— 后者仍是纯开销。

覆盖：
  A 默认关：不落库（表行数 0）、读不到（即便表里已有行）
  B 显式开：写库 / 命中 / hit_count 递增（机制本身仍然完好）
  C 低置信仍不缓存（既有约束在开时照常生效）
  D 变异自证：去掉 `_cache_get` / `_cache_set` 的开关判断 → 断言必须被打破

隔离：`MBSE_DB_PATH` 指向临时库，零污染。
"""
import os
import sys
import tempfile
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TMP = tempfile.mkdtemp(prefix="verify_intent_cache_toggle_")
os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "t.db")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core import config as C                              # noqa: E402
from database import init_db                              # noqa: E402
from agent.intent import IntentRouter                     # noqa: E402

init_db()
PASS, FAIL = [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def set_cfg(v):
    C._CONFIG.setdefault("intent", {})["cache_enabled"] = v


def rows():
    from database import get_db
    c = get_db()
    n = c.execute("SELECT COUNT(*) FROM intent_cache").fetchone()[0]
    c.close()
    return n


def clear():
    from database import get_db
    c = get_db()
    c.execute("DELETE FROM intent_cache")
    c.commit()
    c.close()


print("== 0 前置 ==")
chk("0.1 默认配置就是关（DEFAULT_CONFIG 口径）",
    C.DEFAULT_CONFIG.get("intent", {}).get("cache_enabled") is False,
    C.DEFAULT_CONFIG.get("intent", {}).get("cache_enabled"))
chk("0.2 env 映射已登记（MBSE_INTENT_CACHE_ENABLED）",
    "MBSE_INTENT_CACHE_ENABLED" in getattr(C, "ENV_MAP", {}))

r = IntentRouter()
FP = "fp_test_0001"
TXT = "帮我生成设计模型的 sysml 代码"
clear()

# ── A 默认关 ──
print("\n== A 默认关：不查库、不落库 ==")
set_cfg(False)
r._cache_set(TXT, FP, "design", "rule", 0.95)
chk("A1 关时 _cache_set **不落库**（表仍为 0 行）", rows() == 0, rows())
chk("A2 关时 _cache_get 返回 None", r._cache_get(TXT, FP) is None)

# 手工塞一行：模拟"历史上写入过缓存"，验证关时连读都不读
from database import get_db                            # noqa: E402
_c = get_db()
import hashlib                                         # noqa: E402
_c.execute("INSERT INTO intent_cache (query, query_hash, intent, route, confidence, index_fp) "
           "VALUES (?,?,?,?,?,?)",
           (TXT, hashlib.md5(TXT.lower().encode()).hexdigest(), "design", "rule", 0.95, FP))
_c.commit()
_c.close()
chk("A3 表里已有该行时，关态仍读不到（不是'读了但不用'）",
    r._cache_get(TXT, FP) is None)

# ── B 显式开 ──
print("\n== B 显式开：机制完好 ==")
set_cfg(True)
hit = r._cache_get(TXT, FP)
chk("B1 开时命中已存在行", isinstance(hit, tuple) and hit[0] == "design", hit)
_c = get_db()
_hc = _c.execute("SELECT hit_count FROM intent_cache WHERE query_hash=?",
                 (hashlib.md5(TXT.lower().encode()).hexdigest(),)).fetchone()[0]
_c.close()
chk("B2 命中后 hit_count 递增（≥1）", _hc >= 1, _hc)

clear()
r._cache_set("另一句参考文本abc", FP, "review", "llm", 0.9)
chk("B3 开时 _cache_set 正常落库", rows() == 1, rows())

# ── C 低置信不缓存 ──
print("\n== C 低置信（<0.7）开时也不缓存 ==")
clear()
r._cache_set("弱结论文本xyz", FP, "chat", "semantic_weak", 0.5)
chk("C1 confidence=0.5 → 不落库", rows() == 0, rows())

# ── D 变异自证 ──
print("\n== D 变异自证 ==")
_SRC = open(os.path.join(ROOT, "agent/intent.py"), encoding="utf-8").read()


def _variant(pairs):
    src = _SRC
    for a, b in pairs:
        if a not in src:
            return None
        src = src.replace(a, b)
    mod = types.ModuleType("intent_mut")
    exec(compile(src, "<mut>", "exec"), mod.__dict__)
    return mod


_GUARD_GET = ('        if not self._cfg_get("cache_enabled", False):\n'
              '            return None\n')
_GUARD_SET = ('        if not self._cfg_get("cache_enabled", False):\n'
              '            return\n')

_M1 = _variant([(_GUARD_GET, "")])
if _M1:
    _r1 = _M1.IntentRouter()
    set_cfg(False)
    # ⚠️ 必须**重塞**测试行：C 组 clear() 过，表此时是空的（首版就栽在这 —— M1 恒失败，
    #    表象像"变异没生效"，实际是我的夹具没备好）。
    _c = get_db()
    _c.execute("INSERT INTO intent_cache (query, query_hash, intent, route, confidence, index_fp) "
               "VALUES (?,?,?,?,?,?)",
               (TXT, hashlib.md5(TXT.lower().encode()).hexdigest(), "design", "rule", 0.95, FP))
    _c.commit()
    _c.close()
    chk("M1 去掉 _cache_get 的开关 → A3「关态读不到」被打破",
        _r1._cache_get(TXT, FP) is not None, _r1._cache_get(TXT, FP))
else:
    chk("M1 跳过（锚点失效）", False, "锚点未命中 → 变异失效，必须修")

_M2 = _variant([(_GUARD_SET, "")])
if _M2:
    _r2 = _M2.IntentRouter()
    set_cfg(False)
    clear()
    _r2._cache_set("变异写入文本qqq", FP, "design", "rule", 0.95)
    chk("M2 去掉 _cache_set 的开关 → A1「关时不落库」被打破",
        rows() == 1, rows())
else:
    chk("M2 跳过（锚点失效）", False, "锚点未命中 → 变异失效，必须修")

set_cfg(False)
clear()

print("\n" + "=" * 56)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  - " + f)
sys.exit(1 if FAIL else 0)
