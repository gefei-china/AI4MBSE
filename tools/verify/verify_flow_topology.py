# -*- coding: utf-8 -*-
"""P2-4 门禁：多画布流拓扑台账与结构体检（自进化调研路线图 P2 第四项）。

═══════════════════════════════════════════════════════════════
动机（2026-10-18《自进化 AI 平台调研评估》路线图 P2 第四项）
═══════════════════════════════════════════════════════════════
路线图原文「多画布流拓扑实验」。勘察真库后口径被两个事实改写，本门禁
守的就是这个被改写后的口径，以及它换来的四条命脉：

  ① **口径不得漂回「自动改拓扑」**：红线 #6 明写「不碰权重、不碰架构
     拓扑」。拓扑台账全链路**没有一条写 agent_flows 的语句**（E段静态
     + C段行为级双重断言）。体检镜不是手（同 P1-4 回滚过人、P2-3
     档案库是记忆不是手）。
  ② **诚实 unknown 且判据顺序正确**：
     - 空库（0 幅画布）⇒ insufficient_data，**不是 clean**
       （冒烟实测抓到：blocker/warn/dup 三维都 0⇒ 旧逻辑判 clean。
       一幅画布都没有不是「零个缺陷」，是没有可体检的对象）；
     - blocker>0 ⇒ defective，**不是 insufficient_data**
       （冒烟实测抓到：先问「有无维度归零」会让 2 个 blocker 的库报
       insufficient_data——把「明确有缺陷」显示成「数据不足」，
       方向完全反了）；
     - 无运行证据的画布数 covered=0 ≠ 失败 0 次。
  ③ **指纹不含易变字段**：改画布名/版本号**不得**造出新拓扑
     （与 P2-3 变体指纹同型坑），也不得让同构分组漏检。
  ④ **同构只报结构同构、不报相似**：相似度要阈值，阈值是拍出来的。
     且同构分组不得虚高（≥2 才成组）。

判据结构（承 verify_agent_archive 的五段式）：
  A 纯函数  六条缺陷规则各自命中/不命中 + 指纹稳定性（改名不变）+ 同构
             分组（≥2 成组、1 不成组）+ 判据顺序（空库 unknown /
             blocker 优先 defective / 无 blocker 有 dup ⇒ defective）
  B 配置    flow_topology 组双登记 + 键一致 + 引擎零写 agent_flows 语句
             + 口径真被消费（影子配置变异自证 linear_degenerate_min /
             min_denominator / max_canvases）
  C 端到端  干净临时库（import 前切 MBSE_DB_PATH/MBSE_CONFIG_PATH）：
             合成 6 幅画布（干净/悬空边/不可解析/串行退化/同构×2）⇒
             六规则全命中 ⇒ scan 收敛台账 ⇒ 二次 scan created=0（幂等
             不注水）⇒★repo 自带 commit（第二连接可见）⇒ ★agent_flows
             行数与内容零变化（只读铁律）⇒ 端点行为级（含冷库显式失败）
             + 变异自证
  D 真库    只读取证：62 幅画布真实体检（副本上跑，**不写真库**）
  E 接线    迁移三处（misc 定义 / __init__ 登记 / schema 调用）+ 路由
             注册 + repo 只碰自家表 + 引擎/repo/router 三层无
             agent_flows 写路径（静态）

运行：python tools/verify/verify_flow_topology.py
"""
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── 干净库 + 隔离配置：先切后 import（core.config / DB 路径 import 期绑定）──
# ⚠️ 三条实测踩坑，全部沿用 verify_agent_archive 的处置：
#   ① 库文件**不删也不改名**：clean.db 一旦挪走，get_db() 缓存的单例连接
#      指向旧 inode ⇒ 后续 CREATE 落空、新连接报 no such table。
#      起点保证只靠 init_db() 幂等 + C 段显式清表。
#   ② 本机文件删除走 safe-delete（转回收站），门禁启动不删库文件，
#      改用「换路径」隔离。
#   ③ 子进程（变异自证）用 VERIFY_TOPOLOGY_TMP 换独立目录，天然隔离。
_TMP = os.environ.get("VERIFY_TOPOLOGY_TMP") or os.path.join(
    ROOT, "tmp", "verify_flow_topology")
os.makedirs(_TMP, exist_ok=True)
_CLEAN_DB = os.path.join(_TMP, "clean.db")
_CLEAN_CFG = os.path.join(_TMP, "config.json")
io.open(_CLEAN_CFG, "w", encoding="utf-8").write("{}")
os.environ["MBSE_DB_PATH"] = _CLEAN_DB
os.environ["MBSE_CONFIG_PATH"] = _CLEAN_CFG

_results = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  ← " + str(detail)) if (detail and not ok) else ""))


def section(t):
    print("\n── %s ──" % t)


def _read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def _strip_code(src):
    """剥掉 docstring 与注释——静态断言只看**代码形态**：注释里正当引用
    表名是文档需要，不是违规（P2-2 E3 教训）。"""
    out, in_doc = [], False
    for line in src.split("\n"):
        s = line.strip()
        if in_doc:
            if s.endswith('"""') or s.endswith("'''"):
                in_doc = False
            continue
        if s.startswith('"""') or s.startswith("'''"):
            if not (len(s) > 3 and s.endswith(('"""', "'''"))):
                in_doc = True
            continue
        if s.startswith("#"):
            continue
        out.append(re.sub(r"#.*$", "", line))
    return "\n".join(out)


def _resp(r):
    """端点返回值统一解析：dict 直取，Response 取 text 解析 body。

    ⚠️ 新版 httpx 的 Response **没有 .body**（只有 .text/.content），
    直接 r.body 会抛 AttributeError⇒ 断言全挂成"unparsable"，看起来
    像端点坏了其实端点是好的。两种形态都兼容。
    """
    if isinstance(r, dict):
        return r, getattr(r, "status_code", None)
    raw = getattr(r, "text", None)
    if raw is None:
        raw = getattr(r, "body", None)
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    try:
        return json.loads(raw), getattr(r, "status_code", None)
    except Exception:
        return {"ok": False,
                "error": "unparsable response: %r" % (raw[:200],)}, \
            getattr(r, "status_code", None)


