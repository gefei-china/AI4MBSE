# -*- coding: utf-8 -*-
"""本体一致性：dom/range 漂移修复 + 消费口径统一 的常驻自检（2026-09-23）。

按用户三项拍板与验收要求编写：
  · 决策 A（保语义修复）：悬空 dom/range → 换成卫星域等价类型，**保留**「关系允许哪些类型」的约束价值；
  · 决策 D2（消费最新快照）：体检 / AI 建模注入 / 导出 / 图谱 Schema 统一读 active 版本快照；
  · 决策 ③（不接受豁免）：删掉代码里「确认风险后豁免」的承诺。

验收金标（用户原话「修改后同步校验图谱数据，以及 AI 建模消费流程」）：
  ① `_ontology_check` 的 `high == 0`；
  ② 存量全部边（本库 249 条）全量重校验 **0 非法**；
  ③ AI 建模注入的 schema 文本包含修复后类型、**不含悬空名**。

本脚本**不依赖服务、不写库**（只读打开 mbse.db + 临时库夹具），随时可跑：
    .venv/Scripts/python.exe tools/verify/verify_ontology_dom_range.py

分段：
  [1] 规则目录单一真源（core/ontology_rules.py）+ 前端不再自持清单（防「6 种产 / 5 种映射」漂移复发）
  [2] 消费口径单一真源（ontology_semantics.active_rows）——夹具驱动，编辑态/快照内容**故意不同**
  [3] 真库金标：体检 high==0 且口径为 snapshot
  [4] 真库金标：存量边全量重校验 0 非法（并断言**覆盖面 == 边总数**，防「0 检查」假绿）
  [5] AI 建模消费流程：注入文本口径正确、含修复后类型、无悬空名、约束自洽
  [6] 好本体必过 / 坏样本必挂（6 类问题码各造一个坏样本）
  [7] 修复目标幂等（TARGETS 已全部达标）+ 快照补齐 + 变更留痕可追溯
  [8] 变异自证：把 5 处改动各自还原 → 对应断言必须 FAIL（证明断言不是空转）
  [9] 消费端点冒烟：本体相关 GET 端点无 4xx/5xx + /validate 契约字段齐备
      （用 TestClient 拉起真实 app，含 lifespan→init_db，属本仓库既有范式；
        放在 [8] 之后 → 变异子进程不会跑到）

⚠️ 换行符：本仓库混用 CRLF/LF（ontology_semantics.py / shared.py / prompt.py 是 CRLF，
   ontology_rules.py / 09-impact.js 是 LF）。[8] 段的变异写入按文件原约定归一化 ——
   直接用 LF 锚点去字节匹配 CRLF 文件会「锚点不存在」，极易误判成脚本自身写错。
"""
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

# ── 预检：本脚本需要项目依赖（fastapi 等）。用错解释器时给出明确指引，
#    而不是抛一个让人以为是代码坏了的 ModuleNotFoundError（2026-09-23 实测踩过）。
try:
    import fastapi  # noqa: F401
except Exception:  # pragma: no cover
    print("")
    print("！ 当前解释器缺少项目依赖（fastapi 未安装）。")
    print("  请用项目虚拟环境运行：")
    print("      .venv/Scripts/python.exe tools/verify/verify_ontology_dom_range.py")
    print(f"  （当前解释器：{sys.executable}）")
    sys.exit(2)

DB = REPO / "mbse.db"

OK = 0
FAIL = 0
FAILED_NAMES = []


def check(name, ok, detail=""):
    global OK, FAIL
    if ok:
        OK += 1
        tag = "PASS"
    else:
        FAIL += 1
        FAILED_NAMES.append(name)
        tag = "FAIL"
    print(f"[{tag}] {name}" + (f"  | {detail}" if detail else ""))


def hr(t):
    print()
    print("=" * 90)
    print(t)
    print("=" * 90)


