# -*- coding: utf-8 -*-
"""`requirement_itemize` —— 需求条目化 + 需求视图骨架生成。

为什么一个工具做两件事（这是刻意的设计）
--------------------------------------
N1（需求结构化）的产物是条目数据 `items[]`，但**条目本身不是模型**——
要进 SysML 必须变成 `requirement def` + 关系（refine/derive/satisfy）。
本工具一次产出两份，让 N1 的输出**直接可被 N3 消费**，中间不需要再转一次：

  ① `items`      —— 条目数据（供追溯矩阵、覆盖性分析、报告）
  ② `req_view_code` —— **需求视图骨架代码**（`package` + stakeholder + requirement + 关系）

职责边界（重要，别越界）
------------------------
* 本工具**只生成需求视图**，不生成结构/行为/视图 —— 那是 N3 其他 `view_type` 的活。
* `satisfy`（需求→设计元素）**不在这里生成**：此时还没有 design 元素，
  强行生成会引用不存在的元素（实测报`Must be a valid feature`）。
  satisfy 分配由 **N2 骨架生成后 / N5 追溯核验时**补，本工具只留 `satisfy_target` 字段供后续引用。

    ┌─ 需求条目化 ──► items[]（数据）
    │                req_view_code（需求视图：stakeholder/requirement/refine/derive）
    │                          │
    └──────────────────────────┴──► N3 视图展开（structure/usecase/activity…）
                                        │
                                        ▼N2/N5 补 satisfy 分配（因为需要 design 元素）
"""
import logging
import re
import time
import uuid

logger = logging.getLogger(__name__)

CATEGORIES = ["functional", "performance", "interface", "constraint"]
CAT_CODE = {"functional": "F", "performance": "P", "interface": "I", "constraint": "C"}

# 模糊词（实测来自 requirement_quality.FUZZY_WORDS 的同款判据）
FUZZY = ("良好", "优秀", "快速", "高效", "友好", "灵活", "尽量", "尽可能", "适当",
         "合理", "显著", "大幅", "一定程度", "若干", "某些")
# 量纲词（出现数值+单位 ⇒ 性能类）
# ⚠️ **只收录含中文/明确多字符的单位**。实测踩过：收录单字母 `C`/`s`/`A`/`V`/`W` 后，
#   「通过 CAN 总线通信」会因含 C、A 而被判成 performance ⇒ 接口需求全被误分类。
#   单位要么用中文（分钟/秒/℃），要么用带边界的正则（km/h、percent）。
PERF_HINT = ("℃", "分钟", "秒", "毫秒", "百分比", "年", "个月", "天", "小时",
             "km/h", "km", "kg", "mAh", "Wh", "rpm", "N·m", "kW", "MW")
# 数值 + 单位的严格正则（ASCII 单位须带边界，避免命中普通英文单词）
_PERF_RE = re.compile(
    r"\d+\s*(℃|度|分钟|min|秒|s\b|km/h|km|kg|mAh|Wh|rpm|N·m|kW|MW|W\b|V\b|A\b|%)",
    re.I)
# 接口类线索
IFACE_HINT = ("接口", "通信", "协议", "信号", "总线", "CAN", "LIN", "UART",
              "SPI", "I2C", "OBD", "UDS", "API", "信号量", "针脚")

_SENT_SPLIT = re.compile(r"(?<=[。；！？\n])|(?<=[.;])\s+")


def _as_text(v):
    return str(v or "").strip()


def _looks_perf(s):
    return any(u in s for u in PERF_HINT) or bool(_PERF_RE.search(s))


def _looks_iface(s):
    return any(k in s for k in IFACE_HINT)


def _classify(s):
    """确定性分类：接口线索 > 性能线索 > 约束线索 > 默认功能。"""
    if _looks_iface(s):
        return "interface"
    if _looks_perf(s):
        return "performance"
    if re.search(r"(不得|禁止|必须|应|shall|must|shall not)", s):
        return "constraint"
    return "functional"