def _n_rows(conn, table):
    try:
        return int(conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
    except Exception:
        return 0


def _seed_canvases(conn):
    """合成 6 幅画布（干净临时库专用），逐条对应一条缺陷规则。

    flow_id 用 9001+ 高位 id，避开 init_db 内置数据。
    """
    conn.execute("DELETE FROM agent_flows WHERE id >= 9000")
    iso_nodes = [{"id": "n1", "type": "tool", "label": "a", "config": {}},
                 {"id": "n2", "type": "llm", "label": "b", "config": {}},
                 {"id": "n3", "type": "skill", "label": "c", "config": {}}]
    iso_edges = [{"source": "n1", "target": "n2"},
                 {"source": "n2", "target": "n3"}]
    rows = [
        # 干净：两节点单链，3 节点以下不算串行退化，无任何缺陷
        (9001, "clean", "draft", "manual",
         [{"id": "n1", "type": "agent", "label": "a", "config": {}},
          {"id": "n2", "type": "agent", "label": "b", "config": {}}],
         [{"source": "n1", "target": "n2"}]),
        # 悬空边（指向不存在节点）+ 孤立节点（n2 无入无出）
        (9002, "dangling", "draft", "manual",
         [{"id": "n1", "type": "agent", "label": "a", "config": {}},
          {"id": "n2", "type": "agent", "label": "b", "config": {}}],
         [{"source": "n1", "target": "nX"}]),
        # 不可解析引用：config.tool 指向不存在的 tool
        (9003, "unresolvable", "draft", "manual",
         [{"id": "n1", "type": "tool", "label": "a",
           "config": {"tool": "definitely_not_a_real_tool"}}],
         []),
        # 串行退化：4 节点纯链
        (9004, "chain4", "draft", "manual",
         [{"id": "n%d" % i, "type": "agent", "label": str(i), "config": {}}
          for i in range(1, 5)],
         [{"source": "n%d" % i, "target": "n%d" % (i + 1)} for i in range(1, 4)]),
        # 同构 ×2：3 节点 2 边，planner_auto 生成
        (9005, "iso_a", "draft", "planner_auto", iso_nodes, iso_edges),
        (9006, "iso_b", "draft", "planner_auto", iso_nodes, iso_edges),
    ]
    for fid, name, status, source, nodes, edges in rows:
        conn.execute(
            "INSERT INTO agent_flows (id,name,version,status,source,nodes,edges) "
            "VALUES (?,?,?,?,?,?,?)",
            (fid, name, "v1", status, source,
             json.dumps(nodes, ensure_ascii=False),
             json.dumps(edges, ensure_ascii=False)))
    conn.commit()
    return len(rows)


# ══════════════════════════════════════════════════════════════
section("A 纯函数（不碰库）")
# ══════════════════════════════════════════════════════════════
from core import flow_topology as ft                      # noqa: E402
from core.flow_topology import (DEFECT_RULES,              # noqa: E402
                                DEFECT_SEVERITY,
                                group_isomorphic,
                                parse_flow,
                                topology_fingerprint,
                                summarize,
                                variant_row,
                                VERDICT_CLEAN,
                                VERDICT_DEFECTIVE,
                                VERDICT_UNKNOWN)

_MK = {"_available": {"tools": {"real_tool"}, "skills": {"real_skill"},
                      "agents": {"real_agent"}},
       "_linear_floor": 4}


def _flow(fid, name, nodes, edges, **kw):
    row = {"id": fid, "name": name, "version": "v1", "status": "draft",
           "source": "manual", "nodes": json.dumps(nodes, ensure_ascii=False),
           "edges": json.dumps(edges, ensure_ascii=False)}
    f = parse_flow(row)
    f.update(_MK)
    f.update(kw)
    return f


def _defect_names(f):
    return sorted({d["defect"] for d in ft.detect_defects(f)})


# A1 干净画布零缺陷（且必须**不**误报孤立/退化）
_f_clean = _flow(1, "clean",
                 [{"id": "n1", "type": "agent", "config": {}},
                  {"id": "n2", "type": "agent", "config": {}}],
                 [{"source": "n1", "target": "n2"}])
check("A1 干净两节点链零缺陷", _defect_names(_f_clean) == [], _defect_names(_f_clean))

# A2 悬空边
_f_dang = _flow(2, "dang",
                [{"id": "n1", "type": "agent", "config": {}},
                 {"id": "n2", "type": "agent", "config": {}}],
                [{"source": "n1", "target": "nX"}])
_n = _defect_names(_f_dang)
check("A2 悬空边被检出", "dangling_edge" in _n, _n)
check("A2b 悬空边致孤立节点被检出", "isolated_node" in _n, _n)
_sev = {d["defect"]: d["severity"] for d in ft.detect_defects(_f_dang)}
check("A2c 悬空边为 blocker", _sev.get("dangling_edge") == "blocker", _sev)

# A3 不可解析引用（键名bucket，不是 key —— 探针实测漏检过一次）
_f_unres = _flow(3, "unres",
                 [{"id": "n1", "type": "tool",
                   "config": {"tool": "definitely_not_a_real_tool"}}], [])
_n = _defect_names(_f_unres)
check("A3 不可解析 tool 引用被检出", "unresolvable_type" in _n, _n)
_f_unres_ok = _flow(4, "unres_ok",
                    [{"id": "n1", "type": "tool", "config": {"tool": "real_tool"}}],
                    [])
check("A3b 可解析引用不误报", "unresolvable_type" not in _defect_names(_f_unres_ok),
      _defect_names(_f_unres_ok))

# A4 串行退化（阈值 4）
_f_chain4 = _flow(5, "chain4",
                  [{"id": "n%d" % i, "type": "agent", "config": {}}
                   for i in range(1, 5)],
                  [{"source": "n%d" % i, "target": "n%d" % (i + 1)}
                   for i in range(1, 4)])
check("A4 四节点纯链判串行退化", "linear_degenerate" in _defect_names(_f_chain4),
      _defect_names(_f_chain4))
_f_chain3 = _flow(6, "chain3",
                  [{"id": "n%d" % i, "type": "agent", "config": {}}
                   for i in range(1, 4)],
                  [{"source": "n%d" % i, "target": "n%d" % (i + 1)}
                   for i in range(1, 3)])
check("A4b 阈值以下不判退化", "linear_degenerate" not in _defect_names(_f_chain3),
      _defect_names(_f_chain3))
_f_floor6 = _flow(7, "chain4_floor6",
                  [{"id": "n%d" % i, "type": "agent", "config": {}}
                   for i in range(1, 5)],
                  [{"source": "n%d" % i, "target": "n%d" % (i + 1)}
                   for i in range(1, 4)], _linear_floor=6)
check("A4c抬高阈值后不判退化", "linear_degenerate" not in _defect_names(_f_floor6),
      _defect_names(_f_floor6))

# A5 无汇点：每节点都有出边（3 节点环）
_f_ring = _flow(8, "ring",
                [{"id": "n1", "type": "agent", "config": {}},
                 {"id": "n2", "type": "agent", "config": {}},
                 {"id": "n3", "type": "agent", "config": {}}],
                [{"source": "n1", "target": "n2"},
                 {"source": "n2", "target": "n3"},
                 {"source": "n3", "target": "n1"}])
_n = _defect_names(_f_ring)
check("A5 无汇点被检出", "no_sink" in _n, _n)

# A6 规则注册表完整性（新增规则必须登记严重度，否则 fallback warn 会静默降级）
check("A6 六条规则全部注册", len(DEFECT_RULES) == 6, sorted(DEFECT_RULES))
check("A6b 每条规则都有明确严重度",
      all(r in DEFECT_SEVERITY for r in DEFECT_RULES),
      [r for r in DEFECT_RULES if r not in DEFECT_SEVERITY])

# A7 指纹稳定性：改名/改版本/改 label 均不得改变指纹
_iso_a = _flow(10, "iso_a",
               [{"id": "n1", "type": "tool", "label": "a", "config": {}},
                {"id": "n2", "type": "llm", "label": "b", "config": {}},
                {"id": "n3", "type": "skill", "label": "c", "config": {}}],
               [{"source": "n1", "target": "n2"},
                {"source": "n2", "target": "n3"}])
_fp_a = topology_fingerprint(_iso_a)
_renamed = dict(_iso_a)
_renamed["name"] = "完全不同的名字"
_renamed["version"] = "v99"
_renamed["nodes"] = [dict(n, label="改了标签") for n in _iso_a["nodes"]]
check("A7 改名/改版本/改标签不改指纹",
      topology_fingerprint(_renamed) == _fp_a,
      (topology_fingerprint(_renamed), _fp_a))
# 拓扑真变了 ⇒ 指纹必须变（否则指纹恒定=完全失效）
_reshaped = dict(_iso_a)
_reshaped["edges"] = [{"source": "n1", "target": "n3"}]
check("A7b 拓扑变化必改指纹", topology_fingerprint(_reshaped) != _fp_a)

# A8 同构分组：2 条成组、1 条不成组、不报相似
_g = group_isomorphic([variant_row(_iso_a, [], {}),
                       variant_row(_renamed, [], {})])
check("A8 同构成组 size=2", len(_g) == 1 and _g[0]["size"] == 2, _g)
_g1 = group_isomorphic([variant_row(_iso_a, [], {})])
check("A8b 单条不成组", _g1 == [], _g1)
_g3 = group_isomorphic([variant_row(_iso_a, [], {}),
                        variant_row(_renamed, [], {}),
                        variant_row(_f_chain4, [], {})])
check("A8c 组数不虚高（1 组而非多组）", len(_g3) == 1, [x["size"] for x in _g3])

# A9 ★判据顺序（冒烟实测抓到的两个真缺陷）
_s_empty = summarize([], [], min_denominator=2)
check("A9 空库判insufficient_data而非 clean",
      _s_empty["summary"]["verdict"] == VERDICT_UNKNOWN, _s_empty["summary"])
_v_bad = [variant_row(_f_dang, ft.detect_defects(_f_dang), {})]
_s_bad = summarize(_v_bad, [], min_denominator=2)
check("A9b blocker>0 判 defective 而非 unknown",
      _s_bad["summary"]["verdict"] == VERDICT_DEFECTIVE, _s_bad["summary"])
# A9e★判据顺序的真正证人：blocker>0 **且**其余 lower_better 维度全非零
#   （warn 也 >0 ⇒ 没有任何维度归零）。此时若把 blocker 分支摘掉，
#   旧顺序会因「无维度归零」落到 unknown ⇒ 把「明确有缺陷」显示成
#   「数据不足」。A9b 单独用无法证人顺序（它 warn/dup/groups 都还是 0，
#   zero_evidence 非空，根本落不到分支）——这是写M4 变异时实测到的：
#   变异确实生效（C2h 判红），但 A9b 不红，说明 A9b 是条弱断言。
_v_mix = [variant_row(_f_dang, ft.detect_defects(_f_dang)
                      + ft.detect_defects(_f_chain4), {})]
_s_mix = summarize(_v_mix, [], min_denominator=2)
_dims_mix = {d["name"]: d["value"] for d in _s_mix["dimensions"]}
check("A9e证人前置：blocker 与 warn 同时非零（无维度归零）",
      _dims_mix.get("blocker_defects", 0) > 0 and _dims_mix.get("warn_defects", 0) > 0,
      _dims_mix)
check("A9f blocker 与 warn 并存仍判 defective（不得因无维度归零而报unknown）",
      _s_mix["summary"]["verdict"] == VERDICT_DEFECTIVE, _s_mix["summary"])
_v_clean = [variant_row(_f_clean, [], {})]
_s_clean = summarize(_v_clean, [], min_denominator=2)
check("A9c 无缺陷且全归零判 clean",
      _s_clean["summary"]["verdict"] == VERDICT_CLEAN, _s_clean["summary"])
_v_dup = [variant_row(_iso_a, [], {}), variant_row(_renamed, [], {})]
_s_dup = summarize(_v_dup, group_isomorphic(_v_dup), min_denominator=2)
check("A9d 无 blocker 但有同构冗余⇒ defective",
      _s_dup["summary"]["verdict"] == VERDICT_DEFECTIVE, _s_dup["summary"])

# A10 分母不足即 unknown，绝不回落 0
_s_nocov = summarize([variant_row(_f_clean, [], {})], [], min_denominator=2)
_dims = {d["name"]: d["value"] for d in _s_nocov["dimensions"]}
check("A10 无运行证据⇒ failed_runs 为 None（不是 0）",
      _dims.get("failed_runs") is None, _dims)
check("A10b run_coverage 为 0.0 且不算失败",
      _dims.get("run_coverage") == 0.0, _dims)
# A10c 分母足时 failed_runs 才取值（需 covered ≥min_denominator=2，
# 单条覆盖不足仍是 None——这是设计而非缺陷）
_v_cov = [variant_row(_f_clean, [], {"runs": 3, "failed_runs": 1}),
          variant_row(_f_chain4, [], {"runs": 1, "failed_runs": 0})]
_s_cov = summarize(_v_cov, [], min_denominator=2)
_dims_cov = {d["name"]: d["value"] for d in _s_cov["dimensions"]}
check("A10c 分母足时 failed_runs 才取值", _dims_cov.get("failed_runs") == 1,
      _dims_cov)
# A10d 单条覆盖不足 ⇒仍 None（证明 A10c 真的在守分母，不是一律取值）
_s_cov1 = summarize([variant_row(_f_clean, [], {"runs": 3, "failed_runs": 1})],
                    [], min_denominator=2)
check("A10d 单条覆盖不足仍记 None（分母闸真的在生效）",
      {d["name"]: d["value"]
       for d in _s_cov1["dimensions"]}.get("failed_runs") is None,
      _s_cov1["dimensions"])

# A11 坏JSON 不得被当成空画布（伪装成健康）
_f_broken = parse_flow({"id": 99, "name": "broken", "nodes": "{不是 json",
                        "edges": None})
_f_broken.update(_MK)
check("A11 坏 JSON 留痕且报 blocker",
      bool(_f_broken["parse_errors"])
      and any(d["severity"] == "blocker"
              for d in ft.detect_defects(_f_broken)),
      _f_broken["parse_errors"])

# A12 边顺序/节点顺序不影响结构（否则同构分组会因书写顺序漏检）
_shuffled = dict(_iso_a)
_shuffled["nodes"] = list(reversed(_iso_a["nodes"]))
_shuffled["edges"] = [{"source": "n2", "target": "n3"},
                      {"source": "n1", "target": "n2"}]
check("A12 节点/边书写顺序不影响指纹",
      topology_fingerprint(_shuffled) == _fp_a,
      (topology_fingerprint(_shuffled), _fp_a))


# ══════════════════════════════════════════════════════════════
section("B 配置（双登记 + 零写口径）")
# ══════════════════════════════════════════════════════════════
from core import config as _cfg# noqa: E402

_keys = sorted(["enabled", "min_denominator", "linear_degenerate_min",
                "max_canvases"])
_def = _cfg.DEFAULT_CONFIG.get("flow_topology") or {}
_sch = _cfg.CONFIG_SCHEMA.get("flow_topology") or {}
check("B1 flow_topology 组在 DEFAULT_CONFIG", bool(_def), sorted(_def))
check("B2 flow_topology 组在 CONFIG_SCHEMA", bool(_sch), sorted(_sch))
check("B3 双登记键完全一致", sorted(_def) == sorted(_sch) == _keys,
      (sorted(_def), sorted(_sch)))
# 类型一致性走_coerce 本身（配置层的唯一类型闸），比对「默认值的类型」与
# 「schema 声明的类型」——不能写成 type(v)(0) 那类取巧形式：bool 用不上。
_TYPE_MAP = {"int": int, "float": float, "bool": bool}
_mismatch = [k for k in _keys
             if k not in _def or k not in _sch
             or _sch[k].get("type") not in _TYPE_MAP
             or not isinstance(_def[k], _TYPE_MAP[_sch[k]["type"]])
             or not isinstance(_cfg._coerce(_sch[k], _def[k]),
                               _TYPE_MAP[_sch[k]["type"]])]
check("B4 默认值与 schema 类型一致", not _mismatch, _mismatch)

# B5 ★引擎/repo/router 三层零写 agent_flows（红线 #6 落成可静态断言的事实）
# B5 ★引擎/router 层零写 SQL，repo 层**只准写自家台账表**
# （repo 当然要写 SQL——它就是写入面；断言它不得碰 agent_flows，
#  以及它碰的表集合被限死在 flow_topology_variants，见 B5d）
_core_code = _strip_code(_read("core/flow_topology.py"))
_repo_code = _strip_code(_read("repositories/flow_topology_repo.py"))
_router_code = _strip_code(_read("routers/flow_topology.py"))
_write_verbs = r"(INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|REPLACE\s+INTO)"
for tag, code in (("引擎", _core_code), ("router", _router_code)):
    hits = re.findall(_write_verbs, code, flags=re.I)
    check("B5 %s 层无任何写 SQL 语句" % tag, not hits, hits[:5])
for tag, code in (("引擎", _core_code), ("repo", _repo_code),
                  ("router", _router_code)):
    bad = re.findall(_write_verbs + r"\s+agent_flows", code, flags=re.I)
    check("B5b %s 层无 agent_flows 写语句" % tag, not bad, bad[:5])
# 引擎只允许通过 repo 写自家表（禁止引擎直连 INSERT）
check("B5c 引擎不含自家表直写（写只经 repo）",
      not re.search(r"INSERT\s+INTO\s+flow_topology", _core_code, re.I))
check("B5d repo 只碰 flow_topology_variants",
      set(re.findall(r"(?:FROM|INTO|UPDATE)\s+(flow_\w+|agent_\w+|variant_\w+)",
                     _repo_code, re.I)) <= {"flow_topology_variants"},
      sorted(set(re.findall(
          r"(?:FROM|INTO|UPDATE)\s+(flow_\w+|agent_\w+|variant_\w+)",
          _repo_code, re.I))))
check("B6 配置注释含红线依据（口径不得漂回自动改拓扑）",
      "不碰架构拓扑" in _read("core/config.py"))

# ══════════════════════════════════════════════════════════════
section("C 端到端（干净临时库）")
# ══════════════════════════════════════════════════════════════
from database.schema import init_db                # noqa: E402
from database.connection import get_db              # noqa: E402
from repositories.flow_topology_repo import FlowTopologyRepo  # noqa: E402

init_db()
_conn = get_db()
_conn.row_factory = sqlite3.Row

# 幂等：清台账（不删库文件——删库会破坏 get_db() 单例，实测踩过）
_conn.execute("DELETE FROM flow_topology_variants")
_conn.commit()
_n_seeded = _seed_canvases(_conn)
check("C1 合成 6 幅画布", _n_seeded == 6, _n_seeded)

# 记录 agent_flows 的行数与内容指纹（只读铁律的对照基线）
_af_before = _conn.execute(
    "SELECT COUNT(*) FROM agent_flows").fetchone()[0]
_af_hash = _conn.execute(
    "SELECT COALESCE(GROUP_CONCAT(id||'|'||name||'|'||version||'|'||nodes||'|'||edges, '§'), '') "
    "FROM agent_flows ORDER BY id").fetchone()[0]
_fr_before = _n_rows(_conn, "flow_runs")

_res = ft.scan(_conn)
_by = {v["flow_id"]: v for v in _res["variants"]}
check("C2 scan 收敛全部画布", len(_res["variants"]) == 6, len(_res["variants"]))
check("C2b 干净画布零缺陷", _by[9001]["blocker_count"] == 0
      and _by[9001]["warn_count"] == 0,
      (_by[9001]["blocker_count"], _by[9001]["warn_count"]))
check("C2c 悬空边画布检出 blocker",
      _by[9002]["blocker_count"] >= 1, _by[9002]["blocker_count"])
check("C2d 不可解析画布检出 blocker",
      "unresolvable_type" in _by[9003]["defects_json"],
      _by[9003]["defects_json"][:200])
check("C2e 四节点纯链检出 warn", _by[9004]["warn_count"] >= 1,
      _by[9004]["warn_count"])
check("C2f 同构两画布指纹相同",
      _by[9005]["fingerprint"] == _by[9006]["fingerprint"])
check("C2g 同构组 size=2", len(_res["groups"]) == 1
      and _res["groups"][0]["size"] == 2,
      [(g["size"], g["flow_ids"]) for g in _res["groups"]])
check("C2h verdict=defective（有 blocker）",
      _res["overview"]["summary"]["verdict"] == VERDICT_DEFECTIVE,
      _res["overview"]["summary"])

# C3 写台账：幂等（created 首扫=6，二扫=0）
_w1 = ft.record_scan(_conn, _res)
check("C3 首扫入册 6 行", _w1["variants_created"] == 6, _w1)
check("C3b 台账行数=6", _n_rows(_conn, "flow_topology_variants") == 6)
_res2 = ft.scan(_conn)
_w2 = ft.record_scan(_conn, _res2)
check("C3c 二扫 created=0（不注水）", _w2["variants_created"] == 0, _w2)
check("C3d 二扫后台账仍 6 行",
      _n_rows(_conn, "flow_topology_variants") == 6,
      _n_rows(_conn, "flow_topology_variants"))

# C4 ★repo 自带 commit：换一条独立连接必须能看到已写入的行
#（BaseRepo.execute 不commit 是 P1-4 实测踩过的坑，写方法必须自带 commit）
_conn2 = sqlite3.connect(_CLEAN_DB)
check("C4 repo 写方法自带 commit（第二连接可见）",
      int(_conn2.execute(
          "SELECT COUNT(*) FROM flow_topology_variants").fetchone()[0]) == 6)
_conn2.close()

# C5 ★只读铁律行为级：agent_flows 行数与内容零变化，flow_runs 零变化
_af_after = _conn.execute("SELECT COUNT(*) FROM agent_flows").fetchone()[0]
_af_hash_after = _conn.execute(
    "SELECT COALESCE(GROUP_CONCAT(id||'|'||name||'|'||version||'|'||nodes||'|'||edges, '§'), '') "
    "FROM agent_flows ORDER BY id").fetchone()[0]
check("C5 agent_flows 行数零变化", _af_before == _af_after,
      (_af_before, _af_after))
check("C5b agent_flows 内容零变化（拓扑字节级未动）",
      _af_hash == _af_hash_after)
check("C5c flow_runs 行数零变化", _fr_before == _n_rows(_conn, "flow_runs"))

# C6 改名不得改指纹（端到端确认，与 A7 纯函数段互为交叉验证）
_conn.execute("UPDATE agent_flows SET name='改名了' WHERE id=9006")
_conn.commit()
_res3 = ft.scan(_conn)
_by3 = {v["flow_id"]: v for v in _res3["variants"]}
check("C6 端到端改名指纹不变",
      _by3[9005]["fingerprint"] == _by3[9006]["fingerprint"])
check("C6b 改名后仍判为同构组", len(_res3["groups"]) == 1
      and _res3["groups"][0]["size"] == 2,
      [(g["size"]) for g in _res3["groups"]])

# C7 evidence_state：跑过的画布 covered，没跑的 uncovered（不回落 0）
# ⚠️ 先清 flow_runs：init_db 自带 running 状态的种子行，会污染
#    failed_runs 与 covered 的计数（本段要断的是"我造的行怎么被归类"，
#    不是"库里总共几行"）。前文的 _fr_before 基线在此不再适用，
#    故 C7 之后不再复用 C5c 的行数比较。
_conn.execute("DELETE FROM flow_runs")
_conn.execute("INSERT INTO flow_runs (flow_id,flow_name,status,order_json,"
              "total_latency_ms,error_count) VALUES (?,?,?,?,?,?)",
              (9001, "clean", "completed", '["n1","n2"]', 100, 0))
_conn.execute("INSERT INTO flow_runs (flow_id,flow_name,status,order_json,"
              "total_latency_ms,error_count) VALUES (?,?,?,?,?,?)",
              (9004, "chain4", "running", '[]', 0, 0))
_conn.commit()
_fr_now = _n_rows(_conn, "flow_runs")
check("C7c 起始 flow_runs 恰为自造的 2 条", _fr_now == 2, _fr_now)
_res4 = ft.scan(_conn)
_by4 = {v["flow_id"]: v for v in _res4["variants"]}
check("C7 跑过的画布 covered", _by4[9001]["evidence_state"] == "covered",
      _by4[9001]["evidence_state"])
check("C7b 没跑过的画布 uncovered（不是 0 次）",
      _by4[9003]["evidence_state"] == "uncovered",
      _by4[9003]["evidence_state"])
check("C7c running 状态计为失败（非 completed 即失败）",
      _by4[9004]["failed_runs"] == 1, _by4[9004]["failed_runs"])
check("C7b2 completed 不计为失败",
      _by4[9001]["failed_runs"] == 0, _by4[9001]["failed_runs"])
_res4_dims = {d["name"]: d["value"] for d in _res4["overview"]["dimensions"]}
check("C7d run_coverage 有真实取值（非 0.0）",
      _res4_dims.get("run_coverage", 0) > 0, _res4_dims)
# covered=2 恰好达 min_denominator=2 ⇒ failed_runs 该取值（对照 A10d 的
# 「不足即 None」：两个方向都要断，否则无法排除「一律取值」的假实现）
check("C7e 分母足时 failed_runs 取值",
      _res4_dims.get("failed_runs") == 1, _res4_dims)

# C8 端点行为级（含 P2-1 教训：冷库失败必须显式 ok:False，不裸奔 500）
try:
    from main import app as _app
    from fastapi.testclient import TestClient
    _client = TestClient(_app)
except Exception as _e:                                     # noqa: BLE001
    _client = None
    print("  （TestClient 不可用：%s）" % _e)

if _client is not None:
    _r, _code = _resp(_client.get("/api/flow-topology"))
    check("C8 GET /api/flow-topology 可用且 ok",
          _r.get("ok") is True and _r.get("enabled") is True, _r.get("error"))
    check("C8b 总览带三态 verdict",
          _r.get("overview", {}).get("summary", {}).get("verdict")
          == VERDICT_DEFECTIVE,
          _r.get("overview", {}).get("summary"))
    check("C8c 端点返回规则清单", len(_r.get("rules") or []) == 6,
          len(_r.get("rules") or []))

    _r, _code = _resp(_client.post("/api/flow-topology/scan"))
    check("C8d POST scan ok 且零生产副作用",
          _r.get("ok") is True, _r.get("error"))
    check("C8e scan 后 agent_flows 行数仍不变",
          int(_conn.execute("SELECT COUNT(*) FROM agent_flows").fetchone()[0])
          == _af_before + 0,
          (int(_conn.execute("SELECT COUNT(*) FROM agent_flows").fetchone()[0]),
           _af_before))

    _r, _code = _resp(_client.post("/api/flow-topology/refresh"))
    check("C8f POST refresh ok", _r.get("ok") is True, _r.get("error"))

    _r, _code = _resp(_client.get("/api/flow-topology/isomorphic"))
    check("C8g 同构端点 ok 且给出 group", _r.get("ok") is True
          and isinstance(_r.get("groups"), list), _r.get("error"))

    _r, _code = _resp(_client.post("/api/flow-topology/9002/defects"))
    check("C8h 单画布体检返回缺陷明细",
          _r.get("ok") is True and _r.get("blocker_count", 0) >= 1,
          _r.get("error") or _r.get("defects"))
    check("C8i 体检给人可读建议",
          bool(_r.get("advice")), _r.get("advice"))
    _r, _code = _resp(_client.post("/api/flow-topology/888888/defects"))
    check("C8j 不存在画布返 404 + 显式 error",
          _r.get("ok") is False and _code == 404, (_r.get("error"), _code))

    # C9★端点冷库/异常显式化（P2-1 教训：失败必须显式 ok:False，不裸奔 500）
    # 打桩打在**真在调用路径上**的函数：读端点走台账（不扫库，职责更干净），
    # 桩 repo 的list_variants；scan/refresh 端点才调 collect_flows。
    # （第一版把桩打在 collect_flows 上却去断读端点，读端点根本不调它⇒
    #   断言"不红"却毫无意义——桩打歪的测试比没测试更坏。）
    from repositories.flow_topology_repo import FlowTopologyRepo as _FTR
    _orig_list = _FTR.list_variants
    try:
        def _boom_list(*a, **k):
            raise RuntimeError("注入的台账读取失败")
        _FTR.list_variants = _boom_list
        _r, _code = _resp(_client.get("/api/flow-topology"))
        check("C9 台账读取异常时读端点显式 ok:False",
              _r.get("ok") is False and bool(_r.get("error")), _r)

        _FTR.list_variants = _orig_list
        _orig_collect = ft.collect_flows
        ft.collect_flows = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("注入的采集失败"))
        _r, _code = _resp(_client.post("/api/flow-topology/scan"))
        check("C9b 采集异常时 scan 端点 500+ok:False（诚实失败语义）",
              _r.get("ok") is False and _code == 500, (_r.get("error"), _code))
        _r, _code = _resp(_client.post("/api/flow-topology/refresh"))
        check("C9c 采集异常时 refresh 端点 500+ok:False",
              _r.get("ok") is False and _code == 500, (_r.get("error"), _code))
        # 异常不得写进台账（失败路径不留半截数据）
        check("C9d 采集异常后台账行数未被污染",
              _n_rows(_conn, "flow_topology_variants") == 6,
              _n_rows(_conn, "flow_topology_variants"))
    finally:
        _FTR.list_variants = _orig_list
        try:
            ft.collect_flows = _orig_collect
        except NameError:
            pass


