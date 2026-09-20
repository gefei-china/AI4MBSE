"""外部 HTTP 集成工具注册脚本（幂等，可重复执行）。

把「外部系统接口」注册为系统工具（tools.kind='http'），运行时由通用执行器
http_tool_executor 按 config 调用 —— 适配不同建模软件，配置驱动、零代码接入。

注册来源：
1. 内置 ZHIYUAN_TOOL_DEFS：智源 SysMLv2 AI 5 接口（原有工具自动迁移 kind/config）
2. integrations/*.json：外部系统接口定义文件（每个文件为 {tool_name: 定义} 或 [定义]）
   定义字段：name / description / input_schema / side_effect / risk_level / config / bind_to

用法：
    python register_http_tools.py
"""
import glob
import json
import os
import sqlite3

from core.config import DB_PATH

# ── 智源 SysMLv2 AI 工具定义（v1.2 接口清单；config 用占位符引用系统配置）──
ZHIYUAN_TOOL_DEFS = [
    {
        "name": "zhiyuan_sysmlv2_gen",
        "source": "zhiyuan",
        "description": "从 MBSE 平台建模数据生成 SysMLv2 源码文本（智源 /api/project/ai/sysmlv2/gen）。packageDataId 为空时按整个工程生成，非空时按指定包生成",
        "side_effect": "read", "risk_level": "low", "version": "v1.2",
        "input_schema": {"type": "object", "properties": {
            "vc": {"type": "string", "description": "版本控制上下文，格式 branchId,queryType[,versionNumber]，如 1,0（草稿）/ 100,1,5（已发布 v5）"},
            "packageDataId": {"type": "integer", "description": "目标包 dataId；为空时按整个工程生成（可选）"}},
            "required": ["vc"]},
        "config": {
            "method": "POST",
            "url": "{cfg:zhiyuan.base_url}/api/project/ai/sysmlv2/gen",
            "headers": {"Authorization": "Bearer {cfg:zhiyuan.token}"},
            "body": {"vc": "{args:vc}", "packageDataId": "{args:packageDataId,int,optional}"},
            "data_path": "data", "timeout": "{cfg:zhiyuan.timeout}", "max_response": "{cfg:zhiyuan.max_response}",
            "defaults": {"vc": "{cfg:zhiyuan.default_vc}"},
        },
    },
    {
        "name": "zhiyuan_sysmlv2_check",
        "source": "zhiyuan",
        "description": "检测 SysMLv2 文本语法（智源 /api/project/ai/sysmlv2/check，OMG SysMLInteractive 解析器）。返回 valid 与结构化错误列表（行列/偏移/消息）",
        "side_effect": "read", "risk_level": "low", "version": "v1.2",
        "input_schema": {"type": "object", "properties": {
            "vc": {"type": "string", "description": "版本控制上下文，格式 branchId,queryType[,versionNumber]，如 1,0"},
            "targetPackageDataId": {"type": "integer", "description": "目标包 dataId（保留并替换其内容）"},
            "text": {"type": "string", "description": "待检测的 SysMLv2 文本"}},
            "required": ["vc", "targetPackageDataId", "text"]},
        "config": {
            "method": "POST",
            "url": "{cfg:zhiyuan.base_url}/api/project/ai/sysmlv2/check",
            "headers": {"Authorization": "Bearer {cfg:zhiyuan.token}"},
            "body": {"vc": "{args:vc}", "targetPackageDataId": "{args:targetPackageDataId,int}", "text": "{args:text}"},
            "data_path": "data", "timeout": "{cfg:zhiyuan.timeout}", "max_response": "{cfg:zhiyuan.max_response}",
            "defaults": {"vc": "{cfg:zhiyuan.default_vc}"},
        },
    },
    {
        "name": "zhiyuan_sysmlv2_import",
        "source": "zhiyuan",
        "description": "覆盖导入 SysMLv2 文本到 MBSE 平台（智源 /api/project/ai/sysmlv2/import）。保留目标包及其 dataId，删除其子树后写入新顶层元素；仅支持草稿上下文（vc queryType=0），需分支 PROJECT_EDIT 权限",
        "side_effect": "write", "risk_level": "medium", "version": "v1.2",
        "input_schema": {"type": "object", "properties": {
            "vc": {"type": "string", "description": "版本控制上下文，必须为草稿上下文，格式 branchId,0"},
            "text": {"type": "string", "description": "SysMLv2 文本（UTF-8，换行符 \\n）"},
            "targetPackageDataId": {"type": "integer", "description": "目标包 dataId；为空时使用当前工程根包（可选）"}},
            "required": ["vc", "text"]},
        "config": {
            "method": "POST",
            "url": "{cfg:zhiyuan.base_url}/api/project/ai/sysmlv2/import",
            "headers": {"Authorization": "Bearer {cfg:zhiyuan.token}"},
            "body": {"vc": "{args:vc}", "text": "{args:text}", "targetPackageDataId": "{args:targetPackageDataId,int,optional}"},
            "data_path": "data", "timeout": "{cfg:zhiyuan.timeout}", "max_response": "{cfg:zhiyuan.max_response}",
            "defaults": {"vc": "{cfg:zhiyuan.default_vc}"},
        },
    },
    {
        "name": "zhiyuan_project_list",
        "source": "zhiyuan",
        "description": "查询 MBSE 平台当前工程列表（智源 /api/project/ai/project/list，含当前工程 + 导入工程）。导入工程返回 vc（branchId,1,version）可直接回填复用",
        "side_effect": "read", "risk_level": "low", "version": "v1.2",
        "input_schema": {"type": "object", "properties": {
            "vc": {"type": "string", "description": "版本控制上下文，格式 branchId,queryType[,versionNumber]，如 1,0"}},
            "required": ["vc"]},
        "config": {
            "method": "POST",
            "url": "{cfg:zhiyuan.base_url}/api/project/ai/project/list",
            "headers": {"Authorization": "Bearer {cfg:zhiyuan.token}"},
            "body": {"vc": "{args:vc}"},
            "data_path": "data", "timeout": "{cfg:zhiyuan.timeout}", "max_response": "{cfg:zhiyuan.max_response}",
            "defaults": {"vc": "{cfg:zhiyuan.default_vc}"},
        },
    },
    {
        "name": "zhiyuan_project_tree",
        "source": "zhiyuan",
        "description": "查询 MBSE 平台当前分支包结构树（智源 /api/project/ai/project/tree）。返回嵌套 AiTreeDTO（dataId/name/qualifiedName/children/isLeaf/projectId）",
        "side_effect": "read", "risk_level": "low", "version": "v1.2",
        "input_schema": {"type": "object", "properties": {
            "vc": {"type": "string", "description": "版本控制上下文，格式 branchId,queryType[,versionNumber]，如 1,0"}},
            "required": ["vc"]},
        "config": {
            "method": "POST",
            "url": "{cfg:zhiyuan.base_url}/api/project/ai/project/tree",
            "headers": {"Authorization": "Bearer {cfg:zhiyuan.token}"},
            "body": {"vc": "{args:vc}"},
            "data_path": "data", "timeout": "{cfg:zhiyuan.timeout}", "max_response": "{cfg:zhiyuan.max_response}",
            "defaults": {"vc": "{cfg:zhiyuan.default_vc}"},
        },
    },
]