def ro_conn():
    c = sqlite3.connect("file:" + str(DB).replace("\\", "/") + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _has_cjk(s):
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


def _json(v, default):
    try:
        return json.loads(v or "")
    except Exception:
        return default


def _declared(constraints_json):
    """关系类型 constraints → (src 列表, tgt 列表)。兼容旧单值写法 domain/range。"""
    c = _json(constraints_json, {}) or {}
    av = c.get("allowed_values") or {}
    dom = c.get("domain") if c.get("domain") is not None else av.get("src")
    rng = c.get("range") if c.get("range") is not None else av.get("tgt")

    def _n(v):
        vals = v if isinstance(v, list) else ([v] if v else [])
        return [str(x).strip() for x in vals if str(x).strip() and str(x) != "?"]

    return _n(dom), _n(rng)


# ══════════════════════════════════════════════════════════════════════════════
hr("[1] 规则目录单一真源（core/ontology_rules.py）+ 前端不再自持清单")

from core import ontology_rules as RULES  # noqa: E402

check("O1 规则目录非空，且每条含 label/severity/dimension/why/fix",
      bool(RULES.RULES) and all(
          all(k in v for k in ("label", "severity", "dimension", "why", "fix"))
          for v in RULES.RULES.values()),
      f"{len(RULES.RULES)} 条：{sorted(RULES.RULES)}")

check("O2 severity 取值都在 VALID_SEVERITIES 内",
      all(v["severity"] in RULES.VALID_SEVERITIES for v in RULES.RULES.values()),
      f"档位={sorted(set(v['severity'] for v in RULES.RULES.values()))}")

check("O3 label 均为中文（避免界面直接露出英文 code）",
      all(_has_cjk(v["label"]) for v in RULES.RULES.values()),
      f"样例={RULES.label('bad_dom_range')}")

# 前端源码级守护：函数体不得再出现本地 code→中文 映射表
_JS = "static/js/mods/09-impact.js"
_js_src = io.open(REPO / _JS, encoding="utf-8").read()
_i = _js_src.index("async function oeConsistency(){")
_j = _js_src.index("\n}\n", _i)
_js_fn = _js_src[_i:_j]

# 判据用「是否真的又建了本地映射」而非「是否出现这个名字」——
# 函数里保留了一行说明「为什么移除 TYPE_LABEL」的注释，按字面匹配会误报（2026-09-23 实测）。
_js_fn_code = re.sub(r"^\s*//.*$", "", _js_fn, flags=re.M)          # 去整行注释
_js_fn_code = re.sub(r"/\*.*?\*/", "", _js_fn_code, flags=re.S)     # 去块注释
_js_local_map = bool(re.search(r"(?:const|let|var)\s+TYPE_LABEL\s*=", _js_fn_code)) \
    or bool(re.search(r"\bTYPE_LABEL\s*[\[.]", _js_fn_code))

check("O4 前端 oeConsistency 不再自持 code→中文清单（无 TYPE_LABEL 赋值/引用）",
      not _js_local_map,
      "仍存在本地映射" if _js_local_map else "仅注释提及移除原因，无实际映射")
check("O5 前端渲染走后端 x.label（单一真源）",
      "x.label" in _js_fn)
check("O6 前端回显口径 source / rule_version / ts（结论可复现）",
      "r.source" in _js_fn and "rule_version" in _js_fn and "r.ts" in _js_fn)
check("O7 前端渲染修复建议 x.fix（问题可自解释）",
      "x.fix" in _js_fn)


# ══════════════════════════════════════════════════════════════════════════════
hr("[2] 消费口径单一真源（ontology_semantics.active_rows）—— 夹具驱动")

from ontology_semantics import OntologyValidator, active_rows  # noqa: E402


def _fx_ontology(with_version=True, with_snapshot=True):
    """夹具库：编辑态与快照**内容故意不同**（EDIT_ONLY vs SNAP_ONLY）。

    断言对象是「口径机制」而非本机恰好有什么：只有两侧内容不同，
    「读到了快照」与「读到了编辑态」才可区分 —— 否则断言会殊途同归地通过。
    """
    import tempfile
    p = os.path.join(tempfile.gettempdir(), f"_ont_scope_fixture_{os.getpid()}.db")
    if os.path.exists(p):
        os.remove(p)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE ontology_types (
            id INTEGER PRIMARY KEY, name TEXT, type_kind TEXT, parent_id INTEGER,
            properties TEXT DEFAULT '{}', constraints TEXT DEFAULT '{}',
            description TEXT DEFAULT '', icon TEXT DEFAULT '', color TEXT DEFAULT '', iri TEXT DEFAULT '');
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY, entity_type TEXT, status TEXT DEFAULT 'reviewed',
            branch TEXT DEFAULT 'dev');
        CREATE TABLE ontology_versions (
            id INTEGER PRIMARY KEY, version_label TEXT, active INTEGER DEFAULT 0,
            status TEXT DEFAULT 'released');
        CREATE TABLE ontology_version_snapshots (
            id INTEGER PRIMARY KEY, version_id INTEGER, type_id INTEGER, name TEXT,
            type_kind TEXT, parent_id INTEGER, properties TEXT DEFAULT '{}',
            constraints TEXT DEFAULT '{}', description TEXT DEFAULT '', icon TEXT DEFAULT '',
            color TEXT DEFAULT '', iri TEXT DEFAULT '');
    """)
    # 编辑态：EDIT_ONLY / REL_EDIT
    c.executescript("""
        INSERT INTO ontology_types (id,name,type_kind,constraints) VALUES
          (1,'EDIT_ONLY','entity','{}'),
          (2,'REL_EDIT','relation','{"allowed_values":{"src":["EDIT_ONLY"],"tgt":["EDIT_ONLY"]}}');
    """)
    if with_version:
        c.execute("INSERT INTO ontology_versions (id,version_label,active,status) VALUES (7,'vX',1,'released')")
    if with_snapshot:
        c.executescript("""
            INSERT INTO ontology_version_snapshots
              (version_id,type_id,name,type_kind,constraints) VALUES
              (7,1,'SNAP_ONLY','entity','{}'),
              (7,2,'REL_SNAP','relation','{"allowed_values":{"src":["SNAP_ONLY"],"tgt":["SNAP_ONLY"]}}');
        """)
    c.commit()
    return c, p


class _NoCloseConn:
    """代理连接：close() 无操作（被测函数会在 finally 里 close 掉传入连接）。"""

    def __init__(self, c):
        self._c = c

    def __getattr__(self, k):
        return getattr(self._c, k)

    def close(self):
        pass


def _names(rows):
    return {r["name"] if not isinstance(r, dict) else r["name"] for r in rows}


_fx, _fx_path = _fx_ontology()
try:
    _snap_names = {r["name"] for r in active_rows(_fx)}
    check("O8 ★ active 版本有快照 → active_rows 读快照（编辑态内容不可见）",
          "SNAP_ONLY" in _snap_names and "EDIT_ONLY" not in _snap_names,
          f"names={sorted(_snap_names)}")

    _v_names = {t for t in {r["name"] for r in OntologyValidator(_fx)._load_types().values()}}
    check("O9 ★ OntologyValidator 默认口径 = active_rows（而非编辑态）",
          "SNAP_ONLY" in _v_names and "EDIT_ONLY" not in _v_names,
          f"names={sorted(_v_names)}")

    from routers.knowledge_parts import shared as _shared
    _shared_names = {r["name"] for r in _shared._active_ont_rows(_fx)}
    check("O10 shared._active_ont_rows 与 ontology_semantics.active_rows 同源同结果",
          _shared_names == _snap_names, f"{sorted(_shared_names)} == {sorted(_snap_names)}")

    _sc_rows, _sc_source, _sc_vid = _shared._ont_scope(_fx)
    check("O11 shared._ont_scope 有快照时 source='snapshot' 且回报 version_id",
          _sc_source == "snapshot" and _sc_vid == 7, f"source={_sc_source}, vid={_sc_vid}")

    _ed = _shared._ont_edit_rows(_fx)
    check("O12 shared._ont_edit_rows 返回编辑态（发布门禁拿待发布数据）",
          {r["name"] for r in _ed} == {"EDIT_ONLY", "REL_EDIT"},
          f"names={sorted(r['name'] for r in _ed)}")

    check("O13 显式 rows= 优先于默认口径（可校验指定版本）",
          "EDIT_ONLY" in {r["name"] for r in OntologyValidator(_fx, rows=_ed)._load_types().values()})
finally:
    _fx.close()
    if os.path.exists(_fx_path):
        os.remove(_fx_path)

# 无 active 版本 → 回退编辑态
_fx2, _fx2_path = _fx_ontology(with_version=False)
try:
    _n = {r["name"] for r in active_rows(_fx2)}
    check("O14 无 active 版本 → 回退编辑态（不消费空数据）",
          "EDIT_ONLY" in _n, f"names={sorted(_n)}")
finally:
    _fx2.close()
    if os.path.exists(_fx2_path):
        os.remove(_fx2_path)

# active 版本存在但无快照（存量 released / 快照治理上线前）→ 回退编辑态
_fx3, _fx3_path = _fx_ontology(with_snapshot=False)
try:
    _n = {r["name"] for r in active_rows(_fx3)}
    check("O15 active 版本无快照 → 回退编辑态（不消费空数据）",
          "EDIT_ONLY" in _n, f"names={sorted(_n)}")
    _s2, _src2, _vid2 = __import__("routers.knowledge_parts.shared",
                                   fromlist=["x"])._ont_scope(_fx3)
    check("O16 active 版本无快照 → _ont_scope 口径回报 current（口径透明）",
          _src2 == "current" and _vid2 == 0, f"source={_src2}, vid={_vid2}")
finally:
    _fx3.close()
    if os.path.exists(_fx3_path):
        os.remove(_fx3_path)

# 本体版本表缺失（极早期库 / 残缺夹具）→ 回退编辑态、不抛
_fx4, _fx4_path = _fx_ontology(with_version=False)
try:
    _fx4.executescript("DROP TABLE ontology_versions; DROP TABLE ontology_version_snapshots;")
    _n = {r["name"] for r in active_rows(_fx4)}
    check("O17 本体版本表缺失 → 回退编辑态且不抛异常（优雅降级）",
          "EDIT_ONLY" in _n, f"names={sorted(_n)}")
finally:
    _fx4.close()
    if os.path.exists(_fx4_path):
        os.remove(_fx4_path)


# ══════════════════════════════════════════════════════════════════════════════
hr("[3] 真库金标：体检 high==0 且口径为已发布快照")

conn = ro_conn()
from routers.knowledge_parts.shared import _ontology_check  # noqa: E402

rep = _ontology_check(conn)
check("O18 ★ 真库体检 high==0（无阻断级问题）", rep["high"] == 0,
      f"total={rep['total']} high={rep['high']} warn={rep['warn']} low={rep['low']}")
check("O19 ★ 真库体检口径为 snapshot（与 AI 建模/导出吃同一份 schema）",
      rep["source"] == "snapshot" and rep["ontology_version_id"] > 0,
      f"source={rep['source']}, vid={rep['ontology_version_id']}")
check("O20 体检报告带 rule_version + ts（同输入→同结论）",
      rep["rule_version"] == RULES.RULE_VERSION and bool(rep.get("ts")),
      f"rule_version={rep['rule_version']}, ts={rep['ts']}")

_scope_rows = active_rows(conn)
check("O21 体检 total == 消费口径类型行数（体检读的就是消费那份）",
      rep["total"] == len(_scope_rows), f"{rep['total']} == {len(_scope_rows)}")

for _x in rep["issues"]:
    print(f"      · [{_x['severity']}] {_x['label']} — {_x['name']}: {_x['message']}")


# ══════════════════════════════════════════════════════════════════════════════
hr("[4] 真库金标：存量边全量重校验 0 非法")

_EDGE_SQL = """
SELECT r.id, r.branch, r.relation_type, e1.entity_type AS st, e2.entity_type AS tt
  FROM relations r
  LEFT JOIN entities e1 ON e1.id=r.source_id AND e1.branch=r.branch
  LEFT JOIN entities e2 ON e2.id=r.target_id AND e2.branch=r.branch