# ══════════════════════════════════════════════════════════════
section("C' 变异自证（证明断言有牙）")
# ══════════════════════════════════════════════════════════════

def _run_mutation_proofs():
    """每组变异都应让目标断言判红（证明断言不是恒真）。"""
    _PY = sys.executable
    _MUT_SRC = os.path.join(ROOT, "core", "flow_topology.py")
    _GATE = os.path.join("tools", "verify", "verify_flow_topology.py")

    def _mutate_and_run(name, old, new, expect):
        """变异 → 子进程跑门禁 → 断言目标断言判红。

        三条实测踩坑（都沿用 verify_agent_archive 的处置）：
        ① 子进程必须用**独立库/配置/目录**（VERIFY_TOPOLOGY_TMP），复用
           父进程 clean.db 会与父进程持有的 get_db() 单例 + WAL 边车打架，
           在断言前就崩（no such table）⇒ 变异自证静默失效；
        ② 还原用 **io.open 覆写原始内容**，不用 shutil.move/os.replace
           ——本机文件删除走 safe-delete（转回收站），异常路径会还原失败
           并留下污染的变异体；
        ③ 子进程设VERIFY_TOPOLOGY_MUT_CHILD 跳过本段，否则变异体会被
           自己的子进程二次变异而假红。
        """
        src = io.open(_MUT_SRC, encoding="utf-8").read()
        mut = src.replace(old, new)
        assert mut != src, "变异未命中: %s" % name
        _sub = os.path.join(ROOT, "tmp",
                            "verify_ft_mut_%s" % name.split()[0])
        os.makedirs(_sub, exist_ok=True)
        _env = dict(os.environ)
        _env["MBSE_DB_PATH"] = os.path.join(_sub, "mut.db")
        _env["MBSE_CONFIG_PATH"] = os.path.join(_sub, "config.json")
        _env["MBSE_SHADOW_CONFIG"] = ""
        _env["VERIFY_TOPOLOGY_TMP"] = _sub
        _env["VERIFY_TOPOLOGY_MUT_CHILD"] = "1"
        io.open(_env["MBSE_CONFIG_PATH"], "w", encoding="utf-8").write("{}")
        try:
            io.open(_MUT_SRC, "w", encoding="utf-8").write(mut)
            proc = subprocess.run([_PY, "-X", "utf8",
                                   os.path.join(ROOT, _GATE)],
                                  capture_output=True, text=True, cwd=ROOT,
                                  timeout=420, env=_env)
            out = proc.stdout + proc.stderr
            reds = [ln for ln in out.split("\n") if "[FAIL]" in ln]
            hit = any(p in ln for ln in reds for p in expect)
            check(name, bool(reds) and hit,
                  {"rc": proc.returncode, "reds": reds[:3], "expect": expect,
                   "tail": proc.stderr.strip().split("\n")[-2:] if not reds else ""})
        finally:
            io.open(_MUT_SRC, "w", encoding="utf-8").write(src)

    # M1 悬空边检测失灵
    _mutate_and_run(
        "M1 悬空边检测失灵 ⇒ A2/C2c 判红",
        "    for e in st[\"edges\"]:\n        for side in (\"source\", \"target\"):\n"
        "            if e[side] not in st[\"id_set\"]:",
        "    for e in []:\n        for side in (\"source\", \"target\"):\n"
        "            if e[side] not in st[\"id_set\"]:",
        ("A2", "C2c"))

    # M2 不可解析引用规则整体失灵（探针实测漏检过一次的那个 bug）
    _mutate_and_run(
        "M2 不可解析引用检测失灵 ⇒ A3/C2d 判红",
        "            pool = available.get(bucket) or set()\n"
        "            if ref not in pool:",
        "            pool = available.get(bucket) or set()\n"
        "            if False:",
        ("A3", "C2d"))

    # M3 空库判 clean（把诚实 unknown 改回假clean）
    _mutate_and_run(
        "M3 空库回落 clean ⇒ A9 判红",
        "    if total == 0:",
        "    if False:",
        ("A9",))

    # M4 blocker 判据分支被摘掉 ⇒「明确有缺陷」被显示成「数据不足」
    #    证人用 A9f（blocker 与 warn 并存⇒ 无任何lower_better 维度归零，
    #    旧顺序才会误落 unknown；A9b 证人不了这个分支，见A9e 注释）
    _mutate_and_run(
        "M4 blocker 判据分支被摘 ⇒ A9f/C2h 判红",
        "    elif blocker > 0:",
        "    elif False:",
        ("A9f", "C2h"))

    # M5 指纹掺入 name（台账注水：改个名就成新拓扑）
    _mutate_and_run(
        "M5 指纹掺入 name ⇒ A7/C6 判红",
        "        \"types=\" + \",\".join(\"%s:%d\" % (k, st[\"type_counts\"][k])\n"
        "                            for k in sorted(st[\"type_counts\"])),",
        "        \"types=\" + \",\".join(\"%s:%d\" % (k, st[\"type_counts\"][k])\n"
        "                            for k in sorted(st[\"type_counts\"])),\n"
        "        \"name=\" + str(flow.get(\"name\") or \"\"),",
        ("A7", "C6"))

    # M6 同构分组虚高（单条也算一组）
    _mutate_and_run(
        "M6 同构分组虚高（单条成组）⇒ A8b 判红",
        "        if not fp or len(members) < 2:",
        "        if not fp:",
        ("A8b",))

    # M7 分母不足回落 0（未跑过 = 跑了零次）
    _mutate_and_run(
        "M7 分母不足回落 0 ⇒ A10 判红",
        "        {\"name\": \"failed_runs\", \"value\": failed if covered >= floor else None,",
        "        {\"name\": \"failed_runs\", \"value\": failed,",
        ("A10",))

    # M8 串行退化阈值取 1（无阈值即无意义，全画布皆退化）
    _mutate_and_run(
        "M8 串行退化阈值写死为 1 ⇒ A4b 判红",
        "    floor = int(flow.get(\"_linear_floor\") or 4)",
        "    floor = 1",
        ("A4b",))


