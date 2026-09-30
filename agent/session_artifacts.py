# -*- coding: utf-8 -*-
"""会话产物摘要：把「本会话已产出的东西」压成一段可注入的紧凑事实（多轮上下文）。

为什么需要它（2026-09-30 实测，会话 514「整车需求数据」）：
  第 1 轮产出了**真实内容** —— `sysml_versions` v0.1（元素 IRRequirement + ir1..ir4、
  关系 4 条、本地校验 verdict=pass）、`artifacts` 3 项（需求图 / 活动图 / 2756 字符 SysML 代码）。
  第 2 轮用户只说「优化需求数据，需要支持参与者信息」，模型却把它当**全新需求**从零设计，
  正文里写着「原始需求数据仅有编号与正文是不够的」—— 而那句话指的东西第 1 轮已经建好了。

四处断链（各自独立，实测取证见方案文档 §7）：
  ① 编排入口只把**当前这一句**当 goal（`orchestration.py:148` / `:238`），planner 提示词里
     没有任何「这个会话已经有什么」的事实；
  ② 流式 planner 提示词（`stream.py:77-105`）**连 `context` 字段都不收**（只要 key/title/agent/deps/task_type）
     → 子任务 `agent_tasks.context` 结构性必空；
  ③ 产物链路只有**写**没有**读回**：全仓读 `sysml_versions`/`artifacts` 的生产代码只有
     `agent/utils.py`，且全在归档写入路径（判重 / 取上一版做 diff）；
  ④ 子任务的 `execute_stream` 用 `conversation_id=0` → `_load_history` 对子任务等于空。

本模块只做一件事：**只读**地把会话已有产物压成一段文本，供四处消费
（流式 planner / 非流式 planner / 子任务上下文快照 / 会话历史注入）。

三条设计约束（都有依据，改动前先读）：
  · **无产物 → 返回 ""** —— 调用方据此完全不加段落，与产物无关的会话**提示词逐字节不变**
    （这是本批零回归的保证，也是验收断言 D1 的对象）。
  · **只报事实，不做语义筛选** —— 不判"哪些产物与本轮相关"。本仓已有一次失败先例：
    `context.model_context_entities` 曾尝试按语义过滤既有实体，标定实测 dense/bigram
    两路分布重叠、**无可用阈值**，最终退回 `count`。相关性交给消费它的 LLM 判，
    摘要只负责"把事实端上桌"。
  · **失败静默但留痕** —— 异常一律返回 ""，同时打 warning。本仓多次因"静默兜底"付出排查成本
    （取不到 与 本就没有 症状相同，只能靠留痕区分）。
"""
import json

# 段落标记：调用方（自检/探针）据此识别"摘要段在不在"，改这里要同步 tools/verify/verify_multiturn_context.py
DIGEST_MARK = "【本会话既有产物】"

_LEAD = (
    "本会话此前已产出下列内容。本轮是多轮会话的后续请求：若本请求与它们相关，"
    "请在**既有产物上做增量修改**（明确引用其名称/版本，例如「在 v0.1 的需求模型上新增参与者」），"
    "不要从零重建、也不要把它们当作不存在；若本请求确实与它们无关，忽略本段即可。")

_KIND_LABEL = {"sysml": "SysML 视图", "code": "代码文件", "report": "报告", "document": "文档"}

# 版本状态本地化：`sysml_versions.status` 库里是英文枚举（current/superseded/draft…），
# 直接吐给模型会让摘要中英混排、且「superseded」对中文推理不友好。
_STATUS_LABEL = {"current": "当前", "superseded": "历史版本", "draft": "草稿",
                 "adopted": "已采纳", "rejected": "已否决", "archived": "已归档"}

# 单次摘要内最多列出的产物标题数（防产物体量大时摘要自身膨胀）
_MAX_TITLES = 6
# 单条版本最多列出的元素名数（大模型动辄几十个元素，全列会挤掉别的版本）
_MAX_NODES = 12


def _uniq_titles(titles):
    """去重保序。

    实测（2026-09-30，真库）：`artifacts.title` 大量是**自动占位名** —— 会话 1 的 6 条文档
    标题全是「AI 生成文档」、3 条报告全是「AI 生成报告」。不去重时摘要长成
    「- 文档 6 项（AI 生成文档、AI 生成文档、AI 生成文档、AI 生成文档、AI 生成文档、AI 生成文档）」
    —— 既零信息量又白占预算。
    """
    seen, uniq = set(), []
    for t in titles:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _safe_json(raw, expect):
    """宽松 JSON 解析：类型不符/解析失败 → 返回同类空值（None 表示"不要这条"）。"""
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None
    return v if isinstance(v, expect) else None