"""
_edges = conn.execute(_EDGE_SQL).fetchall()
_edge_total = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]

_bad = []
_unresolved = []
for _e in _edges:
    if not _e["st"] or not _e["tt"]:
        _unresolved.append(_e["id"])
        continue
    _errs = OntologyValidator(conn).validate_edge(_e["st"], _e["relation_type"], _e["tt"])
    if _errs:
        _bad.append((_e["id"], _e["branch"], _e["st"], _e["relation_type"], _e["tt"], _errs))

check("O22 ★ 存量边全量重校验 0 非法", not _bad,
      f"非法 {len(_bad)} 条" + (f"，例：{_bad[:2]}" if _bad else ""))
check("O23 ★ 校验覆盖面 == 存量边总数（防「查询写错→0 条检查」假绿）",
      len(_edges) == _edge_total and _edge_total > 0,
      f"实校 {len(_edges)} / 表内 {_edge_total} 条")
check("O24 存量边端点全部可解析到实体（无悬空边）", not _unresolved,
      f"未解析 {len(_unresolved)} 条：{_unresolved[:5]}")


# ══════════════════════════════════════════════════════════════════════════════
hr("[5] AI 建模消费流程：注入口径 / 含修复后类型 / 无悬空名 / 约束自洽")

# 5a 注入口径：用一个「编辑态≠快照」的夹具证明注入吃的是快照（可被击穿）
_fx5, _fx5_path = _fx_ontology()
try:
    import database as _db
    import agent.pipeline_parts.prompt as _prompt_mod
    _orig_get_db = _db.get_db
    _db.get_db = lambda: _NoCloseConn(_fx5)
    try:
        _hint = _prompt_mod.PromptMixin()._build_ontology_hint()
    finally:
        _db.get_db = _orig_get_db
    check("O25 ★ AI 建模注入口径 = 消费口径（编辑态≠快照时注入的是快照）",
          "SNAP_ONLY" in _hint and "EDIT_ONLY" not in _hint and "REL_SNAP" in _hint,
          f"hint={_hint[:120]!r}")
finally:
    _fx5.close()
    if os.path.exists(_fx5_path):
        os.remove(_fx5_path)

# 5b 真库注入文本
_hint_real = OntologyValidator(conn).schema_text()
check("O26 AI 注入文本非空（本体 schema 真的注进去了）", bool(_hint_real.strip()),
      f"{len(_hint_real)} 字符")

_ty = OntologyValidator(conn)
_rel_names = set(_ty.relation_types())
_ent_names = set(_ty.entity_types())
_injected_rel = set()
if "关系类型: " in _hint_real:
    _line = _hint_real.split("关系类型: ", 1)[1].split("\n", 1)[0]
    _injected_rel = {x.strip() for x in _line.split(",") if x.strip()}
check("O27 注入文本含全部消费口径关系类型（修复后类型确实注入了）",
      _injected_rel == _rel_names and bool(_rel_names),
      f"注入 {len(_injected_rel)} / 应含 {len(_rel_names)}")

# 5c 悬空名判定（硬判据，独立于 [3] 的 bad_dom_range）
_dangling = {}
for _r in _scope_rows:
    if _r["type_kind"] != "relation":
        continue
    _src, _tgt = _declared(_r["constraints"])
    _miss = sorted({x for x in _src + _tgt if x not in _ent_names})
    if _miss:
        _dangling[_r["name"]] = _miss
check("O28 ★ 注入文本不含悬空名（关系 src/tgt 全在本体实体类型内）", not _dangling,
      f"悬空关系 {len(_dangling)} 个：{_dangling}" if _dangling else f"{len(_rel_names)} 个关系全部落地")

# 5d 约束自洽：每个关系的每个 (src,tgt) 组合自身可过校验（约束不是自相矛盾的）
_inconsistent = []
for _r in _scope_rows:
    if _r["type_kind"] != "relation":
        continue
    _src, _tgt = _declared(_r["constraints"])
    for _s in _src:
        for _g in _tgt:
            if _ty.validate_edge(_s, _r["name"], _g):
                _inconsistent.append((_r["name"], _s, _g))
check("O29 约束自洽：每条关系的每个 (src,tgt) 组合自身可通过校验",
      not _inconsistent, f"矛盾组合 {len(_inconsistent)} 个：{_inconsistent[:3]}")


# ══════════════════════════════════════════════════════════════════════════════
hr("[6] 好本体必过 / 坏样本必挂（6 类问题码各一个坏样本）")


def _bad_sample(rows):
    """在夹具库上跑体检：返回 code → issue 列表。"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("CREATE TABLE entities (id INTEGER PRIMARY KEY, entity_type TEXT, "
                    "status TEXT DEFAULT 'reviewed', branch TEXT DEFAULT 'dev');")
    try:
        r = _ontology_check(c, rows=rows)
    finally:
        c.close()
    out = {}
    for x in r["issues"]:
        out.setdefault(x["code"], []).append(x)
    return out


