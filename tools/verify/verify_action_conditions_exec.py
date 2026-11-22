# -*- coding: utf-8 -*-
"""P0-1 第五批次门禁：绑定 pre/post 条件在**执行唯一出口**真消费 + 四态对齐。

## 判据（全部临时库真跑，不碰生产库）
  S1 无绑定 ⇒ 回执无 action_pre/action_post，impl 真执行（零回归锚）
  S2 pre failed ⇒ 拒绝执行，gate=action_pre_condition，impl **未被调用**
  S3 pre unknown ⇒ 放行但回执带 action_pre（可见，不静默）
  S4 post verified ⇒ ok=True，action_post.status=verified
  S5 post failed ⇒ ok=False，gate=action_post_condition（写入已发生，勿重试）
  S6 post unknown ⇒ ok=True 但 retry_safe=False
  S7 静态：post 检查在幂等记账之前（否则重放绕过核验）
  S8 静态：四态常量单一真源（runtime 不另写一份）
  M1/M2/M3 真跑变异：抹掉 pre / post / 回读快照 ⇒ 对应判据必须判红

用法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_action_conditions_exec.py
"""
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

# `--live`：真机探针模式，连生产库 mbse.db 跑 file_write 的 pre/post；
#           绑定行与探针文件测后精确清理，不动任何实体/关系数据。
# 默认：临时库全量判据（CI 与常态验证用）。
_LIVE = "--live" in sys.argv
_TMP = None if _LIVE else tempfile.mkdtemp(prefix="verify_action_cond_")
if not _LIVE:
    os.environ["MBSE_DB_PATH"] = os.path.join(_TMP, "db.sqlite")
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db              # noqa: E402
if not _LIVE:
    init_db()

from agent.pipeline_parts.tools import ToolMixin   # noqa: E402
from agent.registry import AgentRegistry           # noqa: E402
from core import action_conditions as _ac          # noqa: E402
from core import action_binding_runtime as _abr    # noqa: E402
from core import idempotency as _idem              # noqa: E402

# 幂等闸与本判据无关（它有自己的门禁）；关掉避免掩盖 pre/post 的因果。
_idem.enabled = lambda: False

_TOOL = "p01_cond_probe"
_TID = None
_RESULTS = []
_CALLS = []


def _seed():
    global _TID
    c = get_db()
    try:
        c.execute("DELETE FROM tools WHERE name=?", (_TOOL,))
        c.execute("INSERT INTO tools (name, source, description, status, builtin, kind, "
                  "side_effect, input_schema) VALUES (?, 'local', 'probe', 'active', 0, "
                  "'builtin', 'write', '{\"type\":\"object\",\"properties\":{}}')", (_TOOL,))
        ag = c.execute("SELECT id FROM agents WHERE name='chat' LIMIT 1").fetchone()
        if not ag:
            c.execute("INSERT INTO agents (name, display_name, description, system_prompt, status) "
                      "VALUES ('chat','通用助手','test','', 'active')")
            ag = c.execute("SELECT id FROM agents WHERE name='chat' LIMIT 1").fetchone()
        c.execute("DELETE FROM agent_tools WHERE agent_id=? AND tool_name=?", (ag[0], _TOOL))
        c.execute("INSERT INTO agent_tools (agent_id, tool_name, tool_type, enabled) "
                  "VALUES (?, ?, 'tool', 1)", (ag[0], _TOOL))
        c.execute("DELETE FROM ontology_types WHERE name='P01条件测试类型'")
        c.execute("INSERT INTO ontology_types (name, type_kind) VALUES ('P01条件测试类型','entity')")
        _TID = c.execute("SELECT id FROM ontology_types WHERE name='P01条件测试类型'").fetchone()[0]
        c.commit()
    finally:
        c.close()


class _Pipe(ToolMixin):
    def __init__(self):
        self.registry = AgentRegistry()
        c = get_db()
        try:
            self.registry.load_from_db(c, {"role_name": "admin", "role": "admin"})
        finally:
            c.close()
        self._tool_whitelist = None

    def _exec_tool_call_impl(self, name, arguments):
        """假执行体：真写一条实体，让 post 回读是**真回读**而非桩。"""
        _CALLS.append(dict(arguments or {}))
        if (arguments or {}).get("__fail__"):
            return {"ok": False, "result": "impl 主动失败"}
        eid = "P01-COND-" + str(len(_CALLS))
        et = (arguments or {}).get("entity_type") or "组件"
        c = get_db()
        try:
            c.execute("INSERT OR REPLACE INTO entities (id, name, entity_type, branch, properties) "
                      "VALUES (?, ?, ?, 'dev', '{}')", (eid, "探针", et))
            c.commit()
        finally:
            c.close()
        return {"ok": True, "id": eid, "result": "created " + eid}


def _bind(pre=None, post=None):
    c = get_db()
    try:
        c.execute("DELETE FROM ontology_type_actions WHERE type_id=?", (_TID,))
        if pre or post:
            c.execute("INSERT INTO ontology_type_actions "
                      "(type_id, tool_name, pre_condition, post_condition, enabled, rule_version) "
                      "VALUES (?,?,?,?,1,?)",
                      (_TID, _TOOL,
                       json.dumps(pre, ensure_ascii=False) if pre else None,
                       json.dumps(post, ensure_ascii=False) if post else None,
                       _ac.RULE_VERSION))
        c.commit()
    finally:
        c.close()


