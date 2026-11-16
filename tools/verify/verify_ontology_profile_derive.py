# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需生产真库（ontology_types / ontology_profiles / 项目挂接）与随包 pyshacl；CI 无 mbse.db 与 .venv ⇒ 跑不了。本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""本体画像派生 + SHACL 门禁「未静默降级」的门禁（第 19 轮）。

## 两个遗留项，一个门禁（它们同属"B 本体型"这条链路）
1. `ontology_profiles` 实测 **0 行** ⇒ 方法论规约卡的 B 层（"能用什么"）恒空，
   而 `ontology_types` 里 52 条真实类型一直在。本批改为**从类型集投影**（不补种子、不硬编码）。
2. `shacl_findings` 实测 **0 行** —— 这个数字有两种完全相反的含义：
   **真的无违规**，或者**门禁根本没跑**（pyshacl 缺失 / shapes 为空 / mode=off 全都静默返回 0 违规）。
   两者在治理看板上长得一模一样，所以必须把"门禁确实在跑"钉成断言。

## 断言分两组
派生（夹具 + 真库）：
  P1 两遍扫描（属性名常排在实体名之前，单遍会静默丢掉 props —— 本模块第一版就是这个 bug）
  P2 幂等（create → noop，且不写库）
  P3 **人工画像不被覆盖**（同名但非 `profile-derived-` 前缀 ⇒ 报错拒绝）
  P4 真库派生画像与 `build()` 逐字段一致（防手工改库后两边漂移）+ 项目挂接一致
SHACL：
  P5 门禁未静默降级（mode≠off、skipped=False、shapes>0、total_checked>0）
     —— 注意判据是"跑起来了"，**不是**"有没有违规"（违规数取决于数据，不能当门禁）
变异自证：
  M1 断掉属性→实体归集 ⇒ P1 必须红
  M2 去掉 `ID_PREFIX` 守护 ⇒ P3 必须红
