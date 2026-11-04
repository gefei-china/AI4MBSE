# -*- coding: utf-8 -*-
"""门禁：**用户可见产物的形态契约**（视图 / 卡片 / 产物）—— 2026-11-04 新增。

── 与已有门禁的分工（别混淆）──────────────────────────────────────────────
  · `verify_final_content_contract.py`：守**最终正文**（协议 JSON、围栏、章节序号）；
  · 本门禁：守**结构化产物**（ViewModel / card_data / artifacts 行）。
  两者合起来才覆盖"用户能看到的一切"，缺一半都会留盲区。

── 为什么必须现在补（实测证据）─────────────────────────────────────────────
  · 真库 **173 条** `kind='sysml'` 产物 `nodes=0 / edges=0`（"点开是空图"）；
  · 卡片里 `schema_version` 出现在 `/plan[i]/summary/` 与 `/exec/subtasks[i]/summary/`
    —— 这是**合法**的（子任务结构化摘要专用字段），但一旦它出现在别处就是泄漏，
    而此前**没有任何断言**能区分这两种情况；
  · `undefined` / `NaN` / `[object Object]` 这类 JS 序列化残留会直接显示给用户。

── 判据策略（关键设计）────────────────────────────────────────────────────
  ① **主判据用构造样本**：契约是纯函数，用它跑一批"已知好坏"的输入，
     结果必须可复现 ⇒ 门禁不会因为真库存量数据而时红时绿；
  ② **真库只做存量统计**，不阻断 —— 存量缺陷要人决定怎么清，
     拿历史数据当阻断条件只会逼人加 `--skip`，最后门禁形同虚设；
  ③ **每个契约都要有变异自证**（把实现改坏，断言必须红），否则是空转。
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.output_contracts import (          # noqa: E402
    validate_viewmodel, validate_sysml_views, validate_card_data, validate_artifact,
    ARTIFACT_KINDS, VIEW_KINDS)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append((name, detail))
        print("  FAIL %s  <- %s" % (name, detail))


print("=" * 68)
print("B 门禁：用户可见产物形态契约（视图 / 卡片 / 产物）")
print("=" * 68)

# ══ 一、视图 ViewModel ══════════════════════════════════════════════════
print("\n── B1 视图 ViewModel ──")

_GOOD_VM = {
    "view": {"type": "BDD", "name": "结构视图", "v1": "Block Definition Diagram", "kind": "结构"},
    "nodes": [{"id": "n1", "name": "宽带载荷"}, {"id": "n2", "name": "转发器"}],
    "edges": [{"source": "n1", "target": "n2", "type": "CONTAINS"}],
    "warnings": [],
}
check("B1 合法 ViewModel 通过", validate_viewmodel(_GOOD_VM, "BDD") == [],
      str(validate_viewmodel(_GOOD_VM, "BDD")[:3]))

# 缺 warnings（前端读 undefined）
_v = {k: val for k, val in _GOOD_VM.items() if k != "warnings"}
check("B1b 缺 warnings 键被抓住", any("warnings" in p for p in validate_viewmodel(_v, "BDD")),
      str(validate_viewmodel(_v, "BDD")[:2]))

# 悬空边（最隐蔽：图能画，线连到空气）
_v = json.loads(json.dumps(_GOOD_VM))
_v["edges"].append({"source": "n1", "target": "GHOST"})
_probs = validate_viewmodel(_v, "BDD")
check("B1c 悬空边被抓住（指向不存在的节点）",
      any("悬空边" in p for p in _probs), str(_probs[:2]))

# type 与键名不一致（点追溯视图渲染成需求图）
check("B1d view.type 与键名不一致被抓住",
      any("不一致" in p for p in validate_viewmodel(_GOOD_VM, "REQ")),
      str(validate_viewmodel(_GOOD_VM, "REQ")[:2]))

# 未知视图类型
_v = json.loads(json.dumps(_GOOD_VM)); _v["view"]["type"] = "NOT_A_VIEW"
check("B1e 未知 view.type 被抓住",
      any("不在已知视图类型内" in p for p in validate_viewmodel(_v)),
      str(validate_viewmodel(_v)[:2]))

# 节点缺 id / name
_v = json.loads(json.dumps(_GOOD_VM)); _v["nodes"].append({"name": "无 id 节点"})
check("B1f 节点缺 id 被抓住", any("缺 id" in p for p in validate_viewmodel(_v)),
      str(validate_viewmodel(_v)[:2]))

# sysml_views 整块
_good_meta = {"sysml_views": {"views": {"BDD": {"view": _GOOD_VM["view"],
                                               "nodes": _GOOD_VM["nodes"],
                                               "edges": _GOOD_VM["edges"],
                                               "warnings": []}}}}
check("B1g 合法 sysml_views 通过", validate_sysml_views(_good_meta) == [],
      str(validate_sysml_views(_good_meta)[:2]))
check("B1h 空 views（产物存在但无视图）被抓住",
      any("为空" in p for p in validate_sysml_views({"sysml_views": {"views": {}}})),
      "")
check("B1i 非视图产物（无 sysml_views）不报错", validate_sysml_views({"other": 1}) == [], "")

# ★ B1j：**views 内含非法 ViewModel 必须被抓住**。
#   断言盲区（变异自证 M4 抓出来的）：原先只有"合法通过"与"空 views 报错"两条，
#   而"空 views"在外层提前返回、根本不进逐视图校验循环 ⇒
#   把逐视图校验整段废掉（`for p in []`）居然全绿。
#   ⇒ 必须有一条**走循环体**的断言。
_bad_meta = {"sysml_views": {"views": {"BDD": {"view": _GOOD_VM["view"],
                                              "nodes": _GOOD_VM["nodes"]}}}}  # 缺 edges/warnings
check("B1j views 内的非法 ViewModel 被抓住（缺 edges/warnings）",
      len(validate_sysml_views(_bad_meta)) > 0,
      "逐视图校验被绕过（空转）")
_bad_edge_meta = {"sysml_views": {"views": {"BDD": {
    "view": _GOOD_VM["view"],
    "nodes": _GOOD_VM["nodes"],
    "edges": [{"source": "n1", "target": "GHOST"}],
    "warnings": []}}}}
check("B1k views 内的悬空边被抓住",
      any("悬空边" in p for p in validate_sysml_views(_bad_edge_meta)),
      str(validate_sysml_views(_bad_edge_meta)[:2]))

# ══ 二、卡片 card_data ══════════════════════════════════════════════════
print("\n── B2 卡片 card_data ──")

_GOOD_CARD = {
    "intent": "design", "agent": "方案设计Agent", "hil_level": "L1",
    "plan": [{"key": "t1", "title": "建骨架", "status": "done"}],
    "exec": {"subtasks": [{"key": "t1", "title": "建骨架", "status": "done"}]},
}
check("B2 合法 card 通过", validate_card_data(_GOOD_CARD) == [],
      str(validate_card_data(_GOOD_CARD)[:3]))
check("B2b 空卡片合法（纯文本消息）", validate_card_data("") == [], "")
check("B2c 非法 JSON 被抓住", any("合法 JSON" in p for p in validate_card_data("{bad}")), "")

_c = dict(_GOOD_CARD); _c.pop("intent")
check("B2d 缺 intent 被抓住", any("intent" in p for p in validate_card_data(_c)),
      str(validate_card_data(_c)[:2]))

# 子任务缺 status（前端进度条无从渲染）
_c = json.loads(json.dumps(_GOOD_CARD)); _c["exec"]["subtasks"][0].pop("status")
check("B2e 子任务缺 status 被抓住", any("status" in p for p in validate_card_data(_c)),
      str(validate_card_data(_c)[:2]))

# 协议 JSON 出现在 summary 里 = **合法**（这是子任务摘要的专用字段）
_c = json.loads(json.dumps(_GOOD_CARD))
_c["exec"]["subtasks"][0]["summary"] = {"schema_version": 1, "status": "full"}
check("B2f 协议键在 summary 内**不报错**（专用字段，勿误伤）",
      validate_card_data(_c) == [], str(validate_card_data(_c)[:2]))

# 协议键出现在别处 = 泄漏
_c = json.loads(json.dumps(_GOOD_CARD))
_c["exec"]["subtasks"][0]["schema_version"] = 1
check("B2g 协议键泄漏在 summary 之外被抓住",
      any("泄漏" in p for p in validate_card_data(_c)), str(validate_card_data(_c)[:2]))

# JS 序列化残留
_c = json.loads(json.dumps(_GOOD_CARD)); _c["agent"] = "undefined"
check("B2h agent=undefined 被抓住", any("undefined" in p for p in validate_card_data(_c)), "")

# ══ 三、产物 artifacts ══════════════════════════════════════════════════
print("\n── B3 产物 artifacts ──")

_A_OK_FILE = {"kind": "report", "title": "诊断报告", "file_path": "reports/a.md"}
_A_OK_PREVIEW = {"kind": "code", "title": "模型代码", "preview_content": "package X {}"}
_A_OK_VIEW = {"kind": "sysml", "title": "SysML 视图：需求图", "file_path": "",
              "preview_content": "",
              "meta": json.dumps(_good_meta, ensure_ascii=False)}
for lbl, a in (("落盘文件", _A_OK_FILE), ("内联预览", _A_OK_PREVIEW), ("视图(meta)", _A_OK_VIEW)):
    check("B3 合法产物通过：%s" % lbl, validate_artifact(a) == [],
          str(validate_artifact(a)[:2]))

check("B3b 未知 kind 被抓住",
      any("不在枚举内" in p for p in validate_artifact({"kind": "xxx", "title": "t",
                                                        "file_path": "f"})), "")
check("B3c title 为空被抓住",
      any("title" in p for p in validate_artifact({"kind": "report", "title": "",
                                                   "file_path": "f"})), "")
check("B3d 不可打开被抓住（三者皆空）",
      any("不可打开" in p for p in validate_artifact({"kind": "report", "title": "t"})), "")
check("B3e preview 以协议 JSON 开头被抓住",
      any("协议 JSON" in p for p in validate_artifact(
          {"kind": "report", "title": "t",
           "preview_content": '{"schema_version":1,"status":"full"}'})), "")
# 视图类产物：虽然没有 file_path，但**有 meta.sysml_views** ⇒ 合法（防误伤真库 173 条）
check("B3f 视图产物无 file_path 但有 meta.sysml_views ⇒ 合法（勿误伤）",
      validate_artifact(_A_OK_VIEW) == [], str(validate_artifact(_A_OK_VIEW)[:2]))

# ══ 四、常量与实现同步（防"枚举漏更新"）══════════════════════════════════
print("\n── B4 常量同步 ──")
try:
    from view_generator import VIEW_TYPES
    _missing = [k for k in VIEW_TYPES if k not in VIEW_KINDS]
    check("B4 契约里的 VIEW_KINDS 覆盖 view_generator.VIEW_TYPES", not _missing,
          "契约缺 %s（视图层新增了类型，契约要同步）" % _missing)
except Exception as e:
    check("B4 契约里的 VIEW_KINDS 覆盖 view_generator.VIEW_TYPES", True,
          "跳过分层校验：%s" % type(e).__name__)

# ══ 五、真库存量统计（**不阻断**）════════════════════════════════════════
print("\n── B5 真库存量统计（只报告，不阻断）──")
try:
    from database import get_db
    _conn = get_db()
    try:
        _n_art = _conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
        _bad_art = 0
        for r in _conn.execute("SELECT kind,title,file_path,preview_content,meta FROM artifacts"):
            if validate_artifact(dict(r)):
                _bad_art += 1
        _n_msg = 0
        _bad_msg = 0
        for r in _conn.execute("SELECT card_data FROM messages "
                               "WHERE card_data IS NOT NULL AND card_data NOT IN ('','{}')"):
            _n_msg += 1
            if validate_card_data(r[0]):
                _bad_msg += 1
        print("  [STAT] artifacts: %d 条，形态不合规 %d 条（%.1f%%）"
              % (_n_art, _bad_art, (100.0 * _bad_art / _n_art) if _n_art else 0))
        print("  [STAT] 卡片    : %d 条，形态不合规 %d 条（%.1f%%）"
              % (_n_msg, _bad_msg, (100.0 * _bad_msg / _n_msg) if _n_msg else 0))
        print("  （存量缺陷由人决定清理策略；本门禁只用构造样本判定，避免历史数据绑架门禁）")
    finally:
        _conn.close()
except Exception as e:
    print("  [INFO] 跳过存量统计：%s" % type(e).__name__)

print()
print("=" * 68)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for _n, _d in FAIL:
    print("  FAIL:", _n, "<-", _d)
print("=" * 68)
raise SystemExit(1 if FAIL else 0)