def _verifiability(s):
    """返回 (是否可验证, 原因)。不可验证的条目必须进 gaps，不许混进 items。

    ⚠️ 实测踩过：可验证动词表最初只含「能够/可以/应能/支持/实现/提供/完成」，
    结果「系统应通过 CAN 总线与整车控制器通信」被判**不可验证**扔进 gaps
    —— 但接口需求完全可验证（可通过总线测试验收）。
    ⇒ 动词表必须覆盖**接口/安全/约束类**的常见动作词。
    """
    reasons = []
    if any(f in s for f in FUZZY):
        reasons.append("含模糊词（" + "、".join(
            f for f in FUZZY if f in s) + "）")
    if re.search(r"(良好|优秀|快速|高效|灵活|方便)", s) and not _looks_perf(s):
        reasons.append("形容词无量化判据")
    # 可验证动作词：功能 + 接口 + 安全 + 约束四类
    if not re.search(r"[0-9０-９]", s) and not re.search(
            r"(能够|可以|应能|支持|实现|提供|完成|满足|符合|"
            r"通信|交互|传输|发送|接收|连接|集成|预留|"
            r"检测|监测|报警|告警|预警|保护|联锁|追溯|"
            r"不得|禁止|必须|应当)", s):
        reasons.append("无量化指标且无可验证动词")
    return (len(reasons) == 0, "；".join(reasons))


def _camel(name):
    """把中文/任意文本转成合法的 ASCII 标识符片段。"""
    parts = re.findall(r"[A-Za-z0-9]+", name or "")
    if parts:
        return "".join(p[:1].upper() + p[1:] for p in parts) or "Req"
    return "Req"


def itemize(text, domain="", source_ref="", project="EVTM"):
    """核心：自然语言需求 → (items, gaps, req_view_code, stats)"""
    raw = _as_text(text)
    items, gaps = [], []
    seen_text = {}
    used_names = set()          # ⚠️ 已用标识符集合 —— 防止中文名转ASCII 后撞名

    for seg in _SENT_SPLIT.split(raw):
        s = _as_text(seg)
        if len(s) < 4:                      # 太短，多半是标题或噪声
            continue
        ok, why = _verifiability(s)
        if not ok:
            gaps.append({"text": s, "reason": why, "need_clarification": True})
            continue
        if s in seen_text:                  # 完全重复
            gaps.append({"text": s, "reason": "与第 %d 条重复" % seen_text[s],
                         "need_clarification": False})
            continue
        cat = _classify(s)
        rid = "REQ-%s%03d" % (CAT_CODE[cat], len(items) + 1)
        # ⚠️ 标识符**必须唯一**。实测踩过：中文需求名转ASCII 后常只剩数字
        #   （「电池温差不超过 5℃」→ `Req5`、「热泵 COP…2.0」→ `Req2`）⇒ 多条同名 requirement。
        #   规则：`Req` + 语义片段；片段为空或已占用时，用 `reqId` 数字后缀兜底。
        base = _camel(s)[:48]
        name = ("Req" + base) if base and ("Req" + base) not in used_names else None
        if name is None:
            seq = len(items) + 1
            name = "Req%s_%02d" % (base if base else "Item", seq)
            while name in used_names:                # 极端情况：仍撞名则加后缀
                name += "x"
        used_names.add(name)
        seen_text[s] = len(items) + 1
        items.append({
            "id": rid,
            "text": s,
            "category": cat,
            "subject": "",                 # 由 N3 补（需要知道哪个 part 是主体）
            "source_ref": source_ref,
            "ambiguous": bool(re.search(r"(等|若干|某些|适当|尽量)", s)),
            "sysml_name": name,             # 需求视图里的 requirement 名（保证唯一）
        })

    code = _build_view(items, project)
    stats = {"sentences": len(items) + len(gaps), "items": len(items),
             "gaps": len(gaps),
             "by_category": {c: sum(1 for i in items if i["category"] == c)
                             for c in CATEGORIES}}
    return items, gaps, code, stats


def _esc(text):
    """doc /* */ 里不能出现 */ ，转义。"""
    return _as_text(text).replace("*/", "*/")


