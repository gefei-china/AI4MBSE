# -*- coding: utf-8 -*-
"""门禁：**智源写入必须走 MCP 正路** + 写入目标由**解析**而非**猜测**得到。

固定 2026-10-28 conv 9736~9740 暴露的四个根因：

  A. MCP 里明明有 `import_sysml_text`（mode/targetPackageId/packageName/text 齐全，
     实测 `query_projects` 可调通），但注入清单里**从来没有它**：
       · 闭包的动词族词表不含「写入」⇒ 拉不进来；
       · 闸门的动词族词表**另写一份**、同样不含「写入」⇒ 即便拉进来也剔掉；
       · 而 3 个历史上 **16 次调用全失败**的自建 HTTP 工具（`base_url` 为空）
         因为列在 `core_keep_extra` 里被**常驻注入**，模型优先用了它们。
  B. 「只有名字相近」被当成解析成功 ⇒ 会自信地给出错 id。
  C. 覆盖导入的目标闸只挂在 HTTP 分支 ⇒ 真正的 MCP 写入路径**完全绕过**。
  D. `preview_model_changes` 因 `grp in nm` 子串匹配被当成 view 族（**pre**+view），
     白占 8.3KB 契约（整轮载荷的 20%）。

判据全部取自**真库真回执**，并带变异自证（把修复拆掉后判据必须翻红）。
"""

import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

DB = os.path.join(_ROOT, "mbse.db")
_fail = []
_n = [0]


def check(tag, desc, cond, hint=""):
    _n[0] += 1
    if cond:
        print("  OK   [%s] %s" % (tag, desc))
    else:
        print("  FAIL [%s] %s  %s" % (tag, desc, ("| " + str(hint))[:300]))
        _fail.append(tag)


QUERY = ("基于工程“”广汽建模方法论1022“获取工程数据，分析并帮我设计输出通信子系统的时序图，"
         "并完成校验后，写入对应的包”SD_通信子系统时序图“，写入前需要我确认")

print("=" * 78)
print("A. 写入正路必须进注入清单（真库 + 真实输入）")
print("=" * 78)

from agent.pipeline import AgentPipeline                       # noqa: E402
from database import get_db as _getdb                          # noqa: E402

_p = AgentPipeline()
_p.registry.load_from_db(_getdb())
_user = {"id": 1, "username": "gefei", "role_name": "admin"}
_td = _p._build_tools_def("智源对接agent", QUERY, _user)
_names = [t.get("function", {}).get("name") for t in _td]

check("A1", "MCP 写入正路 import_sysml_text 已注入",
      "import_sysml_text" in _names, sorted(_names))
check("A2", "MCP 校验 validate_sysml_text 已注入（写入前必经一步）",
      "validate_sysml_text" in _names, sorted(_names))
check("A3", "MCP 建视图 create_model_view 已注入（时序图落点是视图）",
      "create_model_view" in _names, sorted(_names))

check("A4", "3 个「配置为空 ⇒ 必然失败」的自建工具不再注入",
      not any(n.startswith("zhiyuan_sysmlv2_") for n in _names),
      [n for n in _names if n.startswith("zhiyuan_sysmlv2_")])

check("A5", "重协议且与本轮无关的工具已回退（preview/apply）",
      "preview_model_changes" not in _names and "apply_model_changes" not in _names,
      sorted(_names))

_tot = sum(len(json.dumps(t, ensure_ascii=False)) for t in _td)
check("A6", "工具载荷已收敛（<35k 字符，实测 41,265 → 32,896）",
      _tot < 35000, _tot)

print()
print("=" * 78)
print("B. 动词词表必须是**单一事实源**（这次事故的结构性根因）")
print("=" * 78)

from agent import mcp_tool_meta as M                            # noqa: E402

