# -*- coding: utf-8 -*-
"""save_artifact —— AI 显式归档产物（本地工具，2026-10-17 米爸能力配置需求）。

## 为什么需要（9446 会话取证）

「把代码存入产物文档」的既有通道只有两条，都够不着：
1. harness 兜底（extract_sysml_doc_from_result）：只认**完整文件形态**（JSON 信封
   fileName+.sysml+eof / 裸 package≥500）——AI 按结构分段展示、或取回的是片段时
   判据不命中；
2. 正文代码块判据：AI 正文不给全量代码块（分析性回答）时不命中。
file_write 写的是**沙箱磁盘**，产物库（artifacts 表）根本进不去——AI 说的
「已写入产物文档」只能是空话。

⇒ 给 AI 一个显式通道：调用 save_artifact 把任意内容登记为产物。这是 A2 能力
配置路线的可配工具：技能 48「文件操作」声明它、agent 绑定技能后注入白名单。

## 与 harness 兜底的关系

互补不冲突：兜底是平台承诺（工具结果里的完整交付物**必**入库，不依赖模型自觉）；
save_artifact 是模型主动通道（用户点名要求保存、或模型判断某段内容值得归档时调用）。
同文重复保存由 exists 幂等判据挡住。
"""
import json

__all__ = ["exec_tool", "SAVE_ARTIFACT_TOOL_NAME"]

SAVE_ARTIFACT_TOOL_NAME = "save_artifact"

#: kind 词表（与产物面板 _ART_ICONS / 预览路由对齐；越界值回落 document）
_KINDS = ("code", "document", "sysml", "report")

_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "产物标题（建议带扩展名，如 广汽元模型_master.sysml）"},
        "content": {"type": "string", "description": "产物全文内容（要保存的代码/文档正文）"},
        "kind": {"type": "string", "enum": list(_KINDS),
                 "description": "产物类型：code=代码文件（默认）、document=文档、sysml=SysML视图、report=报告"},
        "lang": {"type": "string", "description": "代码语言（可选，如 sysml/kerml/python）"},
    },
    "required": ["title", "content"],
}


def _norm_kind(kind) -> str:
    k = str(kind or "").strip().lower()
    # 空值默认 code（schema 同口径）；越界值才归 document
    if not k:
        return "code"
    return k if k in _KINDS else "document"


def save_artifact(args: dict, conversation_id: int = 0) -> dict:
    """登记产物。幂等：同 (会话, kind, title) 已存在则不重复插。异常不抛出。"""
    title = str(args.get("title") or "").strip()
    content = str(args.get("content") or "")
    if not title:
        return {"ok": False, "result": "保存失败：title 不能为空"}
    if not content.strip():
        return {"ok": False, "result": "保存失败：content 不能为空（要保存的产物全文）"}
    if not conversation_id:
        return {"ok": False, "result": "保存失败：缺少会话上下文（conversation_id=0）"}
    kind = _norm_kind(args.get("kind"))
    # content 里的 ``` 围栏剥掉：模型常把整块代码带围栏贴进来，产物预览不需要
    body = content.strip()
    if body.startswith("```"):
        _lines = body.splitlines()
        if len(_lines) >= 2 and _lines[-1].strip() == "```":
            body = "\n".join(_lines[1:-1]).strip()
    try:
        from repositories.artifact_repo import ArtifactRepo
        from database import get_db
        conn = get_db()
        repo = ArtifactRepo(conn)
        try:
            # message_id=0：工具执行时本轮 assistant 消息尚未落库；
            # 0 同时让幂等判据跨轮生效（同会话同型同标题不重复登记）
            if repo.exists(int(conversation_id), 0, kind, title):
                return {"ok": True,
                        "result": f"产物「{title}」此前已保存过（同会话同类型同标题），无需重复保存。"}
            lang = str(args.get("lang") or "").strip()
            meta = {"source": "agent_save"}
            if lang:
                meta["lang"] = lang
            aid = repo.create_artifact(
                int(conversation_id), 0, kind, title,
                "code" if kind in ("code", "sysml") else "text", body, meta,
                source="conversation", created_by="AI")
            conn.commit()
            return {"ok": True,
                    "result": (f"已存入产物库：{title}（{kind}，{len(body)} 字符，产物 id={aid}）。"
                               f"用户可在产物面板查看或下载，无需再在回答里复述全文。")}
        finally:
            conn.close()
    except Exception as e:    # noqa: BLE001
        return {"ok": False, "result": f"保存产物失败: {type(e).__name__}: {str(e)[:200]}"}


def exec_tool(name: str, arguments: dict | None = None,
              conversation_id: int = 0) -> dict:
    """统一入口（对齐 file_tools.exec_file_tool 风格）：异常不抛出，返回 {"ok", "result"}。"""
    if name != SAVE_ARTIFACT_TOOL_NAME:
        return {"ok": False, "result": f"未知产物工具: {name}"}
    try:
        return save_artifact(arguments or {}, conversation_id=conversation_id)
    except Exception as e:    # noqa: BLE001
        return {"ok": False, "result": f"产物工具执行失败: {str(e)[:200]}"}


def input_schema() -> str:
    """tools 表 input_schema 字段值。"""
    return json.dumps(_INPUT_SCHEMA, ensure_ascii=False)