_GOOD = [
    {"id": 1, "name": "E1", "type_kind": "entity", "parent_id": None, "constraints": "{}"},
    {"id": 2, "name": "E2", "type_kind": "entity", "parent_id": None, "constraints": "{}"},
    {"id": 3, "name": "R_OK", "type_kind": "relation", "parent_id": None,
     "constraints": json.dumps({"allowed_values": {"src": ["E1"], "tgt": ["E2"]}})},
]
_g = _bad_sample(_GOOD)
check("O30 好样本 → 0 个 high（不误报）",
      not [x for v in _g.values() for x in v if x["severity"] == "high"],
      f"codes={sorted(_g)}")

_bad = _bad_sample(_GOOD + [
    {"id": 4, "name": "R_DANGLE", "type_kind": "relation", "parent_id": None,
     "constraints": json.dumps({"allowed_values": {"src": ["不存在的类型X"], "tgt": ["E2"]}})},
])
_iss = _bad.get("bad_dom_range", [])
check("O31 坏样本：悬空 src/tgt → bad_dom_range 且 severity=high",
      bool(_iss) and _iss[0]["severity"] == "high" and "不存在的类型X" in _iss[0]["message"],
      f"{_iss[0]['message'] if _iss else '未报警'}")

_iss = _bad_sample(_GOOD + [
    {"id": 5, "name": "R_NODR", "type_kind": "relation", "parent_id": None, "constraints": "{}"}]).get(
    "missing_dom_range", [])