def _build_view(items, project):
    """生成需求视图骨架：package + stakeholder + requirement + refine 关系。

    ⚠️ **刻意不生成 satisfy** —— 此时无design 元素，生成必然报
       `Must be a valid feature`（实测）。satisfy 由 N2/N5 补。
    """
    L = []
    L.append("package %s_Requirements {" % project)
    L.append("  //需求视图骨架（由 requirement_itemize 生成）")
    L.append("  // 约定：satisfy 分配待design 元素确定后由 N2/N5 补充")
    L.append("  private import ScalarValues::*;")
    L.append("")
    # stakeholder（按条目 subject 聚合；空则给默认占位）
    subs = sorted({i["subject"] for i in items if i.get("subject")})
    if subs:
        L.append("  // ---------- 需求主体 ----------")
        for s in subs:
            L.append("  part def %s;" % _camel(s))
        L.append("")
    L.append("  // ---------- 需求条目 ----------")
    for i in items:
        nm = i["sysml_name"]
        L.append("  requirement %s {" % nm)
        L.append("    doc /* %s */" % _esc(i["text"]))
        L.append('    attribute reqId = "%s";' % i["id"])
        L.append('    attribute category = "%s";' % i["category"])
        L.append("  }")
    # 派生/细化关系：**只在有明确依据时生成**
    # ⚠️ 实测踩过：早期版本按「同 category 就细化」⇒ 6 条无 subject 的需求全挂到第1 条上，
    #   等于**凭空断言了 5 条不存在的派生关系**。
    #   正确口径：细化必须有依据 —— ① 同 subject（同主体下的性能指标细化于主需求）
    #                ② 文本显式含「细分/细化/其中/之一」等关系词。
    #   否则**不生成关系**，由 N3/N5 依建模判断补。
    REL_HINT = ("细分", "细化", "其中", "之一", "进一步", "子要求", "包括", "分为")
    by = {}
    for i in items:
        sub = i.get("subject") or ""
        if sub:                                # 有主体才可能构成细化
            by.setdefault(sub, []).append(i)
    rel = []
    for sub, lst in by.items():
        if len(lst) < 2:
            continue
        base = lst[0]
        for other in lst[1:]:
            if any(h in other["text"] for h in REL_HINT):
                rel.append((other, base))
    if rel:
        L.append("")
        L.append("  // ---------- 细化关系（:> = refines，有明确依据才生成）----------")
        for o, b in rel:
            L.append("  requirement %s :> %s;" % (o["sysml_name"], b["sysml_name"]))
    L.append("}")
    L.append("  // 注：细化关系仅在「同主体 + 关系词」时生成；"
             "无依据的关系请由 N3/N5 依建模判断补。")
    return "\n".join(L)


def _render(items, gaps, code, stats, domain):
    L = ["【需求条目化】确定性规则通道（无 LLM 参与）", ""]
    L.append(f"切分 {stats['sentences']} 句 → 条目 {stats['items']} 条 / "
             f"待澄清 {stats['gaps']} 条")
    dist = "、".join(f"{c}:{n}" for c, n in stats["by_category"].items() if n)
    L.append(f"分类分布：{dist or '（无）'}")
    L.append("")
    if items:
        L.append("条目清单：")
        for i in items:
            L.append(f"  {i['id']:12} [{i['category']:11}] {i['text'][:52]}")
    if gaps:
        L.append("")
        L.append("⚠ 待澄清（**不可直接建模**，需补量化判据）：")
        for g in gaps[:10]:
            L.append(f"  · {g['text'][:46]}")
            L.append(f"    原因：{g['reason']}")
        if len(gaps) > 10:
            L.append(f"  …… 另有 {len(gaps) - 10} 条")
    L.append("")
    L.append("需求视图骨架（已生成，可直接作为 N3 view_type=requirement 的基础）：")
    L.append("```sysml")
    L.append(code)
    L.append("```")
    L.append("")
    L.append("说明：")
    L.append("  · satisfy（需求→设计元素）**刻意未生成** —— 此时无 design 元素，"
             "生成会报 Must be a valid feature；由 N2/N5 补。")
    L.append("  · 需求项目的 subject（主体）需与架构对照，本工具留空交 N3 判定。")
    if domain:
        L.append(f"  · 领域：{domain}")
    return "\n".join(L)