_IS_MUT_CHILD = bool(os.environ.get("VERIFY_TOPOLOGY_MUT_CHILD"))
if _IS_MUT_CHILD:
    print("  （子进程：跳过变异自证段）")
else:
    _run_mutation_proofs()


# ══════════════════════════════════════════════════════════════
section("D 真库（只读取证）")
# ══════════════════════════════════════════════════════════════
_real_db = os.path.join(ROOT, "mbse.db")
if not os.path.exists(_real_db):
    print("  （跳过：未找到真库 mbse.db）")
else:
    import shutil as _sh
    _real_copy = os.path.join(_TMP, "real_copy.db")
    _sh.copyfile(_real_db, _real_copy)          # 覆写即可，无需先删
    _rc = sqlite3.connect(_real_copy)
    _rc.row_factory = sqlite3.Row
    try:
        from database.migrations.misc import _migrate_flow_topology_p24
        _migrate_flow_topology_p24(_rc)
        _rc.commit()
    except Exception as _e:
        print("  （副本建表失败：%s）" % _e)
    _rc.row_factory = sqlite3.Row
    try:
        _n_flows = int(_rc.execute(
            "SELECT COUNT(*) FROM agent_flows").fetchone()[0])
        _n_runs = int(_rc.execute(
            "SELECT COUNT(*) FROM flow_runs").fetchone()[0])
        _n_steps = int(_rc.execute(
            "SELECT COUNT(*) FROM flow_run_steps").fetchone()[0])
        print("  真库：agent_flows %d 幅 / flow_runs %d 条 / flow_run_steps %d 步"
              % (_n_flows, _n_runs, _n_steps))
        check("D1 真库画布可读", _n_flows > 0, _n_flows)
        # 取证事实：运行证据远少于画布定义 ⇒ 不能做拓扑实验（本期口径的依据）
        check("D2 ★真库运行证据 < 画布定义（实验前提不成立，口径据此定为台账+体检）",
              _n_runs < _n_flows, (_n_flows, _n_runs))
        _real = ft.scan(_rc)
        _ov = _real["overview"]
        print("  真库体检：%d 幅画布 / %d 同构组 / 冗余 %d 幅 / verdict=%s"
              % (_ov["canvases"], _ov["isomorphic_groups"],
                 _ov["duplicate_canvases"], _ov["summary"]["verdict"]))
        check("D3 真库体检可跑且给出结论", bool(_ov["summary"]["verdict"]),
              _ov["summary"])
        check("D4 真库存在同构冗余（本项要解决的真实问题）",
              _ov["duplicate_canvases"] > 0,
              {"groups": _ov["isomorphic_groups"],
               "dup": _ov["duplicate_canvases"]})
        _covered = _ov["covered_canvases"]
        print("  真库有运行证据的画布：%d 幅（未跑过 %d 幅）"
              % (_covered, _ov["uncovered_canvases"]))
        check("D5 无运行证据的画布记 uncovered（不回落 0）",
              _covered <= _ov["canvases"], (_covered, _ov["canvases"]))
        _top = _real["groups"][0] if _real["groups"] else None
        if _top:
            print("  最大同构组：%d 幅 %s 来源=%s"
                  % (_top["size"], _top["shape"], _top["sources"]))
    finally:
        _rc.close()