check("O32 坏样本：关系无 src/tgt → missing_dom_range", bool(_iss),
      f"{_iss[0]['message'] if _iss else '未报警'}")

_iss = _bad_sample(_GOOD + [
    {"id": 6, "name": "E_CYCLE", "type_kind": "entity", "parent_id": 7, "constraints": "{}"},
    {"id": 7, "name": "E_CYCLE2", "type_kind": "entity", "parent_id": 6, "constraints": "{}"}]).get(
    "cycle", [])
check("O33 坏样本：循环继承 → cycle 且 high",
      bool(_iss) and _iss[0]["severity"] == "high", f"{_iss[0]['message'] if _iss else '未报警'}")

_iss = _bad_sample(_GOOD + [
    {"id": 8, "name": "E_ORPHAN", "type_kind": "entity", "parent_id": 999, "constraints": "{}"}]).get(
    "dangling_parent", [])
check("O34 坏样本：悬空父类 → dangling_parent 且 high",
      bool(_iss) and _iss[0]["severity"] == "high", f"{_iss[0]['message'] if _iss else '未报警'}")

_iss = _bad_sample(_GOOD + [
    {"id": 9, "name": "E1", "type_kind": "entity", "parent_id": None, "constraints": "{}"}]).get(
    "duplicate", [])
check("O35 坏样本：同名同类型重复 → duplicate 且 high",
      bool(_iss) and _iss[0]["severity"] == "high", f"{_iss[0]['message'] if _iss else '未报警'}")

_iss = _bad_sample([
    {"id": 1, "name": "E_ONLY", "type_kind": "entity", "parent_id": None, "constraints": "{}"}]).get(
    "isolated", [])
check("O36 坏样本：无实例无子类 → isolated（low，刻意不阻断发布）",
      bool(_iss) and _iss[0]["severity"] == "low", f"{_iss[0]['message'] if _iss else '未报警'}")


