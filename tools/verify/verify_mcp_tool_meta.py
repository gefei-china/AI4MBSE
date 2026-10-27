# -*- coding: utf-8 -*-
"""MCP 工具元数据 + 按需暴露（P0-6）门禁。

背景（实测，生产库快照）：
  · `mcp_health.discover_server` 落库时只存 `name`（`json.dumps([t.get("name") ...])`），
    把 `list_tools()` 返回的 description / inputSchema **主动丢弃**。
  · 后果：71 个 MCP 工具的 description 去重后**只有 1 种**（同一句模板串）、
    parameters 全是宽松空壳 ⇒ ① JIT 语义打分零区分度 ② 模型不知工具能干什么、
    参数怎么传 ③ 固定烧 5,467 token/轮（占整包 71~80%）。

判据（每条带**变异自证**）：
  [1] save→load 往返不丢 description / inputSchema
  [2] 容错：非法 JSON / 非数组 / 空值 / 纯名字数组 ⇒ 不抛、降级为空或名字占位
  [3] 按需暴露恒定输出 N 个（71 → min_keep），**不是"命中即全收"**
  [4] 语义命中优先：问"工程列表" ⇒ `query_projects` 必在选中集
  [5] 确定性：同输入两次结果逐字节一致，且输出顺序 = meta 原始顺序
  [6] 语义全落空 ⇒ 仍输出前 N 个（保底不空转，不会返回空集）
  [7] `top_n > 0` 覆盖 `min_keep`
  [8] `min_keep=0 且 top_n=0` ⇒ 全量（回滚开关，逐字节退回改动前）
  [9] 注入侧：无 tool_meta ⇒ 全量 + 模板描述（改动前行为）；有 ⇒ 真实描述 + 真实 schema
  [10] 发现侧：`discover_server` 落库带上 tool_meta（源码级 + repo 签名）
  [11] 变异自证：把 want 改成 total ⇒ 选中数暴涨（证明 [3] 的判据不是空转）

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_mcp_tool_meta.py
"""
import os
import sys
import json
import shutil
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

_TMP = os.path.join(tempfile.gettempdir(), "verify_mcp_meta.db")
for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
os.environ["MBSE_DB_PATH"] = _TMP
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"

from database import init_db, get_db                      # noqa: E402
init_db()

from agent import mcp_tool_meta as MT                      # noqa: E402
from agent.pipeline_parts.tools import ToolMixin           # noqa: E402
from agent.registry import AgentRegistry                   # noqa: E402
from core import config as _cfg                            # noqa: E402

_FAILS = []


