# -*- coding: utf-8 -*-
"""P0-3（2026-10-28）计划外化的数据访问层 + `todo_write` 工具处理器。

方案：docs/持续自主执行能力-最终方案设计-20261028.md §4.1 / §6 第 3 步。

分层（与既有分工一致）：
  · `agent/agent_todo.py` —— 纯函数层（状态机 / 完成判定 / 障碍分类），零 IO
  · **本文件** —— IO 层（落库 / 读清单）+ 工具 handler

**所有状态迁移必须过 `agent_todo.transition()`**，不在本文件另写一套规则 ——
否则"两处各写一份规则"必然错位（这正是本仓反复踩过的坑）。
"""

from __future__ import annotations

import json
import time

__all__ = [
    "TODO_ENABLED_KEY",
    "todo_tool_def",
    "handle_todo_write",
    "list_todos",
    "load_todo_items",
    "todo_gate_for_conv",
    "upsert_todo",
    "run_todos",
]


def _cfg():
    from core import config as _c
    return _c


# ── 工具定义 ────────────────────────────────────────────────

_TODO_WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "todo_write",
        "description": (
            "管理本会话的**待办清单**（计划外化）。当任务是**多步**（≥3 步）时，"
            "先用它**一次性列全清单**，再逐步推进并结项。\n"
            "action 三选一：\n"
            "· create —— 建待办。**优先用 `items` 批量传**"
            "（[{subject, verify_tool, verify_args, verify}, …]，一次把 4~8 条全列出来）；"
            "只有补建单条时才用 subject+verify。\n"
            "**验收怎么写（2026-11-09 起有硬约束）**：\n"
            "① 能用工具验的：填 `verify_tool`（工具名，必须是池中真实工具）+ `verify_args`（JSON 对象），"
            "例 verify_tool=\"sysml_v2_validate\", verify_args={\"code\": \"…\"}；"
            "verify 再写一句人读的说明即可。\n"
            "② 只有**只读**工具会被真正执行（校验/lint/query/graph_retrieve/file_read 等）；"
            "写类工具（save_artifact / import / requirement_commit / ask_user）**不会**被执行。\n"
            "③ 无法自动验收的写『人工确认』。\n"
            "⚠️ 只写自然语言（如『返回 verdict=pass 或 n_hard=0』）**不会**被执行 —— "
            "会被按人工验收处理，收尾闸看不到真实结果。能自动验的就一定填 verify_tool。\n"
            "· update —— 更新状态。**置 completed 必须同时给 verify_result（验收回执）**"
            "（如『PASS: 0 errors』），否则会被拒绝；置 blocked 必须给 blocked_by 说明卡在哪\n"
            "· list —— 读当前清单（含每条的状态与验收回执）\n"
            "⚠️ 清单不是可选的：完成判定以**待办全部通过验收**为准，"
            "「建议下一步做 X」不算完成 —— 那就建一条 todo 并把它做完。\n"
            "⚠️ 同时至多一条 in_progress。\n"
            "⚠️ 2026-10-28 实测教训：只建**一条**（只列第一步）等于没建 —— "
            "闸门看不到后续步骤，收尾时无法判断「还差什么」。"
            "**收到多步任务的第一件事就是 items 批量建全表**。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "update", "list"],
                    "description": "操作类型：create 建待办 / update 改状态 / list 读清单",
                },
                "items": {
                    "type": "array",
                    "description": "（create 推荐）批量建待办，一次列全所有步骤。每项 {subject, verify}",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string",
                                        "description": "待办标题，具体到对象与动作"},
                            "verify": {"type": "string",
                                       "description": "验收方式（人读说明），必填"},
                            "verify_tool": {"type": "string",
                                            "description": "（推荐）自动验收用的**只读**工具名，如 sysml_v2_validate；非只读工具不会被执行"},
                            "verify_args": {"type": "object",
                                            "description": "（推荐）verify_tool 的 JSON 参数对象，如 {\"code\": \"...\"}；非 JSON 对象不会被使用"},
                            "seq": {"type": "integer",
                                    "description": "（可选）顺序号，从 1 开始"},
                            "note": {"type": "string", "description": "（可选）备注"},
                        },
                        "required": ["subject", "verify"],
                    },
                },
                "id": {
                    "type": "integer",
                    "description": "（update 必填）要更新的待办 id（list 的返回里有）",
                },
                "subject": {
                    "type": "string",
                    "description": "（create 单条时必填）待办标题，具体到对象（如『校验包 P-001 的 SysML v2 语法』）",
                },
                "verify": {
                    "type": "string",
                    "description": "（create 单条时必填）验收方式（人读说明）。例：『sysml_v2_validate 判据 pass』；不可自动验收写『人工确认』",
                },
                "verify_tool": {
                    "type": "string",
                    "description": "（create 单条时推荐）自动验收用的**只读**工具名（如 sysml_v2_validate / sysml_v2_lint / query_projects）",
                },
                "verify_args": {
                    "type": "object",
                    "description": "（create 单条时推荐）verify_tool 的 JSON 参数对象。⚠️ 只写自然语言参数不会被执行",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "blocked", "deleted"],
                    "description": "（update 必填）目标状态",
                },
                "verify_result": {
                    "type": "string",
                    "description": "（update 置 completed 时必填）验收回执，表示验收已通过。例：『PASS: 0 errors』",
                },
                "blocked_by": {
                    "type": "string",
                    "description": "（update 置 blocked 时必填）卡在哪 / 依赖什么",
                },
                "note": {"type": "string", "description": "（可选）备注"},
            },
            "required": ["action"],
        },
    },
}