_overlap = [g for g in M._INTENT_VERBS if g in M._MCP_WRITE_VERB_TABLE]
_same_obj = all(M._INTENT_VERBS[g] is M._MCP_WRITE_VERB_TABLE[g] for g in _overlap)
check("B1", "闭包表与闸门表在重叠族上**是同一个对象**（不可能再各自漂移）",
      _same_obj and len(_overlap) >= 5, _overlap)

check("B2", "「写入」在 import 族里（闭包侧）", "写入" in M._INTENT_VERBS["import"],
      M._INTENT_VERBS["import"][:6])
check("B3", "「写入」在 import 族里（闸门侧，同一个对象）",
      "写入" in M._MCP_WRITE_VERB_TABLE["import"], "")

check("B4", "写表键序与历史一致（调序会让同一工具换族）",
      list(M._MCP_WRITE_VERB_TABLE.keys()) == list(M._MCP_WRITE_GROUP_ORDER),
      list(M._MCP_WRITE_VERB_TABLE.keys()))

check("B5", "validate/view 未混入写表（否则读工具会被当写操作弹确认）",
      "validate" not in M._MCP_WRITE_VERB_TABLE and "view" not in M._MCP_WRITE_VERB_TABLE, "")

check("B6", "写判定未回归：remove_view_presentations 仍归 move 族",
      M.mcp_write_group("remove_view_presentations") == "move",
      M.mcp_write_group("remove_view_presentations"))
check("B7", "写判定未回归：get_model_view / validate_sysml_text 不是写",
      M.mcp_write_group("get_model_view") == "" and
      M.mcp_write_group("validate_sysml_text") == "", "")

print()
print("=" * 78)
print("C. 词元匹配（子串匹配会把 preview 当成 view）")
print("=" * 78)

H = M.name_hits_group
check("C1", "preview_model_changes **不**属 view 族", not H("preview_model_changes", "view"), "")
check("C2", "create_model_view 属 view 族", H("create_model_view", "view"), "")
check("C3", "import_sysml_text / manage_model_imports 属 import 族",
      H("import_sysml_text", "import") and H("manage_model_imports", "import"), "")
check("C4", "validate_sysml_text / validate_model 属 validate 族",
      H("validate_sysml_text", "validate") and H("validate_model", "validate"), "")

print()
print("=" * 78)
print("D. 配置就绪度判据")
print("=" * 78)

from agent import tool_readiness as R                            # noqa: E402

_conn = sqlite3.connect(DB)
_row = _conn.execute("SELECT config FROM tools WHERE name='zhiyuan_sysmlv2_import'").fetchone()
check("D1", "zhiyuan_sysmlv2_import 被判「未就绪」（其 url 依赖 zhiyuan.base_url）",
      bool(R.missing_required(_row[0]) if _row else []),
      R.missing_required(_row[0]) if _row else "no row")

check("D2", "必需项只认 url 里的占位符（headers/body 空值不误杀）",
      R.required_cfg_paths({"url": "{cfg:a.b}/x", "headers": {"H": "{cfg:c.d}"}}) == ["a.b"],
      R.required_cfg_paths({"url": "{cfg:a.b}/x", "headers": {"H": "{cfg:c.d}"}}))

check("D3", "无占位符的工具视为就绪",
      R.is_ready({"url": "http://x/y"}), "")

# D4 变异自证：把 base_url 配上 ⇒ 同一判据必须翻绿（证明判据真的看配置）
try:
    from core import config as _cfg
    _bak = _cfg.get("zhiyuan", "base_url", "")
    _cfg.save_override({"zhiyuan.base_url": "http://127.0.0.1:9"})
    _ok_now = R.is_ready(_row[0]) if _row else False
    _cfg.save_override({"zhiyuan.base_url": _bak or ""})
    check("D4", "变异自证：填上 base_url 后同一工具转为就绪（判据真看配置）",
          _ok_now, "填了仍判未就绪 ⇒ 判据是假的")