def _checks(tag):
    """常态/变异共用同一批判据；tag 隔离 id 与计数。"""
    out = []
    def ck(name, cond, detail=""):
        out.append((bool(cond), tag + " " + name, detail))
    p = _Pipe()

    # S1 无绑定
    _bind(); _CALLS.clear()
    r = p._exec_tool_call(_TOOL, {"entity_type": "需求"})
    ck("S1 无绑定零回归", r.get("ok") is True and "action_pre" not in r
       and "action_post" not in r and len(_CALLS) == 1, str(r)[:160])

    # S2 pre failed ⇒ 拒绝且 impl 未调用
    # 注意：参数**缺失**按规则表归 unknown；要造 failed 必须显式传空值（证实为假）。
    _bind(pre={"kind": "field_not_empty", "arg": "name"}); _CALLS.clear()
    r = p._exec_tool_call(_TOOL, {"entity_type": "需求", "name": ""})
    ck("S2 pre failed 拒绝执行", r.get("ok") is False
       and r.get("gate") == "action_pre_condition"
       and r.get("action_pre", {}).get("status") == "failed"
       and len(_CALLS) == 0, str(r)[:160])

    # S3 pre unknown ⇒ 放行但可见
    _bind(pre={"kind": "entity_type_in", "arg": "meta", "types": ["需求"]}); _CALLS.clear()
    r = p._exec_tool_call(_TOOL, {"entity_type": "需求"})
    ck("S3 pre unknown 放行且留痕", r.get("ok") is True and len(_CALLS) == 1
       and r.get("action_pre", {}).get("status") == "unknown", str(r)[:160])

    # S4 post verified
    _bind(post={"kind": "entity_exists", "arg": "id"}); _CALLS.clear()
    r = p._exec_tool_call(_TOOL, {"entity_type": "需求"})
    ck("S4 post verified", r.get("ok") is True
       and r.get("action_post", {}).get("status") == "verified", str(r)[:160])

    # S5 post failed ⇒ 成功回执改判失败
    _bind(post={"kind": "field_equals", "arg": "id", "field": "entity_type",
                "value": "需求"}); _CALLS.clear()
    r = p._exec_tool_call(_TOOL, {"entity_type": "组件"})
    ck("S5 post failed 改判", r.get("ok") is False
       and r.get("gate") == "action_post_condition"
       and r.get("action_post", {}).get("status") == "failed"
       and "勿盲目重试" in str(r.get("result")), str(r)[:160])

    # S6 post unknown ⇒ 放行但 retry_safe=False
    _bind(post={"kind": "entity_exists", "arg": "absent_key"}); _CALLS.clear()
    r = p._exec_tool_call(_TOOL, {"entity_type": "需求"})
    ck("S6 post unknown 不可重试", r.get("ok") is True
       and r.get("action_post", {}).get("status") == "unknown"
       and r.get("retry_safe") is False, str(r)[:160])
    return out


