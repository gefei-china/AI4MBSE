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
  · **零信息量事实不注入（P1-7，2026-10-01）** —— 标题恰为系统性兜底名
    （`core/artifact_titles.py`）的产物行**整行不列**（连计数一起）；过滤后无内容则整段
    返回 ""。实测这类行会把摘要的「引导语占比」推到 81.8%（conv 351：事实仅 17 字符）。
"""
import json

from core.artifact_titles import is_placeholder_title

# 段落标记：调用方（自检/探针）据此识别"摘要段在不在"，改这里要同步 tools/verify/verify_multiturn_context.py
DIGEST_MARK = "【本会话既有产物】"

# P1-7（2026-10-01）：126 → 54 字符。原句把同一件事说了三遍（"在既有产物上增量"／
# "不要从零重建"／"不要把它们当作不存在"），且示例「在 v0.1 的需求模型上…」是纯教学。
# 实测该引导语在小摘要会话里占到 81.8%。planner 落点另有 stream.py/planner.py 的追加段
# 兜底（措辞更具体：title/context 怎么写、禁止"从零分析"类任务），故压缩不影响 planner；
# 子任务上下文/历史块两个落点只吃本段，故**保留条件句与核心动词短语**，不做电报式极简。
_LEAD = (
    "下列为本会话既有产物：若本请求与之相关，请在**既有产物上做增量修改**"
    "（引用名称/版本），无关则忽略本段。")

_KIND_LABEL = {"sysml": "SysML 视图", "code": "代码文件", "report": "报告", "document": "文档"}

# 版本状态本地化：`sysml_versions.status` 库里是英文枚举（current/superseded/draft…），
# 直接吐给模型会让摘要中英混排、且「superseded」对中文推理不友好。
_STATUS_LABEL = {"current": "当前", "superseded": "历史版本", "draft": "草稿",
                 "adopted": "已采纳", "rejected": "已否决", "archived": "已归档"}

# 单次摘要内最多列出的产物标题数（防产物体量大时摘要自身膨胀）
_MAX_TITLES = 6
# 单条版本最多列出的元素名数（大模型动辄几十个元素，全列会挤掉别的版本）
_MAX_NODES = 12
# 单条版本最多列出的「丢失元素」名数（丢失项是告警，给够证据即可，不铺满预算）
_MAX_REMOVED = 6


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
    #    ★ 2026-11-09：加取 `diff` —— 只报"元素 N 个"是死数字，模型看不出相比上一版
    #      **少了什么**。全库实测 86.5% 的修订轮次在丢元素，而原摘要对此完全沉默。
    try:
        rows = conn.execute(
            "SELECT version_label, status, adopted, element_summary, created_at, "
            "length(COALESCE(code_text,'')), diff FROM sysml_versions WHERE conversation_id=? "
            "ORDER BY id DESC LIMIT ?", (cid, n)).fetchall()
        for r in rows:
            es = _safe_json(r[3], dict) or {}
            chk = _safe_json(es.get("check"), dict) or {}
            df = _safe_json(r[6], dict) or {}
            cov = _safe_json(es.get("coverage"), dict) or {}
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
                "removed_nodes": [str(x) for x in (df.get("removed_nodes") or []) if x],
                "added_nodes": [str(x) for x in (df.get("added_nodes") or []) if x],
                # 累积契约（2026-11-09）：历史上出现过、当前版本没有的元素
                "cov_lost": [str(x) for x in (cov.get("lost") or []) if x],
                "cov_lost_n": int(cov.get("lost_n") or 0),
                "cov_contract": int(cov.get("contract_size") or 0),
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


def render_digest(facts: dict, max_chars: int = 900, *,
                 skip_placeholder_only: bool = True) -> str:
    """事实 → 可注入文本。空事实返回 ""（调用方据此不加段落）。

    skip_placeholder_only（P1-7，2026-10-01）：标题**全是**系统性兜底名
    （`core/artifact_titles.is_placeholder_title`）的 kind 行**整行不列**。
    理由：这类行只有"存在性"、没有可引用的名字，实测把引导语占比推到 81.8%。
    过滤后 lines 为空 → 返回 ""（与「无产物」同语义，调用方按既有逻辑不加段落）。
    keywords-only：避免既有 `render_digest(facts, 120)` 位置调用被静默改变行为。
    """
    if not facts:
        return ""
    lines = []
    for _idx, v in enumerate(facts.get("versions") or []):
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
        # ★ 2026-11-09：把「这一版比上一版少了什么」显式说出来。
        #   之前只报"元素 N 个"，模型无法察觉自己在重写中删掉了东西（实测 86.5% 的修订
        #   轮次在丢元素，用户原话是"让它校验后它把视图代码丢掉了"）。
        #   这里不针对任何具体元素类型，只做集合差集的事实陈述 + 一条通用判据。
        #   ★ 进一层：最新版优先报「相对**累积契约**缺多少」而不是「比上一版少多少」——
        #     逐版比对会漏掉累积蚕食（v2 丢 A、v3 只改别处 ⇒ v3 逐版看完全正常，但 A 已消失两轮）。
        _rm = v.get("removed_nodes") or []
        _cl = v.get("cov_lost") or []
        _cln = int(v.get("cov_lost_n") or 0)
        if _idx == 0 and _cln:
            _shown = "、".join(_cl[:_MAX_REMOVED])
            _tail = f" 等 {_cln} 个" if _cln > _MAX_REMOVED else ""
            lines.append(
                f"  ⚠️ 相对**累计契约**缺 {_cln} 个元素（{_shown}{_tail}）"
                "——这些是历史上写过、当前版本里没有的；若仍属需求范围，必须补回并复核。")
        elif _rm:
            _shown = "、".join(_rm[:_MAX_REMOVED])
            _tail = f" 等 {len(_rm)} 个" if len(_rm) > _MAX_REMOVED else ""
            lines.append(
                f"  ⚠️ 该版比上一版**少了 {len(_rm)} 个元素**（{_shown}{_tail}）"
                "——若它们仍在需求范围内，必须在下一版补回并复核，不得静默省略。")
    for kind, titles in (facts.get("kinds") or {}).items():
        label = _KIND_LABEL.get(kind, kind)
        cnt = int((facts.get("counts") or {}).get(label, len(titles)) or 0)
        uniq = _uniq_titles(titles)
        # P1-7：标题全是系统性兜底名的 kind 行**整行丢弃**（连计数一起）——
        # "文档 6 项（AI 生成文档）"这种行不携带任何可引用信息，留着只稀释预算。
        real = [t for t in uniq if not is_placeholder_title(t)]
        if skip_placeholder_only and uniq and not real:
            continue
        # ⚠️ 开关关闭时必须**逐字节回到改动前**（列全部去重名），不能只列 real ——
        # 否则回滚开关形同虚设（验收 A18 就是这么抓住第一版的）。
        _shown = (real or uniq) if skip_placeholder_only else uniq
        # P0-7：`cnt` 是 SQL 行数、`uniq` 是**去重后名称**，两者口径不同。实测输出
        # 「SysML 视图 4 项（需求图、活动图、追溯视图）」= 4 行 / 3 个名字，读者会以为少列了一项。
        if _shown and len(_shown) != cnt:
            tail = f"（{cnt} 条中不重名 {len(_shown)} 种：{'、'.join(_shown)}）"
        else:
            tail = (f"（{'、'.join(_shown)}）" if _shown else "")
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


def build_digest(conn, conversation_id, max_chars=None, versions=None,
                skip_placeholder_only=None) -> str:
    """对外主入口：受配置开关/上限控制，异常一律返回 "" 并留痕。

    配置（`core/config.py` → context）：
      artifact_digest_enabled（bool，默认 True）
      artifact_digest_max_chars（int，默认 900，<=0 不限）
      artifact_digest_versions（int，默认 3，最近 N 个 SysML 版本）
      artifact_digest_skip_placeholder_only（bool，默认 True；P1-7 新增，false=回到改动前）
    """
    try:
        from core import config as _cfg
        if not _cfg.as_bool("context", "artifact_digest_enabled", True):
            return ""
        if max_chars is None:
            max_chars = int(_cfg.get("context", "artifact_digest_max_chars", 900) or 0)
        if versions is None:
            versions = int(_cfg.get("context", "artifact_digest_versions", 3) or 3)
        if skip_placeholder_only is None:
            skip_placeholder_only = _cfg.as_bool(
                "context", "artifact_digest_skip_placeholder_only", True)
    except Exception:
        if max_chars is None:
            max_chars = 900
        if versions is None:
            versions = 3
        if skip_placeholder_only is None:
            skip_placeholder_only = True
    try:
        return render_digest(collect_facts(conn, conversation_id, versions=versions), max_chars,
                             skip_placeholder_only=bool(skip_placeholder_only))
    except Exception:
        import logging
        import traceback
        logging.getLogger(__name__).warning(
            "[artifact_digest] 会话产物摘要生成失败（conv=%s）：\n%s", conversation_id, traceback.format_exc())
        return ""

def load_parent_user_msgs(conn, conversation_id, count=None, max_chars=None,
                          exclude_latest: bool = True) -> list:
    """读**父会话最近 N 条 user 消息原文**，供编排子任务上下文注入。

    ★★★ 2026-11-19 P0-A 新增（conv 999999905 / msg#7013 → #7014 事故）。

    为什么必须有它（本模块顶部 ④ 条断链的直接后果）：
      编排子任务以 `execute_stream(query, conversation_id=0)` 运行 ⇒ `_load_history` 为空。
      既有补偿只有 `artifact_digest`，而它是**产物清单**（版本名/元素名/关系数），
      **不含用户需求原文**。实测对 msg#7011 的 8 个需求独有短语命中 **0/8**。
      于是子任务 t2「诊断用户原始提示词的模糊点」只能反推，自报「原始提示词未取回」，
      最终 10304 字报告里**没有提示词**，用户追问「我的提示词呢」。

    exclude_latest（默认 True）：
      前端 `/chat/stream` 是**先落 user 消息、再跑 execute_stream** ⇒ id 最大的 user 消息
      就是本轮输入，它已通过 `goal` / `query` 进子任务，重复注入会让子任务误当成"另一条需求"。
      ⚠️ 已知取舍：`resume_run_id`（断点续跑不新增 user 消息）时，最新一条是**上一轮**输入，
      会被排除 ⇒ 少一条。宁可少一条也不要制造"同一条需求出现两次"的歧义。

    返回：[{"label": "第N轮" 或 "更早", "content": "原文"} ...]（按时间正序）
    任何异常都返回 []（绝不阻断编排主链路），与 build_digest 的降级口径一致。
    """
    try:
        from core import config as _cfg
        if count is None:
            count = int(_cfg.get("context", "subtask_parent_msgs_count", 3) or 0)
        if max_chars is None:
            max_chars = int(_cfg.get("context", "subtask_parent_msgs_max_chars", 6000) or 0)
        enabled = _cfg.as_bool("context", "subtask_parent_msgs_enabled", True)
    except Exception:
        count, max_chars, enabled = 3, 6000, True
    if not enabled or int(count) <= 0 or int(max_chars) <= 0:
        return []
    try:
        limit = int(count) + (1 if exclude_latest else 0)
        rows = conn.execute(
            "SELECT id, content FROM messages WHERE conversation_id=? AND role='user' "
            "ORDER BY id DESC LIMIT ?", (conversation_id, limit)).fetchall()
        items = []
        for r in (rows or []):
            try:
                mid, content = r[0], r[1]
            except Exception:
                mid, content = r["id"], r["content"]
            txt = (content or "").strip()
            if not txt:
                continue
            items.append({"id": mid, "content": txt})
        if exclude_latest and items:
            items = items[1:]                 # 去掉最新一条 = 本轮输入
        items.reverse()                       # 时间正序
        if not items:
            return []
        # 逐条均分预算（避免第一条把预算吃光、后面的全被截成空）
        per = max(200, int(max_chars) // max(len(items), 1))
        out = []
        n = len(items)
        for i, it in enumerate(items):
            txt = it["content"]
            if len(txt) > per:
                txt = txt[:per] + "…（已截断）"
            # 标签：越早越靠前的轮次，统一叫「更早」；最近一条叫「上一轮」
            label = "上一轮" if i == n - 1 else ("更早第%d条" % (n - i))
            out.append({"label": label, "content": txt})
        return out
    except Exception:
        import logging
        import traceback
        logging.getLogger(__name__).warning(
            "[parent_user_msgs] 父会话用户原文读取失败（conv=%s）：\n%s",
            conversation_id, traceback.format_exc())
        return []
