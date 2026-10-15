# -*- coding: utf-8 -*-
"""本体一致性：dom/range 漂移修复 + 消费口径统一 + 判据补齐 的常驻自检（2026-09-23）。

按用户三项拍板与验收要求编写：
  · 决策 A（保语义修复）：悬空 dom/range → 换成卫星域等价类型，**保留**「关系允许哪些类型」的约束价值；
  · 决策 D2（消费最新快照）：体检 / AI 建模注入 / 导出 / 图谱 Schema 统一读 active 版本快照；
  · 决策 ③（不接受豁免）：删掉代码里「确认风险后豁免」的承诺。

验收金标（用户原话「修改后同步校验图谱数据，以及 AI 建模消费流程」）：
  ① `_ontology_check` 的 `high == 0`；
  ② 存量全部边（本库 249 条）全量重校验 **0 非法**；
  ③ AI 建模注入的 schema 文本包含修复后类型、**不含悬空名**。

2026-09-23 晚（第二批 O1-2 判据补齐）后追加的金标：
  ④ 规则 6 → 12 → **13 条**（2026-10-15 P0-5 增 `normalized_alias_relation`）；`info` 档
     有两个来源（一侧多声明 + 归一别名）→ 真库 `warn==0`、`info==`独立重算值；
  ⑤ `rule_data_conflict`（声明 vs 存量边，branch × project_id 二维切）真库 **0 命中**；
  ⑥ `degraded == []`（没有扫描被静默跳过 —— 安静地少查 = 假绿）。

本脚本**不依赖服务、不写库**（只读打开 mbse.db + 内存/临时库夹具），随时可跑：
    .venv/Scripts/python.exe tools/verify/verify_ontology_dom_range.py

分段：
  [1] 规则目录单一真源（core/ontology_rules.py）+ 前端不自持清单（防「6 种产 / 5 种映射」漂移复发）
  [2] 消费口径单一真源（ontology_semantics.active_rows）——夹具驱动，编辑态/快照内容**故意不同**
  [3] 真库金标：体检 high==0 / warn==0 / info==独立重算 / 口径==snapshot / degraded 为空
  [4] 真库金标：存量边全量重校验 0 非法（并断言**覆盖面 == 边总数**，防「0 检查」假绿）
  [5] AI 建模消费流程：注入文本口径正确、含修复后类型、无悬空名、约束自洽
  [6] 好本体必过 / 坏样本必挂（**每类问题码各造一个坏样本**）+ 目录↔实现双向覆盖
  [7] 存量实例全量模式（instances=True）：默认关闭、按需打开、真库 0 违例
  [8] 修复目标幂等（TARGETS 已全部达标）+ 快照补齐 + 变更留痕可追溯 + 发布门禁口径
  [9] 变异自证：把改动各自还原 → 对应断言必须 FAIL（证明断言不是空转）
  [10] 消费端点冒烟：本体相关 GET 端点无 4xx/5xx + /validate 契约字段齐备（含 instances=1）
      （用 TestClient 拉起真实 app，含 lifespan→init_db，属本仓库既有范式；
        放在 [9] 之后 → 变异子进程不会跑到）

⚠️ 换行符：本仓库混用 CRLF/LF（ontology_semantics.py / shared.py / prompt.py 是 CRLF，
   ontology_rules.py / 09-impact.js 是 LF）。[9] 段的变异写入按文件原约定归一化 ——
   直接用 LF 锚点去字节匹配 CRLF 文件会「锚点不存在」，极易误判成脚本自身写错。

⚠️ 编号纪律：**机械自检**在脚本末尾（无 O 编号那条）——O 号必须连续、按出现顺序严格递增，
   且 [9] 段引用的每个断言名都真实存在。历史上改编号漏改引用已经出过一次事故。
"""

# ── CI 豁免（2026-10-05 重新定性：B 类，不是 C 类）──────────
# CI-OPTIONAL: B 真库金标门禁 —— O20~O29/O52~O53/O62~O67 断言的是
#   **生产库现值**（249 条存量边、active v170、13 条规则、已发布快照），
#   干净库上必然不成立（O21 口径=snapshot / O27 存量边 0 非法 / O55 10 个关系类型达标）。
#   这不是代码退化，也不是门禁过期 ⇒ 保持本地真库运行，不进 CI。
#   本轮仍修掉两处真缺陷：① 硬编码 REPO/mbse.db（设 MBSE_DB_PATH 无效）
#   ② 快照端点用兜底 vid=170 拼 URL ⇒ 404 伪装成"端点坏了"。

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

# ⚠️ 2026-10-05 修订：原先硬编码 `REPO / "mbse.db"`
# ⇒ 设了 MBSE_DB_PATH 指向干净库时它**仍在读生产库**（本地绿 / CI 红，
#   同 verify_skill_injection、verify_plugin_bind_removed 那个坑，本项目第三次）。
# 改读配置：环境变量优先，缺省回退仓库内 mbse.db。
DB = Path(os.environ.get("MBSE_DB_PATH") or (REPO / "mbse.db"))
if not DB.is_absolute():
    DB = (REPO / DB).resolve()

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
hr("[1] 规则目录单一真源（core/ontology_rules.py）+ 前端不自持清单")

from core import ontology_rules as RULES  # noqa: E402
from core.relmap import REL_CANONICAL  # noqa: E402  （P0-5：别名判据真源，夹具名取自此表）

check("O1 规则目录非空，字段齐全，且 audit_catalog 自检为空",
      bool(RULES.RULES) and not RULES.audit_catalog(),
      f"{len(RULES.RULES)} 条：{sorted(RULES.RULES)}" + (f"；问题={RULES.audit_catalog()}"
                                                      if RULES.audit_catalog() else ""))

check("O2 severity 取值都在 VALID_SEVERITIES 内",
      all(v["severity"] in RULES.VALID_SEVERITIES for v in RULES.RULES.values()),
      f"档位={sorted(set(v['severity'] for v in RULES.RULES.values()))}")

check("O3 label 均为中文（避免界面直接露出英文 code）",
      all(_has_cjk(v["label"]) for v in RULES.RULES.values()),
      f"样例={RULES.label('bad_dom_range')}")

# 档位元数据（SEVERITY_LABELS / SEVERITY_ORDER）与 VALID_SEVERITIES 必须一一对应 ——
# 三者漂移会让前端渲染出空档位名（正是 TYPE_LABEL 那次事故的同一形态）。
check("O4 档位三件套一致：VALID_SEVERITIES == SEVERITY_LABELS.keys() == SEVERITY_ORDER.keys()",
      set(RULES.VALID_SEVERITIES) == set(RULES.SEVERITY_LABELS) == set(RULES.SEVERITY_ORDER),
      f"severities={sorted(RULES.VALID_SEVERITIES)} labels={sorted(RULES.SEVERITY_LABELS)}")

# 前端源码级守护：函数体不得再出现本地 code→中文 映射表
_JS = "static/js/mods/09-impact.js"
_js_src = io.open(REPO / _JS, encoding="utf-8").read()
_i = _js_src.index("async function oeConsistency")
_j = _js_src.index("\n}\n", _i)
_js_fn = _js_src[_i:_j]

# 判据用「是否真的又建了本地映射」而非「是否出现这个名字」——
# 函数里保留了一行说明「为什么移除 TYPE_LABEL」的注释，按字面匹配会误报（2026-09-23 实测）。
_js_fn_code = re.sub(r"^\s*//.*$", "", _js_fn, flags=re.M)          # 去整行注释
_js_fn_code = re.sub(r"/\*.*?\*/", "", _js_fn_code, flags=re.S)     # 去块注释
_js_local_map = bool(re.search(r"(?:const|let|var)\s+TYPE_LABEL\s*=", _js_fn_code)) \
    or bool(re.search(r"\bTYPE_LABEL\s*[\[.]", _js_fn_code))

check("O5 前端 oeConsistency 不再自持 code→中文清单（无 TYPE_LABEL 赋值/引用）",
      not _js_local_map,
      "仍存在本地映射" if _js_local_map else "仅注释提及移除原因，无实际映射")
check("O6 前端渲染走后端 x.label（单一真源）",
      "x.label" in _js_fn)
check("O7 前端回显口径 source / rule_version / ts / rule_count（结论可复现）",
      "r.source" in _js_fn and "rule_version" in _js_fn and "r.ts" in _js_fn)
check("O8 前端渲染修复建议 x.fix + 逐条 why（问题可自解释）",
      "x.fix" in _js_fn and "x.why" in _js_fn)
# 新增档位（info）若靠前端自持「高/低」二字，必然再漂移一次 → 档位名只能来自后端 severity_labels。
# 判据只抓**两种真实退化形态**：① 档位名作为字符串字面量（本地映射表，如 {high:'高危'}）；
# ② 形如「高危 ${...}」的插值标签（旧写法 `${sev?'高':'低'}` 的同类）。
# 刻意**不禁止散文里的档位词**（如空态文案「未发现…高危问题」）—— 那是文案不是清单，
# 一并禁止会让判据在无害改动上误报，正是本仓库反复强调的「断言不要过粗」。
_TIERS = ("高危", "数据冲突", "待补项", "设计说明")
_js_tier_literals = [t for t in _TIERS
                     if re.search(r"""['"`]%s['"`]""" % re.escape(t), _js_fn_code)
                     or (t + " ${") in _js_fn_code]
check("O9 ★ 前端档位名不硬编码（读 r.severity_labels / r.severity_order，无字面量档位名）",
      "severity_labels" in _js_fn and "severity_order" in _js_fn and not _js_tier_literals,
      f"代码内的档位名字面量={_js_tier_literals}")


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