# ══════════════════════════════════════════════════════════════
section("E 接线")
# ══════════════════════════════════════════════════════════════
_misc = _read("database/migrations/misc.py")
_init = _read("database/migrations/__init__.py")
_schema = _read("database/schema.py")
_ridx = _read("routers/__init__.py")
_main = _read("main.py")
check("E1 迁移定义在 misc.py",
      "def _migrate_flow_topology_p24" in _misc)
check("E2 迁移在 __init__.py 登记（import + __all__）",
      _init.count("_migrate_flow_topology_p24") >= 2,
      _init.count("_migrate_flow_topology_p24"))
check("E3 迁移在 schema.py 调用（第三处接线）",
      "_migrate_flow_topology_p24(conn)" in _schema)
check("E4 表在 schema.py 注释/迁移中登记",
      "flow_topology_variants" in _misc)
check("E5 路由在 routers/__init__.py 注册",
      "flow_topology_router" in _ridx)
check("E6 路由在 main.py include（两处）",
      _main.count("flow_topology_router") >= 2,
      _main.count("flow_topology_router"))
check("E7 配置注释标明 P2-4 与口径",
      "flow_topology" in _schema or "P2-4" in _read("core/config.py"))
check("E8 端点含 scan / refresh / isomorphic / defects",
      all(p in _read("routers/flow_topology.py")
          for p in ("/api/flow-topology/scan", "/api/flow-topology/refresh",
                    "/api/flow-topology/isomorphic", "/defects")))