def live_probe():
    """真机模式：生产库 + 真 AgentPipeline，用只读工具 graph_retrieve 探针。

    只短写 ontology_type_actions 的配置行，测后精确删除；不碰实体/关系/审计。
    选 read 工具而非 file_write：真机 admin 夹具无 agent_tool:write 权限，
    会在 RBAC 闸先拒 ⇒ ok=False 时本就不评估 post，测不到目标分支。
    """
    import sqlite3
    from agent.pipeline import AgentPipeline
    from agent.registry import AgentRegistry

    db = os.path.join(_ROOT, "mbse.db")
    tables = ["relations", "entities", "entity_versions", "graph_edit_logs",
              "ontology_questions", "triples", "ontology_type_actions"]

    def counts():
        c = sqlite3.connect(db)
        try:
            return {t: c.execute("select count(*) from " + t).fetchone()[0] for t in tables}
        finally:
            c.close()

    def bind(pre, post):
        c = sqlite3.connect(db, timeout=30)
        try:
            c.execute("DELETE FROM ontology_type_actions WHERE tool_name='graph_retrieve'")
            c.execute("INSERT INTO ontology_type_actions "
                      "(type_id,tool_name,pre_condition,post_condition,enabled,rule_version) "
                      "VALUES (?,?,?,?,1,?)",
                      (tid, "graph_retrieve",
                       json.dumps(pre, ensure_ascii=False) if pre else None,
                       json.dumps(post, ensure_ascii=False) if post else None,
                       _ac.RULE_VERSION))
            c.commit()
        finally:
            c.close()

    def unbind():
        c = sqlite3.connect(db, timeout=30)
        try:
            n = c.execute("DELETE FROM ontology_type_actions WHERE tool_name='graph_retrieve'").rowcount
            c.commit()
            return n
        finally:
            c.close()

    c = sqlite3.connect(db)
    try:
        tid = c.execute("SELECT id FROM ontology_types WHERE type_kind='entity' LIMIT 1").fetchone()[0]
    finally:
        c.close()
    # has_perm() 只读 user["permissions"]；只给 role_name 会连 read 都被拒
    # （真机实测过：graph_retrieve 返回 permission_denied，非 pre/post 的问题）。
    user = {"role_name": "系统管理员", "role": "系统管理员",
            "permissions": {"admin": ["all"], "agent_tool": ["read", "write"]}}
    p = AgentPipeline()
    p.registry = AgentRegistry()
    c = get_db()
    try:
        p.registry.load_from_db(c, user)
    finally:
        c.close()
    p._tool_user = user
    p._tool_whitelist = None

    print("LIVE_BASE", json.dumps(counts(), sort_keys=True))
    ok = []
    try:
        bind({"kind": "field_not_empty", "arg": "query"}, None)
        r1 = p._exec_tool_call("graph_retrieve", {"query": ""})
        a = (r1.get("action_pre") or {}).get("status") if isinstance(r1, dict) else None
        c1 = r1.get("ok") is False and r1.get("gate") == "action_pre_condition" and a == "failed"
        ok.append(c1)
        print("  [%s] LIVE pre failed 拒绝执行（read 工具零副作用）  gate=%s status=%s" %
              ("PASS" if c1 else "FAIL", r1.get("gate"), a))

        bind(None, {"kind": "entity_exists", "arg": "absent_key"})
        r2 = p._exec_tool_call("graph_retrieve", {"query": "P05 真机探针"})
        b = (r2.get("action_post") or {}).get("status") if isinstance(r2, dict) else None
        c2 = (r2.get("ok") is True and b == "unknown" and r2.get("retry_safe") is False)
        ok.append(c2)
        print("  [%s] LIVE post unknown 放行且 retry_safe=False  status=%s  detail=%s" %
              ("PASS" if c2 else "FAIL", b, str(r2)[:180]))
    finally:
        n = unbind()
        print("LIVE_CLEAN binding_rows=%d" % n)
        print("LIVE_FINAL", json.dumps(counts(), sort_keys=True))
    print("=" * 70)
    print("结论：真机 %s" % ("全部通过" if all(ok) else "未通过"))
    return 0 if all(ok) else 1


def main():
    if _LIVE:
        return live_probe()
    try:
        _seed()
        base = _checks("BASE")
        for ok, name, detail in base:
            _RESULTS.append((ok, name, detail))

        src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py"),
                   encoding="utf-8").read()
        i_post = src.find("_post = _abr.check_post")
        i_record = src.find("_idem.record(")
        _RESULTS.append((0 <= i_post < i_record, "S7 post 先于幂等记账",
                         "post=%s record=%s" % (i_post, i_record)))
        _RESULTS.append((("_ac.VALID_STATUS" in open(
            os.path.join(_ROOT, "core", "action_binding_runtime.py"),
            encoding="utf-8").read()) or
            ("action_conditions" in open(
                os.path.join(_ROOT, "core", "action_binding_runtime.py"),
                encoding="utf-8").read()),
            "S8 四态口径单一真源", ""))

        # ---- 真跑变异 ----
        print("\n--- 变异自证 ---")
        orig_pre, orig_post, orig_snap = _abr.check_pre, _abr.check_post, _abr.build_post_snapshot
        try:
            _abr.check_pre = lambda *a, **k: None
            m1 = _checks("M1")
            s2 = next(x for x in m1 if "S2" in x[1])
            caught1 = not s2[0]
            print("  [%s] M1 抹掉 pre 消费 ⇒ S2 判红" % ("PASS" if caught1 else "FAIL"))

            _abr.check_pre = orig_pre
            _abr.check_post = lambda *a, **k: None
            m2 = _checks("M2")
            names2 = [x[1] for x in m2 if not x[0] and any(
                s in x[1] for s in ("S4", "S5", "S6"))]
            caught2 = len(names2) == 3
            print("  [%s] M2 抹掉 post 消费 ⇒ S4/S5/S6 判红" % ("PASS" if caught2 else "FAIL"))

            _abr.check_post = orig_post
            _abr.build_post_snapshot = lambda *a, **k: {}
            m3 = _checks("M3")
            s4 = next(x for x in m3 if "S4" in x[1])
            caught3 = not s4[0]
            print("  [%s] M3 抹掉回读快照 ⇒ S4 判红" % ("PASS" if caught3 else "FAIL"))
        finally:
            _abr.check_pre, _abr.check_post = orig_pre, orig_post
            _abr.build_post_snapshot = orig_snap

        print()
        for ok, name, detail in _RESULTS:
            print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                                   ("  <- " + detail) if detail else ""))
        n_fail = sum(1 for x in _RESULTS if not x[0])
        mut_ok = caught1 and caught2 and caught3
        print("=" * 70)
        if n_fail or not mut_ok:
            print("结论：未通过（常态 %d 失败 / 变异 %s）" % (n_fail, mut_ok))
            return 1
        print("结论：全部通过（常态 %d 条 + 变异 3 组按预期判红）" % len(_RESULTS))
        return 0
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