# Agent 绑定映射（agent name → 智源工具名列表；外部集成文件可用 bind_to 各自指定）
# 2026-09-11 起 zhiyuan 工具职责收敛到专用主 Agent zhiyuan_mgmt。
# 2026-09-17 C2 决策：register_zhiyuan_agent.py 已删除（它从未接入 seed_all，效果已落库 agents id=275；
#   权威定义留档 docs/_archive/zhiyuan_mgmt-agent-snapshot-20260917.json），此处绑定保持不变。
ZHIYUAN_BINDINGS = {
    "zhiyuan_mgmt": [t["name"] for t in ZHIYUAN_TOOL_DEFS],
}

INTEGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "integrations")


def _norm(entry: dict, default_source: str = "external") -> dict:
    """规范化外部接口定义（补默认字段）。"""
    return {
        "name": entry["name"],
        "source": entry.get("source", default_source),
        "description": entry.get("description", ""),
        "side_effect": entry.get("side_effect", "read"),
        "risk_level": entry.get("risk_level", "low"),
        "version": entry.get("version", "v1.0"),
        "input_schema": entry.get("input_schema", {"type": "object", "properties": {}, "required": []}),
        "config": entry.get("config", {}),
        "bind_to": entry.get("bind_to", []),
    }


def _load_integration_files() -> list:
    """扫描 integrations/*.json 加载外部接口定义（*.example.json 为文档示例，跳过）。"""
    defs = []
    pattern = os.path.join(INTEGRATIONS_DIR, "*.json")
    for path in sorted(glob.glob(pattern)):
        if path.endswith(".example.json"):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[register] ⚠️ 跳过 {os.path.basename(path)}（解析失败: {e}）")
            continue
        items = data.values() if isinstance(data, dict) and "name" not in data else ([data] if isinstance(data, dict) else data)
        for it in items:
            if isinstance(it, dict) and it.get("name") and it.get("config"):
                defs.append(_norm(it))
    return defs


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """幂等确保 tools 表具备 kind/config 列（与 database.init_db 迁移一致，注册脚本自包含）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tools)").fetchall()}
    if "kind" not in cols:
        conn.execute("ALTER TABLE tools ADD COLUMN kind TEXT DEFAULT ''")
        print("[register] 迁移: tools 增加列 kind")
    if "config" not in cols:
        conn.execute("ALTER TABLE tools ADD COLUMN config TEXT DEFAULT '{}'")
        print("[register] 迁移: tools 增加列 config")


def _upsert_tool(conn: sqlite3.Connection, t: dict) -> None:
    conn.execute(
        "INSERT INTO tools (name, source, description, status, input_schema, version, side_effect, risk_level, kind, config) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "source=excluded.source, description=excluded.description, status='active', "
        "input_schema=excluded.input_schema, version=excluded.version, side_effect=excluded.side_effect, "
        "risk_level=excluded.risk_level, kind=excluded.kind, config=excluded.config",
        (t["name"], t["source"], t["description"], "active",
         json.dumps(t["input_schema"], ensure_ascii=False), t["version"],
         t["side_effect"], t["risk_level"], "http", json.dumps(t["config"], ensure_ascii=False)),
    )


def _bind_agents(conn: sqlite3.Connection, tool_name: str, agent_names: list) -> int:
    bound = 0
    for agent_name in (agent_names or []):
        agent = conn.execute("SELECT id FROM agents WHERE name=?", (agent_name,)).fetchone()
        if not agent:
            print(f"[register] ⚠️ 跳过绑定：Agent '{agent_name}' 不存在")
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO agent_tools (agent_id, tool_type, tool_name, enabled) VALUES (?, 'tool', ?, 1)",
            (agent["id"], tool_name),
        )
        bound += cur.rowcount
    return bound


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_columns(conn)
        total_tools, total_binds = 0, 0
        # 1) 智源内置定义（幂等 upsert + 绑定）
        for t in ZHIYUAN_TOOL_DEFS:
            _upsert_tool(conn, t)
            total_tools += 1
        for agent_name, tool_list in ZHIYUAN_BINDINGS.items():
            for tname in tool_list:
                total_binds += _bind_agents(conn, tname, [agent_name])

        # 2) 外部集成文件（integrations/*.json）
        ext = _load_integration_files()
        for t in ext:
            _upsert_tool(conn, t)
            total_tools += 1
            if t.get("bind_to"):
                total_binds += _bind_agents(conn, t["name"], t["bind_to"])
        conn.commit()

        print(f"[register] HTTP 集成工具: {total_tools} 个（智源 {len(ZHIYUAN_TOOL_DEFS)} + 外部文件 {len(ext)}）| 绑定 {total_binds} 条")
        print("\n=== tools 表（kind='http'）===")
        for r in conn.execute(
            "SELECT name, source, status, side_effect, kind FROM tools WHERE kind='http' ORDER BY name"
        ).fetchall():
            print(f"  {r['name']} [{r['source']}/{r['side_effect']}/{r['kind']}]")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
