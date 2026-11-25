# -*- coding: utf-8 -*-
"""需求分析产出的**后置处理**（P0-3 + P0-4，2026-11-10）。

## 为什么需要这个模块（真库实测的两个空洞）
1. **需求分析跑完不留资产**：`requirement_items` 真库 0 行（`sqlite_sequence=8`
   说明历史上插入过、后来被清），而覆盖度工具算的是 2026-09-21 手工补齐的 26 条实体
   ⇒ **需求分析的产出与追溯链完全脱钩**，跨会话不可复用。
2. **质量门禁不在主链路上**：`requirement_quality` 只在用户显式问"需求质量"时才跑；
   需求分析产出的不可验证条目**从不被自动标记**。

## 本模块做什么（确定性、零 LLM）
对一轮需求分析产出的正文，依次做：
  ① `itemize()`   —— 切句 / 分类 / **可验证性闸**（`_verifiability`）/ **验收标准抽取**
  ② `commit()`    —— 幂等落库（`requirement_items`，item 与 gap 分表语义）
  ③ 汇总统计      —— items / gaps / **验收标准覆盖率**，回给调用方做可观测

## 为什么"自动"是安全的（三道约束）
- **只读不进写**：本模块只在**需求类意图**跑完后触发；其它意图零调用。
- **幂等**：`RequirementRepo.commit` 按 `(project_id, fingerprint)` 去重 ⇒
  同一段需求反复跑只会 update，不会堆重复行。
- **可回滚 + 可关**：`requirements.auto_commit=False` 立即退回改动前行为；
  已落库的条目可 `retire()`（留痕不物理删）。

## 口径（不要在这里另立一套判据）
- 可验证性判据唯一真源是 `requirement_itemize_tools._verifiability`；
- 验收标准唯一真源是 `requirement_itemize_tools._acceptance_criteria`；
- 落库唯一真源是 `repositories/requirement_repo.RequirementRepo.commit`。
本模块**只做编排**（决定"什么时候、对哪段文本、用哪个工程"），不重写任何判据 ——
本仓反复踩过"两处各写一份必然漂移"。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("mbse.requirements.postprocess")

__all__ = ["should_process", "process", "PREFERRED_INTENTS"]

#: 哪些意图的产出值得做需求后置处理。
#: `requirement_analysis` 是主目标；`requirement_structuring` 自己就会调 itemize/commit
#: （重复跑是幂等的，但白花一次切句，故默认不含）；`requirement_quality` 是评审不是产出。
PREFERRED_INTENTS = ("requirement_analysis",)

#: 正文最短长度：太短的答复（"已在知识库中找到…"）没有条目化价值，
#: 强行切句只会产出噪声 gap 行，污染 `requirement_items`。
_MIN_LEN = 60


def _conn_alive(conn) -> bool:
    """连接是否还能用（`conn.close()` 之后再执行会抛 ProgrammingError）。

    为什么需要它：主链路的 `with db_conn()` 块在 done 事件之前就退出了，
    后置处理拿到的很可能是**已关闭**的连接。不探活就会把
    "连接已关"伪装成"落库失败"，让人以为是写库逻辑有问题（实测踩过）。
    """
    try:
        conn.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def _cfg_bool(group: str, key: str, default: bool) -> bool:
    try:
        from core import config as _c
        return bool(_c.get(group, key, default))
    except Exception:
        return default


def _intents() -> tuple:
    """允许自动落库的意图集合（配置真源；解析失败回落常量，**不静默放行**）。

    ⚠️ 配置项 `requirements.auto_commit_intents` 必须**真被消费**，否则又是一个
    "登记了但代码不读"的幽灵配置（本仓已因同类问题栽过：开关写了没人读）。
    """
    try:
        from core import config as _c
        raw = str(_c.get("requirements", "auto_commit_intents",
                         ",".join(PREFERRED_INTENTS)) or "")
    except Exception:
        return PREFERRED_INTENTS
    got = tuple(x.strip() for x in raw.split(",") if x.strip())
    return got or PREFERRED_INTENTS


def should_process(intent: str, content: str) -> tuple:
    """是否该对本轮产出做后置处理。返回 `(bool, reason)`。

    ⚠️ 每一个 `False` 分支都要有 reason —— 否则"为什么这轮没落库"无法回答，
    而这类"静默跳过"正是本仓历史上反复出现的问题形态。
    """
    if not _cfg_bool("requirements", "auto_commit", True):
        return False, "开关关闭（requirements.auto_commit=False）"
    it = str(intent or "")
    _allow = _intents()
    if it not in _allow:
        return False, "意图不在自动落库白名单（%s ∉ %s）" % (it or "空", ",".join(_allow))
    txt = str(content or "").strip()
    if len(txt) < _MIN_LEN:
        return False, "正文过短（%d 字符 < %d），条目化无意义" % (len(txt), _MIN_LEN)
    return True, "ok"


def process(conn, content: str, *, intent: str = "", conversation_id: int = 0,
            project: str = "", source_ref: str = "", created_by: str = "") -> dict:
    """对需求分析正文做 条目化 → 落库 → 统计。

    :returns: {ok, skipped, reason, items, gaps, ac_rate, project_id, message}

    纪律：**任何异常都不许冒泡**到主链路（需求后置处理是"增值"，不是"业务前提"），
    但**必须留痕**（`ok=False` + reason），不允许静默。
    """
    ok, reason = should_process(intent, content)
    if not ok:
        return {"ok": False, "skipped": True, "reason": reason,
                "items": 0, "gaps": 0, "ac_rate": 0.0}

    try:
        from services.tools.requirement_itemize_tools import itemize, commit_requirements
    except Exception as exc:                      # noqa: BLE001
        return {"ok": False, "skipped": False,
                "reason": "条目化模块不可用：%s: %s" % (type(exc).__name__, exc)}

    # ★★ 2026-11-10（真机实测修正）：**连接必须可用**。
    #   实测：主链路在 `done` 之前已经 `conn.close()`（`with db_conn()` 块已退出），
    #   传进来的 conn 是**已关闭**的 ⇒ `ProgrammingError: Cannot operate on a closed database`。
    #   而这条错误被下面的兜底转成"落库失败"，于是**真机上永远不落库**（能力装上了但跑不通）。
    #   处理：先探活；不可用就自己开一条短连接（后置处理是独立事务，不与主链路共享）。
    _own_conn2 = False
    if conn is None or not _conn_alive(conn):
        try:
            from database import get_db as _gdb
            conn = _gdb()
            _own_conn2 = True
        except Exception as exc:                  # noqa: BLE001
            return {"ok": False, "skipped": False,
                    "reason": "无法获得数据库连接：%s: %s" % (type(exc).__name__, exc)}

    # ① 条目化（含可验证性闸 + 验收标准抽取，判据都在 itemize 里）
    try:
        items, gaps, _code, st = itemize(content, project=(project or "REQ"))
    except Exception as exc:                      # noqa: BLE001
        return {"ok": False, "skipped": False,
                "reason": "条目化失败：%s: %s" % (type(exc).__name__, exc)}

    if not items and not gaps:
        # 一段可条目化的句子都没有（清洗后为空 / 纯代码 / 纯汇报）
        # ⇒ 本轮没有需求资产可沉淀。语义上属于 skipped（不适用），不是"失败"。
        return {"ok": False, "skipped": True,
                "reason": "清洗后无可条目化内容（正文可能是纯代码/纯汇报/纯澄清话术）",
                "items": 0, "gaps": 0, "ac_rate": 0.0}

    # ★★ 2026-11-10（真机实测修正）：**一条可验证条目都没有 ⇒ 不落库**。
    #   实测场景：Agent 走了澄清流程，正文是"⏸ 暂停等你确认：有 3 处关键口径缺失…
    #   请确认按哪种口径推进？"（169 字，超过 _MIN_LEN=60）。
    #   这种正文会被条目化全部判成 gap（全是问句，无量化指标/无动词）⇒
    #   若照旧落库，`requirement_items` 里会堆满"请确认口径"这类**噪声 gap 行**，
    #   把可建模率这个指标污染成毫无意义（分母全是废话）。
    #   口径：**items=0 时本轮没有需求资产可沉淀**，如实返回 skipped + 原因
    #   （不是静默跳过 —— 原因要能回答"为什么这轮没落库"）。
    if not items:
        return {"ok": False, "skipped": True,
                "reason": "本轮无可验证需求条目（%d 句全部不可验证，疑似澄清/追问轮次）；"
                          "按「没有条目就不落库」不写入资产" % len(gaps),
                "items": 0, "gaps": len(gaps), "ac_rate": 0.0}

    # ★★ 2026-11-10（真机实测第 4 个缺陷）：**工程归属为空 ⇒ 不落库**。
    #   实测 run 990653：会话无工程归属（`conversations.project_id=''`）
    #   ⇒ 139 行落到 `project_id=''` ⇒ 变成**孤儿归属**。
    #   本仓已有 `tools/cleanup_residue.py` 的 `cleanup_orphan_project` 专门清理这类数据
    #   （实测清理过 35 条 sysml_versions + 48 条 conversations）——
    #   说明孤儿归属是已知的老坑，不该由我这边再造一批。
    #   口径：宁可这一轮不落库并如实报告，也不写孤儿数据。
    _pid0 = str(project or "").strip()
    if not _pid0:
        try:
            if int(conversation_id or 0) > 0:
                _r = conn.execute("SELECT project_id FROM conversations WHERE id=?",
                                  (int(conversation_id),)).fetchone()
                if _r:
                    _pid0 = str((_r[0] if not hasattr(_r, "keys") else _r["project_id"]) or "").strip()
        except Exception:
            _pid0 = ""
    if not _pid0:
        if _own_conn2:
            try:
                conn.close()
            except Exception:
                pass
        return {"ok": False, "skipped": True,
                "reason": "会话未归属任何工程（project_id 为空）⇒ 不落库，避免产生孤儿需求资产；"
                          "请先把会话绑定到工程",
                "items": st.get("items", 0), "gaps": st.get("gaps", 0),
                "ac_rate": st.get("ac_rate", 0.0)}
    project = _pid0

    # ② 落库（幂等；工程归属走与 `requirement_commit` 完全相同的解析函数）
    try:
        res = commit_requirements(
            {"items": items, "gaps": gaps, "project": project or "",
             "source_ref": source_ref or ("%s#conv%d" % (intent or "req", int(conversation_id or 0)))},
            conversation_id=int(conversation_id or 0), created_by=created_by or "",
            conn=conn)      # ⚠️ 必须传：否则落库永远落到生产库（门禁曾因此假失败）
    except Exception as exc:                      # noqa: BLE001
        if _own_conn2:
            try:
                conn.close()
            except Exception:
                pass
        return {"ok": False, "skipped": False,
                "reason": "落库失败：%s: %s" % (type(exc).__name__, exc),
                "items": st.get("items", 0), "gaps": st.get("gaps", 0),
                "ac_rate": st.get("ac_rate", 0.0)}

    if not res.get("ok"):
        if _own_conn2:
            try:
                conn.close()
            except Exception:
                pass
        return {"ok": False, "skipped": False,
                "reason": res.get("result", "落库未成功（工具如实返回失败）")[:200],
                "items": st.get("items", 0), "gaps": st.get("gaps", 0),
                "ac_rate": st.get("ac_rate", 0.0)}

    stats = res.get("stats") or {}
    if _own_conn2:
        try:
            conn.close()      # 只关自己开的那条（调用方传入的连接由调用方负责）
        except Exception:
            pass
    return {"ok": True, "skipped": False, "reason": "ok",
            "items": int(stats.get("items") or 0),
            "gaps": int(stats.get("gaps") or 0),
            "ac_filled": int(stats.get("ac_filled") or 0),
            "ac_rate": float(stats.get("ac_rate") or 0.0),
            "project_id": res.get("project_id") or "",
            "inserted": int(res.get("inserted") or 0),
            "updated": int(res.get("updated") or 0),
            "message": (res.get("result") or "")[:400]}


def process_for_run(conn, content: str, *, run_id: int = 0, intent: str = "",
                    conversation_id: int = 0, project: str = "") -> dict:
    """`process()` 的 run 维度包装：把结果落进 `agent_tasks` 级别的可观测字段。

    为什么单独一层：**"落库了没"必须可查**（P0-4 的可观测要求）。
    历史教训：`requirement_items` 从 8 行变 0 行而无人察觉，就是因为
    "本轮到底该不该落、落了没"在数据层没有任何痕迹。
    这里把结果写进 `orch_interrupts` 之外的一处更自然的位置：
    `orch_checkpoints.params_json`（每 run 一行，不新增表）。
    """
    out = process(conn, content, intent=intent, conversation_id=conversation_id,
                  project=project, source_ref=("run#%d" % run_id) if run_id else "")
    if not run_id:
        return out
    try:
        import json as _j
        row = conn.execute("SELECT params_json FROM orch_checkpoints WHERE run_id=?",
                           (int(run_id),)).fetchone()
        if row is None:
            return out
        raw = row[0] if not hasattr(row, "keys") else row["params_json"]
        try:
            params = _j.loads(raw or "{}")
        except Exception:
            params = {}
        params["requirement_postprocess"] = {
            "ok": bool(out.get("ok")), "skipped": bool(out.get("skipped")),
            "reason": str(out.get("reason") or "")[:200],
            "items": int(out.get("items") or 0), "gaps": int(out.get("gaps") or 0),
            "ac_rate": float(out.get("ac_rate") or 0.0),
            "inserted": int(out.get("inserted") or 0),
        }
        conn.execute("UPDATE orch_checkpoints SET params_json=? WHERE run_id=?",
                     (_j.dumps(params, ensure_ascii=False), int(run_id)))
        conn.commit()
    except Exception as exc:                      # noqa: BLE001
        # 观测写入失败不影响业务（但留一行日志，排查"为什么看不到落库结果"时有据可查）
        logger.warning("[req-postprocess] 写 run 观测失败 run=%s: %s", run_id, exc)
    return out