_fx, _fx_path = _fx_ontology()
try:
    _snap_names = {r["name"] for r in active_rows(_fx)}
    check("O10 ★ active 版本有快照 → active_rows 读快照（编辑态内容不可见）",
          "SNAP_ONLY" in _snap_names and "EDIT_ONLY" not in _snap_names,
          f"names={sorted(_snap_names)}")

    _v_names = {t for t in {r["name"] for r in OntologyValidator(_fx)._load_types().values()}}
    check("O11 ★ OntologyValidator 默认口径 = active_rows（而非编辑态）",
          "SNAP_ONLY" in _v_names and "EDIT_ONLY" not in _v_names,
          f"names={sorted(_v_names)}")

    from routers.knowledge_parts import shared as _shared
    _shared_names = {r["name"] for r in _shared._active_ont_rows(_fx)}
    check("O12 shared._active_ont_rows 与 ontology_semantics.active_rows 同源同结果",
          _shared_names == _snap_names, f"{sorted(_shared_names)} == {sorted(_snap_names)}")

    _sc_rows, _sc_source, _sc_vid = _shared._ont_scope(_fx)
    check("O13 shared._ont_scope 有快照时 source='snapshot' 且回报 version_id",
          _sc_source == "snapshot" and _sc_vid == 7, f"source={_sc_source}, vid={_sc_vid}")

    _ed = _shared._ont_edit_rows(_fx)
    check("O14 shared._ont_edit_rows 返回编辑态（发布门禁拿待发布数据）",
          {r["name"] for r in _ed} == {"EDIT_ONLY", "REL_EDIT"},
          f"names={sorted(r['name'] for r in _ed)}")

    check("O15 显式 rows= 优先于默认口径（可校验指定版本）",
          "EDIT_ONLY" in {r["name"] for r in OntologyValidator(_fx, rows=_ed)._load_types().values()})
finally:
    _fx.close()
    if os.path.exists(_fx_path):
        os.remove(_fx_path)

# 无 active 版本 → 回退编辑态
_fx2, _fx2_path = _fx_ontology(with_version=False)
try:
    _n = {r["name"] for r in active_rows(_fx2)}
    check("O16 无 active 版本 → 回退编辑态（不消费空数据）",
          "EDIT_ONLY" in _n, f"names={sorted(_n)}")
finally:
    _fx2.close()
    if os.path.exists(_fx2_path):
        os.remove(_fx2_path)