def todo_tool_def() -> dict:
    """返回 `todo_write` 的工具定义（供注入侧使用）。"""
    return _TODO_WRITE_TOOL


# ── 数据访问 ────────────────────────────────────────────────

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def list_todos(conn, conversation_id: int, include_deleted: bool = False) -> list:
    """读会话待办清单（按 seq、id 排序）。

    :returns: list[dict]，含 id/subject/status/verify/verify_result/blocked_by/note
    """
    if not conversation_id:
        return []
    _cols_ev = ("SELECT id, seq, subject, status, verify, verify_result, blocked_by, note, "
                "verify_tool, verify_args, COALESCE(evidence,'') "
                "FROM agent_todos WHERE conversation_id=?")
    _cols_legacy = ("SELECT id, seq, subject, status, verify, verify_result, blocked_by, note, "
                    "verify_tool, verify_args "
                    "FROM agent_todos WHERE conversation_id=?")
    _tail = ("" if include_deleted else " AND status != 'deleted'") \
        + " ORDER BY COALESCE(seq, id), id"
    params = [int(conversation_id)]
    rows, has_ev = [], True
    try:
        rows = conn.execute(_cols_ev + _tail, params).fetchall()
    except Exception:
        # 未跑迁移的库（门禁脚本自建测试库等）没有 evidence 列 → 静默回退旧查询，
        # 但绝不因此把清单返回成空（"表没建"与"没有待办"症状不同，不能让调用方混淆）
        try:
            rows = conn.execute(_cols_legacy + _tail, params).fetchall()
            has_ev = False
        except Exception:
            return []
    out = []
    for r in rows:
        # ⚠️ sqlite3.Row 没有 .get() —— 用位置索引，别用 .get()
        _evd = {}
        if has_ev and r[10]:
            try:
                _evd = json.loads(r[10]) or {}
            except Exception:
                _evd = {}
        out.append({
            "id": r[0], "seq": r[1], "subject": r[2], "status": r[3],
            "verify": r[4] or "", "verify_result": r[5] or "",
            "blocked_by": r[6] or "", "note": r[7] or "",
            "verify_tool": r[8] or "", "verify_args": r[9] or "",
            "evidence": _evd,
        })
    return out


