# -*- coding: utf-8 -*-
"""P0-1 G2 门禁：Action 绑对象类型的**注入回归锚** + 过滤判据 + 变异自证。

## 为什么需要
`_build_tools_def` 是唯一注入入口，累积了 12+ 道历史闸（角色/状态/JIT/白名单/
写意图/就绪度…）。新增 `scope_types` 维度最容易出的不是新功能坏，而是
**老功能悄悄变**：一次绑定漏配让写类工具集体消失（与 `tools.status='disabled'`
那次「用户以为能力不存在」同型）。

## 判据
  [G2-1] **回归锚**：`scope_types=None` 时注入集合与基线快照**逐项相等**
         （基线由 `--save-baseline` 在改 `_build_tools_def` 之前拍下）。
  [G2-2] 传 `scope_types=['单元需求']` 时：注入的 **write/destructive 工具集 ⊆ 绑定集**；
         **read 工具数不变**（只卡写不卡读，避免"连查都查不到"）。
  [G2-3] 剔除**留痕**：被类型过滤挡掉的工具出现在 `self._dropped_by_type`。
  [G2-4] 开关关闭（`ontology.type_action_inject=false`）时，即使传 scope_types
         也与基线逐项相等（默认关闭的硬约束）。
  [M1]   变异：把 write 工具绑定清空 ⇒ G2-2 的 write 子集断言必须变红。
  [M2]   变异：关掉开关 ⇒ G2-2 必须变红（证明开关真的在控过滤）。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_tool_inject_by_type.py --save-baseline
  ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_tool_inject_by_type.py
"""
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = os.path.join(tempfile.gettempdir(), "verify_type_actions.db")
for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
os.environ["MBSE_DB_PATH"] = _TMP
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db        # noqa: E402
init_db()

from agent.pipeline_parts.tools import ToolMixin   # noqa: E402
from agent.registry import AgentRegistry           # noqa: E402
from core import config as _cfg                    # noqa: E402

_BASELINE = os.path.join(_ROOT, "tools", "verify", "baselines",
                         "tool_inject_scope_none.json")

_FAILS = []


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        _FAILS.append(name)
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))


# ── 固定夹具：覆盖 read / write / destructive 三类副作用 ──
# 用固定工具集而非真库，避免"配置漂移"让基线隔天就假红。
_TOOLS = [
    # name, desc, side_effect
    ("graph_retrieve", "检索图谱实体与关系", "read"),
    ("validate", "校验模型一致性", "read"),
    ("impact_analyze", "分析变更影响面", "read"),
    ("query_projects", "查询工程列表", "read"),
    ("entity_create", "创建实体并入库", "write"),
    ("file_write", "保存内容到工作区文件", "write"),
    ("requirement_itemize", "把需求文本条目化并落库", "write"),
    ("entity_delete", "删除实体（不可逆）", "destructive"),
    ("delete_model_elements", "删除模型元素（不可逆）", "destructive"),
]

con = get_db()
for name, desc, se in _TOOLS:
    con.execute("DELETE FROM tools WHERE name=?", (name,))
    con.execute(
        "INSERT INTO tools (name, source, description, status, builtin, kind, "
        "side_effect, input_schema) VALUES (?, 'local', ?, 'active', 0, 'builtin', ?, ?)",
        (name, desc, se, '{"type":"object","properties":{}}'))
_ag = con.execute("SELECT id FROM agents WHERE name='chat' LIMIT 1").fetchone()
if not _ag:
    con.execute("INSERT INTO agents (name, display_name, description, system_prompt, status) "
                "VALUES ('chat','通用助手','test','', 'active')")
    con.commit()
    _ag = con.execute("SELECT id FROM agents WHERE name='chat' LIMIT 1").fetchone()
_aid = _ag[0]
for name, _d, _se in _TOOLS:
    con.execute("DELETE FROM agent_tools WHERE agent_id=? AND tool_name=?", (_aid, name))
    con.execute("INSERT INTO agent_tools (agent_id, tool_name, tool_type, enabled) "
                "VALUES (?, ?, 'tool', 1)", (_aid, name))
# 造本体类型（绑定表 type_id 的软引用目标）
con.execute("DELETE FROM ontology_types WHERE name IN ('单元需求','系统需求','组件')")
for nm in ("单元需求", "系统需求", "组件"):
    con.execute("INSERT INTO ontology_types (name, type_kind) VALUES (?, 'entity')", (nm,))
con.commit()
con.close()


class _Pipe(ToolMixin):
    def __init__(self):
        self.registry = AgentRegistry()
        _c = get_db()
        try:
            self.registry.load_from_db(_c, {"role_name": "admin", "role": "admin"})
        finally:
            _c.close()
        self._tool_whitelist = None


USER = {"role_name": "admin", "role": "admin"}
# 含写意图词 ⇒ 写类工具过写意图闸；语义弱相关 ⇒ JIT 空回退。
# 把 jit_fallback_top_n 固定设 0 ⇒ 空回退走"全量注入"，基线稳定可复现。
_Q = "创建实体并保存"
_sec = _cfg._CONFIG.setdefault("context_audit", {})
_old_fb = _sec.get("jit_fallback_top_n", 8)
_sec["jit_fallback_top_n"] = 0
# 语义阈值设极大值 ⇒ chosen 恒空 ⇒ 走空回退全量分支，夹具 9 个工具全部进基线。
# 否则 JIT 会把候选裁到 top6，write/destructive 覆盖不足，回归锚锁不住每类副作用。
_emb = _cfg._CONFIG.setdefault("embedding", {})
_old_tb, _old_td = _emb.get("tool_threshold", 0.12), _emb.get("tool_threshold_dense", 0.53)
_emb["tool_threshold"], _emb["tool_threshold_dense"] = 999, 999