def _exec(args):
    text = args.get("text") or args.get("raw_text")
    if not _as_text(text):
        return {"ok": False,
                "result": "参数 text 为空：请把待条目化的需求原文整段传入。"}
    domain = _as_text(args.get("domain"))
    source_ref = _as_text(args.get("source_ref"))
    project = _as_text(args.get("project")) or "EVTM"
    project = re.sub(r"[^A-Za-z0-9_]", "", project) or "EVTM"
    try:
        items, gaps, code, stats = itemize(str(text), domain, source_ref, project)
    except Exception as exc:                                       # noqa: BLE001
        return {"ok": False,
                "result": f"需求条目化失败：{type(exc).__name__}: {exc}。"
                          f"⚠️ 这不代表原文有问题 —— 请如实说明『未能条目化』。"}

    if not items and gaps:
        return {"ok": True,
                "result": ("【需求条目化】未能提取任何可验证条目，"
                           f"{len(gaps)} 句全部待澄清：\n"
                           + "\n".join(f"  · {g['text'][:46]} —— {g['reason']}"
                                       for g in gaps[:10])
                           + "\n\n⚠️ 不要跳过澄清直接建模 —— 不可验证的需求会产出无法验收的模型。"),
                "items": [], "gaps": gaps, "req_view_code": None,
                "stats": stats}

    return {
        "ok": True,
        "result": _render(items, gaps, code, stats, domain),
        "items": items,
        "gaps": gaps,
        "req_view_code": code,
        "stats": stats,
        "itemize_ratio": round(len(items) / max(1, stats["sentences"]), 3),
    }


def _resolve_project(conn, project: str, conversation_id: int) -> tuple:
    """工程归属：显式参数 > 会话归属 > 空串。返回 `(project_id, note)`。

    ## ⚠️ 2026-11-07 实测踩到：`project` 参数会被模型当成"包名前缀"
    `requirement_itemize` 的 `project` 是**包名**（`package EVTM_Requirements`），
    `requirement_commit` 如果照抄同一个值当"工程 id"，就会把条目挂到一个**不存在的工程**上：
    真机实测模型传了 `project="EVTM"`（规约卡默认包名），于是 3 条条目落到 `project_id='EVTM'`
    —— 又造出一批孤儿归属，且**看起来一切正常**（条目确实入库了）。
    规则：显式值**必须能在 `projects` 里查到**才认，否则回落会话归属并明确告知模型。
    另一条：**不做"猜一个默认工程"的兜底** —— 挂错工程比不挂更难排查
    （追溯链会连到别的项目的设计元素上，而且不报错）。
    """
    p = _as_text(project)
    note = ""
    if p:
        try:
            hit = conn.execute("SELECT 1 FROM projects WHERE id=?", (p,)).fetchone()
        except Exception as exc:                                # noqa: BLE001
            hit = None
            logger.warning("[requirement_commit] 工程校验失败: %s", exc)
        if hit:
            return p, ""
        note = (f"⚠️ 传入的 project=「{p}」不是已存在的工程（它更像包名前缀），"
                f"已忽略，改按会话归属落库。")
    if not conversation_id:
        return "", note
    try:
        row = conn.execute("SELECT project_id FROM conversations WHERE id=?",
                           (int(conversation_id),)).fetchone()
        if row:
            return _as_text(row["project_id"] if hasattr(row, "keys") else row[0]), note
    except Exception as exc:                                    # noqa: BLE001
        logger.warning("[requirement_commit] 会话归属读取失败 conv=%s: %s", conversation_id, exc)
    return "", note