def _log_informative(before_json, after_json):
    """变更留痕是否**有信息量**：before 必须真的不同于 after。

    2026-09-23 教训：本判据的**前身**只断言「存在一条 update 留痕」——
    而当时 10 条留痕因浅拷贝 bug 全部 `before == after`（等于没记录修复前状态），
    旧判据照样 PASS，是一条**空转断言**。现把判据改成「留痕必须真的记录了一次变更」，
    并用坏样本自证可被击穿（O37）。
    """
    b = list(_declared(before_json)[0]) + list(_declared(before_json)[1])
    a = list(_declared(after_json)[0]) + list(_declared(after_json)[1])
    if not b and not a:
        return False, "before/after 均无端点（不是 allowed_values 形状）"
    if before_json == after_json:
        return False, "before == after（没记录到任何变更）"
    if sorted(b) == sorted(a):
        return False, "before/after 端点集合相同（只改了顺序），无信息量"
    return True, ""


_GOOD_LOG = json.dumps({"allowed_values": {"src": ["E1"], "tgt": ["E2"]}})
_BAD_LOG = json.dumps({"allowed_values": {"src": ["不存在的类型X"], "tgt": ["E2"]}})
check("O37 留痕判据可被击穿：before==after 判为「无信息量」，真实变更判为「有信息量」",
      _log_informative(_BAD_LOG, _BAD_LOG)[0] is False
      and _log_informative(_BAD_LOG, _GOOD_LOG)[0] is True,
      _log_informative(_BAD_LOG, _BAD_LOG)[1])


# ══════════════════════════════════════════════════════════════════════════════
hr("[7] 修复目标幂等 + 快照补齐 + 变更留痕")

# 按文件路径加载修复脚本（不往 sys.path 塞 tools/ —— 那儿模块名杂乱，易发生影子导入）
import importlib.util  # noqa: E402

_rep_spec = importlib.util.spec_from_file_location(
    "_ontology_dom_range_repair", str(REPO / "tools" / "_ontology_dom_range_repair.py"))
_rep_mod = importlib.util.module_from_spec(_rep_spec)
_rep_spec.loader.exec_module(_rep_mod)

_not_done = []
for _name, _tgt in _rep_mod.TARGETS.items():
    _row = conn.execute("SELECT constraints FROM ontology_types WHERE name=? AND type_kind='relation'",
                        (_name,)).fetchone()
    if not _row:
        _not_done.append((_name, "类型不存在"))
        continue
    _src, _tgt_now = _declared(_row["constraints"])
    if sorted(_src) != sorted(_tgt["src"]) or sorted(_tgt_now) != sorted(_tgt["tgt"]):
        _not_done.append((_name, f"src={_src} tgt={_tgt_now}"))
check(f"O38 修复目标 {len(_rep_mod.TARGETS)} 个关系类型在真库中已全部达标（脚本可幂等重跑）",
      not _not_done, f"未达标：{_not_done}" if _not_done else "全部达标")

