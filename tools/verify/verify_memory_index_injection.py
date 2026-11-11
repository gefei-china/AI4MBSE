# -*- coding: utf-8 -*-
"""P1-1 门禁（2026-11-11）：记忆注入层真的接上了「顶层目录、底层仓库」。

Claude Code 只把 MEMORY.md 前 200 行/25KB 当索引注入，细节按需检索。
本门禁验证我们不是只写了个 `build_index()` 函数放在那里，而是**真的注入**了，
且注入的是目录（主题 + 编号 + 校验状态）而不是正文。

  I1 配置键在 core/config.py 有声明（防幽灵开关）
  I2 默认开启
  I3 有记忆命中时，注入含【记忆目录】节且导航行 ≤ 配置上限
  I4 导航行不含正文（只有编号 / 主题 / 状态）
  I5 开关关闭 ⇒ 不含目录节（可回滚，零行为漂移）
  I6 无记忆命中 ⇒ 整块为空，不产生目录
  M1/M2 变异自证：切断注入 / 放开行数上限 ⇒ 对应判据必须翻红
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

_TMP = os.path.join(tempfile.gettempdir(), "verify_memory_index_injection.db")
for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
os.environ["MBSE_DB_PATH"] = _TMP
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db  # noqa: E402
init_db()

FAILS, PASSES = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print(("[OK  ] " if cond else "[FAIL] ") + name +
          ("  — " + detail if detail and not cond else ""), flush=True)


def load_mixin(mutate=None):
    path = os.path.join(ROOT, "agent", "pipeline_parts", "memory.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    if mutate:
        src = mutate(src)
    # memory.py 顶层有 `from .common import *`，必须给它一个真实的包上下文，
    # 否则相对导入会报 "no known parent package"。
    ns = {"__name__": "agent.pipeline_parts.memory_mut",
          "__package__": "agent.pipeline_parts", "__file__": path}
    exec(compile(src, path, "exec"), ns)
    return ns["MemoryMixin"]


def make_pipe(MixinCls):
    class _Pipe(MixinCls):
        def __init__(self):
            self._mem_ctx = None
            self._rag_memory_hits = []

        def _memory_scopes(self, intent, user=None):
            return []
    return _Pipe()


def hint(MixinCls, text="SysML v2 语法规范", intent="knowledge_qa"):
    return make_pipe(MixinCls)._build_memory_hint(text, intent, user=None)


def nav_lines(text):
    marker = "【记忆目录"
    if marker not in text:
        return []
    tail = text.split(marker, 1)[1]
    return [ln for ln in tail.splitlines() if ln.startswith("- ")]


def set_switch(on: bool):
    """改**生效**配置（`_CONFIG`），不是 DEFAULT_CONFIG —— config.get 只读前者。"""
    from core import config
    config._CONFIG.setdefault("context_audit", {})["memory_index_enabled"] = on


# ── 数据：一条 core 真偏好（能过噪音闸门）──
con = get_db()
con.execute(
    "INSERT INTO agent_memory (agent_id, mem_type, content, source, relevance, "
    "tier, activation, forgotten) VALUES (?,?,?,?,?,?,?,0)",
    ("knowledge_qa", "preference", "用户偏好白底黑字蓝框蓝线的 draw.io 图表，用于 PPT 演示",
     "user_stated", 1.0, "core", 1.5))
# 额外记忆：让 build_index 有足够条目，M2「放开行数上限」才可能翻红
for _i in range(7):
    con.execute(
        "INSERT INTO agent_memory (agent_id, mem_type, content, source, relevance, "
        "tier, activation, forgotten) VALUES (?,?,?,?,?,?,?,0)",
        ("knowledge_qa", "fact", "建模经验 %d：视图层级按包嵌套组织" % _i,
         "rule_agent", 1.0, "recall", 1.0))
con.commit()
con.close()

print("\n[1] 配置声明与默认", flush=True)
from core import config as _cfg
check("I1 memory_index_enabled / memory_index_rows 均在 config 声明",
      "memory_index_enabled" in _cfg.DEFAULT_CONFIG["context_audit"]
      and "memory_index_rows" in _cfg.DEFAULT_CONFIG["context_audit"])
check("I2 索引默认开启且行数默认 3",
      _cfg.DEFAULT_CONFIG["context_audit"]["memory_index_enabled"] is True
      and _cfg.DEFAULT_CONFIG["context_audit"]["memory_index_rows"] == 3)

Real = load_mixin()
print("\n[2] 注入行为", flush=True)
h_on = hint(Real)
nav = nav_lines(h_on)
check("I3 有记忆命中 ⇒ 注入含【记忆目录】且导航行 ≤3",
      bool(nav) and len(nav) <= 3, "实得导航行 %d：%r" % (len(nav), nav[:5]))
check("I4 导航行是目录而非正文（含 #编号、不含长正文）",
      all("#" in ln and len(ln) <= 80 for ln in nav), "实得 %r" % nav[:3])
check("I4b 原有核心偏好块仍在（索引是增强，不替代正文）",
      "【核心偏好" in h_on)

set_switch(False)
h_off = hint(Real)
set_switch(True)
check("I5 开关关闭 ⇒ 不含目录节（可回滚）",
      "【记忆目录" not in h_off and "【核心偏好" in h_off)

con = get_db()
con.execute("UPDATE agent_memory SET forgotten=1")
con.commit()
con.close()
h_empty = hint(Real)
check("I6 无记忆命中 ⇒ 整块为空，不产生目录", h_empty == "",
      "实得 %r" % h_empty[:120])
con = get_db()
con.execute("UPDATE agent_memory SET forgotten=0")
con.commit()
con.close()

print("\n[3] 变异自证", flush=True)


def _mut_disconnect(src):
    anchor = "                if _idx_on and _idx_rows and lines:"
    assert anchor in src, "M1 锚点失效（注入层已被改写）"
    return src.replace(anchor, "                if False:", 1)


def _mut_unbounded(src):
    """行数上限是**双保险**：build_index(max_lines) 截断 + 注入层切片。
    只拆掉一道，另一道仍挡住（这正是 M3 要证明的）；要翻红必须两道一起放开。"""
    a1 = "max_lines=_idx_rows + 2,"
    a2 = 'if ln.startswith("- ")][:_idx_rows]'
    assert a1 in src and a2 in src, "M2 锚点失效（双保险已改写）"
    return src.replace(a1, "max_lines=1000,", 1).replace(
        a2, 'if ln.startswith("- ")][:1000]', 1)


def _mut_slice_only(src):
    a2 = 'if ln.startswith("- ")][:_idx_rows]'
    assert a2 in src, "M3 锚点失效"
    return src.replace(a2, 'if ln.startswith("- ")][:1000]', 1)


for label, mut, expect_break in (
    ("M1 切断索引注入 ⇒ I3 翻红", _mut_disconnect, True),
    ("M2 双保险同时放开 ⇒ I3 翻红", _mut_unbounded, True),
    ("M3 只放开切片 ⇒ build_index 硬上限仍挡住（不翻红）", _mut_slice_only, False),
):
    try:
        M = load_mixin(mut)
        nav_m = nav_lines(hint(M))
        if expect_break:
            check(label, (not nav_m) or len(nav_m) > 3,
                  "变异后导航行仍为 %d" % len(nav_m))
        else:
            check(label, len(nav_m) <= 3,
                  "单拆一道就失守，说明并无冗余保护：%d 行" % len(nav_m))
    except AssertionError as e:
        check(label, False, str(e))
    except Exception as e:  # noqa: BLE001
        check(label, False, "%s: %s" % (type(e).__name__, e))

print("\n=== %d PASS / %d FAIL ===" % (len(PASSES), len(FAILS)), flush=True)
for f in FAILS:
    print("  x %s" % f, flush=True)
sys.exit(1 if FAILS else 0)