def run_todos(conn, run_id: int) -> list:
    """读**一个编排批次**（run 维度）建过的全部待办。

    为什么按 run 而不按 conversation：编排批次里**所有子任务共享同一个
    编排批次号**（`stream.py` 的 run_id，子任务不覆盖父值），待办全部落在
    父会话下 ⇒ 会话维度会把「上一轮/上一批」的待办也混进来，无法回答
    「**这一批**建了多少、完成了多少」。这也是编排收口「如实告知」
    （`AutoPilotMixin.build_orch_todo_notice`）的数据源。

    :returns: list[dict]（含 subject/status/verify/verify_tool/verify_args）；
              run_id 为 0 或表异常时返回 []（调用方按"无待办"处理）。
    """
    if not run_id:
        return []
    try:
        rows = conn.execute(
            "SELECT id, subject, status, verify, verify_tool, verify_args "
            "FROM agent_todos WHERE run_id=? AND status != 'deleted' "
            "ORDER BY COALESCE(seq, id), id", (int(run_id),)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        out.append({
            "id": r[0], "subject": r[1] or "", "status": r[2] or "",
            "verify": r[3] or "", "verify_tool": r[4] or "", "verify_args": r[5] or "",
        })
    return out


def load_todo_items(conn, conversation_id: int, include_deleted: bool = False):
    """读清单并转成 `agent_todo.TodoItem`（供 `todo_gate()` 消费）。"""
    from agent import agent_todo as _at
    rows = list_todos(conn, conversation_id, include_deleted)
    return [_at.TodoItem(subject=r["subject"], status=r["status"],
                         verify=r["verify"], verify_result=r["verify_result"],
                         blocked_by=r["blocked_by"], note=r["note"])            for r in rows]


def todo_gate_for_conv(conn, conversation_id: int) -> dict:
    """对本会话跑 `todo_gate()`；无 todo ⇒ 返回 fallback 形态（走旧路径）。

    ⚠️ 关键：**"有 todo 但清单全被删" ≠ "没有 todo"**。
       前者必须判为未完成（否则"删光待办就变成自动完成"成了后门）。
       ⇒ 用 `include_deleted=True` 统计"是否存在过 todo"，
         用 `include_deleted=False` 参与完成判定。
    """
    from agent import agent_todo as _at
    if not conversation_id:
        return {"done": False, "next_step": "", "remaining": [],
                "fallback": True, "reason": "无会话上下文 ⇒ 退回 LLM 评估器"}
    try:
        live = load_todo_items(conn, conversation_id, include_deleted=False)
        ever = load_todo_items(conn, conversation_id, include_deleted=True)
    except Exception:
        return {"done": False, "next_step": "", "remaining": [],
                "fallback": True, "reason": "读待办失败 ⇒ 退回 LLM 评估器"}
    if not ever:
        return _at.todo_gate(live, has_todos=False)
    return _at.todo_gate(live, has_todos=True)


def machine_evidence(conn, conversation_id: int, verify_tool, run_id: int = 0) -> dict:
    """给「置 completed」找一条**机器回执**。零执行：只查 `tool_call_logs`，不调用任何工具。

    三种结果（写进 agent_todos.evidence）：
      · machine —— 找到该工具在本会话的**成功真实调用**（call_kind='tool'，ok=1）
      · claim   —— 没有 verify_tool（纯自然语言验收），或找到了工具名但没找到调用记录
      · unknown —— 查询本身失败（表缺失/连接异常），**不当作 claim**，避免把环境问题
                   误记成模型撒谎（否则"取不到"与"本就没有"症状相同，无法排查）

    为什么排除 call_kind='skill'：实测 conv 990541 的 01:49 那条 `SysML v2 校验与修复`
    是 **skill 注入**（result 明写"未独立执行"），把它当回执等于用"看了说明书"证明"做过了"。

    ★ 2026-11-10（run 维度下沉到判据层）：新增 `run_id` 参数。
      下沉前判据只有会话维度 ⇒ **上一批编排里跑过的同一个工具，能给这一批的待办背书**：
      同一会话第二轮没有真调 `sysml_v2_validate`，但第一轮调过，
      `ORDER BY id DESC LIMIT 1` 会捞到第一轮那条 ⇒ 判成 `machine`（假回执）。
      这正是"跑在真库上才显形"的一类错：夹具单批次时永远绿。
      口径：
        · `run_id > 0` ⇒ 只认**本批次**的回执（严格）；
        · `run_id == 0` ⇒ 维持旧行为（按会话查）—— 单 Agent 直行路径没有批次号，
          这里不能因为"拿不到 run"就把所有回执判成 claim（那会把正常路径变成全员说谎）。
    """
    vt = str(verify_tool or "").strip()
    if not vt:
        return {"kind": "claim", "reason": "无 verify_tool（纯自然语言验收，无法机器核验）"}
    try:
        _rid = int(run_id or 0)
    except (TypeError, ValueError):
        _rid = 0
    try:
        if _rid > 0:
            row = conn.execute(
                "SELECT id, tool_name, created_at FROM tool_call_logs "
                "WHERE conversation_id=? AND tool_name=? AND ok=1 "
                "AND COALESCE(call_kind,'tool')='tool' AND run_id=? "
                "ORDER BY id DESC LIMIT 1",
                (int(conversation_id or 0), vt, _rid)).fetchone()
        else:
            row = conn.execute(
                "SELECT id, tool_name, created_at FROM tool_call_logs "
                "WHERE conversation_id=? AND tool_name=? AND ok=1 "
                "AND COALESCE(call_kind,'tool')='tool' "
                "ORDER BY id DESC LIMIT 1",
                (int(conversation_id or 0), vt)).fetchone()
    except Exception as e:
        return {"kind": "unknown", "reason": "查询回执失败：%r" % (e,)}
    if not row:
        _scope = ("本批次（run=%d）" % _rid) if _rid > 0 else "本会话"
        return {"kind": "claim", "reason": "%s未找到 %s 的成功调用记录" % (_scope, vt),
                "run_id": _rid}
    return {"kind": "machine", "tool": vt, "run_id": _rid,
            "log_id": int(row[0]), "at": str(row[2] or "")}


def upsert_todo(conn, conversation_id: int, *, subject=None, verify=None,
                seq=None, note=None, verify_tool=None, verify_args=None,
                run_id: int = 0) -> dict:
    """建一条待办（create）。返回 {ok, id, result}。

    `verify_tool` / `verify_args`（2026-11-09）：结构化验收。
      · 落库时 args 统一序列化成 JSON 字符串（NULL/空 ⇒ ''）
      · **不在这里校验白名单** —— 判定统一由 `harness_gate.parse_verify_spec` 做，
        本文件再写一份就会出现"两处规则各写一份"（本仓反复踩过的坑）。
    """
    from agent import agent_todo as _at
    subject = str(subject or "").strip()
    if not subject:
        return {"ok": False, "result": "todo_write create 缺少 subject（待办标题）"}
    if not conversation_id:
        return {"ok": False, "result": "todo_write 无会话上下文，无法建待办"}
    verify = str(verify or "").strip()
    if not verify:
        return {"ok": False,
                "result": "todo_write create 缺少 verify（验收方式）——"
                          "每条待办必须写清楚『怎么算做完了』；"
                          "不可自动验收的写『人工确认』"}
    # seq：同会话内递增（取 max+1），保证"第一个 pending"有确定序
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM agent_todos WHERE conversation_id=?",
            (int(conversation_id),)).fetchone()
        auto_seq = int(row[0]) + 1 if row else 1
    except Exception:
        auto_seq = 1
    use_seq = int(seq) if seq not in (None, "") else auto_seq
    _vt = str(verify_tool or "").strip()
    _va = ""
    if verify_args not in (None, ""):
        _va = verify_args if isinstance(verify_args, str) else json.dumps(
            verify_args, ensure_ascii=False)
    if _vt and not _va:
        _va = "{}"          # 有工具名无参数 ⇒ 显式空对象（区别于"没填"）
    try:
        cur = conn.execute(
            "INSERT INTO agent_todos (conversation_id, run_id, seq, subject, status, "
            "verify, verify_tool, verify_args, verify_result, blocked_by, note, "
            "created_at, updated_at) "
            "VALUES (?,?,?,?,'pending',?,?,?,'','',?,?,?)",
            (int(conversation_id), int(run_id or 0), use_seq, subject, verify, _vt, _va,
             str(note or ""), _now(), _now()))
        conn.commit()
        _tail = ("；自动验收：%s" % _vt) if _vt else ""
        return {"ok": True, "id": int(cur.lastrowid),
                "result": "已建待办 #%d「%s」（验收方式：%s%s）"
                          % (int(cur.lastrowid), subject[:60], verify[:60], _tail)}
    except Exception as e:
        return {"ok": False, "result": "建待办失败：%r" % (e,)}