# active 版本存在但无快照（存量 released / 快照治理上线前）→ 回退编辑态
_fx3, _fx3_path = _fx_ontology(with_snapshot=False)
try:
    _n = {r["name"] for r in active_rows(_fx3)}
    check("O17 active 版本无快照 → 回退编辑态（不消费空数据）",
          "EDIT_ONLY" in _n, f"names={sorted(_n)}")
    _s2, _src2, _vid2 = __import__("routers.knowledge_parts.shared",
                                   fromlist=["x"])._ont_scope(_fx3)
    check("O18 active 版本无快照 → _ont_scope 口径回报 current（口径透明）",
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
    check("O19 本体版本表缺失 → 回退编辑态且不抛异常（优雅降级）",
          "EDIT_ONLY" in _n, f"names={sorted(_n)}")
finally:
    _fx4.close()
    if os.path.exists(_fx4_path):
        os.remove(_fx4_path)


# ══════════════════════════════════════════════════════════════════════════════
hr("[3] 真库金标：体检 high==0 / warn==0 / info 独立重算 / 口径==snapshot")

conn = ro_conn()
from routers.knowledge_parts.shared import _ontology_check  # noqa: E402

rep = _ontology_check(conn)
check("O20 ★ 真库体检 high==0（无阻断级问题）", rep["high"] == 0,
      f"total={rep['total']} high={rep['high']} warn={rep['warn']} "
      f"low={rep['low']} info={rep['info']}")
check("O21 ★ 真库体检口径为 snapshot（与 AI 建模/导出吃同一份 schema）",
      rep["source"] == "snapshot" and rep["ontology_version_id"] > 0,
      f"source={rep['source']}, vid={rep['ontology_version_id']}")
check("O22 体检报告带 rule_version + ts + rule_count（同输入→同结论）",
      rep["rule_version"] == RULES.RULE_VERSION and bool(rep.get("ts"))
      and rep.get("rule_count") == len(RULES.RULES),
      f"rule_version={rep['rule_version']}, ts={rep['ts']}, rule_count={rep.get('rule_count')}")

_scope_rows = active_rows(conn)
check("O23 体检 total == 消费口径类型行数（体检读的就是消费那份）",
      rep["total"] == len(_scope_rows), f"{rep['total']} == {len(_scope_rows)}")

# O24/O25 是**独立重算**的金标，不是把当前输出抄进断言：
#   · warn==0：rule_data_conflict / dangling_instance 两条新规则在真库都必须是 0（修复完整的证据）；
#   · info 计数：从原始约束行**重新数**「一侧声明 >1 个类型」的类型个数，与报告比对。
check("O24 ★ 真库 warn==0（声明 vs 存量边 0 冲突；无未注册实例类型；无互斥特性/对称非成对）",
      rep["warn"] == 0, f"warn={rep['warn']}")

_exp_multi = 0
for _r in _scope_rows:
    _c = _json(_r["constraints"], {}) or {}
    if _r["type_kind"] == "relation":
        _av = _c.get("allowed_values") or {}
        _d, _g = _declared(_r["constraints"])
    elif _r["type_kind"] == "attribute":
        _d = _c.get("domain_classes") or []
        _d = _d if isinstance(_d, list) else ([_d] if _d else [])
        _g = _c.get("range_classes") or []
        _g = _g if isinstance(_g, list) else ([_g] if _g else [])
    else:
        continue
    if len([x for x in _d if str(x).strip()]) > 1 or len([x for x in _g if str(x).strip()]) > 1:
        _exp_multi += 1
# 2026-10-15（P0-5）：info 档现有**两个**来源 —— 一侧多声明 + 归一别名型关系。
# 独立重算须两个都数：只数其一会让断言口径与实现分裂（正是本脚本编号自检要防的那类病）。
# 数法与实现同源但**独立重写**：从约束行判「关系且两侧皆空」+「名字在 REL_CANONICAL 内」。
_exp_alias = 0
for _r in _scope_rows:
    if _r["type_kind"] != "relation":
        continue
    _d, _g = _declared(_r["constraints"])
    if not [x for x in _d if str(x).strip()] and not [x for x in _g if str(x).strip()] \
            and _r["name"] in REL_CANONICAL:
        _exp_alias += 1
_EXP_ALIAS = _exp_multi + _exp_alias
# 2026-10-15（P1-5）：info 档新增**第三个**来源 —— missing_inverse（有边但无反向声明）。
# 独立重算同步跟上：关系且有 dom/range、有存量边、非 symmetric、无 inverse_of。
_edge_cnt = {r["relation_type"]: r["c"] for r in conn.execute(
    "SELECT relation_type, COUNT(*) c FROM relations GROUP BY relation_type").fetchall()}
_exp_minv = 0
for _r in _scope_rows:
    if _r["type_kind"] != "relation":
        continue
    _d, _g = _declared(_r["constraints"])
    _c = _json(_r["constraints"], {}) or {}
    _ch = _c.get("characteristics") or []
    if (_d or _g) and _edge_cnt.get(_r["name"], 0) > 0 \
            and "symmetric" not in _ch and not str(_c.get("inverse_of") or "").strip():
        _exp_minv += 1
_EXP_INFO = _exp_multi + _exp_alias + _exp_minv
# ⚠️ 断言名里**不写具体数字**：末尾「编号自检」的正则按 `check(` + 引号 + `O数字` 匹配，
#   而「多声明 / 归一别名 / 缺反向」各多少条随真库变化 —— 数字进名字会让换环境即假红。
#   数字只出现在详情串里。MUT 的变异引用与本断言名**逐字一致**（自检会校验）。
#   （同理：本注释里也不得写出该正则的字面形态，否则会被自己当成一条真断言 → 编号重号。）
check("O25 ★ info 档计数 == 独立重算（多声明 + 归一别名 + 缺反向三来源之和）；info 是设计意图，须单独一档",
      rep["info"] == _EXP_INFO and rep["info"] > 0,
      f"报告 info={rep['info']} / 独立重算={_EXP_INFO}（多声明 {_exp_multi} + 别名 {_exp_alias} + 缺反向 {_exp_minv}）")

check("O26 ★ 体检无降级项（degraded 为空 = 该查的都查了），且 info 档 issue 契约齐备",
      rep["degraded"] == [] and all(
          all(k in x for k in ("code", "label", "severity", "dimension", "why", "fix"))
          for x in rep["issues"]),
      f"degraded={rep['degraded']}")

for _x in rep["issues"]:
    if _x["severity"] != "info":
        print(f"      · [{_x['severity']}] {_x['label']} — {_x['name']}: {_x['message']}")

print(f"      · （info {rep['info']} 条为「一侧多声明」的设计说明，不逐条打印）")


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

check("O27 ★ 存量边全量重校验 0 非法", not _bad,
      f"非法 {len(_bad)} 条" + (f"，例：{_bad[:2]}" if _bad else ""))
check("O28 ★ 校验覆盖面 == 存量边总数（防「查询写错→0 条检查」假绿）",
      len(_edges) == _edge_total and _edge_total > 0,
      f"实校 {len(_edges)} / 表内 {_edge_total} 条")
check("O29 存量边端点全部可解析到实体（无悬空边）", not _unresolved,
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
    check("O30 ★ AI 建模注入口径 = 消费口径（编辑态≠快照时注入的是快照）",
          "SNAP_ONLY" in _hint and "EDIT_ONLY" not in _hint and "REL_SNAP" in _hint,
          f"hint={_hint[:120]!r}")
finally:
    _fx5.close()
    if os.path.exists(_fx5_path):
        os.remove(_fx5_path)

# 5b 真库注入文本
_hint_real = OntologyValidator(conn).schema_text()
check("O31 AI 注入文本非空（本体 schema 真的注进去了）", bool(_hint_real.strip()),
      f"{len(_hint_real)} 字符")

_ty = OntologyValidator(conn)
_rel_names = set(_ty.relation_types())
_ent_names = set(_ty.entity_types())
_injected_rel = set()
if "关系类型: " in _hint_real:
    _line = _hint_real.split("关系类型: ", 1)[1].split("\n", 1)[0]
    _injected_rel = {x.strip() for x in _line.split(",") if x.strip()}
check("O32 注入文本含全部消费口径关系类型（修复后类型确实注入了）",
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
check("O33 ★ 注入文本不含悬空名（关系 src/tgt 全在本体实体类型内）", not _dangling,
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
check("O34 约束自洽：每条关系的每个 (src,tgt) 组合自身可通过校验",
      not _inconsistent, f"矛盾组合 {len(_inconsistent)} 个：{_inconsistent[:3]}")


# ══════════════════════════════════════════════════════════════════════════════
hr(f"[6] 好本体必过 / 坏样本必挂（{len(RULES.RULES)} 类问题码各一个坏样本）")


def _fx_db(entities=(), relations=(), types=()):
    """内存夹具：按被测扫描**真正读到的列**建表。

    ⚠️ 2026-09-23 第二次同型事故的教训：夹具手抄 DDL 一旦漏列，被测代码里的静默兜底会把
    「查不动」伪装成「没问题」→ 断言以错误理由通过/失败（`documents.lifecycle_status`
    与 `entity_versions` 表各踩过一次）。故：① 列齐备；② 用 `degraded == []` 兜住
    「扫描被降级」——降级时断言当场失败，而不是悄悄少查。
    """
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY, name TEXT DEFAULT '', entity_type TEXT,
            properties TEXT DEFAULT '{}', status TEXT DEFAULT 'reviewed',
            branch TEXT DEFAULT 'dev', project_id TEXT DEFAULT 'p1');
        CREATE TABLE relations (
            id INTEGER PRIMARY KEY, source_id INTEGER, target_id INTEGER,
            relation_type TEXT, status TEXT DEFAULT 'reviewed',
            branch TEXT DEFAULT 'dev', project_id TEXT DEFAULT 'p1');
        CREATE TABLE ontology_types (
            id INTEGER PRIMARY KEY, name TEXT, type_kind TEXT, parent_id INTEGER,
            properties TEXT DEFAULT '{}', constraints TEXT DEFAULT '{}');
        CREATE TABLE swrl_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, comment TEXT DEFAULT '',
            body TEXT DEFAULT '', head TEXT DEFAULT '', priority INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1);
    """)
    for e in entities:
        c.execute("INSERT INTO entities (id,name,entity_type,properties,status,branch,project_id) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (e["id"], e.get("name", ""), e["entity_type"], e.get("properties", "{}"),
                   e.get("status", "reviewed"), e.get("branch", "dev"), e.get("project_id", "p1")))
    for r in relations:
        c.execute("INSERT INTO relations (id,source_id,target_id,relation_type,status,branch,project_id) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (r["id"], r["source_id"], r["target_id"], r["relation_type"],
                   r.get("status", "reviewed"), r.get("branch", "dev"), r.get("project_id", "p1")))
    for t in types:
        c.execute("INSERT INTO ontology_types (id,name,type_kind,parent_id,properties,constraints) "
                  "VALUES (?,?,?,?,?,?)",
                  (t["id"], t["name"], t["type_kind"], t.get("parent_id"),
                   t.get("properties", "{}"), t.get("constraints", "{}")))
    c.commit()
    return c


def _bad_sample(rows, entities=(), relations=(), instances=False):
    """在夹具库上跑体检：返回 (code → issue 列表, 完整报告)。

    rows 同时写进 `ontology_types` 表 —— 因为存量实例扫描（dangling_instance /
    check_instances）读的是**表**而不是入参 rows，不写表就会因缺数据而静默漏判。

    ⚠️ 副作用（有意为之）：把本次产出的 code 记进 `_SEEN_CODES` 覆盖台账，
    供 O50 断言「目录全集 ↔ 实现可触发」双向一致，省得每个用例手工收集。
    """
    c = _fx_db(entities=entities, relations=relations, types=rows)
    try:
        r = _ontology_check(c, rows=rows, instances=instances)
    finally:
        c.close()
    out = {}
    for x in r["issues"]:
        out.setdefault(x["code"], []).append(x)
    _SEEN_CODES.update(out)
    return out, r


# ── 基线夹具（好本体）──────────────────────────────────────────────────────
_SEEN_CODES = set()   # 覆盖台账：所有坏样本产出的 code（O50 用）
_T_E1 = {"id": 1, "name": "E1", "type_kind": "entity", "parent_id": None, "constraints": "{}"}
_T_E2 = {"id": 2, "name": "E2", "type_kind": "entity", "parent_id": None, "constraints": "{}"}
_T_ROK = {"id": 3, "name": "R_OK", "type_kind": "relation", "parent_id": None,
          "constraints": json.dumps({"allowed_values": {"src": ["E1"], "tgt": ["E2"]}})}
_GOOD = [_T_E1, _T_E2, _T_ROK]
# 端点齐备、与声明一致 → good fixture 不含 rule_data_conflict，也不含 dangling_instance
_E_OK = [{"id": 1, "name": "e1", "entity_type": "E1"}, {"id": 2, "name": "e2", "entity_type": "E2"}]
_R_OK = [{"id": 1, "source_id": 1, "target_id": 2, "relation_type": "R_OK"}]

_g, _grep = _bad_sample(_GOOD, entities=_E_OK, relations=_R_OK)
check("O35 好样本 → 0 个 high，且体检无降级项（夹具列齐备 → 已查的都查了）",
      not [x for v in _g.values() for x in v if x["severity"] == "high"] and _grep["degraded"] == [],
      f"codes={sorted(_g)} degraded={_grep['degraded']}")

_bad, _ = _bad_sample(_GOOD + [
    {"id": 4, "name": "R_DANGLE", "type_kind": "relation", "parent_id": None,
     "constraints": json.dumps({"allowed_values": {"src": ["不存在的类型X"], "tgt": ["E2"]}})},
], entities=_E_OK, relations=_R_OK)
_iss = _bad.get("bad_dom_range", [])
check("O36 坏样本：悬空 src/tgt → bad_dom_range 且 severity=high",
      bool(_iss) and _iss[0]["severity"] == "high" and "不存在的类型X" in _iss[0]["message"],
      f"{_iss[0]['message'] if _iss else '未报警'}")

_iss = _bad_sample(_GOOD + [
    {"id": 5, "name": "R_NODR", "type_kind": "relation", "parent_id": None, "constraints": "{}"}],
    entities=_E_OK, relations=_R_OK)[0].get("missing_dom_range", [])
check("O37 坏样本：关系无 src/tgt → missing_dom_range", bool(_iss),
      f"{_iss[0]['message'] if _iss else '未报警'}")

# 2026-10-15（P0-5）：归一别名型关系的坏样本。紧随 O37，取号 38（编号须连续 1..N）。
#
# 夹具名取 **core.relmap.REL_CANONICAL 里真实存在的键**（不臆造名字）——
# 校验器正是拿这张表判别名，名字对不上等于测了另一条分支。
# 断言三点：① 能触发 ② severity=info（**不是 low**）
#   ③ 同一形状的**非别名**关系仍报 missing_dom_range —— 防「判据写太宽，把所有
#   无 dom/range 的关系都吞成 info」；③ 不过，①② 在判据过宽时照样会通过。
_alias_name = next(n for n in REL_CANONICAL if n not in {"连接", "执行"})
_iss_alias = _bad_sample(_GOOD + [
    {"id": 51, "name": _alias_name, "type_kind": "relation", "parent_id": None, "constraints": "{}"}],
    entities=_E_OK, relations=_R_OK)[0].get("normalized_alias_relation", [])
_iss_plain = _bad_sample(_GOOD + [
    {"id": 52, "name": "R_PLAIN", "type_kind": "relation", "parent_id": None, "constraints": "{}"}],
    entities=_E_OK, relations=_R_OK)[0].get("missing_dom_range", [])
check("O38 坏样本：归一别名型关系（无 dom/range）→ normalized_alias_relation 且 severity=info；"
      "同形状非别名关系仍报 missing_dom_range（判据未写宽）",
      bool(_iss_alias) and _iss_alias[0]["severity"] == "info" and bool(_iss_plain),
      f"别名={_alias_name} → alias={'触发' if _iss_alias else '未触发'}"
      f"(sev={_iss_alias[0]['severity'] if _iss_alias else '-'})；"
      f"非别名 R_PLAIN → missing_dom_range={'仍触发' if _iss_plain else '被误吞'}")

_iss = _bad_sample(_GOOD + [
    {"id": 6, "name": "E_CYCLE", "type_kind": "entity", "parent_id": 7, "constraints": "{}"},
    {"id": 7, "name": "E_CYCLE2", "type_kind": "entity", "parent_id": 6, "constraints": "{}"}],
    entities=_E_OK, relations=_R_OK)[0].get("cycle", [])
check("O39 坏样本：循环继承 → cycle 且 high",
      bool(_iss) and _iss[0]["severity"] == "high", f"{_iss[0]['message'] if _iss else '未报警'}")

_iss = _bad_sample(_GOOD + [
    {"id": 8, "name": "E_ORPHAN", "type_kind": "entity", "parent_id": 999, "constraints": "{}"}],
    entities=_E_OK, relations=_R_OK)[0].get("dangling_parent", [])
check("O40 坏样本：悬空父类 → dangling_parent 且 high",
      bool(_iss) and _iss[0]["severity"] == "high", f"{_iss[0]['message'] if _iss else '未报警'}")

_iss = _bad_sample(_GOOD + [
    {"id": 9, "name": "E1", "type_kind": "entity", "parent_id": None, "constraints": "{}"}],
    entities=_E_OK, relations=_R_OK)[0].get("duplicate", [])
check("O41 坏样本：同名同类型重复 → duplicate 且 high",
      bool(_iss) and _iss[0]["severity"] == "high", f"{_iss[0]['message'] if _iss else '未报警'}")

_iss = _bad_sample([{"id": 1, "name": "E_ONLY", "type_kind": "entity", "parent_id": None,
                     "constraints": "{}"}])[0].get("isolated", [])
check("O42 坏样本：无实例无子类 → isolated（low，刻意不阻断发布）",
      bool(_iss) and _iss[0]["severity"] == "low", f"{_iss[0]['message'] if _iss else '未报警'}")

# ── O1-2 新增的 5 类坏样本 ────────────────────────────────────────────────
_iss = _bad_sample(_GOOD + [
    {"id": 11, "name": "P_NODOM", "type_kind": "attribute", "parent_id": None,
     "constraints": "{}"}], entities=_E_OK, relations=_R_OK)[0].get("attr_domain_missing", [])
check("O43 坏样本：属性无 domain_classes → attr_domain_missing（low，属性侧此前完全无检查）",
      bool(_iss) and _iss[0]["severity"] == "low", f"{_iss[0]['message'] if _iss else '未报警'}")

_iss = _bad_sample(_GOOD + [
    {"id": 12, "name": "P_BADDOM", "type_kind": "attribute", "parent_id": None,
     "constraints": json.dumps({"domain_classes": ["不存在的类型Y"]})}],
    entities=_E_OK, relations=_R_OK)[0].get("bad_attr_domain", [])
check("O44 坏样本：属性 domain_classes 悬空 → bad_attr_domain 且 high",
      bool(_iss) and _iss[0]["severity"] == "high" and "不存在的类型Y" in _iss[0]["message"],
      f"{_iss[0]['message'] if _iss else '未报警'}")

_iss = _bad_sample(_GOOD + [
    {"id": 13, "name": "R_MULTI", "type_kind": "relation", "parent_id": None,
     "constraints": json.dumps({"allowed_values": {"src": ["E1", "E2"], "tgt": ["E2"]}})}],
    entities=_E_OK, relations=_R_OK)[0].get("multi_domain_range", [])
check("O45 坏样本：一侧声明多个类型 → multi_domain_range 且 severity=info（登记设计意图，不误报为缺陷）",
      bool(_iss) and _iss[0]["severity"] == "info", f"{_iss[0]['message'] if _iss else '未报警'}")

# rule_data_conflict：夹具里造一条**端点超出声明**的存量边（src 是 E2，声明只允许 E1）
_R_CONFLICT = _R_OK + [{"id": 2, "source_id": 2, "target_id": 1, "relation_type": "R_OK"}]
_bad2, _ = _bad_sample(_GOOD, entities=_E_OK, relations=_R_CONFLICT)
_iss = _bad2.get("rule_data_conflict", [])
check("O46 ★ 坏样本：存量边端点超出声明 → rule_data_conflict 且 warn，带边数与样例",
      bool(_iss) and _iss[0]["severity"] == "warn" and _iss[0].get("edge_count") == 1,
      f"{_iss[0]['message'][:90] if _iss else '未报警'}")

# 二维切（branch × project_id）：同一冲突出现在 2 个分支 → 必须**各出 1 条并带归属**，不能合并成 1 条。
# 这是本仓库的既有纪律：relations/entities 同时有 branch 与 project_id，只按一维切会掩盖另一维。
# 注意夹具必须让**每个分支各自有一套可解析的端点**（端点按 id+branch 解析）：
# personal 分支若直接复用 dev 的实体 id，端点会解析不到 → 扫描正确地跳过它 → 断言会假失败。
_E_2BR = [
    {"id": 1, "name": "e1", "entity_type": "E1", "branch": "dev"},
    {"id": 2, "name": "e2", "entity_type": "E2", "branch": "dev"},
    {"id": 3, "name": "e1p", "entity_type": "E1", "branch": "personal"},
    {"id": 4, "name": "e2p", "entity_type": "E2", "branch": "personal"},
]
_R_2BR = [
    {"id": 1, "source_id": 2, "target_id": 1, "relation_type": "R_OK", "branch": "dev"},
    {"id": 2, "source_id": 4, "target_id": 3, "relation_type": "R_OK", "branch": "personal"},
]
_iss = _bad_sample(_GOOD, entities=_E_2BR, relations=_R_2BR)[0].get("rule_data_conflict", [])
_branches = sorted({x.get("branch") for x in _iss})
check("O47 ★ rule_data_conflict 按 branch × project_id 二维切（2 分支各出 1 条并带归属，不合并）",
      len(_iss) == 2 and _branches == ["dev", "personal"]
      and all(x.get("project_id") == "p1" for x in _iss),
      f"条数={len(_iss)} 分支={_branches}")

_iss = _bad_sample(_GOOD, entities=_E_OK + [{"id": 9, "name": "ghost", "entity_type": "幽灵类型"}],
                   relations=_R_OK)[0].get("dangling_instance", [])
check("O48 坏样本：实体类型未注册 → dangling_instance 且 warn（issue.name 即未注册的类型名）",
      bool(_iss) and _iss[0]["severity"] == "warn" and _iss[0]["name"] == "幽灵类型"
      and "ghost" in _iss[0]["message"],
      f"{_iss[0]['message'] if _iss else '未报警'}")

# 逐实例约束（xsd）：只在 instances=True 时才查得到 —— 同时证明这个开关真的接上了
_P_BADVAL = {"id": 14, "name": "P_NUM", "type_kind": "attribute", "parent_id": None,
             "constraints": json.dumps({"xsd_type": "integer", "domain_classes": ["E1"]})}
_E_BADVAL = [{"id": 1, "name": "e1", "entity_type": "E1", "properties": json.dumps({"P_NUM": "abc"})},
             {"id": 2, "name": "e2", "entity_type": "E2"}]
_off = _bad_sample(_GOOD + [_P_BADVAL], entities=_E_BADVAL, relations=_R_OK)[0]
_on, _ = _bad_sample(_GOOD + [_P_BADVAL], entities=_E_BADVAL, relations=_R_OK, instances=True)
check("O49 ★ 坏样本：存量属性值不满足 xsd → instances=False 抓不到、instances=True 抓到",
      not _off.get("instance_constraint_violation") and bool(_on.get("instance_constraint_violation")),
      f"off={len(_off.get('instance_constraint_violation') or [])} 条 / "
      f"on={len(_on.get('instance_constraint_violation') or [])} 条")

_P_OKVAL = {"id": 15, "name": "P_NUM2", "type_kind": "attribute", "parent_id": None,
            "constraints": json.dumps({"xsd_type": "integer", "domain_classes": ["E1"]})}
_E_OKVAL = [{"id": 1, "name": "e1", "entity_type": "E1", "properties": json.dumps({"P_NUM2": "42"})},
            {"id": 2, "name": "e2", "entity_type": "E2"}]
_gg, _ggrep = _bad_sample(_GOOD + [_P_OKVAL], entities=_E_OKVAL, relations=_R_OK, instances=True)
check("O50 ★ 好样本在 instances=True 下也无 warn（逐实例模式不误报）",
      not [x for v in _gg.values() for x in v if x["severity"] in ("high", "warn")]
      and _ggrep["degraded"] == [],
      f"codes={sorted(_gg)} degraded={_ggrep['degraded']}")

# 目录 ↔ 实现双向一致：**所有**坏样本产出的 code 集合 == 目录全集
# （反向由 O1 的 audit_catalog 保证「每条目录项字段完备」，正向由此保证「每个 code 都可被触发」。
#  台账 `_SEEN_CODES` 由 `_bad_sample` 自动累积 —— 此前手工收集漏掉 9 条，属"断言写松"的典型。）
# ── P1-5 新增 3 类坏样本：**提前计算**（注册进 _SEEN_CODES 供 O51 覆盖断言），
# check() 断言在 O61~O63 —— 编号自检要求 check() 按文件序严格递增，
# 而覆盖断言要求样本先注册，故计算与断言分离（计算无副作用，仅跑内存夹具）。
_S_CONF = _bad_sample(_GOOD + [
    {"id": 16, "name": "R_CONF", "type_kind": "relation", "parent_id": None,
     "constraints": json.dumps({"allowed_values": {"src": ["E1"], "tgt": ["E2"]},
                                "characteristics": ["symmetric", "asymmetric"]})}],
    entities=_E_OK, relations=_R_OK)[0]
# P2-1（2026-10-15）：第二个互斥对 reflexive+asymmetric —— Pellet 实测该组合直接判本体
# inconsistent（真库 ALLOCATED_TO 即此伤），结构规则必须与 DL 推理器同判。
_S_CONF2 = _bad_sample(_GOOD + [
    {"id": 19, "name": "R_CONF2", "type_kind": "relation", "parent_id": None,
     "constraints": json.dumps({"allowed_values": {"src": ["E1"], "tgt": ["E2"]},
                                "characteristics": ["reflexive", "asymmetric"]})}],
    entities=_E_OK, relations=_R_OK)[0]
# P2-4（2026-10-15）：swrl_orphan —— active SWRL 规则引用本体不存在的类型（真库曾 4 条全灭）。
# _fx_db 已建 swrl_rules 表；坏样本插入一条引用 Person（不存在类型）的 active 死规则。
_S_SWRL = _bad_sample(_GOOD, entities=_E_OK, relations=_R_OK)[0]   # 先跑一遍空表基线
_c = _fx_db(entities=_E_OK, relations=_R_OK, types=_GOOD)
_c.execute("INSERT INTO swrl_rules (name, body, head, is_active) VALUES "
           "('DEAD_RULE', 'Person(?p), hasAge(?p, ?age)', 'isHuman(?p)', 1)")
_c.commit()
try:
    _r_swrl = _ontology_check(_c, rows=_GOOD)
finally:
    _c.close()
for _x in _r_swrl["issues"]:
    _S_SWRL.setdefault(_x["code"], []).append(_x)
_SEEN_CODES.update({k: v for k, v in _S_SWRL.items()})
_S_SYM = _bad_sample(_GOOD + [
    {"id": 17, "name": "R_SYM", "type_kind": "relation", "parent_id": None,
     "constraints": json.dumps({"allowed_values": {"src": ["E1"], "tgt": ["E2"]},
                                "characteristics": ["symmetric"]})}],
    entities=_E_OK,
    relations=_R_OK + [{"id": 3, "source_id": 1, "target_id": 2, "relation_type": "R_SYM"}])[0]
_S_DIR = _bad_sample(_GOOD + [
    {"id": 18, "name": "R_DIR", "type_kind": "relation", "parent_id": None,
     "constraints": json.dumps({"allowed_values": {"src": ["E1", "E2"], "tgt": ["E1", "E2"]}})}],
    entities=_E_OK,
    relations=_R_OK + [{"id": 4, "source_id": 1, "target_id": 2, "relation_type": "R_DIR"}])[0]

_covered = set(_SEEN_CODES)
check(f"O51 ★ 目录↔实现双向覆盖：坏样本产出的 code 集合 == 目录 {len(RULES.RULES)} 条（无僵尸规则）",
      _covered == set(RULES.RULES),
      f"未触发={sorted(set(RULES.RULES) - _covered)}；多出={sorted(_covered - set(RULES.RULES))}")


# ══════════════════════════════════════════════════════════════════════════════
hr("[7] 存量实例全量模式（instances=True）：默认关闭 / 按需打开 / 真库 0 违例")

check("O52 ★ 真库默认口径不含逐实例扫描（instances_checked=False：默认零性能回归）",
      rep["instances_checked"] is False and _ontology_check(conn, instances=True)["instances_checked"] is True,
      f"默认={rep['instances_checked']}")

_rep_full = _ontology_check(conn, instances=True)
check("O53 ★ 真库逐实例模式：0 违例且无降级项（存量实例满足当前约束）",
      not [x for x in _rep_full["issues"] if x["code"] == "instance_constraint_violation"]
      and _rep_full["degraded"] == [],
      f"degraded={_rep_full['degraded']}")

_off_codes = {(x["code"], x["name"]) for x in rep["issues"]}
_on_codes = {(x["code"], x["name"]) for x in _rep_full["issues"]}
check("O54 真库两口径问题集差集为空（当前无存量违例，故开不开都一样）",
      _on_codes == _off_codes,
      f"仅逐实例模式多出={sorted(_on_codes - _off_codes)}")

# 共享层必须**复用**既有校验器，不重写一份（否则又是"两处副本必然漂移"）。
# 注意判据要精确：shared.py 里出现 `cons.get("xsd_type")` 是**读取**该字段做展示，
# 不等于实现了 xsd 校验 —— 若按"出现 xsd_type 就算重写"来判，会误报（本条初版即如此）。
_shared_path = REPO / "routers" / "knowledge_parts" / "shared.py"
_shared_src = io.open(_shared_path, encoding="utf-8").read()
check("O55 ★ 共享层复用 services.ontology_migration 的现成校验器（不重复实现 xsd 判定）",
      "from services.ontology_migration import check_instances" in _shared_src
      and "from services.ontology_migration import scan_dangling_instances" in _shared_src
      and "_xsd_check" not in _shared_src and "def _xsd" not in _shared_src,
      f"复用 import {'齐' if 'check_instances' in _shared_src else '缺'}；"
      f"shared.py 内 xsd 判定实现={'有' if '_xsd_check' in _shared_src else '无'}")


# ══════════════════════════════════════════════════════════════════════════════
hr("[8] 修复目标幂等 + 快照补齐 + 变更留痕 + 发布门禁口径")

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
check(f"O56 修复目标 {len(_rep_mod.TARGETS)} 个关系类型在真库中已全部达标（脚本可幂等重跑）",
      not _not_done, f"未达标：{_not_done}" if _not_done else "全部达标")

_act = conn.execute("SELECT id, version_label, snapshot_count FROM ontology_versions "
                    "WHERE active=1 ORDER BY id DESC LIMIT 1").fetchone()
_snap_n = conn.execute("SELECT COUNT(*) FROM ontology_version_snapshots WHERE version_id=?",
                       (_act["id"],)).fetchone()[0] if _act else 0
check("O57 active 版本快照行数 == 消费口径类型行数（快照未缺行，否则回退掩盖分裂）",
      bool(_act) and _snap_n == len(_scope_rows),
      f"v{_act['id']}({_act['version_label']}) 快照 {_snap_n} / 类型 {len(_scope_rows)}")


def _log_informative(before_json, after_json):
    """变更留痕是否**有信息量**：before 必须真的不同于 after。

    2026-09-23 教训：本判据的**前身**只断言「存在一条 update 留痕」——
    而当时 10 条留痕因浅拷贝 bug 全部 `before == after`（等于没记录修复前状态），
    旧判据照样 PASS，是一条**空转断言**。现把判据改成「留痕必须真的记录了一次变更」，
    并用坏样本自证可被击穿（O58）。
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


_logged = []
_log_bad = []
_log_no_dangle = []
for _name in sorted(_rep_mod.TARGETS):
    _tid = conn.execute("SELECT id FROM ontology_types WHERE name=? AND type_kind='relation'",
                        (_name,)).fetchone()
    if not _tid:
        _logged.append((_name, "无类型"))
        continue
    # 2026-10-15（P1-5/R2）口径修订：不再只看「最新一条」留痕 —— 修复后的合法后续编辑
    # （如 R2 摘 ALLOCATED_TO 的 symmetric）会叠在修复留痕之上，把「最新」顶掉。
    # 本断言的本质是「修复留痕可回放」：历史中**存在**一条有信息量且 before 含修复前悬空名的留痕即可。
    _lg_all = conn.execute("SELECT before, after FROM ontology_change_logs WHERE type_id=? AND action='update'",
                           (_tid["id"],)).fetchall()
    if not _lg_all:
        _logged.append((_name, "无留痕"))
        continue
    _ok_rows = [_lg for _lg in _lg_all if _log_informative(_lg["before"], _lg["after"])[0]]
    if not _ok_rows:
        _log_bad.append((_name, "全部留痕 before==after"))
        continue
    _has_dangle = any([x for x in list(_declared(_lg["before"])[0]) + list(_declared(_lg["before"])[1])
                       if x not in _ent_names] for _lg in _ok_rows)
    if not _has_dangle and _name != "VERIFIED_BY":
        _log_no_dangle.append(_name)
check("O58 ★ 修复留痕可回放：每类修复在历史留痕中存在「before≠after 且 before 含修复前悬空名」的记录",
      not _logged and not _log_bad and not _log_no_dangle,
      f"缺留痕 {_logged}；无信息量 {_log_bad}；不可回放（历史留痕均不含修复前悬空名）= {sorted(_log_no_dangle)}"
      f"（VERIFIED_BY 修复前本就无悬空，只是 src 增量补齐，故豁免）")

_GOOD_LOG = json.dumps({"allowed_values": {"src": ["E1"], "tgt": ["E2"]}})
_BAD_LOG = json.dumps({"allowed_values": {"src": ["不存在的类型X"], "tgt": ["E2"]}})
check("O59 留痕判据可被击穿：before==after 判为「无信息量」，真实变更判为「有信息量」",
      _log_informative(_BAD_LOG, _BAD_LOG)[0] is False
      and _log_informative(_BAD_LOG, _GOOD_LOG)[0] is True,
      _log_informative(_BAD_LOG, _BAD_LOG)[1])

# 发布门禁必须校验**待发布的那份数据**（编辑态），而不是旧快照 —— 决策 D2 的配套约束。
# 否则「改了本体 → 拿旧快照体检 → 带病放行」，正是 D2 要消灭的失败形态。
# 唯一允许的裸调用是体检端点 `/validate`（它就该用消费口径，与 AI 建模看到的一致）。
_ov_src = io.open(REPO / "routers/knowledge_parts/ontology_version.py", encoding="utf-8").read()
# P1-1（2026-10-15）重构：publish/release 的编辑态检查收敛进 `_gate_block` 单一真源
# （此前两处内联 `_ontology_check(conn, rows=_ont_edit_rows(conn))`）。
# P1-4（2026-10-15）再重构：_gate_block → _gate_run（返回 (gate, chk)，修复成功路径 chk NameError
#   —— 旧 publish/release 回包引用不存在的 chk，一旦放行即 NameError）+ 放行时附带 CQ 结果。
# 锁的本质不变：① 门禁检查编辑态口径恰好 1 处（在 _gate_run 内）
# ② publish/release 两端点各调用 _gate_run 一次 ③ 裸口径调用只在 /validate。
_n_gate_site = _ov_src.count("_ontology_check(conn, rows=_ont_edit_rows(conn))")
_n_gate_call = _ov_src.count("gate, chk = _gate_run(conn)")
_i0 = _ov_src.index("def ontology_validate(")
_validate_body = _ov_src[_i0:_ov_src.index("\n@router", _i0)]
_bare_elsewhere = [
    ln for ln in re.findall(r".*_ontology_check\(conn\).*", _ov_src)
    if ln not in _validate_body
]
check("O60 发布门禁：编辑态口径检查恰好 1 处（_gate_run 单一真源）；"
      "publish/release 各接 1 次；成功路径带回 chk（NameError 修复）；裸口径调用只在 /validate",
      _n_gate_site == 1 and _n_gate_call == 2
      and "_check_brief(chk) if chk else {}" in _ov_src
      and "_ontology_check(conn)" in _validate_body and not _bare_elsewhere
      and "instances=True" in _validate_body and "scope=scope" in _validate_body,
      f"门禁编辑态检查 {_n_gate_site} 处 / _gate_block 接线 {_n_gate_call} 处"
      f" / 体检端点裸调用={'有' if '_ontology_check(conn)' in _validate_body else '无'}"
      f" / 其它裸调用 {len(_bare_elsewhere)} 处")


# ── P1-5（2026-10-15）新增 4 类坏样本断言（样本已在 O51 前计算注册）──────────
# 特性互斥：symmetric+asymmetric 同声明 → char_conflict 且 warn
_iss = _S_CONF.get("char_conflict", [])
_iss2 = _S_CONF2.get("char_conflict", [])
check("O61 坏样本：symmetric+asymmetric 与 reflexive+asymmetric 两个互斥对 → char_conflict 且 warn",
      bool(_iss) and _iss[0]["severity"] == "warn"
      and bool(_iss2) and _iss2[0]["severity"] == "warn",
      f"sym+asym: {_iss[0]['message'] if _iss else '未报警'}；"
      f"refl+asym: {_iss2[0]['message'] if _iss2 else '未报警'}")

# 对称非成对：声明 symmetric + 只有单向边 → symmetric_not_paired 且 warn（带未成对明细）
_iss = _S_SYM.get("symmetric_not_paired", [])
check("O62 坏样本：声明对称但边非成对 → symmetric_not_paired 且 warn，带未成对明细",
      bool(_iss) and _iss[0]["severity"] == "warn" and bool(_iss[0].get("unpaired")),
      f"{_iss[0]['message'][:90] if _iss else '未报警'}")

# 方向不可判：dom==rng 同集合 + 非 symmetric + 有边 → unspecified_direction 且 low
_iss = _S_DIR.get("unspecified_direction", [])
check("O63 坏样本：dom==rng 同集合且非对称有边 → unspecified_direction 且 low",
      bool(_iss) and _iss[0]["severity"] == "low",
      f"{_iss[0]['message'] if _iss else '未报警'}")

# 缺反向声明：有边、非 symmetric、无 inverse_of → missing_inverse 且 info。
# 好夹具 R_OK 本身即命中（有边、无反向声明）——这正是 info「按需补」定位：
# 好本体也可以带此提示，登记而非缺陷（与 multi_domain_range 同型）。
_iss = _g.get("missing_inverse", [])
check("O64 坏样本：有边但无反向声明 → missing_inverse 且 info（好本体也可带，登记而非缺陷）",
      bool(_iss) and _iss[0]["severity"] == "info" and _iss[0]["name"] == "R_OK",
      f"{_iss[0]['message'][:90] if _iss else '未报警'}")


# ══════════════════════════════════════════════════════════════════════════════
hr("[9] 变异自证：把改动各自还原 → 对应断言必须 FAIL")

if os.environ.get("ODR_MUT_CHILD") == "1":
    print("（变异子进程：跳过 [9] 段）")
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
     ["O11 ★ OntologyValidator 默认口径 = active_rows（而非编辑态）",
      "O30 ★ AI 建模注入口径 = 消费口径（编辑态≠快照时注入的是快照）"]),

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
     ["O21 ★ 真库体检口径为 snapshot（与 AI 建模/导出吃同一份 schema）"]),

    ("把 bad_dom_range 降级为 low（高危问题被静默）",
     "core/ontology_rules.py",
     """        "label": "定义域/值域指向不存在的类型",
        "severity": "high",""",
     """        "label": "定义域/值域指向不存在的类型",
        "severity": "low",""",
     ["O36 坏样本：悬空 src/tgt → bad_dom_range 且 severity=high"]),

    ("把 multi_domain_range 的 info 档降为 low（设计意图被混进待补项）",
     "core/ontology_rules.py",
     """        "label": "一侧声明多个类型（设计用法）",
        "severity": "info",""",
     """        "label": "一侧声明多个类型（设计用法）",
        "severity": "low",""",
     ["O25 ★ info 档计数 == 独立重算（多声明 + 归一别名 + 缺反向三来源之和）；info 是设计意图，须单独一档",
      "O45 坏样本：一侧声明多个类型 → multi_domain_range 且 severity=info（登记设计意图，不误报为缺陷）"]),

    # 2026-10-15（P0-5）：归一别名判据若失效（退回一律报 missing_dom_range），真库 low 会
    # 从 1 反弹到 3、info 少 2 条 → O25 的独立重算（别名 2 条）当场抓住。
    ("归一别名判据失效（别名关系被误报为 missing_dom_range 待补项）",
     "routers/knowledge_parts/shared.py",
     """                from core.relmap import REL_CANONICAL as _RC
                _is_alias = t["name"] in _RC""",
     """                from core.relmap import REL_CANONICAL as _RC
                _is_alias = False and t["name"] in _RC""",
     ["O25 ★ info 档计数 == 独立重算（多声明 + 归一别名 + 缺反向三来源之和）；info 是设计意图，须单独一档"]),

    ("前端恢复本地 TYPE_LABEL（前后端清单漂移复发）",
     "static/js/mods/09-impact.js",
     """async function oeConsistency(withInstances){
  const box = document.getElementById('oe-consistency'); if(!box) return;""",
     """async function oeConsistency(withInstances){
  const TYPE_LABEL = { bad_dom_range: 'DANGLING' };
  const box = document.getElementById('oe-consistency'); if(!box) return;""",
     ["O5 前端 oeConsistency 不再自持 code→中文清单（无 TYPE_LABEL 赋值/引用）"]),

    ("前端档位名改回硬编码（新增档位时必然再漂移一次）",
     "static/js/mods/09-impact.js",
     "    const sevLabel = k => SL[k] || k;",
     "    const sevLabel = k => ({high:'高危',warn:'提示',low:'低',info:'说明'})[k];",
     ["O9 ★ 前端档位名不硬编码（读 r.severity_labels / r.severity_order，无字面量档位名）"]),

    ("前端停止渲染修复建议 fix（问题不再自解释）",
     "static/js/mods/09-impact.js",
     "${x.fix?`<div style=\"font-size:11px;color:var(--mut);margin-top:3px;\">🛠 ${esc(x.fix)}</div>`:''}",
     "",
     ["O8 前端渲染修复建议 x.fix + 逐条 why（问题可自解释）"]),

    ("关掉 rule_data_conflict 扫描（§1.3 的守门人失效）",
     "routers/knowledge_parts/shared.py",
     "        for g in _scan_edge_conflicts(conn, decl):",
     "        for g in []:   # MUTATION",
     ["O46 ★ 坏样本：存量边端点超出声明 → rule_data_conflict 且 warn，带边数与样例"]),

    ("关掉属性域检查（属性侧重新变盲区）",
     "routers/knowledge_parts/shared.py",
     """        if not dom_l:
            issues.append(_issue("attr_domain_missing", t["name"],""",
     """        if False:
            issues.append(_issue("attr_domain_missing", t["name"],""",
     ["O43 坏样本：属性无 domain_classes → attr_domain_missing（low，属性侧此前完全无检查）"]),

    ("把 check_instances 变成空实现（逐实例体检静默失效，且不报降级）",
     "services/ontology_migration.py",
     """    out = []
    sql = "SELECT name, type_kind, properties, constraints FROM ontology_types\"""",
     """    return []   # MUTATION
    sql = "SELECT name, type_kind, properties, constraints FROM ontology_types\"""",
     ["O49 ★ 坏样本：存量属性值不满足 xsd → instances=False 抓不到、instances=True 抓到"]),

    # 2026-10-15（P1-5）：新规则的断言可被击穿（变异自证）
    ("把 char_conflict 降级为 low（特性矛盾被静默）",
     "core/ontology_rules.py",
     """        "label": "推理性质互斥声明",
        "severity": "warn",""",
     """        "label": "推理性质互斥声明",
        "severity": "low",""",
     ["O61 坏样本：symmetric+asymmetric 与 reflexive+asymmetric 两个互斥对 → char_conflict 且 warn"]),

    ("把 symmetric_not_paired 降级为 low（声明与数据打架被静默）",
     "core/ontology_rules.py",
     """        "label": "声明对称但存量边非成对",
        "severity": "warn",""",
     """        "label": "声明对称但存量边非成对",
        "severity": "low",""",
     ["O62 坏样本：声明对称但边非成对 → symmetric_not_paired 且 warn，带未成对明细"]),
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
    print(f"  已施加 {len(MUT)} 处变异，重跑 [1]~[8] 断言集…")
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
    check(f"O65 变异后全部 {len(MUT_TAGS)} 条相关断言转为 FAIL（断言可被击穿）",
          len(seen_fail) == len(MUT_TAGS), f"仅 {len(seen_fail)}/{len(MUT_TAGS)}")