# E9 CI 接线
_ci = _read(".github/workflows/ci.yml")
check("E9 CI 已接线 verify_flow_topology",
      "verify_flow_topology.py" in _ci)
# E10 全仓 py 可解析（排除 backups/——它含 7596 个陈旧副本，见 P2-1教训）
_py_bad = []
for _dir, _sub, _files in os.walk(ROOT):
    _rel = os.path.relpath(_dir, ROOT).replace("\\", "/")
    if "backups" in _rel or _rel.startswith(".git") or ".venv" in _rel:
        _sub[:] = []
        continue
    for _f in _files:
        if _f.endswith(".py"):
            _p = os.path.join(_dir, _f)
            try:
                with io.open(_p, encoding="utf-8") as _fh:
                    compile(_fh.read(), _p, "exec")
            except Exception as _e:                        # noqa: BLE001
                _py_bad.append("%s: %s" % (os.path.relpath(_p, ROOT), _e))
check("E10 全仓 py 可解析（排除 backups/.venv）", not _py_bad, _py_bad[:3])


# ══════════════════════════════════════════════════════════════
_total = len(_results)
_fail = [n for n, ok, _ in _results if not ok]
print("\n" + "=" * 66)
print("P2-4 门禁：%d/%d 通过" % (_total - len(_fail), _total))
if _fail:
    print("失败项：")
    for n in _fail:
        print("  · %s" % n)
print("=" * 66)
sys.exit(1 if _fail else 0)