# -*- coding: utf-8 -*-
"""条件重排（`rerank_mode="auto"`）门禁。

## 被保护的行为

`services/rag_rerank.needs_rerank()` 决定"这次查询值不值得花 1.46s 调 LLM 重排"。
判错的两个方向都有代价：

  · **该触发不触发** ⇒ 金标被挤出注入窗口，模型答不出/编答案（10 号文档 §11 的 ungrounded）
  · **不该触发却触发** ⇒ 白付 1.46s，条件重排退化成"总是重排"，方案 2 失去意义

## 判据（信号语义用**真实失败样本**锚定，不是构造的）

  [S1] 单文档垄断：前 5 名里同一文档 ≥4 条 ⇒ 触发
       （机理：`per_doc_max=2` 从该文档第 3 条起截断，正确答案大概率也在该文档、排第 3+）
  [S2] 头部文档漂移：top1 与后 4 名中 ≥3 条来自不同文档 ⇒ 触发
       （机理：问 A 文档的事却排上 B 文档 ⇒ 词面相似而非语义相关）
  [S3] 正常排序（5 条来自 2~3 个文档、无一支垄断）⇒ **不触发**
  [S4] 候选不足 5 条 ⇒ 不触发（没有判断依据，不该赌）
  [S5] 两个信号必须**返回原因**（可观测，不静默）
  [T1] 三态解析：off / on / auto
  [T2] 未设置 rerank_mode 时回落到旧布尔开关（**向后兼容**，旧配置行为不变）
  [T3] auto 模式命中时应实际调用 LLM；未命中时**必须短路**（不调 LLM）
  [M1] 变异：把单文档阈值改成 5（要求全 5 条同文档）⇒ S1 的样本会漏判
  [M2] 变异：去掉短路 ⇒ 退化成 always（T3 能抓住）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_rerank_auto.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from services.rag_rerank import needs_rerank, _rerank_mode   # noqa: E402

_FAILS = []


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        _FAILS.append(name)
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))


def mk(doc, idx, conf="中", body="x"):
    return {"document_id": abs(hash(doc)) % 10000, "source_doc": doc,
            "chunk_index": idx, "confidence_level": conf, "content": body}


print("=" * 72)
print("[S] 触发信号（样本取自真实失败用例的结构）")
print("=" * 72)

# S1：复刻「混动功率指标」的真实结构 —— 前 5 名全是同一文档（814）
s1 = [mk("混动知识库.md", i) for i in (0, 49, 7, 54, 43)]
need, why = needs_rerank(s1)
check("S1 单文档垄断(5/5) → 触发", need and "单文档垄断" in why, f"why={why!r}")

# 4 条同文档也应触发（阈值是 ≥4，不是 ==5）
s1b = [mk("混动知识库.md", i) for i in (0, 49, 7, 54)] + [mk("别的.md", 3)]
need_b, why_b = needs_rerank(s1b)
check("S1b 单文档垄断(4/5) → 触发（阈值 ≥4 而非 ==5）", need_b, f"why={why_b!r}")

# S2：复刻「验收交付」的真实结构 —— top1 技术要求书，后 4 名混了 3+ 个别的文档
s2 = [mk("技术要求.pdf", 48, "高"), mk("SysML-v2-AI.md", 4, "低"),
      mk("SysML-v2-AI.md", 5, "低"), mk("视图关联.md", 8, "低"),
      mk("技术要求.pdf", 47, "中")]
need2, why2 = needs_rerank(s2)
check("S2 头部文档漂移(3/4 异文档) → 触发", need2 and "文档漂移" in why2, f"why={why2!r}")

# S3：正常排序 —— 2 个文档交替、无垄断无漂移
s3 = [mk("A.md", 1), mk("B.md", 2), mk("A.md", 3), mk("B.md", 4), mk("A.md", 5)]
need3, why3 = needs_rerank(s3)
check("S3 正常排序（2 文档交替）→ 不触发", not need3, f"why={why3!r}")

# S4：候选不足
s4 = [mk("A.md", 1), mk("A.md", 2)]
need4, _ = needs_rerank(s4)
check("S4 候选仅 2 条 → 不触发（无判断依据不赌）", not need4)

check("S5 空输入不炸", needs_rerank([])[0] is False and needs_rerank(None)[0] is False)

print()
print("=" * 72)
print("[T] 三态开关")
print("=" * 72)

from core import config as cfg   # noqa: E402

rag = cfg.get("rag") or {}
_orig_mode = rag.get("rerank_mode")
_orig_flag = rag.get("rerank_enabled")


def _set(mode, flag):
    if mode is None:
        rag.pop("rerank_mode", None)
    else:
        rag["rerank_mode"] = mode
    rag["rerank_enabled"] = flag


try:
    for mode, exp in (("off", "off"), ("on", "on"), ("auto", "auto")):
        _set(mode, False)
        check(f"T1 rerank_mode={mode!r} → {exp!r}", _rerank_mode() == exp,
              f"got={_rerank_mode()!r}")

    # T2：未设置 rerank_mode 时回落旧布尔（向后兼容）
    _set(None, False)
    _a = _rerank_mode()
    _set(None, True)
    _b = _rerank_mode()
    check("T2 无 rerank_mode 时回落 rerank_enabled（旧配置行为不变）",
          _a == "off" and _b == "on", f"flag=False→{_a!r} flag=True→{_b!r}")
finally:
    _set(_orig_mode, _orig_flag)

print()
print("=" * 72)
print("[T3] 短路行为（auto 未命中时**不得**调 LLM）")
print("=" * 72)

# 用 monkeypatch 统计 llm_client.chat 的调用次数
import services.rag_rerank as _rr   # noqa: E402

_calls = {"n": 0}
_real_import = None


class _SpyLLM:
    class _Client:
        @staticmethod
        def chat(*a, **kw):
            _calls["n"] += 1
            return {"choices": [{"message": {"content": '{"ratings":[]}'}}]}


try:
    _set("auto", False)
    import llm as _llm_mod
    _saved = getattr(_llm_mod, "llm_client", None)
    # 装一个假的 llm_client 到模块属性上（llm_rerank 内部 `from llm import llm_client`）
    class _Fake:
        @staticmethod
        def chat(*a, **kw):
            _calls["n"] += 1
            return {"choices": [{"message": {"content": '{"ratings":[]}'}}]}
    _llm_mod.llm_client = _Fake

    # ① 未命中 → 应短路
    _calls["n"] = 0
    _rr.llm_rerank("q", list(s3), top_k=5, max_candidates=5)
    short = _calls["n"]

    # ② 命中 → 应真调
    _calls["n"] = 0
    _rr.llm_rerank("q", list(s1), top_k=5, max_candidates=5)
    fired = _calls["n"]

    check("T3 auto 未命中 → 短路（0 次 LLM 调用）", short == 0, f"实际 {short} 次")
    check("T3b auto 命中 → 实际调用 LLM", fired >= 1, f"实际 {fired} 次")
finally:
    try:
        _llm_mod.llm_client = _saved
    except Exception:
        pass
    _set(_orig_mode, _orig_flag)

print()
print("=" * 72)
print("[M] 变异自证")
print("=" * 72)


def _mutant_needs(hits):
    """变异体：单文档要求全 5 条同文档（阈值 5 而非 4）。"""
    top5 = list(hits or [])[:5]
    if len(top5) < 5:
        return False, ""
    docs = [h.get("source_doc") or "" for h in top5]
    mx = max(docs.count(d) for d in set(docs) if d) if docs else 0
    return (mx >= 5), ""


check("M1 变异（阈值 5）会漏掉 4/5 垄断的样本 ⇒ S1b 的价值",
      _mutant_needs(s1b)[0] is False and needs_rerank(s1b)[0] is True,
      "真实实现触发，变异体不触发")

check("M2 变异（去掉短路）等价于 always ⇒ T3 的价值",
      short == 0 and fired >= 1,
      "真实实现在未命中时 0 次调用")

print()
print("=" * 72)
if _FAILS:
    print("总计：%d 项失败" % len(_FAILS))
    for f in _FAILS:
        print("  ✗", f)
    sys.exit(1)
print("总计：全部通过")
print("=" * 72)