finally:
    for rel, b in backup.items():
        (REPO / rel).write_bytes(b)
        _drop_pycache(rel)
    bad = [rel for rel, b in backup.items() if (REPO / rel).read_bytes() != b]
    check("O66 变异源文件已逐字节还原", not bad, f"未还原：{bad}" if bad else f"{len(backup)} 个文件一致")


# ══════════════════════════════════════════════════════════════════════════════
# 放在 [9] 之后：变异子进程在 [9] 开头就退出，故 [10] 只在父进程跑一次（省一次 app 导入）
hr("[10] 消费端点冒烟：本体相关 GET 端点无 4xx/5xx + 契约字段齐备")

# P0-2 辅助：体检历史行数（表不存在返回 None，旧库跳过不断言）
def _hl_count():
    """ontology_health_logs 行数；表不存在（P0-2 迁移前的库）返回 None。"""
    try:
        return conn.execute("SELECT COUNT(*) FROM ontology_health_logs").fetchone()[0]
    except Exception:
        return None

try:
    from fastapi.testclient import TestClient  # noqa: E402
    from main import app  # noqa: E402
    _cli = TestClient(app)
    _EPS = [
        "/api/knowledge/ontology/validate?record=0",
        "/api/knowledge/ontology/validate?instances=1&record=0",
        "/api/knowledge/ontology/schema",
        "/api/knowledge/ontology/graph",
        "/api/knowledge/ontology/shacl",
        "/api/knowledge/ontology/export?fmt=owl",
        "/api/knowledge/ontology/version",
        "/api/knowledge/ontology/binding",
        "/api/knowledge/ontology/changelog",
    ]
    # ⚠️ 2026-10-05：快照端点依赖"库里真有已发布版本"。
    # 不能用 `rep['ontology_version_id']` 判断 —— 它来自 `_ont_scope`，无版本时会回落到
    # 兜底常量（实测新库给出 170）⇒ 拼出 `/version/170/snapshot` ⇒ 404，
    # 看起来像"端点坏了"，其实是"这个版本根本不存在"。
    # ⇒ 直接查库里真实存在的版本 id；没有就记 SKIP（"还没发布过"不是缺陷）。
    _vid = None
    try:
        _vrow = conn.execute("SELECT id FROM ontology_versions ORDER BY id DESC LIMIT 1").fetchone()
        _vid = _vrow["id"] if _vrow else None
    except Exception:
        _vid = None
    if _vid:
        _EPS.insert(6, f"/api/knowledge/ontology/version/{_vid}/snapshot")
    else:
        print("  SKIP  /version/{id}/snapshot —— 本库尚无已发布本体版本（新库常态，非缺陷）")
    _bad_ep = []
    for _e in _EPS:
        _r = _cli.get(_e)
        if _r.status_code >= 400:
            _bad_ep.append((_e, _r.status_code))
    check(f"O67 本体相关 GET 端点全部可用（{len(_EPS)} 个，无 4xx/5xx）",
          not _bad_ep, f"异常：{_bad_ep}" if _bad_ep else "全部 200")

    # 2026-10-15（P0-2）★ 趋势线不被探针污染。
    # 背景：/validate 的体检落库最初无条件开启，实测跑一次本脚本（端点冒烟段 4 次 HTTP 调用）
    # 趋势线就多 4 行 —— 机器噪声把"人/Agent 的真实体检历史"冲掉，趋势不可读。
    # 故探针一律带 record=0；此处再用**不带** record 的两次调用验证默认行为，
    # 若有人把默认改回"无条件落库"，本断言立刻转红。
    _hl_b = _hl_count()
    _cli.get("/api/knowledge/ontology/validate")            # 默认 record=1 → 应当写 1 行
    _cli.get("/api/knowledge/ontology/validate?instances=1")
    _hl_a = _hl_count()
    _cli.get("/api/knowledge/ontology/validate?record=0")    # 显式关闭 → 不得写
    _cli.get("/api/knowledge/ontology/validate?instances=1&record=0")
    _hl_c = _hl_count()
    check("O68 ★ 体检历史不被探针污染：/validate 默认 record=1（真实体检留痕），"
          "探针须显式 record=0（不带则写库）—— 故探针调用全部带 record=0",
          # 默认(record=1) 写库 → +2 行；显式 record=0 的探针 → +0 行。
          # 两个方向都要成立：只测其一会漏掉"record=0 失效"与"默认被改成 0"两类回归。
          _hl_b is not None and _hl_a == _hl_b + 2 and _hl_c == _hl_a,
          f"默认 record=1：{_hl_b} → {_hl_a}（应 +2）；显式 record=0：{_hl_a} → {_hl_c}（应 +0）"
          + ("" if _hl_b is not None else "（本库无 health_logs 表，旧库跳过）"))

    _v = _cli.get("/api/knowledge/ontology/validate?record=0").json()
    _need = {"code", "label", "severity", "dimension", "why", "fix", "message", "name"}
    _have = set((_v.get("issues") or [{}])[0].keys())
    check("O69 /validate 回包 issue 携带新契约字段（label/why/fix/dimension）",
          _need <= _have and bool(_v.get("rule_version")) and bool(_v.get("ts")),
          f"缺={sorted(_need - _have)}")
    # ⚠️ 2026-10-05 修订：原判据硬要求 `source == "snapshot"`，而**全新库根本没发布过版本**
    # ⇒ 只能回落 current ⇒ 恒红，红因是"还没发布"不是"代码坏了"（与 verify_skill_injection 同型）。
    # 口径改为：有已发布版本 ⇒ 必须读 snapshot；没有 ⇒ 回落 current 是设计行为。
    # 真正的不变式是 **high==0**（本体自洽、可过发布门禁）——两环境都必须成立。
    # 编号不新增：拆成 O68b 会被「编号连续」自检判为重复 68，故并回一条。
    _src_ok = (_v.get("source") == "snapshot") if _vid else (_v.get("source") in ("current", "snapshot"))
    check("O70 /validate 口径正确且 high==0（有版本读快照 / 新库回落 current；本体须自洽）",
          _src_ok and _v.get("high") == 0,
          f"source={_v.get('source')}, high={_v.get('high')}, 有已发布版本={bool(_vid)}"
          "（high≠0 ⇒ 本体有悬空引用，发布门禁会拒绝）")

    _s = _cli.get("/api/knowledge/ontology/schema").json()
    check("O71 /schema 返回非空本体文本（AI 建模消费端点可用）",
          len(_s.get("schema") or "") > 0, f"{len(_s.get('schema') or '')} 字符")

    _g = _cli.get("/api/knowledge/ontology/graph").json()
    _ge = [e for e in (_g.get("edges") or []) if e.get("kind") == "ontology_constraint"]
    check("O72 /graph 含本体类型节点与 dom/range 约束边（修复后的约束真的进图谱了）",
          bool(_g.get("nodes")) and bool(_ge),
          f"节点 {len(_g.get('nodes') or [])} / 约束边 {len(_ge)}")

    _vi = _cli.get("/api/knowledge/ontology/validate?instances=1&record=0").json()
    check("O73 /validate?instances=1 回包 instances_checked=True 且档位元数据齐备",
          _vi.get("instances_checked") is True
          and set(_vi.get("severity_labels") or {}) == set(RULES.VALID_SEVERITIES)
          and set(_vi.get("severity_order") or {}) == set(RULES.VALID_SEVERITIES)
          and _vi.get("rule_count") == len(RULES.RULES),
          f"instances_checked={_vi.get('instances_checked')}, "
          f"rule_count={_vi.get('rule_count')}, degraded={_vi.get('degraded')}")

    # ══ [11] P1-4：CQ（能力问题）机制 ══════════════════════════════════════
    from services import ontology_cq as _ocq  # noqa: E402
    from services.ontology_cq import validate_query as _vq, run_cqs as _runq  # noqa: E402

    # O74 只读白名单（防注入判据，不是装饰）
    _reject = ["UPDATE entities SET status='x'", "PRAGMA table_info(entities)",
               "SELECT 1; DROP TABLE entities", "DELETE FROM relations",
               "ATTACH DATABASE 'x' AS y", "WITH a AS (SELECT 1) INSERT INTO entities DEFAULT VALUES"]
    _accept = ["SELECT 1", "  select id from entities  ", "WITH a AS (SELECT 1) SELECT * FROM a"]
    check("O74 ★ CQ 只读白名单：写操作/PRAGMA/ATTACH/多语句全拒，SELECT/WITH 放行",
          all(not _vq(q)[0] for q in _reject) and all(_vq(q)[0] for q in _accept),
          "拒绝=" + str([_vq(q)[1] for q in _reject if _vq(q)[0]])
          + " 误放=" + str([q for q in _accept if not _vq(q)[0]]))

    # O75 引擎判定：expected=empty / nonempty 两方向 + 非法 SQL → skip（降级可见，不假绿）
    _fc = sqlite3.connect(":memory:")
    _fc.row_factory = sqlite3.Row
    _fc.executescript("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT, entity_type TEXT, "
                      "branch TEXT DEFAULT 'dev', status TEXT DEFAULT 'active');"
                      "CREATE TABLE relations (id INTEGER PRIMARY KEY, source_id INTEGER, "
                      "target_id INTEGER, relation_type TEXT, branch TEXT DEFAULT 'dev');"
                      "CREATE TABLE ontology_questions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                      "question TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'sql', query TEXT NOT NULL, "
                      "expected TEXT NOT NULL DEFAULT 'empty', enabled INTEGER NOT NULL DEFAULT 1, "
                      "source TEXT NOT NULL DEFAULT 'user', created_by TEXT DEFAULT '', "
                      "created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);"
                      "CREATE TABLE ontology_cq_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                      "ts TEXT DEFAULT CURRENT_TIMESTAMP, question_id INTEGER NOT NULL, "
                      "source TEXT DEFAULT '', status TEXT NOT NULL, row_count INTEGER DEFAULT 0, "
                      "message TEXT DEFAULT '');")
    _fc.execute("INSERT INTO ontology_questions (question, kind, query, expected, enabled, source) "
                "VALUES ('空测试','sql','SELECT id FROM relations WHERE relation_type=''__不存在__''','empty',1,'fixture')")
    _fc.execute("INSERT INTO ontology_questions (question, kind, query, expected, enabled, source) "
                "VALUES ('非空测试','sql','SELECT id FROM entities','nonempty',1,'fixture')")
    _fc.execute("INSERT INTO ontology_questions (question, kind, query, expected, enabled, source) "
                "VALUES ('坏SQL','sql','UPDATE relations SET relation_type=1','empty',1,'fixture')")
    _fc.commit()
    _fc.execute("INSERT INTO entities (id, name, entity_type) VALUES (1, 'e1', 'E1')")
    _fc.execute("INSERT INTO relations (id, source_id, target_id, relation_type) VALUES (1, 1, 1, 'X')")
    _fc.commit()
    _cq_res = {x["question"]: x for x in _runq(_fc, source="fixture", record=False)}
    check("O75 ★ CQ 引擎判定：empty 空表=pass / nonempty 有行=pass / 非法 SQL=skip（降级不假绿）",
          _cq_res["空测试"]["status"] == "pass" and _cq_res["非空测试"]["status"] == "pass"
          and _cq_res["坏SQL"]["status"] == "skip" and "未通过只读校验" in _cq_res["坏SQL"]["message"],
          " / ".join(f"{k}={v['status']}" for k, v in _cq_res.items()))
    _fc.close()

    # O76 种子 CQ 在真库全绿（≥5 条；经 TestClient 走真实端点，顺带完成建库种子）
    _run_r = _cli.post("/api/knowledge/ontology/cqs/run", json={}).json()
    _cq_s = _run_r.get("summary") or {}
    check("O76 ★ 真库种子 CQ 全绿（≥5 条可执行且 pass，无 skip —— 端点完整性/类型注册全过）",
          (_cq_s.get("total") or 0) >= 5 and (_cq_s.get("failed") or 0) == 0
          and (_cq_s.get("skipped") or 0) == 0,
          f"total={_cq_s.get('total')} failed={_cq_s.get('failed')} skipped={_cq_s.get('skipped')} "
          + str([x["question"] for x in (_run_r.get("results") or []) if x["status"] != "pass"]))

    # O77 /validate 回包附 cq 节（record=0 探针也不落 CQ 历史 —— 同 O68 防污染纪律）
    _vq_rep = _cli.get("/api/knowledge/ontology/validate?record=0").json()
    _vq_cq = _vq_rep.get("cq") or {}
    check("O77 /validate 回包附 cq 节（validation 与 verification 同报告下发，且不混入四档计数）",
          _vq_cq.get("total", 0) >= 5 and not _vq_cq.get("error")
          and all(("failed" not in _vq_rep and "skipped" not in _vq_rep) for _ in [1]),
          f"cq.total={_vq_cq.get('total')} failed={_vq_cq.get('failed')} skipped={_vq_cq.get('skipped')}")

    # O78 CRUD 冒烟：登记 → 执行单题 → 修改 → 删除（真实 HTTP，数据不残留）
    _cr_add = _cli.post("/api/knowledge/ontology/cqs", json={
        "question": "【验证脚本临时】孤立实体测试", "kind": "sql",
        "query": "SELECT id FROM entities WHERE name='__no_such_entity__'", "expected": "empty"}).json()
    _cr_add2 = _cli.post("/api/knowledge/ontology/cqs", json={
        "question": "【验证脚本临时-应被拒】", "kind": "sql",
        "query": "DELETE FROM relations", "expected": "empty"})
    _cr_run = _cli.post("/api/knowledge/ontology/cqs/run", json={"id": _cr_add.get("id")}).json()
    _cr_put = _cli.put(f"/api/knowledge/ontology/cqs/{_cr_add.get('id')}", json={
        "question": "【验证脚本临时】改名后", "expected": "nonempty"}).json()
    _cr_del = _cli.delete(f"/api/knowledge/ontology/cqs/{_cr_add.get('id')}").json()
    _gone = conn.execute(
        "SELECT COUNT(*) FROM ontology_questions WHERE question LIKE '【验证脚本临时%'").fetchone()[0]
    check("O78 ★ CQ CRUD 冒烟：登记→执行→改→删全 2xx；写操作查询在登记口就被拒（400）",
          _cr_add.get("ok") and _cr_put.get("ok") and _cr_del.get("ok")
          and _cr_run["results"][0]["status"] == "pass" and _cr_add2.status_code == 400
          and _gone == 0,
          f"add={_cr_add.get('ok')} run={_cr_run['results'][0]['status'] if _cr_run.get('results') else '?'} "
          f"put={_cr_put.get('ok')} del={_cr_del.get('ok')} 拒写={_cr_add2.status_code} 残留={_gone}")

    # O79 门禁结构：_gate_run 放行时带回 chk 且附带 CQ（NameError 修复 + R1 不拦截的证据）
    # cq_record=False：验证脚本跑几百次不污染执行历史（同 O68 探针纪律）
    from routers.knowledge_parts.ontology_version import _gate_run as _grun  # noqa: E402
    _g2, _chk2 = _grun(conn, cq_record=False)
    _g_cq = (_chk2 or {}).get("cq") or {}
    check("O79 ★ 门禁 _gate_run：放行时返回完整体检报告且附 CQ 摘要（chk NameError 修复的证据）",
          _g2 is None and bool(_chk2) and _g_cq.get("total", 0) >= 5
          and _g_cq.get("failed", 0) == 0 and _g_cq.get("skipped", 0) == 0,
          f"gate={'放行' if _g2 is None else _g2.get('gate_blocked_on')} "
          f"cq.total={_g_cq.get('total')} failed={_g_cq.get('failed')} skipped={_g_cq.get('skipped')}")

    # O80 swrl_orphan 坏样本（P2-4，样本已在 O51 前计算注册）
    _iss = _S_SWRL.get("swrl_orphan", [])
    check("O80 坏样本：SWRL 规则引用未注册类型（Person/hasAge/isHuman）→ swrl_orphan 且 warn，带缺失清单",
          bool(_iss) and _iss[0]["severity"] == "warn"
          and set(_iss[0].get("unknown") or []) == {"Person", "hasAge", "isHuman"},
          f"{_iss[0]['message'][:90] if _iss else '未报警'}")

    # O81 DL 深检（P2-1）：/validate?dl=1 附带 Pellet 结果 —— 真库修复后应 CONSISTENT；
    # ~2s/次，verify 里仅此一断言跑真推理（R2b 修复 + CONSISTENT 复跑的固化回归）。
    _vd = _cli.get("/api/knowledge/ontology/validate?dl=1&record=0").json()
    _dl = _vd.get("dl") or {}
    check("O81 ★ DL 深检（/validate?dl=1）：Pellet 判 CONSISTENT 且带规模/耗时（R2b 修复后语义一致）",
          _dl.get("ran") is True and _dl.get("consistent") is True
          and _dl.get("classes", 0) > 0 and _dl.get("elapsed_ms", 0) > 0,
          f"ran={_dl.get('ran')} consistent={_dl.get('consistent')} "
          f"classes={_dl.get('classes')} props={_dl.get('properties')} elapsed={_dl.get('elapsed_ms')}ms "
          f"err={(_dl.get('error') or '')[:80]}")

    # O82 前端静态引用完整性（SWRL 面板 + DL 按钮；agent-browser 不稳，沿用静态检查降级纪律）
    _ont_js = io.open("static/js/mods/21-ontology.js", encoding="utf-8").read()
    _imp_js = io.open("static/js/mods/09-impact.js", encoding="utf-8").read()
    _idx_html = io.open("static/index.html", encoding="utf-8").read()
    _fe_ok = ('onclick="openOntSWRL()"' in _idx_html
              and "async function openOntSWRL()" in _ont_js
              and all(f in _ont_js for f in ("ontSwrlRun", "ontSwrlToggle", "ontSwrlDel",
                                             "ontSwrlEdit", "ontSwrlSave", "ontSwrlFact"))
              and "/api/swrl/rules" in _ont_js and "/api/swrl/facts" in _ont_js
              and "function oeDlCheck" in _imp_js and "dl=1" in _imp_js
              and "oeDlCheck()" in _imp_js)
    check("O82 SWRL 面板 + DL 深检按钮：前端入口/定义/API 契约静态引用完整（第四个无入口机制补齐）",
          _fe_ok,
          f"入口={'onclick=openOntSWRL()' in _idx_html} 面板={'openOntSWRL' in _ont_js} "
          f"API={'/api/swrl/rules' in _ont_js} DL按钮={'oeDlCheck' in _imp_js}")