"""
import inspect
import json
import os
import sqlite3
import sys
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

from services import ontology_profile_service as ops  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# ── 夹具：最小可用的 ontology_types / ontology_profiles ────────────────────
def _fixture():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE ontology_types (name TEXT, type_kind TEXT, constraints TEXT, status TEXT);
        CREATE TABLE ontology_profiles (
            id TEXT PRIMARY KEY, name TEXT, profile_type TEXT, scenario_template_id TEXT,
            entities TEXT, relations TEXT, attributes TEXT, description TEXT, status TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    rows = [
        # ⚠️ 属性名刻意排在实体名之前（AAA < ZZZ）——单遍扫描在这里必丢 props
        ("AAA属性", "attribute", json.dumps({"domain_classes": ["ZZZ实体"]}), "released"),
        ("BBB属性", "attribute", json.dumps({"domain_classes": ["未注册实体"]}), "released"),
        ("ZZZ实体", "entity", "{}", "released"),
        ("REL_FIXED", "relation", json.dumps({"allowed_values": {"src": ["ZZZ实体"], "tgt": ["ZZZ实体"]}}), "released"),
        ("REL_OPEN", "relation", "{}", "released"),
        ("DEPRECATED实体", "entity", "{}", "deprecated"),
    ]
    c.executemany("INSERT INTO ontology_types (name,type_kind,constraints,status) VALUES (?,?,?,?)", rows)
    c.commit()
    return c


print("[1] 派生：两遍扫描 / 悬空域 / open 关系 / 排除 deprecated（夹具）")
_fx = _fixture()
_p = ops.build(_fx)
_by = {e["type"]: e["props"] for e in _p["entities"]}
check("P1a 属性归集到实体（属性名排在实体之前也不丢）",
      _by.get("ZZZ实体") == ["AAA属性"], _by)
check("P1b 未注册属性域计入 dangling_domains（不凭空造实体）",
      _p["stats"]["dangling_domains"] == 1 and "未注册实体" not in _by, _p["stats"])
_rel = {r["type"]: r for r in _p["relations"]}
check("P1c 无端点约束的关系标 open=True，有约束的为 False",
      _rel.get("REL_OPEN", {}).get("open") is True
      and _rel.get("REL_FIXED", {}).get("open") is False, _rel)
check("P1d deprecated 类型不进画像",
      "DEPRECATED实体" not in _by, list(_by))

print("[2] 幂等（create → noop）")
_pl = ops.plan(_fx)
check("P2a 首次 plan = create", _pl.get("action") == "create", _pl.get("action"))
_r1 = ops.apply(_fx, _pl)
_pl2 = ops.plan(_fx)
check("P2b 二次 plan = noop（不写库）",
      _pl2.get("action") == "noop" and not (_pl2.get("diff") or []),
      "%s diff=%s" % (_pl2.get("action"), _pl2.get("diff")))
check("P2c 落库画像 id 带派生前缀",
      (ops._stable_id(ops.DEF_NAME) == _r1.get("profile_id")), _r1.get("profile_id"))

print("[3] 人工画像不被覆盖")
_fx2 = _fixture()
_fx2.execute("INSERT INTO ontology_profiles (id,name,status) VALUES "
             "('profile-human','当前本体快照','active')")
_fx2.commit()
_p3 = ops.plan(_fx2)
check("P3 同名但非派生行 ⇒ 拒绝（不静默覆盖人工结果）",
      _p3.get("ok") is False and _p3.get("reason") == "name_taken_by_human_profile",
      _p3.get("reason"))
_p4 = ops.plan(_fx2, name="另一个名字")
check("P3b 换个名字就能建（不因拒绝而卡死）", _p4.get("action") == "create", _p4.get("action"))

print("[4] 真库：派生画像与类型集一致（防手工改库后漂移）")
_rc = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
_rc.row_factory = sqlite3.Row
try:
    _real = ops.build(_rc)
    _row = _rc.execute(
        "SELECT id,name,entities,relations,attributes FROM ontology_profiles "
        "WHERE id LIKE ? ORDER BY created_at DESC, id DESC LIMIT 1",
        (ops.ID_PREFIX + "%",)).fetchone()
    # ⚠️ detail 只在失败时给"为什么失败" —— 通过时也打一句 "无派生行"/"skipped=True"
    #    会让日志自相矛盾（本轮第一版就写成 `_res.get("reason") or "skipped=True"`，
    #    于是 PASS 行后面挂着 skipped=True）。判据日志必须能被直接读懂。
    check("P4a 真库存在派生画像", _row is not None,
          "" if _row is not None else "ontology_profiles 无派生行")
    if _row:
        check("P4b 画像 entities 与当前类型集逐字一致",
              json.loads(_row["entities"]) == _real["entities"],
              "实体 %d vs 库内 %d" % (len(_real["entities"]), len(json.loads(_row["entities"] or "[]"))))
        check("P4c 画像 relations 与当前类型集逐字一致",
              json.loads(_row["relations"]) == _real["relations"])
        _pr = _rc.execute("SELECT id,ontology_profile_id FROM projects").fetchall()
        _bad = [r["id"] for r in _pr if r["ontology_profile_id"] not in (None, "", _row["id"])]
        check("P4d 项目挂接指向派生画像（或为空）", not _bad, _bad)
        check("P4e 画像规模与类型集一致（实体>0）", _real["stats"]["entities"] > 0, _real["stats"])

    print("[5] SHACL 门禁：未静默降级")
    import ingest_gate as ig
    _mode = ig.gate_mode(_rc)
    _res = ig.check_triples(_rc, record=False)
    check("P5a gate_mode 不是 off", str(_mode) != "off", _mode)
    check("P5b 未因缺依赖/空 shapes 被跳过", _res.get("skipped") is False,
          "" if _res.get("skipped") is False else ("被跳过：%s" % (_res.get("reason") or "?")))
    check("P5c 实际比对了数据三元组（total_checked>0）",
          int(_res.get("total_checked") or 0) > 0, _res.get("total_checked"))
    _sh = ig._shapes_graph(_rc)
    check("P5d SHACL shapes 非空（形状由 ontology_types 派生）",
          _sh is not None and len(_sh) > 0, 0 if _sh is None else len(_sh))
    print("     （参考）本次违规数 = %d；该值随数据变化，**不作门禁判据**"
          % len(_res.get("violations") or []))
finally:
    _rc.close()

print("[6] 变异自证")
_bsrc = textwrap.dedent(inspect.getsource(ops.build))


def _twin_fn(src, mutate, tag, name, ns_extra=None):
    s = mutate(src)
    if s == src:
        check("M%s 变异未生效（锚点没命中，属脚本缺陷）" % tag, False, "源码未被改动")
        return None
    ns = dict(vars(ops))
    if ns_extra:
        ns.update(ns_extra)
    exec(compile(s, "<mutant_%s>" % tag, "exec"), ns)
    return ns[name]


def _mut_drop_props(s):
    """M1：断掉属性→实体归集（等价于"单遍扫描时属性先读到就丢"）。"""
    return s.replace("        if dc in ents:\n            ents[dc].append(a)",
                     "        if False:\n            ents[dc].append(a)")


_m1 = _twin_fn(_bsrc, _mut_drop_props, "1", "build")
_pm = _m1(_fixture()) if _m1 else None
_m1_bad = bool(_pm) and {e["type"]: e["props"] for e in _pm["entities"]}.get("ZZZ实体") != ["AAA属性"]
check("M1 断掉属性归集 → P1a 必须红", _m1_bad,
      _pm["entities"] if _pm else "n/a")

_psrc = textwrap.dedent(inspect.getsource(ops.plan))


def _mut_no_guard(s):
    """M2：去掉人工画像守护（任何同名行都覆盖）。"""
    return s.replace('    if not str(cur.get("id") or "").startswith(ID_PREFIX):',
                     "    if False:")


_m2 = _twin_fn(_psrc, _mut_no_guard, "2", "plan")
_fx3 = _fixture()
_fx3.execute("INSERT INTO ontology_profiles (id,name,status) VALUES "
             "('profile-human','当前本体快照','active')")
_fx3.commit()
_pm2 = _m2(_fx3) if _m2 else None
check("M2 去掉 ID_PREFIX 守护 → P3 必须红",
      bool(_pm2) and _pm2.get("ok") is True,
      "%s / %s" % ((_pm2 or {}).get("ok"), (_pm2 or {}).get("reason")))

print()
print("=" * 78)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL:", f)
raise SystemExit(1 if FAIL else 0)