def collect_facts(conn, conversation_id, versions=3, titles=_MAX_TITLES) -> dict:
    """只读采集会话产物事实（不含 LLM、不改库）。

    返回 {"versions": [...], "kinds": {kind: [title,...]}, "reports": [...], "counts": {...}}。
    取数列一律用**下标**而非列名 —— 调用方连接可能未设 row_factory（本仓两种都有）。
    """
    facts = {"versions": [], "kinds": {}, "reports": [], "counts": {}}
    try:
        cid = int(conversation_id or 0)
    except Exception:
        return facts
    if not cid:
        return facts
    n = max(1, int(versions or 3))

    # ① SysML 模型版本链（最新在前）：版本号 / 状态 / 元素与关系数 / 元素名 / 本地校验结论
    try:
        rows = conn.execute(
            "SELECT version_label, status, adopted, element_summary, created_at, "
            "length(COALESCE(code_text,'')) FROM sysml_versions WHERE conversation_id=? "
            "ORDER BY id DESC LIMIT ?", (cid, n)).fetchall()
        for r in rows:
            es = _safe_json(r[3], dict) or {}
            chk = _safe_json(es.get("check"), dict) or {}
            facts["versions"].append({
                "label": str(r[0] or ""),
                "status": str(r[1] or ""),
                "adopted": bool(r[2]),
                "at": str(r[4] or "")[:16],
                "entities": int(es.get("entities") or 0),
                "relations": int(es.get("relations") or 0),
                "views": int(es.get("views") or 0),
                "nodes": [str(x) for x in (es.get("nodes") or []) if x],
                "verdict": str(chk.get("verdict") or ""),
                "code_len": int(r[5] or 0),
            })
    except Exception:
        pass

    # ② 会话产物（按 kind 归组，标题即人类可读名）
    try:
        rows = conn.execute(
            "SELECT kind, title FROM artifacts WHERE conversation_id=? ORDER BY id", (cid,)).fetchall()
        for r in rows:
            k = str(r[0] or "")
            facts["kinds"].setdefault(k, [])
            t = str(r[1] or "").strip()
            if t and len(facts["kinds"][k]) < max(1, int(titles or _MAX_TITLES)):
                facts["kinds"][k].append(t)
        facts["counts"] = {
            _KIND_LABEL.get(k, k): int(conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE conversation_id=? AND kind=?", (cid, k)).fetchone()[0])
            for k in facts["kinds"]}
    except Exception:
        pass

    # ③ 已归档报告（reports.conversation_id 在存量数据里可能为空 → 忽略）
    try:
        rows = conn.execute(
            "SELECT title FROM reports WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
            (cid, max(1, int(titles or _MAX_TITLES)))).fetchall()
        facts["reports"] = [str(r[0]) for r in rows if r[0]]
    except Exception:
        pass
    return facts


def render_digest(facts: dict, max_chars: int = 900) -> str:
    """事实 → 可注入文本。空事实返回 ""（调用方据此不加段落）。"""
    if not facts:
        return ""
    lines = []
    for v in (facts.get("versions") or []):
        bits = []
        _st = v.get("status") or ""
        if v.get("adopted"):
            bits.append("已采纳")
        elif _st:
            bits.append(_STATUS_LABEL.get(_st, _st))
        if v.get("verdict"):
            bits.append(f"本地校验 {v['verdict']}")
        head = f"- SysML 模型版本 {v.get('label') or '?'}"
        if bits:
            head += "（" + "，".join(bits) + "）"
        detail = []
        if v.get("entities"):
            _all = [str(x) for x in (v.get("nodes") or [])]
            if len(_all) > _MAX_NODES:
                _shown = "、".join(_all[:_MAX_NODES]) + f"、…（共 {v['entities']} 个）"
            else:
                _shown = "、".join(_all)
            detail.append(f"元素 {v['entities']} 个" + (f"（{_shown}）" if _shown else ""))
        if v.get("relations"):
            detail.append(f"关系 {v['relations']} 条")
        if v.get("views"):
            detail.append(f"视图 {v['views']} 个")
        if v.get("code_len"):
            detail.append(f"模型代码 {v['code_len']} 字符")
        lines.append(head + ("：" + "；".join(detail) if detail else ""))
    for kind, titles in (facts.get("kinds") or {}).items():
        label = _KIND_LABEL.get(kind, kind)
        cnt = int((facts.get("counts") or {}).get(label, len(titles)) or 0)
        uniq = _uniq_titles(titles)
        # P0-7：`cnt` 是 SQL 行数、`uniq` 是**去重后名称**，两者口径不同。实测输出
        # 「SysML 视图 4 项（需求图、活动图、追溯视图）」= 4 行 / 3 个名字，读者会以为少列了一项。
        if uniq and len(uniq) != cnt:
            tail = f"（{cnt} 条中不重名 {len(uniq)} 种：{'、'.join(uniq)}）"
        else:
            tail = (f"（{'、'.join(uniq)}）" if uniq else "")
        lines.append(f"- {label} {cnt} 项" + tail)
    if facts.get("reports"):
        lines.append("- 已归档报告：" + "、".join(_uniq_titles(facts["reports"])))
    if not lines:
        return ""
    text = DIGEST_MARK + "\n" + _LEAD + "\n" + "\n".join(lines)
    cap = int(max_chars or 0)
    if cap > 0 and len(text) > cap:
        text = text[:max(0, cap - 12)].rstrip() + "\n…（摘要已截断）"
    return text


def build_digest(conn, conversation_id, max_chars=None, versions=None) -> str:
    """对外主入口：受配置开关/上限控制，异常一律返回 "" 并留痕。

    配置（`core/config.py` → context）：
      artifact_digest_enabled（bool，默认 True）
      artifact_digest_max_chars（int，默认 900，<=0 不限）
      artifact_digest_versions（int，默认 3，最近 N 个 SysML 版本）
    """
    try:
        from core import config as _cfg
        if not _cfg.as_bool("context", "artifact_digest_enabled", True):
            return ""
        if max_chars is None:
            max_chars = int(_cfg.get("context", "artifact_digest_max_chars", 900) or 0)
        if versions is None:
            versions = int(_cfg.get("context", "artifact_digest_versions", 3) or 3)
    except Exception:
        if max_chars is None:
            max_chars = 900
        if versions is None:
            versions = 3
    try:
        return render_digest(collect_facts(conn, conversation_id, versions=versions), max_chars)
    except Exception:
        import logging
        import traceback
        logging.getLogger(__name__).warning(
            "[artifact_digest] 会话产物摘要生成失败（conv=%s）：\n%s", conversation_id, traceback.format_exc())
        return ""