except Exception as _e:  # pragma: no cover
    check("端点冒烟（O67~O72）可运行", False, f"拉不起来：{type(_e).__name__}: {_e}")

# ── D2/D3/D4 缺陷修复保护（2026-10-15 E2E 走查产出）──
# O83 体检口径双字段：issue_total=问题数（==len(issues)==档位之和），total=扫描类型数。
#   此前 total 一字段两义（52 类型 vs 28 问题），下游极易误读成问题总数画趋势。
_rep_d2 = _ontology_check(conn)
check("O83 ★ 体检口径双字段：issue_total==len(issues)==档位之和；total 为独立语义（扫描类型数）",
      _rep_d2.get("issue_total") == len(_rep_d2.get("issues") or [])
      and _rep_d2["issue_total"] == sum(int(_rep_d2.get(k) or 0)
                                        for k in ("high", "warn", "low", "info"))
      and isinstance(_rep_d2.get("total"), int),
      f"total={_rep_d2.get('total')} issue_total={_rep_d2.get('issue_total')} "
      f"issues={len(_rep_d2.get('issues') or [])}")

# O84 SWRL 幂等落库（D4）+ 部署自检脚本（D3）静态契约。
#   D4 前：每次推理「清未裁决→重插」⇒ fact id 全换（实测 27→39 churn）；改为内容幂等 upsert。
#   D3 前：换解释器部署时 owlready2 缺失 → /api/swrl/run 500，无显式报告。
_swrl_src = io.open("services/swrl_engine.py", encoding="utf-8").read()
_dep_script = Path("tools/check_deploy_readiness.py")
_d4_ok = ("facts_deduped" in _swrl_src                    # RunSummary 字段 + to_dict
          and "keep_keys" in _swrl_src                    # 内容幂等 key 集
          and "本轮不再产出" in _swrl_src                  # 陈旧 pending 清理
          and "DELETE FROM inferred_facts WHERE is_accepted=0" not in _swrl_src)  # 旧 churn 源头已移除
