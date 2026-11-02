# -*- coding: utf-8 -*-
"""RAG LLM Rerank（V2.6，用户拍板新增）：对 hybrid_search 的 RRF 候选做 LLM 相关性重排。

- 配置开关：~/.workbuddy/mbse_config.json → "rag": {"rerank_enabled": true}（默认开启）
- 模型：复用系统默认 LLM（deepseek），零新增依赖；LLM 不可用/超时/解析失败 → 静默回退原排序
- 打分：0-10 相关性分，写入 hit["rerank_score"]，hit["reranked"]=True（可观测）
"""
import json
import re

#: 元数据/标题型块的特征词（实测 `815/0` 的真实内容："文档编号/版本号/编制日期/页数/代号"）
#: 这类块正文极短、只承载文档头信息，靠**文档标题**匹配空降高位，白占注入槽。
_META_PAT = re.compile(r"(文档编号|版本号|编制日期|页\s*数|代\s*号|文档性质|受控信息)")


def needs_rerank(hits: list, inject_k: int | None = None) -> tuple:
    """条件重排的触发判据：**只在这两个信号命中时才值得花那 1.46s**。

    返回 `(是否需要重排, 触发原因)`，供日志/观测使用（**不静默**）。

    ## 为什么要"条件"而不是"总是"
    全量重排（`rerank_mode="on"`）实测每条 +1.46s，串行阻塞在用户请求里。
    但 17 条用例里只有 3 条真的需要（金标被挤出注入窗口）⇒ 无条件重排有 82% 是白花钱。

    ## 两个信号（都经过实测筛选，**不是拍脑袋**）
    候选过 5 个信号，用 17 条用例量化"召回率 / 误触发率"
    （`tools/diag/diag_rerank_trigger.py`）：

    | 信号 | 召回(覆盖失败) | 误触发 | 结论 |
    |---|---|---|---|
    | 注入窗口低置信 | 3/3 | 13/14 | ❌ 等于 always，降级态下全是低 |
    | top1 低置信 | 0/3 | 1/14 | ❌ 无预测力 |
    | 分数区分度低 | 0/3 | 3/14 | ❌ 无预测力 |
    | **单文档垄断** | 2/3 | 3/14 | ✅ 保留 |
    | **头部文档漂移** | 1/3 | 1/14 | ✅ 保留 |
    | 元数据块占位 | 1/3 | 0/14 | 精度 100%，但两条已被上面覆盖 |

    两个信号并集：**召回 3/3（全覆盖）、误触发 4/14、触发率 7/17 = 41%**。
    代价从 1.46s/次 降到 ≈0.60s/次（省 59%），收益不打折。

    ## 信号 ①：单文档垄断（前 5 名里同一文档 ≥4 条）
    机理：`per_doc_max=2` 会从该文档第 3 条起截断 ⇒ 候选池被砍掉一半，
    而"前 5 名被同一文档占满"恰恰说明正确答案大概率也在这份文档里（只是排在第 3+ 位被配额挡掉）。
    实测命中：`混动功率指标`（金标 RRF 第 4，同一文档占满前 5）。

    ## 信号 ②：头部文档漂移（top1 与后 4 名中 ≥3 条来自不同文档）
    机理：问的是 A 文档的事、排上来的却是 B 文档 ⇒ 是**词面相似**而非语义相关。
    实测命中：`验收交付要交哪些文档`（top1=技术要求书 §10，但 top3~5 全是
    SysML 文档的"已知局限/文档自检"，金标被挤到第 9）。

    ## 为什么不做"元数据块占位"
    它精度 100%（1/1 命中失败，0 误触发），但那唯一命中的用例已被"单文档垄断"覆盖。
    **加进来只增加代码复杂度不增收益** —— 保留判据在诊断脚本里，等真实语料出现
    "只有元数据块占位、不垄断也不漂移"的失败形态时再加。
    """
    hits = list(hits or [])
    if len(hits) < 5:
        return False, ""
    top5 = hits[:5]

    # ── 信号 ①：单文档垄断 ──
    docs = [(h.get("source_doc") or "") for h in top5]
    if docs:
        mx = max(docs.count(d) for d in set(docs) if d)
    else:
        mx = 0
    if mx >= 4:
        return True, "单文档垄断(%d/5)" % mx

    # ── 信号 ②：头部文档漂移 ──
    d1 = top5[0].get("source_doc") or ""
    if d1:
        later = [(h.get("source_doc") or "") for h in top5[1:5]]
        diff = sum(1 for d in later if d and d != d1)
        if diff >= 3:
            return True, "头部文档漂移(%d/4 异文档)" % diff

    return False, ""


def _rerank_mode() -> str:
    """解析重排模式（三态）。返回 "off" | "on" | "auto"。

    优先级：`rag.rerank_mode`（新） > `rag.rerank_enabled`（旧布尔，兼容）。
    - 旧配置只有 `rerank_enabled`：False→off、True→on，**行为与改动前完全一致**
    - 新配置 `rerank_mode`：显式三态，"auto" 即条件重排
    """
    try:
        from core import config as _cfg
    except Exception:
        return "off"
    try:
        mode = str(_cfg.get("rag", "rerank_mode", "") or "").strip().lower()
        if mode in ("off", "on", "auto"):
            return mode
    except Exception:
        pass
    try:
        return "on" if _cfg.get("rag", "rerank_enabled", False) else "off"
    except Exception:
        return "off"