_act = conn.execute("SELECT id, version_label, snapshot_count FROM ontology_versions "
                    "WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
_snap_n = conn.execute("SELECT COUNT(*) FROM ontology_version_snapshots WHERE version_id=?",
                       (_act["id"],)).fetchone()[0] if _act else 0
check("O39 active 版本快照行数 == 消费口径类型行数（快照未缺行，否则回退掩盖分裂）",
      bool(_act) and _snap_n == len(_scope_rows),
      f"v{_act['id']}({_act['version_label']}) 快照 {_snap_n} / 类型 {len(_scope_rows)}")

_logged = []
_log_bad = []
_log_no_dangle = []
for _name in sorted(_rep_mod.TARGETS):
    _tid = conn.execute("SELECT id FROM ontology_types WHERE name=? AND type_kind='relation'",
                        (_name,)).fetchone()
    if not _tid:
        _logged.append((_name, "无类型"))
        continue
    _lg = conn.execute("SELECT before, after FROM ontology_change_logs WHERE type_id=? AND action='update' "
                       "ORDER BY id DESC LIMIT 1", (_tid["id"],)).fetchone()
    if not _lg:
        _logged.append((_name, "无留痕"))
        continue
    _ok, _why = _log_informative(_lg["before"], _lg["after"])
    if not _ok:
        _log_bad.append((_name, _why))
        continue
    # before 里应当还留着「修复前悬空、修复后消失」的名字 —— 证明记录的确是修复前状态
    _b = list(_declared(_lg["before"])[0]) + list(_declared(_lg["before"])[1])
    if not [x for x in _b if x not in _ent_names]:
        _log_no_dangle.append(_name)
check("O40 ★ 留痕记录的是**真修复前状态**：before≠after 且 9 条 before 含已消失的悬空名",
      not _logged and not _log_bad and sorted(_log_no_dangle) == ["VERIFIED_BY"],
      f"缺留痕 {_logged}；无信息量 {_log_bad}；before 无悬空名的 = {sorted(_log_no_dangle)}"
      f"（应恰为 ['VERIFIED_BY' —— 它修复前本就无悬空，只是 src 增量补齐]）")

# 发布门禁必须校验**待发布的那份数据**（编辑态），而不是旧快照 —— 决策 D2 的配套约束。
# 否则「改了本体 → 拿旧快照体检 → 带病放行」，正是 D2 要消灭的失败形态。
# 唯一允许的裸调用是体检端点 `/validate`（它就该用消费口径，与 AI 建模看到的一致）。
_ov_src = io.open(REPO / "routers/knowledge_parts/ontology_version.py", encoding="utf-8").read()
_n_gate = _ov_src.count("_ontology_check(conn, rows=_ont_edit_rows(conn))")
_i0 = _ov_src.index("def ontology_validate(")
_validate_body = _ov_src[_i0:_ov_src.index("\n@router", _i0)]
_bare_elsewhere = [
    ln for ln in re.findall(r".*_ontology_check\(conn\).*", _ov_src)
    if ln not in _validate_body
]
check("O41 发布门禁 2 处显式传编辑态；裸口径调用只出现在体检端点 /validate",
      _n_gate == 2 and "_ontology_check(conn)" in _validate_body and not _bare_elsewhere,
      f"编辑态调用 {_n_gate} 处 / 体检端点裸调用={'有' if '_ontology_check(conn)' in _validate_body else '无'}"
      f" / 其它裸调用 {len(_bare_elsewhere)} 处")


# ══════════════════════════════════════════════════════════════════════════════
hr("[8] 变异自证：把 5 处改动各自还原 → 对应断言必须 FAIL")

if os.environ.get("ODR_MUT_CHILD") == "1":
    print("（变异子进程：跳过 [8] 段）")
    print()
    print("=" * 90)
    print(f"结果：{OK} pass / {FAIL} fail")
    print("=" * 90)
    sys.exit(1 if FAIL else 0)

MUT = [
    ("还原 OntologyValidator 默认为「读编辑态」（口径分裂复发）",
     "ontology_semantics.py",
     """            if self._pre_rows is not None:
                rows = self._pre_rows
            else:
                rows = active_rows(self.conn)""",
     """            if self._pre_rows is not None:
                rows = self._pre_rows
            else:
                rows = self.conn.execute("SELECT * FROM ontology_types").fetchall()""",
     ["O9 ★ OntologyValidator 默认口径 = active_rows（而非编辑态）",
      "O25 ★ AI 建模注入口径 = 消费口径（编辑态≠快照时注入的是快照）"]),

    ("还原 _ontology_check 缺省为「读编辑态」（体检与消费口径再分叉）",
     "routers/knowledge_parts/shared.py",
     """    if rows is None:
        rows, source, ovid = _ont_scope(conn)
    else:
        source, ovid = "current", 0""",
     """    if rows is None:
        rows, source, ovid = _ont_edit_rows(conn), "current", 0
    else:
        source, ovid = "current", 0""",
     ["O19 ★ 真库体检口径为 snapshot（与 AI 建模/导出吃同一份 schema）"]),

    ("把 bad_dom_range 降级为 low（高危问题被静默）",
     "core/ontology_rules.py",
     """        "label": "定义域/值域指向不存在的类型",
        "severity": "high",""",
     """        "label": "定义域/值域指向不存在的类型",
        "severity": "low",""",
     ["O31 坏样本：悬空 src/tgt → bad_dom_range 且 severity=high"]),

    ("前端恢复本地 TYPE_LABEL（前后端清单漂移复发）",
     "static/js/mods/09-impact.js",
     """async function oeConsistency(){
  const box = document.getElementById('oe-consistency'); if(!box) return;""",
     """async function oeConsistency(){
  const TYPE_LABEL = { bad_dom_range: 'DANGLING' };
  const box = document.getElementById('oe-consistency'); if(!box) return;""",
     ["O4 前端 oeConsistency 不再自持 code→中文清单（无 TYPE_LABEL 赋值/引用）"]),

    ("前端停止渲染修复建议 fix（问题不再自解释）",
     "static/js/mods/09-impact.js",
     "${x.fix?`<div style=\"font-size:11px;color:var(--mut);margin-top:3px;\">🛠 ${esc(x.fix)}</div>`:''}",
     "",
     ["O7 前端渲染修复建议 x.fix（问题可自解释）"]),
]

MUT_TAGS = sorted({t for _, _, _, _, tags in MUT for t in tags})

backup = {}


def _mutate(rel, old, new):
    """按文件原有换行约定写入变异 —— 锚点一律用 LF 书写，此处归一化。"""
    p = REPO / rel
    raw = p.read_bytes()
    crlf, lf = raw.count(b"\r\n"), raw.count(b"\n")
    assert crlf == 0 or crlf == lf, f"该文件混用换行符，变异写入不安全：{rel}"
    nl = "\r\n" if crlf else "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    assert old in text, f"变异锚点不存在（脚本自身错）：{rel}"
    p.write_bytes(text.replace(old, new, 1).replace("\n", nl).encode("utf-8"))


def _drop_pycache(rel):
    """清掉对应 .pyc，避免「同秒写回 + 同长度」时命中陈旧字节码（防御性）。"""
    p = REPO / rel
    d = p.parent / "__pycache__"
    if d.is_dir():
        for f in d.glob(p.stem + ".*.pyc"):
            try:
                f.unlink()
            except OSError:
                pass


seen_fail = []
try:
    for _, rel, old, new, _ in MUT:
        if rel not in backup:
            backup[rel] = (REPO / rel).read_bytes()
        _mutate(rel, old, new)
        _drop_pycache(rel)
    print(f"  已施加 {len(MUT)} 处变异，重跑 [1]~[7] 断言集…")
    r = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve())],
                       cwd=str(REPO), capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "ODR_MUT_CHILD": "1"})
    out = (r.stdout or "") + (r.stderr or "")
    for tag in MUT_TAGS:
        if re.search(r"\[FAIL\] " + re.escape(tag), out):
            seen_fail.append(tag)
        else:
            print(f"    !! 变异后该断言仍通过（=空转断言）：{tag}")
    check(f"O42 变异后全部 {len(MUT_TAGS)} 条相关断言转为 FAIL（断言可被击穿）",
          len(seen_fail) == len(MUT_TAGS), f"仅 {len(seen_fail)}/{len(MUT_TAGS)}")
