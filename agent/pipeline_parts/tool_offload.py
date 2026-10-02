# -*- coding: utf-8 -*-
"""P1-4 工具结果 offload（Tier1：大响应落库 + 引用 + 按需重读）—— 2026-10-02。

评估报告 §2.1 P1：此前模型侧工具结果 `_TOOL_MODEL_CAP=3000` **纯截断，剪掉即丢失**；
且 execute.py 工具循环**根本没有模型侧封顶**（全量回填，长结果被每轮重发）。

本模块提供单一真源 `model_side_content`：>cap 的工具结果全文落库 `tool_result_offloads` 表，
回填给模型的换成**引用块**（头部摘要 + offload id + 重读指引），模型可调用
`tool_result_fetch`（进程内直通，读类无副作用）按 id 取回全文——可寻址召回，非破坏性。

纪律：
· stream / execute 两条路径共用本 helper（此前各写一份截断逻辑 → execute 漏封顶正是漂移后果）；
· offload 自身故障不阻断主链路（回退为旧式截断 + 留痕）；
· 弱相关 hint 等路径特有叠加由调用方负责，本模块只管 result 字段的封顶/offload。
"""
import json

#: 引用块头部摘要长度（字符）——足够模型判断"值不值得重读"
_HEAD_CHARS = 600


def save_offload(tool_name: str, content: str, conversation_id: int = 0) -> int:
    """全文落库，返回 offload id。失败抛异常（调用方回退截断）。"""
    from database import db_conn
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tool_result_offloads (conversation_id, tool_name, content) VALUES (?,?,?)",
            (int(conversation_id or 0), tool_name or "", content))
        conn.commit()
        return int(cur.lastrowid)


def fetch_offload(offload_id: int) -> str:
    """按 id 取回全文；不存在返回空串。"""
    if not offload_id or int(offload_id) <= 0:
        return ""
    from database import get_db
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT content FROM tool_result_offloads WHERE id=?", (int(offload_id),)).fetchone()
        return (row["content"] or "") if row else ""
    finally:
        conn.close()


def build_reference_block(offload_id: int, tool_name: str, full_len: int, full_text: str) -> str:
    """引用块：头部摘要（可判断是否值得重读）+ offload 编号 + 重读指引。"""
    head = (full_text or "")[:_HEAD_CHARS]
    return (
        f"[工具结果已 offload #{offload_id}（原 {full_len} 字符，工具 {tool_name}）]\n"
        f"头部摘要：\n{head}\n…\n"
        f"（如需完整内容，请调用工具 tool_result_fetch，参数 {{\"id\": {offload_id}}}）"
    )


def model_side_content(tool_name: str, result: dict, conversation_id: int = 0,
                       cap: int = 3000) -> tuple:
    """构造回填给模型的 tool 消息 content（stream/execute 两路径唯一真源）。

    返回 (content_dict, offloaded_bool)：
    · ≤cap      → 全量（与旧行为一致）
    · >cap      → 全文落库 + 引用块 + 重读指引（offloaded=True，调用方须注入
                  tool_result_fetch 工具定义与白名单）
    · offload 故障 → 回退旧式截断（不阻断主链路）
    """
    content = {"ok": bool(result.get("ok"))}
    full = str(result.get("result") or "")
    if len(full) <= cap:
        content["result"] = full
        return content, False
    try:
        oid = save_offload(tool_name, full, conversation_id)
        content["result"] = build_reference_block(oid, tool_name, len(full), full)
        content["truncated"] = True
        content["note"] = (f"工具结果过长（原 {len(full)} 字符）已 offload 为 #{oid}；"
                           f"如需完整内容请调用 tool_result_fetch（参数 id={oid}）")
        return content, True
    except Exception:
        content["result"] = full[:cap]
        content["truncated"] = True
        content["note"] = "工具结果过长已截断，请基于以上内容作答"
        return content, False


#: tool_result_fetch 的 OpenAI function 定义（offload 发生时动态注入 tools_def）
TOOL_FETCH_DEF = {
    "type": "function",
    "function": {
        "name": "tool_result_fetch",
        "description": "读取此前被 offload 的完整工具结果（当某条工具结果提示已 offload 且需要全文时使用）",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "offload 编号（见结果提示）"}},
            "required": ["id"],
        },
    },
}


def ensure_fetch_tool(tools_def: list, whitelist=None) -> None:
    """offload 发生后调用：把 tool_result_fetch 注入本轮 tools_def（幂等）并放行白名单。

    · tools_def 为 None/空（纯问答）时跳过注入但**仍放行白名单**——引用块文本已引导模型调用；
    · whitelist 为 None 表示未启用最小权限（无需放行动作）。
    """
    if whitelist and "tool_result_fetch" not in whitelist:
        whitelist.append("tool_result_fetch")
    if tools_def and not any(
            (t.get("function") or {}).get("name") == "tool_result_fetch" for t in tools_def):
        tools_def.append(json.loads(json.dumps(TOOL_FETCH_DEF)))  # 深拷贝防调用方复用污染


def cleanup_expired(days: int = 30) -> int:
    """TTL 清理：删 created_at 早于 now-days 的 offload 行（main.py lifespan 启动钩子搭车调用）。

    ⚠️ SQLite 时间修饰符**不能带空格**：`'-30 days'` ✓ / `'- 30 days'` ✗（返回 NULL，
    2026-09-30 在 MEMORY 登记过同族坑——counts 恒 0 被误读成"没调用"）。
    """
    from database import db_conn
    with db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM tool_result_offloads WHERE created_at < datetime('now', ?)",
            (f"-{int(days)} days",))
        conn.commit()
        return cur.rowcount


def cleanup_conversation(conversation_id: int) -> int:
    """会话删除级联：清该会话的全部 offload（routers/conversations.py delete 端点调用）。"""
    from database import db_conn
    with db_conn() as conn:
        cur = conn.execute(
            "DELETE FROM tool_result_offloads WHERE conversation_id=?", (int(conversation_id),))
        conn.commit()
        return cur.rowcount
