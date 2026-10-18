# CI-OPTIONAL: A 需真实 LLM rerank provider（2026-10-18 干净库实测 LLMUnavailableError「未配置 API Key」⇒ 0/3 条被打分）；CI 无 secret
"""门禁：检索重排（rerank）必须"真生效"，而不只是"开关是开的"。

══════════════════════════════════════════════════════════════════════
为什么需要这道门禁（2026-10-10 实测教训）
══════════════════════════════════════════════════════════════════════
我把 `rag.rerank_enabled=True` 打开后，**排序一字未变**，
排查了 **6 轮**才找到真因。过程中三个层次的"看起来都对了"：

① 开关是True ✅
② `llm_rerank` 直调返回了列表 ✅
③ 但 `rerank_score` 全是 `None`、`reranked` 全是 False ❌

真因链（三层，任一层都能单独让重排失效）：
  L1 provider 整体不可用（ds-v4-flash-neibu/109 所有 intent 一律 31.7s 超时、
     ct=0）⇒ 需要**可独立指定 rerank provider**（`rag.rerank_provider_id`）
  L2 **思考污染 JSON**（reasoning 模型在 JSON 前输出思考 ⇒ json.loads 失败）
     ⇒ 需要**结构化输出轮自动关思考**（`_looks_structured_output` 形态规则）
  L3 排序段NameError 被 `except` 静默吞掉
     ⇒ 需要**三处失败都留痕**（解析失败 / 无评分 / 排序异常）

⚠️ **最毒的一点**：三层故障的症状**完全相同**（"顺序未变"），
所以"开关打开"绝不能当成"重排生效"的证据 —— **必须验证 `reranked=True` 且
正确答案被提升**。
"""
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

#: 打桩用（不烧真实额度）
CASES = [
    # (query, 候选列表, 期望排第1 的内容片段)
    ("part def 与 part 的区别是什么", [
        {"content": "SysML 的包（package）用于组织模型元素。", "section": "包"},
        {"content": "本文档的修订记录与版本历史列表。", "section": "修订记录"},
        {"content": "part def 定义一个类型（Type），part 是该类型的实例。", "section": "part"},
    ], "part def 定义一个类型"),
]


def check_enabled_config() -> list:
    from core import config
    from core.config import CONFIG_SCHEMA
    out = [
        ("`rag.rerank_enabled` 已开启",
         config.as_bool("rag", "rerank_enabled", False) is True),
        ("★ `rerank_provider_id` 已登记 CONFIG_SCHEMA['rag']",
         "rerank_provider_id" in CONFIG_SCHEMA.get("rag", {})),
        ("★ `rerank_provider_id` 非 0（默认 provider 实测会整体不可用："
         "31.7s 超时 ct=0 ⇒ 重排 100% 静默降级）",
         int(config.get("rag", "rerank_provider_id", 0) or 0) != 0),
        ("`rag.rerank_max_candidates` = 20（行业标准 retrieve 20）",
         int(config.get("rag", "rerank_max_candidates", 0) or 0) == 20),
        ("`rag.inject_k` 在 3~5（行业：send 3~5 给 LLM）",
         3 <= int(config.get("rag", "inject_k", 3) or 3) <= 5),
    ]
    return out


def check_shape_rule() -> list:
    """结构化输出轮必须自动关思考（否则 JSON 被思考污染）。"""
    import httpx
    import llm.providers.openai_compat as oc
    from llm.providers.openai_compat import OpenAICompatProvider as P

    def probe(system_msg):
        cap = []
        orig = httpx.post

        def spy(url, json=None, **k):
            cap.append(json or {})
            class R:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return {"choices": [{"message": {"content": "{}"}}]}
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()
        httpx.post = spy
        cfg = {"id": 9005, "name": "p", "model_name": "m",
               "base_url": "https://x.invalid", "api_key": "k",
               "max_tokens": 2048, "context_window": 32000,
               "temperature": 0.0, "model_params": "{}", "status": "active"}
        try:
            P(cfg).chat([{"role": "system", "content": system_msg},
                         {"role": "user", "content": "x"}], _intent="t")
        except Exception:
            pass
        finally:
            httpx.post = orig
        return (cap[-1].get("thinking") if cap else None)

    rerank = probe('只输出 JSON：{"ratings":[{"i":0,"score":9}]}')
    gen = probe("请生成 SysML v2 视图代码。")
    return [
        (f"★ 结构化输出轮下发 disabled（实测 {rerank}）",
         rerank == {"type": "disabled"}),
        (f"★ 重产码生成轮**不受影响**（实测 {gen}）", gen is None),
    ]