finally:
    for rel, b in backup.items():
        (REPO / rel).write_bytes(b)
        _drop_pycache(rel)
    bad = [rel for rel, b in backup.items() if (REPO / rel).read_bytes() != b]
    check("O43 变异源文件已逐字节还原", not bad, f"未还原：{bad}" if bad else f"{len(backup)} 个文件一致")


# ══════════════════════════════════════════════════════════════════════════════
# 放在 [8] 之后：变异子进程在 [8] 开头就退出，故 [9] 只在父进程跑一次（省一次 app 导入）
hr("[9] 消费端点冒烟：本体相关 GET 端点无 4xx/5xx + 契约字段齐备")

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from main import app  # noqa: E402
    _cli = TestClient(app)
    _EPS = [
        "/api/knowledge/ontology/validate",
        "/api/knowledge/ontology/schema",
        "/api/knowledge/ontology/graph",
        "/api/knowledge/ontology/shacl",
        "/api/knowledge/ontology/export?fmt=owl",
        "/api/knowledge/ontology/version",
        f"/api/knowledge/ontology/version/{rep['ontology_version_id']}/snapshot",
        "/api/knowledge/ontology/binding",
        "/api/knowledge/ontology/changelog",
    ]
    _bad_ep = []
    for _e in _EPS:
        _r = _cli.get(_e)
        if _r.status_code >= 400:
            _bad_ep.append((_e, _r.status_code))
    check(f"O44 本体相关 GET 端点全部可用（{len(_EPS)} 个，无 4xx/5xx）",
          not _bad_ep, f"异常：{_bad_ep}" if _bad_ep else "全部 200")

    _v = _cli.get("/api/knowledge/ontology/validate").json()
    _need = {"code", "label", "severity", "dimension", "why", "fix", "message", "name"}
    _have = set((_v.get("issues") or [{}])[0].keys())
    check("O45 /validate 回包 issue 携带新契约字段（label/why/fix/dimension）",
          _need <= _have and bool(_v.get("rule_version")) and bool(_v.get("ts")),
          f"缺={sorted(_need - _have)}")
    check("O46 /validate 端点口径 = snapshot 且 high==0（与内部体检结论一致）",
          _v.get("source") == "snapshot" and _v.get("high") == 0,
          f"source={_v.get('source')}, high={_v.get('high')}")

    _s = _cli.get("/api/knowledge/ontology/schema").json()
    check("O47 /schema 返回非空本体文本（AI 建模消费端点可用）",
          len(_s.get("schema") or "") > 0, f"{len(_s.get('schema') or '')} 字符")

    _g = _cli.get("/api/knowledge/ontology/graph").json()
    _ge = [e for e in (_g.get("edges") or []) if e.get("kind") == "ontology_constraint"]
    check("O48 /graph 含本体类型节点与 dom/range 约束边（修复后的约束真的进图谱了）",
          bool(_g.get("nodes")) and bool(_ge),
          f"节点 {len(_g.get('nodes') or [])} / 约束边 {len(_ge)}")
except Exception as _e:  # pragma: no cover
    check("O44~O48 端点冒烟可运行", False, f"拉不起来：{type(_e).__name__}: {_e}")

conn.close()

print()
print("=" * 90)
print(f"结果：{OK} pass / {FAIL} fail")
if FAILED_NAMES:
    print("失败项：")
    for n in FAILED_NAMES:
        print(f"  - {n}")
print("=" * 90)
print("口径提示：O18~O24、O42~O46 为**真库/真实 app 金标**（依赖 mbse.db 现值：249 条边、active v170）；")
print("          O8~O17、O25、O30~O36 为夹具驱动（临时库/内存库），不受本机数据现状影响；")
print("          O38~O41 依赖 tools/_ontology_dom_range_repair.py 的 TARGETS 与真库一致；")
print("          O42/O43 为变异自证（会临时改写 4 个源文件并逐字节还原，跑完 git status 应无额外改动）。")
sys.exit(1 if FAIL else 0)