_d3_ok = _dep_script.exists() and "dependency_status" in _dep_script.read_text(encoding="utf-8")
check("O84 SWRL 幂等落库（内容 upsert 替代清空重插，id 稳定）+ 部署就绪自检脚本存在",
      _d4_ok and _d3_ok,
      f"D4={'facts_deduped/keep_keys/陈旧清理' if _d4_ok else '契约缺失'} "
      f"D3={_dep_script.exists()}")

conn.close()




# ══════════════════════════════════════════════════════════════════════════════
# 编号自检（无 O 编号）：机械保证「改断言必须同时改引用」。
# 2026-09-23 实测两类事故都发生过：
#   ① 加了新编号、漏改区间引用（靠人眼复核才发现）；
#   ② 改了断言名、忘了同步 MUT 的引用 → [9] 段把「变异未被击穿」误报成空转断言
#      （真因是引用文本过期，不是断言无效 —— 这条自检正是为它加的）。
_self_src = io.open(Path(__file__).resolve(), encoding="utf-8").read()
_nums = [int(m) for m in re.findall(r'check\(\s*[rf]?"O(\d+)', _self_src)]
_dup = sorted({n for n in _nums if _nums.count(n) > 1})
# MUT_TAGS 必须逐字命中某个 check("...") 的名字（否则 [9] 段匹配不上 → 假"空转断言"）
_stale_ref = [t for t in MUT_TAGS
              if not re.search(r'check\(\s*[rf]?"' + re.escape(t) + r'"', _self_src)]
check("编号自检：O 号连续且按出现顺序严格递增；变异引用的断言名逐字存在",
      _nums == sorted(_nums) and _nums == list(range(1, len(_nums) + 1)) and not _dup
      and not _stale_ref,
      f"共 {len(_nums)} 条 / 重复={_dup} / 引用过期={_stale_ref}")

print()
print("=" * 90)
print(f"结果：{OK} pass / {FAIL} fail")
if FAILED_NAMES:
    print("失败项：")
    for n in FAILED_NAMES:
        print(f"  - {n}")
print("=" * 90)
print("口径提示：O20~O29、O52~O53、O66~O70 为**真库/真实 app 金标**"
      f"（依赖 mbse.db 现值：249 条边、{len(RULES.RULES)} 条规则）；")
print("          O10~O19、O30、O35~O50、O61~O64 为夹具驱动（内存/临时库），不受本机数据现状影响；")
print("          O59~O63 依赖 tools/_ontology_dom_range_repair.py 的 TARGETS 与真库一致；")
print("          O65/O66 为变异自证（会临时改写源文件并逐字节还原，跑完 git status 应无额外改动）。")
sys.exit(1 if FAIL else 0)