def commit_requirements(args, conversation_id=0, created_by=""):
    """`requirement_commit`：把条目化结果写入需求实体表（写操作，走人工确认）。

    为什么与 `requirement_itemize` 分成两个工具（本仓既有口径：query/apply 分离）：
    条目化的**分析**部分（切句、分类、生成视图骨架）是只读的，每轮都跑；
    而**落库**会产生被追溯链引用的稳定编号（REQ-F001…），是有后果的写。
    合成一个工具只有两种结果：要么只读也要逐条确认（确认疲劳），
    要么写库不确认（绕过写前闸）。两者都不可接受。
    """
    from database import get_db
    from repositories.requirement_repo import RequirementRepo

    items = args.get("items")
    gaps = args.get("gaps")
    # 允许"只给原文"——由本工具内部先条目化再落库（免去模型手工搬运 20 条）
    if not items:
        text = args.get("text") or args.get("raw_text")
        if not _as_text(text):
            return {"ok": False,
                    "result": "既没有 items 也没有 text：请传入条目化结果的 items/gaps，"
                              "或传入需求原文由本工具条目化后落库。"}
        try:
            items, gaps, _code, _st = itemize(
                str(text), _as_text(args.get("domain")), _as_text(args.get("source_ref")),
                re.sub(r"[^A-Za-z0-9_]", "", _as_text(args.get("project")) or "EVTM") or "EVTM")
        except Exception as exc:                                # noqa: BLE001
            return {"ok": False,
                    "result": f"条目化失败：{type(exc).__name__}: {exc}。⚠️ 这不代表原文有问题 —— "
                              f"请如实说明『未能条目化』，不要编造条目。"}
    conn = get_db()
    try:
        pid, _pnote = _resolve_project(conn, args.get("project"), conversation_id)
        repo = RequirementRepo(conn)
        # SQLite 单写者：调用方可能正持有一个写事务（编排链路里常见）。
        # 只对**锁冲突**做有限重试（不是对任何异常都重试）——否则"落库失败"会被
        # 伪装成"落库成功"，正是本仓记录过的静默兜底事故。
        _attempt = 0
        while True:
            try:
                res = repo.commit(
                    items or [], gaps or [], project_id=pid,
                    conversation_id=int(conversation_id or 0),
                    version_id=int(args.get("version_id") or 0),
                    source_ref=_as_text(args.get("source_ref")),
                    domain=_as_text(args.get("domain")), created_by=created_by)
                break
            except Exception as _e:                             # noqa: BLE001
                _msg = str(_e).lower()
                if "database is locked" in _msg and _attempt < 3:
                    _attempt += 1
                    time.sleep(0.25 * _attempt)
                    continue
                raise
        st = repo.stats(pid)
        conn.commit()
    except Exception as exc:                                    # noqa: BLE001
        # ⚠️ 不能静默兜底：落库失败若返回"成功"，模型会据此宣称"已入库"，
        #    而实际一条都没有 —— 这正是本仓记录过的"静默丢弃"同型事故。
        return {"ok": False,
                "result": f"需求条目落库失败：{type(exc).__name__}: {exc}。"
                          f"⚠️ 请如实说明『未能入库』，不要声称已入库。"}
    finally:
        try:
            conn.close()
        except Exception:
            pass
    head = (f"【需求入库】工程={pid or '（无工程会话）'}：新增 {res['inserted']} 条 / "
            f"已存在更新 {res['updated']} 条；当前该工程共 "
            f"{st['items']} 条需求、{st['gaps']} 条待澄清（可建模率 {st['itemize_ratio']:.1%}）")
    if _pnote:
        head = _pnote + "\n" + head
    tail = []
    if res["items"]:
        tail.append("需求编号：" + "、".join(res["items"][:12])
                    + ("…" if len(res["items"]) > 12 else ""))
    if res["gaps"]:
        tail.append("待澄清编号：" + "、".join(res["gaps"][:12])
                    + ("…" if len(res["gaps"]) > 12 else ""))
    return {"ok": True, "result": "\n".join([head] + tail),
            "project_id": pid, **res, "stats": st}


def exec_tool(name: str, arguments: dict | None = None, conversation_id: int = 0,
              created_by: str = "") -> dict:
    args = arguments or {}
    if name == "requirement_itemize":
        return _exec(args)
    if name == "requirement_commit":
        return commit_requirements(args, conversation_id=conversation_id, created_by=created_by)
    return {"ok": False, "result": f"未知需求工具: {name}"}


REQ_TOOL_NAMES = ("requirement_itemize", "requirement_commit")


if __name__ == "__main__":
    demo = ("EV 热管理系统需求：系统应支持 30 分钟低温启动，-20℃ 环境下 30 分钟内完成充电与放电准备；"
            "电池工作温度在 15~45℃ 之间；电池温差不超过 5℃；"
            "热管理总能耗不超过整车可用能量的 15%；"
            "热失控预警应至少提前 5 分钟触发保护动作并自动执行；"
            "系统应具有良好的用户体验；热泵 COP 在 -10℃ 时不低于 2.0；"
            "系统应通过 CAN 总线与整车控制器通信。")
    r = _exec({"text": demo, "domain": "thermal_mgmt", "project": "EVTM"})
    print(r["result"])
    print("\n--- 结构化字段 ---")
    for k in ("ok", "itemize_ratio"):
        print(f"  {k} = {r.get(k)}")
    print(f"  items = {len(r.get('items', []))} 条")
    print(f"  gaps  = {len(r.get('gaps', []))} 条")
    print(f"  stats = {r.get('stats')}")