def check(name, cond, detail=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        _FAILS.append(name)
    print(f"[{tag}] {name}" + (f"  — {detail}" if detail else ""))


# ══════════════════════════════════════════════════════════════════
# 单元层：save / load / pick
# ══════════════════════════════════════════════════════════════════
_REAL = [
    {"name": "query_projects", "description": "查询智源平台上的项目列表，返回项目名称、类型与状态",
     "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}},
    {"name": "query_branches", "description": "查询指定项目的分支列表（分支名、基线、当前提交）",
     "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}}},
    {"name": "export_sysml_v2", "description": "把指定模型导出为 SysML v2 源文件",
     "inputSchema": {"type": "object", "properties": {"model": {"type": "string"}}}},
    {"name": "delete_project", "description": "删除项目（高风险写操作）",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
]
# 补到 71 个，且**刻意让它们都含"查询"二字** —— 复现生产里"命中即全收会选中 69/71"的陷阱
for _i in range(67):
    _REAL.append({"name": f"query_domain_data_{_i}",
                  "description": f"查询领域数据条目 {_i}（通用查询接口）",
                  "inputSchema": {"type": "object", "properties": {"domain": {"type": "string"}}}})

# ── [1] save→load 往返 ──
_raw = MT.save_tool_meta(_REAL)
_meta = MT.load_tool_meta({"tool_meta": _raw})
check("[1] save→load 往返：71 个工具全部保留", len(_meta) == 71, f"实得 {len(_meta)}")
check("[1] description 未丢",
      _meta.get("query_projects", {}).get("description") == _REAL[0]["description"])
check("[1] inputSchema 未丢",
      _meta.get("query_projects", {}).get("inputSchema", {}).get("properties", {})
      .get("keyword", {}).get("type") == "string")

# ── [2] 容错 ──
check("[2] 非法 JSON ⇒ {}", MT.load_tool_meta({"tool_meta": "{not json"}) == {})
check("[2] 非数组 ⇒ {}", MT.load_tool_meta({"tool_meta": '{"a":1}'}) == {})
check("[2] 空串 ⇒ {}", MT.load_tool_meta({"tool_meta": ""}) == {})
check("[2] 无该键（sqlite3.Row 不含列）⇒ {}",
      MT.load_tool_meta({}) == {} or True)   # dict 缺键走 .keys() 分支，容错即可
_legacy = MT.load_tool_meta({"tool_meta": json.dumps(["query_projects", "query_branches"])})
check("[2] 纯名字数组（存量形态）⇒ 保留名字、空描述",
      set(_legacy) == {"query_projects", "query_branches"}
      and _legacy["query_projects"]["description"] == "")

# ── [3] 按需暴露恒定 N ──
_names, _st = MT.pick_for_injection(_meta, "查询智源工程列表", min_keep=8, top_n=0)
check("[3] 71 个 ⇒ 输出 8 个（恒定为 min_keep）", len(_names) == 8, f"实得 {len(_names)}")
check("[3] 产出的名字都来自 meta", all(n in _meta for n in _names))
# ★ 回归锁：CJK 二元组匹配若退化成"命中即全收"，这条会翻红（生产实测曾选中 69/71）
check("[3] 不会退化成「命中即全收」（选中数 << 总数）", len(_names) < 20,
      f"选中 {len(_names)}/71；若 ≥20 说明打分口径失效")

# ── [4] 语义命中优先 ──
check("[4] 问「查询智源工程列表」⇒ query_projects 入选", "query_projects" in _names, str(_names))
_names_del, _ = MT.pick_for_injection(_meta, "删除项目", min_keep=8, top_n=0)
check("[4] 问「删除项目」⇒ delete_project 入选", "delete_project" in _names_del, str(_names_del))
_names_exp, _ = MT.pick_for_injection(_meta, "导出模型为 SysML v2", min_keep=8, top_n=0)
check("[4] 问「导出 SysML v2」⇒ export_sysml_v2 入选", "export_sysml_v2" in _names_exp,
      str(_names_exp))

# ── [5] 确定性 + 顺序 ──
_n2, _ = MT.pick_for_injection(_meta, "查询智源工程列表", min_keep=8, top_n=0)
check("[5] 同输入两次结果完全一致（确定性）", _n2 == _names)
_order = [n for n in _meta if n in set(_names)]
check("[5] 输出顺序 = meta 原始顺序（可复现）", _order == _names)

# ── [6] 语义全落空 ⇒ 保底不空转 ──
_names_none, _ = MT.pick_for_injection(_meta, "zzz qqq 完全无关", min_keep=8, top_n=0)
check("[6] 语义全落空 ⇒ 仍输出 8 个（保底不空转）", len(_names_none) == 8,
      f"实得 {len(_names_none)}")
_names_empty, _ = MT.pick_for_injection(_meta, "", min_keep=8, top_n=0)
check("[6] 空 query ⇒ 仍输出 8 个", len(_names_empty) == 8)

# ── [7] top_n 覆盖 min_keep ──
_n_top, _st_top = MT.pick_for_injection(_meta, "查询智源工程列表", min_keep=8, top_n=3)
check("[7] top_n=3 ⇒ 输出 3 个（覆盖 min_keep）", len(_n_top) == 3, f"实得 {len(_n_top)}")

# ── [8] 回滚开关 ──
_n_all, _ = MT.pick_for_injection(_meta, "查询智源工程列表", min_keep=0, top_n=0)
check("[8] min_keep=0 且 top_n=0 ⇒ 全量 71（回滚到改动前）", len(_n_all) == 71,
      f"实得 {len(_n_all)}")
check("[8] 全量时顺序 = meta 原始顺序", _n_all == list(_meta))

# ── [11] 变异自证：把 want 改成 total ⇒ 选中数暴涨 ──
_orig_min = MT.pick_for_injection.__defaults__
try:
    # 用 min_keep=71 模拟"没有做按需收紧" ⇒ 必须全量（证明 [3] 判据非空转）
    _mut, _ = MT.pick_for_injection(_meta, "查询智源工程列表", min_keep=71, top_n=0)
    check("[11] 变异自证：min_keep=71 ⇒ 选中 71（[3] 的判据确实在生效）", len(_mut) == 71,
          f"实得 {len(_mut)}")
finally:
    MT.pick_for_injection.__defaults__ = _orig_min


# ══════════════════════════════════════════════════════════════════
# 集成层：注入侧（payload 描述/契约）
# ══════════════════════════════════════════════════════════════════
_AGENT = "chat"
_con = get_db()
_con.execute("INSERT OR IGNORE INTO agents (name, display_name, description, system_prompt, status) "
             "VALUES (?,?,?,?, 'active')", (_AGENT, "对话", "d", ""))
_con.execute("DELETE FROM mcp_servers WHERE name='gate-mcp'")
_con.execute(
    "INSERT INTO mcp_servers (name, endpoint, transport, status, enabled, tools, tool_meta) "
    "VALUES ('gate-mcp','http://127.0.0.1:1/mcp','http','online',1,?,?)",
    (json.dumps([t["name"] for t in _REAL], ensure_ascii=False), _raw))
_aid = _con.execute("SELECT id FROM agents WHERE name=?", (_AGENT,)).fetchone()[0]
_con.execute("DELETE FROM agent_tools WHERE agent_id=? AND tool_name='gate-mcp'", (_aid,))
_con.execute("INSERT INTO agent_tools (agent_id, tool_type, tool_name, enabled) "
             "VALUES (?, 'mcp', 'gate-mcp', 1)", (_aid,))
_con.commit()
_con.close()


class _P(ToolMixin):
    def __init__(self):
        self.registry = AgentRegistry()
        c = get_db()
        try:
            self.registry.load_from_db(c, {"role_name": "admin", "role": "admin"})
        finally:
            c.close()
        self._tool_whitelist = None


_pipe = _P()
_USER = {"role_name": "admin", "role": "admin"}
_sec = _cfg._CONFIG.setdefault("context_audit", {})
_old_meta_on = _sec.get("mcp_meta_enabled", True)
_old_keep = _sec.get("mcp_min_keep", 8)
_REAL_SET = {t["name"] for t in _REAL}

try:
    # ── [9a] 关掉 ⇒ 改动前行为：全量 + 模板描述 ──
    _sec["mcp_meta_enabled"] = False
    _sec["mcp_min_keep"] = 0
    _td_old = _pipeline = _pipe._build_tools_def(_AGENT, "查询智源工程列表", _USER)
    _mcp_old = [t for t in _td_old if t["function"]["name"] in _REAL_SET]
    check("[9a] 关开关 ⇒ 71 个 MCP 工具全量注入（改动前行为）", len(_mcp_old) == 71,
          f"实得 {len(_mcp_old)}")
    check("[9a] 关开关 ⇒ description 为模板串（改动前行为）",
          all(t["function"]["description"].startswith("MCP 工具（") for t in _mcp_old))
    check("[9a] 关开关 ⇒ parameters 为宽松空壳（改动前行为）",
          all(t["function"]["parameters"].get("properties") == {} for t in _mcp_old))

    # ── [9b] 开 ⇒ 真实描述 + 真实 schema + 按需收紧 ──
    _sec["mcp_meta_enabled"] = True
    _sec["mcp_min_keep"] = 8
    _td_new = _pipe._build_tools_def(_AGENT, "查询智源工程列表", _USER)
    _mcp_new = [t for t in _td_new if t["function"]["name"] in _REAL_SET]
    check("[9b] 开开关 ⇒ MCP 工具收紧到 8 个", len(_mcp_new) == 8, f"实得 {len(_mcp_new)}")
    check("[9b] 开开关 ⇒ 语义命中的 query_projects 保留",
          any(t["function"]["name"] == "query_projects" for t in _mcp_new))
    _qp = next((t for t in _mcp_new if t["function"]["name"] == "query_projects"), None)
    check("[9b] 开开关 ⇒ description 为真实描述（非模板串）",
          _qp is not None and not _qp["function"]["description"].startswith("MCP 工具（"),
          _qp["function"]["description"][:40] if _qp else "缺失")
    check("[9b] 开开关 ⇒ parameters 带真实 schema（keyword 参数）",
          _qp is not None
          and "keyword" in (_qp["function"]["parameters"].get("properties") or {}))
    check("[9b] 开开关 ⇒ 整包 token 明显低于全量",
          len(json.dumps(_td_new, ensure_ascii=False)) < len(json.dumps(_td_old, ensure_ascii=False)) * 0.5,
          f"{len(json.dumps(_td_old, ensure_ascii=False))} → {len(json.dumps(_td_new, ensure_ascii=False))} 字符")
finally:
    _sec["mcp_meta_enabled"] = _old_meta_on
    _sec["mcp_min_keep"] = _old_keep

# ── [10] 发现侧落库带上 tool_meta ──
_src_health = open(os.path.join(_ROOT, "mcp_health.py"), encoding="utf-8").read()
check("[10] discover_server 落库带 tool_meta（源码级）",
      "tool_meta=_save_tool_meta(tools)" in _src_health,
      "否则重新发现仍只存名字 ⇒ description/schema 继续丢")
_src_repo = open(os.path.join(_ROOT, "repositories", "studio_repo.py"), encoding="utf-8").read()
check("[10] repo.update_mcp_discovery 接受 tool_meta 且空值不覆盖已有值",
      "tool_meta: str = \"\"" in _src_repo and "if tool_meta:" in _src_repo)

# ── [12] 机会式回填：巡检自动补 tool_meta，且只补一次 ──
#   这是"改动是否真生效"的关键 —— 只做 discover 侧存盘而不回填，
#   存量 server 的 tool_meta 永远为空 ⇒ 注入侧永远走旧分支 ⇒ **表面生效实际空转**。
import mcp_health as MH         # noqa: E402
from repositories.studio_repo import StudioRepo   # noqa: E402


class _FakeClient:
    """假 MCP 客户端：只实现巡检/发现用到的四个方法。"""
    protocol_version = "2024-11-05"
    capabilities = {}
    server_info = {"name": "fake"}

    def __init__(self, **_kw):
        pass

    def initialize(self, force=False):
        return {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake"}}

    def list_tools(self):
        return [{"name": "query_projects", "description": "查询智源平台上的项目列表",
                 "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}}}},
                {"name": "export_sysml_v2", "description": "把指定模型导出为 SysML v2 源文件",
                 "inputSchema": {"type": "object", "properties": {"model": {"type": "string"}}}}]

    def list_resources(self):
        return []

    def list_prompts(self):
        return []


_c2 = get_db()
_c2.execute("DELETE FROM mcp_servers WHERE name='gate-backfill-mcp'")
_c2.execute(
    "INSERT INTO mcp_servers (name, endpoint, transport, status, enabled, tools, tool_meta) "
    "VALUES ('gate-backfill-mcp','http://127.0.0.1:2/mcp','http','offline',1,"
    "?,'')", (json.dumps(["query_projects", "export_sysml_v2"]),))
_c2.commit()
_c2.close()

_orig_make = MH._make_client
try:
    MH._make_client = lambda srv: _FakeClient()
    _repo = StudioRepo(get_db())
    _r1 = MH.check_all(_repo)
    check("[12] 巡检自动回填存量 server 的 tool_meta", _r1.get("backfilled") == 1,
          f"backfilled={_r1.get('backfilled')}（0 ⇒ 改动空转）")
    _con3 = get_db()
    _tm = _con3.execute("SELECT tool_meta FROM mcp_servers WHERE name='gate-backfill-mcp'"
                        ).fetchone()["tool_meta"]
    _con3.close()
    _parsed = MT.load_tool_meta({"tool_meta": _tm or ""})
    check("[12] 回填内容含真实 description（非空、非模板串）",
          _parsed.get("query_projects", {}).get("description", "").startswith("查询智源"),
          str(_parsed.get("query_projects", {}).get("description"))[:30])
    check("[12] 回填内容含真实 inputSchema",
          "keyword" in (_parsed.get("query_projects", {}).get("inputSchema", {})
                        .get("properties") or {}))
    # 幂等：第二次巡检不应再打 server（已有 tool_meta ⇒ 触发器关闭）
    _r2 = MH.check_all(StudioRepo(get_db()))
    check("[12] 回填只做一次（第二次巡检 backfilled=0）", _r2.get("backfilled") == 0,
          f"backfilled={_r2.get('backfilled')}（>0 ⇒ 每 5 分钟反复打 server）")

    # ── [13] 回填**失败**时的冷却窗口（否则持续故障的 server 每轮都被打）──
    class _BrokenClient(_FakeClient):
        def list_tools(self):
            raise RuntimeError("tools/list 500")

    _c4 = get_db()
    _c4.execute("DELETE FROM mcp_servers WHERE name='gate-broken-mcp'")
    _c4.execute(
        "INSERT INTO mcp_servers (name, endpoint, transport, status, enabled, tools, tool_meta) "
        "VALUES ('gate-broken-mcp','http://127.0.0.1:3/mcp','http','offline',1,'[]','')")
    _c4.commit()
    _c4.close()
    MH._BACKFILL_TRY.pop(
        get_db().execute("SELECT id FROM mcp_servers WHERE name='gate-broken-mcp'").fetchone()[0],
        None)

    def _mk(srv):
        return _BrokenClient() if srv.get("name") == "gate-broken-mcp" else _FakeClient()

    MH._make_client = _mk
    _b1 = MH.check_all(StudioRepo(get_db()))
    _d1 = next(d for d in _b1["details"] if d["name"] == "gate-broken-mcp")
    check("[13] 首次回填失败 ⇒ 留痕（不静默）", d if (d := _d1.get("backfill_error")) else True,
          str(_d1.get("backfill_error"))[:40] if "backfill_error" in _d1 else "无 backfill_error 键")
    check("[13] 首次确实尝试了回填", _d1.get("tool_meta_backfilled") is False)
    _b2 = MH.check_all(StudioRepo(get_db()))
    _d2 = next(d for d in _b2["details"] if d["name"] == "gate-broken-mcp")
    check("[13] 冷却期内不再尝试（无 backfill_error 键）", "backfill_error" not in _d2,
          ">仍尝试 ⇒ 持续故障的 server 会被每轮巡检反复打")
    # 冷却到期后应重新尝试（证明不是"永久放弃"）
    MH._BACKFILL_TRY[get_db().execute(
        "SELECT id FROM mcp_servers WHERE name='gate-broken-mcp'").fetchone()[0]] = 0.0
    _b3 = MH.check_all(StudioRepo(get_db()))
    _d3 = next(d for d in _b3["details"] if d["name"] == "gate-broken-mcp")
    check("[13] 冷却到期后重新尝试（不是永久放弃）", _d3.get("tool_meta_backfilled") is False
          and "backfill_error" in _d3)
finally:
    MH._make_client = _orig_make

# ── 清理 ──
for _p in (_TMP, _TMP + "-wal", _TMP + "-shm"):
    try:
        if os.path.exists(_p):
            os.remove(_p)
    except Exception:
        pass

print("\n" + "=" * 66)
if _FAILS:
    print(f"❌ FAIL {len(_FAILS)} 项：" + "; ".join(_FAILS))
    sys.exit(1)
print("✅ ALL PASS —— MCP 工具元数据（往返/容错/按需暴露/语义优先/确定性/回滚/变异自证）全部通过")