# ── 工具 handler ────────────────────────────────────────────

def handle_todo_write(arguments: dict, conversation_id: int, conn=None,
                      run_id: int = 0, task_key: str = "") -> dict:
    """`todo_write` 工具 handler。返回 {ok, result, ...}。

    状态迁移**一律走** `agent_todo.transition()` —— 不在本文件另写规则。

    `run_id` / `task_key`（2026-11-10，run 维度下沉）：调用方传编排批次号与子任务键，
    用于 ① 待办行落 `agent_todos.run_id`（此前恒 0 —— 列在、判据拿不到）；
    ② `machine_evidence` 只认**本批次**的回执（防上一轮的工具调用给这一轮的待办背书）。
    默认 0 = 无批次上下文（单 Agent 直行），行为与下沉前一致。

    :param conn: 可选的数据库连接。**默认 None ⇒ 用生产 `db_conn()`**。
                 显式传入 ⇒ 用给定连接（门禁脚本用独立测试库，不碰生产库）。
                 ⚠️ 这一参数不是"为测试而设"：它让本函数**可被独立驱动**，
                   也避免"生产库尚未建表时工具直接报 no such table"这类
                   无法区分「表没建」与「参数错」的失败。
    """
    from agent import agent_todo as _at

    a = arguments or {}
    action = str(a.get("action") or "").strip().lower()
    if action not in ("create", "update", "list"):
        return {"ok": False, "result": "todo_write 的 action 必须是 create / update / list"}

    def _run(c):
        if action == "list":
            rows = list_todos(c, conversation_id)
            if not rows:
                return {"ok": True, "result": "当前会话没有待办清单（空）",
                        "todos": []}
            lines = []
            for r in rows:
                # ★ 2026-11-09：把「会不会被自动验收」直接告诉模型 ——
                #   否则它写了自然语言以为能自动验，收尾时被当人工验收放行，
                #   双方认知不一致（生产上正是这样漏拦的）。
                _auto = "（自动验收：%s）" % r["verify_tool"] if r.get("verify_tool") else "（无自动验收 ⇒ 按人工放行）"
                # 2026-11-09：completed 的行标注"有没有机器回执"。模型据此知道
                # 「自己说完成」不等于「系统认了完成」，而不是下次继续自述。
                _evd = r.get("evidence") or {}
                if r.get("status") == "completed" and _evd.get("kind") == "machine":
                    _evtag = "｜✅机器回执 #%s（%s）" % (_evd.get("log_id"), _evd.get("tool"))
                elif r.get("status") == "completed" and _evd.get("kind") == "claim":
                    _evtag = "｜⚠️仅声称完成（无机器回执）"
                else:
                    _evtag = ""
                lines.append(
                    "#%d [%s] %s｜验收：%s｜%s%s｜回执：%s"
                    % (r["id"], r["status"], r["subject"],
                       r["verify"] or "（未填）", _auto, _evtag, r["verify_result"] or "（无）"))
            return {"ok": True, "result": "当前待办清单：\n" + "\n".join(lines),
                    "todos": rows}

        if action == "create":
            # ★ 2026-10-28（conv 9732 实测）：支持 `items` **批量建**。
            #   真机现象：模型只建了 1 条（"定位工程三元组"）就继续干活，
            #   清单里永远没有"校验""写入"两条 ⇒ 收尾时闸门看不出还差什么。
            #   根因是旧 schema 只能逐条 create —— 建全表要调 4~8 次工具，
            #   模型自然偷懒。批量形态把这一步降到 1 次调用。
            _items = a.get("items")
            if isinstance(_items, list) and _items:
                _made, _errs = [], []
                for _i, _it in enumerate(_items, 1):
                    if not isinstance(_it, dict):
                        _errs.append("第 %d 项不是对象" % _i)
                        continue
                    _r = upsert_todo(c, conversation_id,
                                     subject=_it.get("subject"),
                                     verify=_it.get("verify"),
                                     seq=_it.get("seq") or _i,
                                     note=_it.get("note"),
                                     verify_tool=_it.get("verify_tool"),
                                     verify_args=_it.get("verify_args"),
                                     run_id=run_id)
                    if _r.get("ok"):
                        _made.append(_r)
                    else:
                        _errs.append("第 %d 项：%s" % (_i, _r.get("result")))
                if not _made:
                    return {"ok": False,
                            "result": "todo_write create（批量）全部失败：" + "；".join(_errs)}
                _lines = ["已建 %d 条待办（清单如下，后续用 update 逐条结项）：" % len(_made)]
                for _r in _made:
                    _lines.append("  #%d %s" % (_r["id"], _r["result"]))
                if _errs:
                    _lines.append("⚠️ 有 %d 项未建成：%s" % (len(_errs), "；".join(_errs)))
                _lines.append("下一步：挑第一条置 in_progress 并开始做；"
                              "**每条完成后必须带 verify_result 才能置 completed**。")
                return {"ok": True, "result": "\n".join(_lines),
                        "ids": [_r["id"] for _r in _made],
                        "todos": list_todos(c, conversation_id)}
            return upsert_todo(c, conversation_id,
                               subject=a.get("subject"), verify=a.get("verify"),
                               seq=a.get("seq"), note=a.get("note"),
                               verify_tool=a.get("verify_tool"),
                               verify_args=a.get("verify_args"), run_id=run_id)

        # ── update ──
        tid = a.get("id")
        try:
            tid = int(tid)
        except Exception:
            return {"ok": False,
                    "result": "todo_write update 缺少有效的 id（先用 action=list 查）"}
        row = c.execute(
            "SELECT id, subject, status, verify, verify_result, blocked_by, note, "
            "COALESCE(verify_tool,'') FROM agent_todos WHERE id=? AND conversation_id=?",
            (tid, int(conversation_id or 0))).fetchone()
        if not row:
            # ★ 2026-11-19（真库归因：8 条「待办 #N 不存在」真实失败）：
            #   模型常**编造/记错**待办编号（#1 / #5 / #85 / #600 等），旧文案只说"不存在"，
            #   模型于是继续猜下一个编号（与 `tool_result_fetch` 的 offload 编号同一形态）。
            #   修法：**把本会话确实存在的 id 列出来**，让它一次纠正到位。
            try:
                _avail = [r[0] for r in c.execute(
                    "SELECT id FROM agent_todos WHERE conversation_id=? "
                    "AND status NOT IN ('deleted') ORDER BY id", (int(conversation_id or 0),)).fetchall()]
            except Exception:
                _avail = []
            _hint = ("本会话现有待办 id：%s" % _avail) if _avail else \
                "本会话当前**没有任何待办**——先用 action=add 建，再 update"
            return {"ok": False,
                    "result": ("todo_write update：待办 #%d 不存在（或不属于本会话）。%s。"
                               "不要按顺序猜编号；id 只能取自 action=list 或 add 的返回"
                               % (tid, _hint)),
                    "available_ids": _avail}
        item = _at.TodoItem(subject=row[1], status=row[2], verify=row[3],
                            verify_result=row[4], blocked_by=row[5], note=row[6])
        to_status = str(a.get("status") or "").strip().lower()
        if not to_status:
            return {"ok": False, "result": "todo_write update 缺少 status（目标状态）"}
        try:
            _at.transition(item, to_status,
                           verify_result=a.get("verify_result"),
                           blocked_by=a.get("blocked_by"),
                           note=a.get("note"))
        except _at.TransitionError as e:
            # 非法迁移**如实报错**（不静默接受）——模型要知道自己写错了
            return {"ok": False, "result": "todo_write update 被拒：%s" % e}
        try:
            # 2026-11-09：置 completed 时同步核验机器回执（零执行，只查日志）。
            #   不阻断 status 迁移 —— 现在处于 shadow 阶段：先让「声称 vs 有回执」
            #   在数据里可见，再由 harness 决定是否把 claim 当未完成。
            _ev = ""
            if item.status == "completed":
                _evd = machine_evidence(c, conversation_id, row[7], run_id=run_id)
                _ev = json.dumps(_evd, ensure_ascii=False)
            # 未跑迁移的库（门禁脚本自建测试库）没有 evidence 列。
            # ⚠️ 这里必须**先探测再分支**：直接写死列名会让测试库抛
            #    `no such column: evidence`，把「表结构未迁移」伪装成「待办更新失败」——
            #    首跑 verify_todo_gate_p01 就是这么连红 5 条的。
            _has_ev = True
            try:
                _has_ev = "evidence" in [
                    _c[1] for _c in c.execute("PRAGMA table_info(agent_todos)").fetchall()]
            except Exception:
                _has_ev = False
            if _has_ev:
                c.execute(
                    "UPDATE agent_todos SET status=?, verify=?, verify_result=?, "
                    "blocked_by=?, note=?, evidence=COALESCE(NULLIF(?,''),evidence), "
                    "updated_at=? WHERE id=? AND conversation_id=?",
                    (item.status, item.verify, item.verify_result,
                     item.blocked_by, item.note, _ev, _now(), tid, int(conversation_id or 0)))
            else:
                c.execute(
                    "UPDATE agent_todos SET status=?, verify=?, verify_result=?, "
                    "blocked_by=?, note=?, updated_at=? WHERE id=? AND conversation_id=?",
                    (item.status, item.verify, item.verify_result,
                     item.blocked_by, item.note, _now(), tid, int(conversation_id or 0)))
            c.commit()
        except Exception as e:
            return {"ok": False, "result": "更新待办失败：%r" % (e,)}
        if item.status == "completed":
            try:
                _d = json.loads(_ev) if _ev else {}
            except Exception:
                _d = {}
            if _d.get("kind") == "machine":
                _evnote = "；机器回执 #%s（%s）" % (_d.get("log_id"), _d.get("tool"))
            elif _d.get("kind") == "claim":
                _evnote = "；⚠️ 仅声称完成（%s）" % (_d.get("reason") or "")
            else:
                _evnote = "；回执核验不可用"
        else:
            _evnote = ""
        return {"ok": True,
                "result": "待办 #%d「%s」→ %s%s%s"
                          % (tid, item.subject[:50], item.status,
                             ("（回执：%s）" % item.verify_result[:40])
                             if item.verify_result else "", _evnote)}

    try:
        if conn is not None:
            return _run(conn)
        from database import db_conn
        with db_conn() as c:
            return _run(c)
    except Exception as e:
        return {"ok": False, "result": "todo_write 执行异常：%r" % (e,)}