finally:
    try:
        from core import config as _cfg2
        _cfg2.save_override({"zhiyuan.base_url": _bak or ""})
    except Exception:
        pass

print()
print("=" * 78)
print("E. 写入目标**解析**（不是校验）—— 用真机会话回执")
print("=" * 78)

from agent import write_target_resolver as W                     # noqa: E402

_r9736 = W.resolve(_conn, 9736, QUERY)
check("E1", "conv 9736 精确解出目标包（SD_通信子系统时序图）",
      _r9736["status"] == "resolved" and "SD_通信子系统时序图" == _r9736["name"],
      _r9736)
check("E2", "解出的 id 与真机回执一致（2115359040604536832）",
      _r9736["target_id"] == "2115359040604536832", _r9736["target_id"])
check("E3", "同时解出父级名（可用于卡片展示：写在谁的下面）",
      _r9736["parent_name"] == "通信子系统", _r9736["parent_name"])

# E4 ★ 只有名字相近 ⇒ 必须 ambiguous（不能自信给错 id）
_r9740 = W.resolve(_conn, 9740, QUERY)
check("E4", "★conv 9740（只有「通信子系统时序图」这种相近名）判 ambiguous 而非 resolved",
      _r9740["status"] == "ambiguous", _r9740)
check("E5", "ambiguous 时把候选交给用户选（不猜）",
      bool(_r9740["candidates"]), _r9740.get("candidates"))

check("E6", "闸判定：父包 id ⇒ wrong",
      W.check("2114902516325482496", _r9736)[0] == "wrong", "")
check("E7", "闸判定：正确 id ⇒ ok",
      W.check("2115359040604536832", _r9736)[0] == "ok", "")
check("E8", "闸判定：解析不出 ⇒ unknown（不阻断，交人工）",
      W.check("2115359040604536832", _r9740)[0] == "unknown", "")

print()
print("=" * 78)
print("F. 统一写入网关（MCP 与 HTTP 两条通道都过同一道闸）")
print("=" * 78)

_ic = {"user_input": QUERY}
_bad = _p._import_target_gate(
    "import_sysml_text",
    {"mode": "OVERWRITE_PACKAGE", "targetPackageId": "2114902516325482496",
     "requestId": "r", "text": "package p {}"}, _ic, 9736)
check("F1", "MCP import_sysml_text 喂父包 id ⇒ 被拦（旧实现漏的正是这条）",
      bool(_bad) and _bad.get("code") == "WRONG_TARGET_PACKAGE", _bad)
check("F2", "拦截回执里**直接给出正确 id**（模型可自我修正，不用下一轮猜）",
      bool(_bad) and "2115359040604536832" in str(_bad.get("result")), "")

_ok = _p._import_target_gate(
    "import_sysml_text",
    {"mode": "OVERWRITE_PACKAGE", "targetPackageId": "2115359040604536832",
     "requestId": "r", "text": "package p {}"}, _ic, 9736)
check("F3", "正确 id ⇒ 放行", _ok is None, _ok)

check("F4", "查询类工具不经过本闸（不误伤读操作）",
      _p._import_target_gate("query_data_tree", {"parentId": "x"}, _ic, 9736) is None, "")
check("F5", "ADD_PACKAGE（新建、无目标 id）不拦",
      _p._import_target_gate("import_sysml_text",
                             {"mode": "ADD_PACKAGE", "packageName": "P"}, _ic, 9736) is None, "")

_src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py"), encoding="utf-8").read()
check("F6", "网关位置在 MCP 分派**之前**（否则真写入路径绕过）",
      _src.find("_itg_res = self._import_target_gate") <
      _src.find("MCP 工具：从 DB 找所属服务器端点") > 0, "")
check("F7", "旧的内联闸已移除（不留两份同语义实现）",
      "if str(name) == \"zhiyuan_sysmlv2_import\":" not in _src, "")