def llm_rerank(query: str, hits: list, top_k: int = 5, max_candidates: int = 8,
               timeout: int = 25) -> list:
    """按 LLM 相关性分重排 hits（原列表元素就地补 rerank_score/reranked 字段）。

    返回排序后的列表（截断到 top_k）。任何异常/关闭 → 原样返回（静默降级，不阻断检索主链路）。

    ★ 2026-11-02 三态开关（`rag.rerank_mode`）：
      - `off`  永不重排
      - `on`   总是重排（每条 +1.46s）
      - `auto` **条件重排**：仅当 `needs_rerank()` 命中时才调用 LLM
              （实测触发率 41%、召回 3/3，平均代价 ≈0.60s/次）
    判据与实测数据见 `needs_rerank()` 文档。
    """
    if not hits or len(hits) <= 1:
        return hits
    _mode = _rerank_mode()
    if _mode == "off":
        return hits[:top_k]
    if _mode == "auto":
        _need, _why = needs_rerank(hits)
        if not _need:
            return hits[:top_k]
        try:
            import logging
            logging.getLogger(__name__).info("条件重排触发：%s", _why)
        except Exception:
            pass
    try:
        from core import config as _cfg
        from llm import llm_client
        cand = hits[:max_candidates]
        segs = "\n".join(
            f"[{i}] 章节：{h.get('section', '') or '-'}\n内容：{(h.get('content') or '')[:300]}"
            for i, h in enumerate(cand))
        # ★ 可指定重排专用 provider（2026-10-10）：
        #   实测默认 provider（ds-v4-flash-neibu/109）当时**整体不可用**
        #   （所有 intent 一律31.7s 超时、ct=0），导致重排 100% 静默降级。
        #   ⇒ 重排对延迟更敏感（每次查询都要跑），应能独立指定一个更稳的 provider。
        _pid = 0
        try:
            _pid = int(_cfg.get("rag", "rerank_provider_id", 0) or 0)   # ★ rag 组（与 SCHEMA/DEFAULT 一致）
        except Exception:
            _pid = 0
        # ★★ 这里**必须用 `_cfg.get(...)`，不能写 `_("rag", ...)`**——
        #   `_` 在本函数里**没有定义**（那是配置文件里的组名，不是函数）⇒
        #   会抛 `NameError: name '_' is not defined`，而症状是
        #   **静默回退原排序**（表现为"开关开了但顺序一字未变"），排查花了 6 轮。
        resp = llm_client.chat(
            [{"role": "system", "content": (
                "你是检索重排器。给定用户查询与若干候选片段，逐段评估与查询的相关性（0=无关，10=直接回答查询）。"
                '只输出 JSON：{"ratings":[{"i":候选序号,"score":0到10}]}，必须覆盖全部候选，不要其他文字。')},
             {"role": "user", "content": f"查询：{query}\n\n候选片段：\n{segs}"}],
            _intent="rag_rerank",
            **({"provider_id": _pid} if _pid else {}))
        msg = (resp.get("choices") or [{}])[0].get("message", {}) or {}
        try:
            m = json.loads(msg.get("content") or "{}")
        except Exception as e:
            # ★★ 失败必须留痕（2026-10-10）：原先 `except: return hits[:top_k]`
            #   把"provider 整体不可用"这类**系统性故障**完全静默，
            #   表现为"开关开了但顺序一字未变"，排查花了 4 轮。
            # ★★ 2026-10-21 修 logging 格式化 bug：原格式串只有 3 个占位符
            #   （provider_id / 异常类型 / content）却传了 **4 个**实参
            #   （多传了一个 str(e)）⇒ logging 内部抛
            #   `TypeError: not all arguments converted during string formatting`
            #   ⇒ **这条唯一的诊断线索被 logging 自己吞掉**（只往 stderr 打
            #   一段 traceback），warning 记录为空 ⇒ 等于"留痕"失效。
            #   症状：重排失败时日志里既没有 provider 也没有 content。
            #   实参个数必须与占位符严格一致（下方 verify_rerank_logging_fmt 门禁守）。
            import logging
            logging.getLogger(__name__).warning(
                "llm_rerank 解析失败（provider_id=%s）: %s: %s | content 前 80=%r",
                _pid or "(默认)", type(e).__name__, str(e)[:80],
                str(msg.get("content") or "")[:80])
            return hits[:top_k]
        scores = {int(x["i"]): float(x["score"]) for x in (m.get("ratings") or [])
                  if isinstance(x, dict) and str(x.get("i", "")).lstrip("-").isdigit()}
        if not scores:
            #★ 也要留痕（2026-10-10）："scores 为空"与"解析失败"是两种不同故障，
            #   但都表现为"顺序未变"，不打印就无法区分。
            import logging
            logging.getLogger(__name__).warning(
                "llm_rerank 未拿到评分（provider_id=%s）"
                "|ratings=%r|content=%r",
                _pid or "(默认)", (m.get("ratings") or [])[:3],
                str(msg.get("content") or "")[:80])
            return hits[:top_k]
        for i, h in enumerate(cand):
            s = scores.get(i)
            if s is not None:
                h["rerank_score"] = round(max(0.0, min(10.0, s)), 2)
                h["reranked"] = True
            else:
                h["rerank_score"] = 0.0
                h["reranked"] = False
        reranked = sorted(cand, key=lambda h: h.get("rerank_score", 0.0), reverse=True) + hits[max_candidates:]
        return reranked[:top_k]
    except Exception as e:
        #★★ 最外层也留痕（2026-10-10 排查 6 轮才找到这里）：
        #   排序段之后的任何异常都被这一行静默吞掉，症状同样是"顺序未变"。
        import logging
        logging.getLogger(__name__).warning(
            "llm_rerank 排序段异常（provider_id=%s）: %s: %s",
            _pid or "(默认)", type(e).__name__, str(e)[:160])
        return hits[:top_k]