def injected_names(scope_types=None):
    p = _Pipe()
    # 签名兼容：拍基线阶段 _build_tools_def 尚未加 scope_types 参数（那时 None 即
    # 改动前行为）；实现后就传新参数。两条路都得覆盖，脚本才能跨"改前/改后"两个阶段跑。
    import inspect
    accepts = "scope_types" in inspect.signature(
        p._build_tools_def).parameters
    if accepts:
        defs = p._build_tools_def("chat", _Q, USER, scope_types=scope_types)
    else:
        defs = p._build_tools_def("chat", _Q, USER)
    return sorted(t["function"]["name"] for t in defs), p


def save_baseline():
    names, _ = injected_names(None)
    os.makedirs(os.path.dirname(_BASELINE), exist_ok=True)
    with open(_BASELINE, "w", encoding="utf-8") as f:
        json.dump({"scope_types": None, "names": names}, f,
                  ensure_ascii=False, indent=2, sort_keys=True)
    print("BASELINE_SAVED %s (%d tools)" % (_BASELINE, len(names)))
    print(json.dumps(names, ensure_ascii=False))


def run_checks():
    if not os.path.exists(_BASELINE):
        print("NO_BASELINE 请先跑 --save-baseline"); return 1
    base = json.load(open(_BASELINE, encoding="utf-8"))["names"]

    # G2-1：scope_types=None 逐项相等
    names_none, _ = injected_names(None)
    check("G2-1 scope_types=None 回归锚逐项相等",
          names_none == base,
          "diff=%s" % (sorted(set(names_none) ^ set(base)) or "none"))

    # 绑定：只绑 2 个写工具 + 1 个 destructive，read 一概不绑（验证 read 不受约束）
    con = get_db()
    try:
        # 新表可能尚未迁移（拍基线时无），此时跳过绑定相关判据
        has_tbl = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ontology_type_actions'"
        ).fetchone()
        if not has_tbl:
            print("SKIP G2-2/3/4 + M1/M2：ontology_type_actions 表尚未创建（当前处于拍基线阶段）")
            return 0 if not _FAILS else 1
        tid = con.execute("SELECT id FROM ontology_types WHERE name='单元需求'").fetchone()[0]
        con.execute("DELETE FROM ontology_type_actions")
        for tn in ("entity_create", "file_write", "entity_delete"):
            con.execute(
                "INSERT INTO ontology_type_actions (type_id, tool_name, enabled, rule_version) "
                "VALUES (?, ?, 1, '2026-11-22.1')", (tid, tn))
        con.commit()
    finally:
        con.close()

    # G2-4：开关关闭时即使传值也与基线相等
    _cfg._CONFIG.setdefault("ontology", {})["type_action_inject"] = False
    names_off, _ = injected_names(["单元需求"])
    check("G2-4 开关关闭时传 scope_types 仍与基线相等",
          names_off == base,
          "diff=%s" % (sorted(set(names_off) ^ set(base)) or "none"))

    # G2-2/3：开关打开，传 scope_types
    _cfg._CONFIG["ontology"]["type_action_inject"] = True
    names_on, pipe_on = injected_names(["单元需求"])
    se_map = {n: se for n, _d, se in _TOOLS}
    read_base = {n for n in base if se_map.get(n, "read") == "read"}
    write_on = {n for n in names_on if se_map.get(n, "read") in ("write", "destructive")}
    bound = {"entity_create", "file_write", "entity_delete"}
    check("G2-2 write/destructive 注入集 ⊆ 绑定集",
          write_on <= bound, "write_on=%s" % sorted(write_on))
    check("G2-2 read 工具数不变",
          {n for n in names_on if se_map.get(n, "read") == "read"} == read_base,
          "missing=%s" % sorted(read_base - set(names_on)))
    dropped = getattr(pipe_on, "_dropped_by_type", []) or []
    check("G2-3 剔除留痕", all(n in dropped for n in ("requirement_itemize",
                                                       "delete_model_elements")),
          "dropped=%s" % sorted(dropped))

    # M1 变异：清空绑定 ⇒ write 子集断言必须变红
    con = get_db()
    try:
        con.execute("DELETE FROM ontology_type_actions")
        con.commit()
    finally:
        con.close()
    names_m1, _ = injected_names(["单元需求"])
    write_m1 = {n for n in names_m1 if se_map.get(n, "read") in ("write", "destructive")}
    # 清空绑定 ⇒ 没有任何 write 工具在绑定集里 ⇒ 全部被挡 ⇒ write_m1 为空。
    # 这才是"过滤真在生效"的证明（若过滤是空转，write_m1 会仍等于 write_on）。
    check("M1 变异自证：清空绑定 ⇒ write 注入集清空",
          bool(write_on) and not write_m1,
          "write_on=%s → write_m1=%s" % (sorted(write_on), sorted(write_m1)))

    # M2 变异：关掉开关 ⇒ 与基线相等（证明开关真在控过滤）
    _cfg._CONFIG["ontology"]["type_action_inject"] = False
    names_m2, _ = injected_names(["单元需求"])
    check("M2 变异自证：关开关 ⇒ 回到基线注入", names_m2 == base,
          "diff=%s" % (sorted(set(names_m2) ^ set(base)) or "none"))

    _sec["jit_fallback_top_n"] = _old_fb
    _emb["tool_threshold"], _emb["tool_threshold_dense"] = _old_tb, _old_td
    return 1 if _FAILS else 0


if __name__ == "__main__":
    if "--save-baseline" in sys.argv:
        save_baseline()
        sys.exit(0)
    rc = run_checks()
    print("\n结论：%s（FAIL %d）" % ("全绿" if rc == 0 else "有红", len(_FAILS)))
    sys.exit(rc)
