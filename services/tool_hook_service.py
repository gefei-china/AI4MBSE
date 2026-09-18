"""确定性工具钩子（对齐 Claude Code PreToolUse）：工具调用前强制校验，不依赖 LLM 概率。

数据表 tool_hooks（可配置）：
- tool_pattern：精确名 | 前缀（file_*）| 后缀（*_delete）
- event：钩子事件（本轮仅 pre_tool_use）
- action：block（强制拦截）| require_confirm（进 HIL 人工确认队列）| warn（放行+提示）
- condition_args：可选参数条件 JSON {"key": "path", "contains": ".sysml" | "equals": "x"}

调用方（agent/pipeline.py::_exec_tool_call）在门控链最前执行 run()：
白名单拒绝（最小权限）→ 本钩子 → HIL L2 写确认 → destructive 确认 → 执行。
钩子故障静默放行（不阻断主流程，与既有降级原则一致）。
"""
import json

ACTION_RANK = {"block": 3, "require_confirm": 2, "warn": 1}  # 越严排名越前


class ToolHookService:
    """tool_hooks 表的读取 + 匹配 + 动作判定。"""

    # ── 匹配规则 ──
    @staticmethod
    def _pattern_match(pattern: str, tool_name: str) -> bool:
        """pattern 匹配：精确名 | 前缀（file_*）| 后缀（*_delete）。"""
        p = (pattern or "").strip()
        t = (tool_name or "").strip()
        if not p or not t:
            return False
        if p.endswith("*") and p.startswith("*"):
            return p[1:-1] in t  # *xxx* 包含
        if p.endswith("*"):
            return t.startswith(p[:-1])  # 前缀 file_*
        if p.startswith("*"):
            return t.endswith(p[1:])  # 后缀 *_delete
        return p == t  # 精确

    @staticmethod
    def _condition_match(condition_json: str, arguments: dict) -> bool:
        """参数条件校验：JSON {"key","contains"|"equals"}；无条件或解析失败 → True。"""
        if not condition_json or not str(condition_json).strip():
            return True
        try:
            cond = json.loads(condition_json)
        except Exception:
            return True  # 条件损坏视为不限制（放行），确定性规则由管理员修复
        if not isinstance(cond, dict):
            return True
        key = cond.get("key")
        if not key:
            return True
        val = arguments.get(key) if isinstance(arguments, dict) else None
        val_s = str(val) if val is not None else ""
        if "contains" in cond:
            return str(cond["contains"]) in val_s
        if "equals" in cond:
            return val_s == str(cond["equals"])
        return True

    def load(self, conn) -> list:
        """enabled=1 钩子全量（按 id 升序，稳定匹配优先级）。"""
        try:
            rows = conn.execute(
                "SELECT * FROM tool_hooks WHERE enabled=1 ORDER BY id").fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def match(self, tool_name: str, arguments: dict, hooks: list | None = None) -> list:
        """返回命中的钩子列表（pattern + 条件双命中），按 action 严重度降序。"""
        if hooks is None:
            return []
        hits = [h for h in hooks
                if self._pattern_match(h.get("tool_pattern", ""), tool_name)
                and self._condition_match(h.get("condition_args", ""), arguments or {})]
        hits.sort(key=lambda h: ACTION_RANK.get(h.get("action", "warn"), 0), reverse=True)
        return hits

    def run(self, tool_name: str, arguments: dict) -> dict:
        """入口：独立短连接加载钩子 → 匹配 → 返回最严命中。

        返回 {"hit": False} 或 {"hit": True, "action", "message", "name"}。
        任何异常（表不存在/连接失败）→ 静默放行 {"hit": False}。
        """
        try:
            from database import get_db
            conn = get_db()
            try:
                hooks = self.load(conn)
            finally:
                conn.close()
        except Exception:
            return {"hit": False}
        hits = self.match(tool_name, arguments or {}, hooks)
        if not hits:
            return {"hit": False}
        h = hits[0]
        return {"hit": True, "action": h.get("action", "warn"),
                "message": h.get("message", "") or f"工具「{tool_name}」被自动化钩子命中",
                "name": h.get("name", ""), "hook_id": h.get("id")}