def check_rerank_effective() -> list:
    """真实调 LLM，验证 `reranked=True` 且正确答案被提升。"""
    from services.rag_rerank import llm_rerank
    out = []
    for q, cands, want in CASES:
        hits = [dict(c, bm25_score=float(i + 1), vec_score=0.30 + i * 0.01)
                for i, c in enumerate(cands)]
        t0 = time.time()
        r = llm_rerank(q, hits, top_k=3, max_candidates=8)
        dt = time.time() - t0
        scored = [h for h in r if h.get("reranked")]
        out.append((f"★ 真实调用成功（{dt:.1f}s，{len(scored)}/{len(r)} 条被打分，"
                    f"须 >0）", len(scored) > 0))
        if scored:
            top = r[0]
            out.append((f"★ 正确答案被提升到第 1（实测 top1=「{top['content'][:24]}」）",
                        want in top["content"]))
            out.append((f"重排耗时 {dt:.1f}s < 8s（超时会导致 100% 静默降级）", dt < 8))
        else:
            out.append(("★ 无法验证提升效果（未打分）——检查 provider 可用性", False))
    return out


def check_failure_visible() -> list:
    """三处失败必须留痕（否则又变成"静默失效"）。"""
    with open(os.path.join(_ROOT, "services", "rag_rerank.py"), encoding="utf-8") as f:
        src = f.read()
    return [
        ("解析失败有warning 留痕", "解析失败（provider_id=%s）" in src),
        ("无评分有 warning 留痕", "未拿到评分（provider_id=%s）" in src),
        ("★ 排序段异常有 warning 留痕（这层曾静默吞掉 NameError）",
         "排序段异常（provider_id=%s）" in src),
    ]


def mutation_check() -> bool:
    """变异自证：把 `_cfg.get(...)` 改回 `_(...)`（重现 NameError）⇒ 必须判红。"""
    path = os.path.join(_ROOT, "services", "rag_rerank.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    anchor = '_pid = int(_cfg.get("rag", "rerank_provider_id", 0) or 0)'
    if anchor not in src:
        print("     [变异] 锚点未找到")
        return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(src.replace(anchor, '_pid = int(_("rag", "rerank_provider_id", 0) or 0)', 1))
        import importlib
        import services.rag_rerank as rr
        importlib.reload(rr)
        hits = [dict(c, bm25_score=1.0, vec_score=0.3) for _, c, _ in CASES for c in [cands[0]]] \
            if False else [dict(c, bm25_score=float(i + 1), vec_score=0.3)
                               for i, c in enumerate(CASES[0][1])]
        r = rr.llm_rerank(CASES[0][0], hits, top_k=3, max_candidates=8)
        red = not any(h.get("reranked") for h in r)
        print(f"[变异] 改回 _(...) 后重排失效（reranked 全空）⇒ "
              f"{'已判红（判据确实观测到生效路径）' if red else '未判红'}")
        return red
    finally:
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        import importlib
        import services.rag_rerank as rr
        importlib.reload(rr)
        back = rr.llm_rerank(CASES[0][0],
                            [dict(c, bm25_score=float(i + 1), vec_score=0.3)
                             for i, c in enumerate(CASES[0][1])],
                            top_k=3, max_candidates=8)
        print(f"[收尾] 已回滚，reranked 恢复={any(h.get('reranked') for h in back)}")


def main() -> int:
    print("=" * 76)
    print("重排门禁（配置 / 结构化输出规则 / 真生效 / 失败可见）")
    print("=" * 76)
    ok = True
    for title, checks in [("① 配置（行业标准 retrieve20→rerank→inject3~5）", check_enabled_config()),
                          ("② 结构化输出轮自动关思考", check_shape_rule()),
                          ("③ 真生效（reranked=True + 正确答案被提升）", None),
                          ("④ 失败必须可见（不再静默）", check_failure_visible())]:
        print(f"\n{title}")
        cs = check_rerank_effective() if checks is None else checks
        for name, passed in cs:
            print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
            ok = ok and passed
    print("\n[变异自证] 重现 NameError（改回 `_(...)`）⇒ 应判红")
    ok = ok and mutation_check()
    print("\n" + "=" * 76)
    print(f"结论：{'全部通过' if ok else '存在失败项'}")
    print("=" * 76)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())