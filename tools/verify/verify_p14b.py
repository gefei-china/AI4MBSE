# -*- coding: utf-8 -*-
"""P1-4b 自检：检索分层参数化 + 占比制预算（2026-09-21）。

三层验证：
  A 静态断言 —— 配置键存在、默认值正确、硬编码已被替换
  B 行为断言 —— 猴补配置后真实消费点取值随之改变（不落文件、不动生产配置）
  C 变异自证 —— 把改动还原回旧写法，断言必须 FAIL（证明断言不是空转）

运行：.venv/Scripts/python.exe -X utf8 tools/verify/verify_p14b.py
"""

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# CI-OPTIONAL: C 实测本地红（C1 变异后 6 条断言未转红）⇒ 需先修
#   分类：A=需服务在跑/ B=需密钥或写真库/ C=实测就红需先修。
#   依据见 docs/遗留优化项-第二轮盘点-20261005.md；
#   由 tools/verify/verify_gate_wiring.py 强制要求（要么接线，要么写理由）。
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.chdir(REPO)
sys.path.insert(0, str(REPO))

OK = FAIL = 0
RESULTS = []


def check(tag, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        RESULTS.append(("PASS", tag, ""))
        print(f"[PASS] {tag}")
    else:
        FAIL += 1
        RESULTS.append(("FAIL", tag, detail))
        print(f"[FAIL] {tag} —— {detail}")


def read(p):
    return (REPO / p).read_text(encoding="utf-8")


print("=" * 78)
print("P1-4b 自检：检索分层参数化 + 占比制预算")
print("=" * 78)

# ══ A 静态断言 ══════════════════════════════════════════════════════════════
print("\n── A 静态断言 ──")
cfg_src = read("core/config.py")
ke_src = read("knowledge_engine.py")
ctx_src = read("agent/pipeline_parts/context.py")
hist_src = read("agent/pipeline_parts/history.py")

from core.config import DEFAULT_CONFIG as D, CONFIG_SCHEMA as S

rag, ctx = D["rag"], D["context"]
check("A1 rag.recall_k 默认 20", rag.get("recall_k") == 20, f"got {rag.get('recall_k')}")
check("A2 rag.top_k 默认 20（原 4）", rag.get("top_k") == 20, f"got {rag.get('top_k')}")
check("A3 rag.inject_k 默认 3", rag.get("inject_k") == 3, f"got {rag.get('inject_k')}")
check("A4 rag.rerank_max_candidates 默认 10（原 8）",
      rag.get("rerank_max_candidates") == 10, f"got {rag.get('rerank_max_candidates')}")
check("A5 漏斗约束 recall_k ≥ top_k ≥ rerank_max_candidates ≥ inject_k（默认值）",
      rag["recall_k"] >= rag["top_k"] >= rag["rerank_max_candidates"] >= rag["inject_k"],
      f"{rag['recall_k']}/{rag['top_k']}/{rag['rerank_max_candidates']}/{rag['inject_k']}")
check("A6 context 新增 budget_window_tokens/retrieval_ratio/history_ratio",
      all(k in ctx for k in ("budget_window_tokens", "budget_retrieval_ratio", "budget_history_ratio")),
      f"缺 {[k for k in ('budget_window_tokens','budget_retrieval_ratio','budget_history_ratio') if k not in ctx]}")
check("A7 占比键默认 0（零行为漂移）",
      ctx.get("budget_retrieval_ratio") == 0 and ctx.get("budget_history_ratio") == 0)
for k in ("recall_k", "inject_k", "budget_window_tokens", "budget_retrieval_ratio", "budget_history_ratio"):
    grp = "rag" if k in ("recall_k", "inject_k") else "context"
    check(f"A8 schema 声明 {grp}.{k}", k in (S.get(grp) or {}), "schema 中缺失")

# 硬编码替换检查（注意：回落分支里的 `or top_k * 2` 是**有意保留**的旧行为兜底，
# 只断言「检索调用点」不再用 top_k*2，否则会把合法的回落逻辑误判为违规）
check("A9 knowledge_engine 检索调用点已无 top_k*2 硬编码",
      "top_k=top_k * 2" not in ke_src and "[:top_k * 2]" not in ke_src,
      "调用点仍有硬编码")
check("A10 knowledge_engine 读取 rag.recall_k",
      '_cfg.get("rag", "recall_k"' in ke_src)
check("A11 context.py 已无 `chunk_hits[:3]` 硬编码注入",
      "chunk_hits[:3]" not in ctx_src)
check("A12 context.py 读取 rag.inject_k 并以 [:_inject_k] 注入",
      'rag", "inject_k"' in ctx_src and "chunk_hits[:_inject_k]" in ctx_src)
check("A13 history.py 定义 ctx_budget_tokens 且两处调用",
      "def ctx_budget_tokens(" in hist_src and hist_src.count("ctx_budget_tokens(") >= 3,
      f"调用数 {hist_src.count('ctx_budget_tokens(')}")
check("A14 history.py 已无旧的裸预算读取式",
      'int(_cfg.get("context", "budget_retrieval_tokens", 0))' not in hist_src)

# ══ B 行为断言 ══════════════════════════════════════════════════════════════
print("\n── B 行为断言 ──")
from core import config as _cfg
from agent.pipeline_parts.history import ctx_budget_tokens

snap = {k: dict(v) for k, v in _cfg._CONFIG.items() if isinstance(v, dict)}


def setc(sec, key, val):
    _cfg._CONFIG.setdefault(sec, {})[key] = val


try:
    # B1 占比 = 0 → 用绝对值
    setc("context", "budget_history_ratio", 0.0)
    setc("context", "budget_history_tokens", 2000)
    check("B1 占比=0 → 取绝对值 2000", ctx_budget_tokens("history", "budget_history_chars", 3000) == 2000,
          f"got {ctx_budget_tokens('history', 'budget_history_chars', 3000)}")

    # B2 占比 > 0 → 窗口 × 比例
    setc("context", "budget_window_tokens", 65536)
    setc("context", "budget_history_ratio", 0.05)
    exp = int(65536 * 0.05)
    got = ctx_budget_tokens("history", "budget_history_chars", 3000)
    check(f"B2 占比=0.05 → 窗口×比例 = {exp}", got == exp, f"got {got}")

    # B3 占比 > 0 时绝对值被忽略（这是「占比优先」的强断言）
    setc("context", "budget_history_tokens", 999)
    got = ctx_budget_tokens("history", "budget_history_chars", 3000)
    check("B3 占比>0 时绝对值键被忽略（仍按占比）", got == exp, f"got {got}（若=999 说明回落到绝对值）")

    # B4 retrieval 与 history 互不串用
    setc("context", "budget_retrieval_ratio", 0.0)
    setc("context", "budget_retrieval_tokens", 5000)
    check("B4 检索区（占比=0）取自己的绝对值 5000",
          ctx_budget_tokens("retrieval", "budget_retrieval_chars", 4000) == 5000)

    # B5 全是 0/缺失 → 回落 fallback（注意必须用**不存在的**字符键，
    # 否则会读到 config 里真实存在的 budget_history_chars=3000，断言语义就错了）
    setc("context", "budget_history_ratio", 0.0)
    setc("context", "budget_history_tokens", 0)
    _g = ctx_budget_tokens("history", "budget_no_such_key_chars", 1234)
    check("B5 占比=0 且绝对值=0 → 回落 fallback 1234", _g == 1234, f"got {_g}")

    # ── recall_k 行为：spy 捕获真实传给检索器的 top_k ──
    import knowledge_engine as KE
    import vector_index as VI          # ChunkVectorIndex 在 hybrid_search 内是函数级导入
    cap = {}
    orig_bm25 = KE.BM25Engine.search
    orig_vec = VI.ChunkVectorIndex.search

    def spy_bm25(self, query, top_k=5):
        cap["bm25"] = top_k
        return orig_bm25(self, query, top_k=top_k)

    def spy_vec(conn, q, version, top_k=10, only_ids=None):
        cap["vec"] = top_k
        return orig_vec(conn, q, version, top_k=top_k, only_ids=only_ids)

    KE.BM25Engine.search = spy_bm25
    VI.ChunkVectorIndex.search = spy_vec

    db = str(REPO / "mbse.db")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row      # 必须：hybrid_search 依赖 Row 行对象（漏了会在 _bm25_cache_get 崩）
    try:
        setc("rag", "recall_k", 7)
        KE.hybrid_search(conn, "转发器", top_k=3)
        check("B6 recall_k=7 → 检索器收到 top_k=7", cap.get("bm25") == 7 and cap.get("vec") == 7,
              f"bm25={cap.get('bm25')} vec={cap.get('vec')}")

        cap.clear()
        setc("rag", "recall_k", 33)
        KE.hybrid_search(conn, "转发器", top_k=3)
        check("B7 recall_k=33 → 检索器收到 top_k=33", cap.get("bm25") == 33 and cap.get("vec") == 33,
              f"bm25={cap.get('bm25')} vec={cap.get('vec')}")

        cap.clear()
        setc("rag", "recall_k", 0)
        KE.hybrid_search(conn, "转发器", top_k=6)
        check("B8 recall_k=0 → 回落旧行为 top_k*2 = 12",
              cap.get("bm25") == 12 and cap.get("vec") == 12,
              f"bm25={cap.get('bm25')} vec={cap.get('vec')}")
    finally:
        KE.BM25Engine.search = orig_bm25
        VI.ChunkVectorIndex.search = orig_vec
        conn.close()
finally:
    for k, v in snap.items():
        _cfg._CONFIG[k] = v
print("  （配置已还原为运行前快照）")

# ══ C 变异自证 ══════════════════════════════════════════════════════════════
# 护栏：变异子进程只跑 A/B（由 P14B_MUT_CHILD=1 标记），否则 subprocess 自调用会无限递归
if os.environ.get("P14B_MUT_CHILD") == "1":
    print("\n（变异子进程：跳过 C 段）")
    print("\n" + "=" * 78)
    print(f"结果：{OK} pass / {FAIL} fail")
    print("=" * 78)
    sys.exit(1 if FAIL else 0)

print("\n── C 变异自证（把改动还原 → 断言必须 FAIL）──")
# 每项：(说明, 文件, 原文, 替换为旧写法, 期望失败的断言标签)
MUT = [
    ("还原向量路 recall_k 回硬编码", "knowledge_engine.py",
     "top_k=_recall_k, only_ids=allowed_ids", "top_k=top_k * 2, only_ids=allowed_ids",
     ["A9 knowledge_engine 检索调用点已无 top_k*2 硬编码", "B6 recall_k=7 → 检索器收到 top_k=7"]),
    ("还原 BM25 路 recall_k 回硬编码", "knowledge_engine.py",
     "bm25.search(query, top_k=_recall_k)", "bm25.search(query, top_k=top_k * 2)",
     ["A9 knowledge_engine 检索调用点已无 top_k*2 硬编码", "B6 recall_k=7 → 检索器收到 top_k=7"]),
    ("还原 inject_k 回硬编码 [:3]", "agent/pipeline_parts/context.py",
     "chunk_hits[:_inject_k]", "chunk_hits[:3]",
     ["A11 context.py 已无 `chunk_hits[:3]` 硬编码注入", "A12 context.py 读取 rag.inject_k 并以 [:_inject_k] 注入"]),
    ("还原占比制（ratio 分支失效）", "agent/pipeline_parts/history.py",
     "if ratio > 0:", "if False:",
     ["B2 占比=0.05 → 窗口×比例 = 3276", "B3 占比>0 时绝对值键被忽略（仍按占比）"]),
]
MUT_LABELS = [t for _, _, _, _, tags in MUT for t in tags]

mutation_failed_as_expected = []
backup = {}
try:
    for _, p, old, new, _ in MUT:
        cur = (REPO / p).read_bytes()
        if p not in backup:
            backup[p] = cur          # 首次即原始字节（供还原）
        assert old.encode() in cur, f"变异锚点不存在（脚本自身错）：{old} @ {p}"
        (REPO / p).write_bytes(cur.replace(old.encode(), new.encode(), 1))   # 累积变异
    print(f"  已施加 {len(MUT)} 处变异，重跑 A/B 断言集…")
    r = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve())],
                       cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env={**os.environ, "P14B_MUT_CHILD": "1"})   # 护栏：子进程不再进 C 段
    out = (r.stdout or "") + (r.stderr or "")
    for tag in sorted(set(MUT_LABELS)):
        hit = re.search(r"\[FAIL\] " + re.escape(tag), out)
        if hit:
            mutation_failed_as_expected.append(tag)
        else:
            print(f"    !! 变异后该断言仍通过（=空转断言）：{tag}")
    uniq = sorted(set(MUT_LABELS))
    check(f"C1 变异后全部 {len(uniq)} 条相关断言转为 FAIL",
          len(mutation_failed_as_expected) == len(uniq),
          f"仅 {len(mutation_failed_as_expected)}/{len(uniq)} 条：{mutation_failed_as_expected}")
    check("C2 四处变异的断言组全部被抓到",
          len(set(mutation_failed_as_expected)) == len(uniq),
          f"未抓到 {[t for t in uniq if t not in mutation_failed_as_expected]}")
finally:
    for p, b in backup.items():
        (REPO / p).write_bytes(b)
    print("  变异已还原；逐文件 diff 校验…")
    bad = []
    for p, b in backup.items():
        if (REPO / p).read_bytes() != b:
            bad.append(p)
    check("C3 变异源文件已逐字节还原", not bad, f"未还原：{bad}")

print("\n" + "=" * 78)
print(f"结果：{OK} pass / {FAIL} fail")
print("=" * 78)
sys.exit(1 if FAIL else 0)