check("F8", "tools.py 补上了模块级 logger（此前 5 处留痕全抛 NameError 被静默吞掉）",
      "logger = _logging.getLogger(__name__)" in _src, "")

print()
print("=" * 78)
print("G. 变异自证：把修复拆掉，判据必须翻红")
print("=" * 78)

# G1：把「写入」从 import 族里去掉 ⇒ A1 那条必然不成立
_orig_imp = M._VERB_SYNONYMS["import"]
try:
    M._VERB_SYNONYMS["import"] = tuple(w for w in _orig_imp if w != "写入")
    M._INTENT_VERBS["import"] = M._VERB_SYNONYMS["import"]
    _meta = M.load_tool_meta({"tool_meta": _conn.execute(
        "select tool_meta from mcp_servers where id=161").fetchone()[0]})
    _picked, _ = M.pick_for_injection(_meta, QUERY, min_keep=8, top_n=0)
    _mut_no_write_word = "import_sysml_text" not in _picked
finally:
    M._VERB_SYNONYMS["import"] = _orig_imp
    M._INTENT_VERBS["import"] = _orig_imp

_picked2, _ = M.pick_for_injection(_meta, QUERY, min_keep=8, top_n=0)
check("G1", "★变异自证：去掉「写入」一词后 import_sysml_text 落选（当前实现必须入选）",
      _mut_no_write_word and ("import_sysml_text" in _picked2),
      f"变异={_mut_no_write_word} 现状={('import_sysml_text' in _picked2)}")

# G2：体积回退本身要**可证生效** —— 把阈值压到极低，闭包补入的重工具必须被砍掉，
#     同时"命中动词族"的正路工具必须**砍不掉**（否则规矩写反了）。
#     ⚠️ 第一版这里是"取消回退 ⇒ preview 应回来"，实测不成立：preview 现在是被
#        **词元匹配**挡住的（不再误判为 view 族），与体积回退是两道独立防线。
#        把断言挂在错误的那道防线上 = 一条永远为真/为假的假判据，故重写。
_orig_cap = M.MCP_HEAVY_SCHEMA_CHARS
try:
    M.MCP_HEAVY_SCHEMA_CHARS = 10 ** 9
    _p_high, _ = M.pick_for_injection(_meta, QUERY, min_keep=8, top_n=0)
    M.MCP_HEAVY_SCHEMA_CHARS = 200
    _p_low, _ = M.pick_for_injection(_meta, QUERY, min_keep=8, top_n=0)
finally:
    M.MCP_HEAVY_SCHEMA_CHARS = _orig_cap
_cut = set(_p_high) - set(_p_low)
check("G2", "变异自证：阈值压到 200 后确实砍掉闭包补入的重工具（体积回退真生效）",
      len(_cut) >= 1 and len(_p_low) < len(_p_high), f"被砍 {len(_cut)} 个")
check("G2b", "阈值压到 200 时，命中动词族的正路工具**砍不掉**（import/validate/view）",
      all(n in _p_low for n in ("import_sysml_text", "validate_sysml_text", "create_model_view")),
      [n for n in ("import_sysml_text", "validate_sysml_text", "create_model_view") if n not in _p_low])

# G3：把名字匹配退回子串匹配 ⇒ preview 又会被当 view 族
_orig_h = M.name_hits_group
try:
    M.name_hits_group = lambda nm, g: (g or "").lower() in str(nm or "").lower()
    _mut_sub = M.name_hits_group("preview_model_changes", "view")
finally:
    M.name_hits_group = _orig_h
check("G3", "变异自证：改回子串匹配后 preview 会被误判为 view 族",
      _mut_sub and not H("preview_model_changes", "view"), f"变异={_mut_sub}")

_conn.close()

print()
print("=" * 78)
print("合计 %d 条，失败 %d 条" % (_n[0], len(_fail)))
if _fail:
    print("FAIL:", ", ".join(_fail))
print("=" * 78)
sys.exit(1 if _fail else 0)
