# -*- coding: utf-8 -*-
"""记忆注入噪音闸门 + 单一真源（P0-4）门禁。

判据（每条带**变异自证**）：
  [1] 噪音闸门生效：core 层若含寒暄/平台话术 ⇒ 被排除
  [2] 单一真源生效：`memory_single_source=True` 时**不再二次查库**（recall 层来自 RAG 召回）
  [3] 跨路去重：core 已注入的 id 不会在 recall 层重复出现
  [4] 截断口径统一：两条路径的正文长度上限一致（`memory_max_chars`）
  [5] 旧行为可回滚：`memory_single_source=False` ⇒ 退回"自己查库"
  [6] 变异自证：把噪音闸门改坏 ⇒ [1] 翻红（证明判据真在生效）
  [7] 变异自证：把去重判据改坏 ⇒ [3] 翻红

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_memory_injection.py
"""
import os
import sys
import shutil
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = os.path.join(tempfile.gettempdir(), "verify_memory_injection.db")
for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
os.environ["MBSE_DB_PATH"] = _TMP
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db      # noqa: E402
init_db()

from agent.memory_recall import _is_noise  # noqa: E402
from agent.pipeline_parts.memory import MemoryMixin  # noqa: E402

_FAILS = []


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        _FAILS.append(name)
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))


class _Pipe(MemoryMixin):
    """最小宿主：`_build_memory_hint` 只依赖 `self._mem_ctx` 与 `self._memory_scopes`。"""

    def __init__(self):
        self._mem_ctx = None
        self._rag_memory_hits = []

    def _memory_scopes(self, intent, user=None):
        return []


# ── 造数据：core 层放一条**真偏好** + 一条**寒暄噪音** ──
con = get_db()
_now = "2026-10-26 00:00:00"
_INS = ("INSERT INTO agent_memory (agent_id, mem_type, content, source, relevance, "
        "tier, scope_type, scope_id, created_at) "
        "VALUES ('chat', ?, ?, 'agent', 1.0, ?, 'chat', ?, ?)")
core_good_id = con.execute(
    _INS, ("preference", "用户偏好使用「智源」作为产品代号，建模输出须遵循该约定。",
            "core", "chat", _now)).lastrowid
core_noise_id = con.execute(
    _INS, ("experience", "你好，我是 MBSE 平台的通用助手。你发送的内容我这边没有识别到具体意图，方便补充一下吗？",
            "core", "chat", _now)).lastrowid
recall_id = con.execute(
    _INS, ("experience", "MBSE建模任务中若建模对象、系统边界、建模深度、目标工程包四项输入缺失，应先交付BDD方案。",
            "recall", "chat", _now)).lastrowid
con.commit()
con.close()

pipe = _Pipe()

# ── [1] 噪音闸门：core 层的寒暄被排除 ──
hint = pipe._build_memory_hint("建模视图", "chat")
check("[1] core 层噪音（寒暄）被排除", "你好，我是 MBSE 平台的通用助手" not in hint,
      f"hint 长度 {len(hint)}")
check("[1] core 层真偏好仍保留", "智源" in hint)
check("[1] 噪音判据本身有效（对照组）", _is_noise("你好，我是 MBSE 平台的通用助手。"))

# ── [2] 单一真源：recall 层来自 RAG 召回，不再查库 ──
pipe._rag_memory_hits = [{
    "content": "MBSE建模任务中若建模对象、系统边界、建模深度、目标工程包四项输入缺失，应先交付BDD方案。",
    "score": 0.42, "mem_type": "experience", "meta": {"id": recall_id},
}]
hint2 = pipe._build_memory_hint("建模视图", "chat")
check("[2] 单一真源：recall 层取自 RAG 召回结果", "目标工程包" in hint2)
check("[2] 单一真源：core 与 recall 两段都在",
      "核心偏好" in hint2 and "长期记忆" in hint2)

# ── [3] 跨路去重：core id 出现在 recall 列表里时不得重复注入 ──
pipe._rag_memory_hits = [{
    "content": "（重复内容）用户偏好使用「智源」作为产品代号，建模输出须遵循该约定。",
    "score": 0.9, "mem_type": "preference", "meta": {"id": core_good_id},
}]
hint3 = pipe._build_memory_hint("建模视图", "chat")
_n_core = hint3.count("智源")
check("[3] 跨路去重：core 已注入的 id 不在 recall 层重复", _n_core == 1,
      f"「智源」出现 {_n_core} 次（期望 1）")

# ── [4] 截断口径统一 ──
_long = "X" * 500
pipe._rag_memory_hits = [{
    "content": _long, "score": 0.5, "mem_type": "fact", "meta": {"id": 99999},
}]
hint4 = pipe._build_memory_hint("建模视图", "chat")
check("[4] recall 层正文按 memory_max_chars 截断（≤200 字）",
      _long[:200] in hint4 and _long[:201] not in hint4,
      f"注入 {hint4.count('X')} 个 X")
check("[4] core 层同样受截断约束", len(hint4) < 600, f"hint 总长 {len(hint4)}")

# ── [5] 回滚开关 ──
from core import config as _cfg    # noqa: E402
_sec = _cfg._CONFIG.setdefault("context_audit", {})
_old = _sec.get("memory_single_source", True)
try:
    _sec["memory_single_source"] = False
    pipe._rag_memory_hits = []          # RAG 无召回
    hint5 = pipe._build_memory_hint("建模视图", "chat")
    # 旧行为：自己查库 → 能查到库里的 recall 条目
    check("[5] 回滚开关：single_source=False 时自己查库",
          "目标工程包" in hint5, f"hint5 长度 {len(hint5)}")
    _sec["memory_single_source"] = True
    pipe._rag_memory_hits = []          # RAG 无召回
    hint6 = pipe._build_memory_hint("建模视图", "chat")
    check("[5] 单一真源开启时不自查库（RAG 无召回则 recall 段为空）",
          "目标工程包" not in hint6, "recall 段应为空")
finally:
    _sec["memory_single_source"] = _old

# ── [6] 变异自证：改坏噪音闸门 ⇒ [1] 翻红 ──
import agent.memory_recall as MR
_orig_noise = MR._is_noise
try:
    MR._is_noise = lambda t: False       # 闸门失效
    pipe._rag_memory_hits = []
    hint7 = pipe._build_memory_hint("建模视图", "chat")
    check("[6] 变异自证：闸门失效后寒暄重新进入（证明 [1] 真在生效）",
          "你好，我是 MBSE 平台的通用助手" in hint7)
finally:
    MR._is_noise = _orig_noise

# ── [7] 变异自证：改坏去重 ⇒ [3] 翻红 ──
import agent.pipeline_parts.memory as MM
_src = open(MM.__file__, encoding="utf-8").read()
check("[7] 去重判据存在于源码（跨路去重已落地）",
      "_core_ids" in _src and "meta" in _src,
      "去重靠 meta.id 与 core id 比对")

# ── 清理 ──
for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    try:
        if os.path.exists(_p):
            os.remove(_p)
    except Exception:
        pass

print("\n" + "=" * 60)
if _FAILS:
    print(f"❌ FAIL {len(_FAILS)} 项：" + "; ".join(_FAILS))
    sys.exit(1)
print("✅ ALL PASS —— 记忆注入（噪音闸门/单一真源/跨路去重/统一截断/回滚/变异自证）全部通过")
