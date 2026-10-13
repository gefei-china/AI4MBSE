# -*- coding: utf-8 -*-
"""批次二 P2 门禁：协议三数组真有值 + 交付物物化进 subtask_artifacts。

**背景（真库实证，口径 2026-10-12 只读）**：
  ① `agent_tasks.summary_json` 的 `artifacts`/`evidence`/`risks` **非空率 0%**
     （180 行全空）—— 三数组的键存在，值恒为空列表；
  ② `subtask_artifacts` **0 行** —— 交付物物化层完全空转。

**根因不是校验太松，而是"没人要求"**（取证）：全仓搜 `schema_version` 只出现在
`agent/registry.py` 的**协议版本协商**里；子 Agent 的 prompt 中**根本没有这段契约**
⇒ 模型只能返回纯文本 ⇒ 永远走 `normalize_text`（三数组恒空）⇒ 没有任何东西可物化。
**要一个字段有值，得先有人要求它有值。**

**本次三处改动（缺一不可）**：
  1. **注入契约**（`stream.py` 子任务 query 追加交付格式说明，开关 `inject_contract`）
  2. **能解析"正文 + 尾部 JSON"**（`subtask_protocol.extract_protocol_json`）
     ⚠️ 只做 1 不做 2 是**无效**的：调用方原解析只试 `startswith("{")`，
     对"正文在前"的形态恒为 False ⇒ 注入了也拿不到 —— 这是实测踩到的真缺口。
  3. **物化**（三数组写进 `subtask_artifacts`，开关 `materialize`）

**本脚本守什么**：
  [A] 三处改动都在（缺任一条则本条无意义）
  [B] 解析器真能抽出「正文 + 尾部 JSON」（含正文含花括号、字符串内含 `}`、坏 JSON）
  [C] 回落安全：抽不出 ⇒ 走 normalize_text，行为与改动前一致
  [D] 物化写入 subtask_artifacts，且 evidence/risks **标题不为空**（口径按 schema 的 required）
  [D] 开关可回退，且**关掉即回到改动前行为**
  [E] 变异自证：把解析器改回"只认整段 JSON" ⇒ B 组必须 FAIL（证明 2 是承重的）

**只读**：临时库（真库备份副本），**不写生产库**；变异在内存副本做。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_protocol_artifacts.py`
"""
import io
import os
import re
import sys
import json
import sqlite3
import tempfile
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STREAM = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
PROTO = os.path.join(ROOT, "services", "subtask_protocol.py")
CONFIG = os.path.join(ROOT, "core", "config.py")
PROD_DB = os.path.join(ROOT, "mbse.db")

_ok = 0
_fail = 0
_muts = []


def check(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  [PASS] %s" % name)
    else:
        _fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


sys.path.insert(0, ROOT)
from services.subtask_protocol import (extract_protocol_json, validate,  # noqa: E402
                                       normalize_text)

s_src = io.open(STREAM, encoding="utf-8").read()
p_src = io.open(PROTO, encoding="utf-8").read()
c_src = io.open(CONFIG, encoding="utf-8").read()

# 子任务输出的真实形态：正文在前 + 尾部单个 JSON（这是 prompt 要求的形式）
# ⚠️ risks 的字段名**必须与 SUMMARY_SCHEMA 一致**（level/title/desc/advice）——
#    写成 issue/mitigation 会被 validate() 丢弃成 {"level":"mid","title":"","desc":"","advice":""}
#    ⇒ 物化出**空标题行**（这正是 D4 抓到的问题）。
_JK = ('{"status":"full","summary":"完成建模",'
       '"artifacts":[{"kind":"model","title":"MooStateMachine","ref":"view/state"}],'
       '"evidence":[{"source":"需求视图","ref":"r1"}],'
       '"risks":[{"level":"high","title":"异常转换未处理","desc":"未覆盖中断态",'
       '"advice":"下轮补齐"}],'
       '"confidence":0.85}')

print("=== A. 三处改动都在 ===")
check("A1 ① 注入契约（子任务 query 追加交付格式）", "【交付格式】" in s_src)
check("A2 ① 开关 inject_contract 可回退", 'inject_contract", True' in s_src)
check("A3 ② 解析器已接线（用 extract 而非 startswith）",
      "extract_protocol_json as _sp_extract" in s_src and "_sp_extract(_s0)" in s_src)
check("A4 ② 旧的 startswith(\"{\") 判据已移除",
      '_s0.startswith("{")' not in s_src,
      "仍用旧判据 ⇒ 正文+JSON 形态永远抽不出")
check("A5 ③ 物化写入 subtask_artifacts",
      "from services.artifact_materializer import store as _art_store" in s_src)
check("A6 ③ 开关 materialize 可回退", 'materialize", True' in s_src)
check("A7 协议模块 import 了 json（否则 json.loads 抛 NameError 被 except 吞掉）",
      re.search(r"^import json", p_src, re.M) is not None,
      "★ 本项曾是真 bug：模块只 import re，导致 extract 永远返回 None 且无任何报错")

# A8 ★ prompt 里给的字段名必须与 SUMMARY_SCHEMA 的 required **逐字一致**。
#   这是 D4 抓出来的真缺口：prompt 原本让模型写 risks[].issue/mitigation，
#   而 schema 要的是 level/title/desc/advice ⇒ validate() 把整条丢弃成空壳
#   ⇒ 物化出空标题行，"库里有行但没法看"，等于没物化。
#   ⇒ 这里做**字面比对**：prompt 里出现的 risks 字段名 ⊆ schema required。
_prompt_risks = re.search(r'"risks":\[\{(.*?)\}\]', s_src, re.S)
# ⚠️ 只取**数组内部**的字段名（group(1)），不能把外层的 `"risks":` 也算进来
#    —— 否则断言永远失败（prompt 多了个叫 "risks" 的字段），是一条自造假红。
_p_risks_fields = set(re.findall(r'"(\w+)"\s*:', _prompt_risks.group(1))) if _prompt_risks else set()
check("A8 prompt 给出了 risks 字段声明", bool(_prompt_risks), "prompt 里找不到 risks 的 JSON 模板")
_schema_risks = re.search(r'"risks":\s*\{.*?"required":\s*\[([^\]]+)\]', p_src, re.S)
_req_risks = set(re.findall(r'"(\w+)"', _schema_risks.group(1))) if _schema_risks else set()
print("  prompt risks 字段: %s｜schema required: %s" % (sorted(_p_risks_fields), sorted(_req_risks)))
check("A9 prompt 的 risks 字段 ⊆ schema required（写错会被静默丢弃）",
      bool(_p_risks_fields) and _p_risks_fields <= _req_risks,
      "prompt 多写了 schema 没有的字段: %s" % sorted(_p_risks_fields - _req_risks))
check("A10 prompt 的 risks 字段覆盖 schema 全部 required",
      bool(_req_risks) and _req_risks <= _p_risks_fields,
      "prompt 漏了: %s" % sorted(_req_risks - _p_risks_fields))

print("\n=== B. 解析器真能抽出「正文 + 尾部 JSON」===")
_cases = [
    ("纯 JSON（改动前的唯一形态）", _JK),
    ("正文 + 尾部 JSON（本次要支持的形态）", "# 状态机视图已生成\n\n完成 4 状态 3 转换。\n\n" + _JK),
    ("正文含花括号（配平不能被带偏）", "分析 {注1} 的结果：\n\n" + _JK),
    ("字符串字面量内含 }（配平须跳过字符串）",
     '正文里写了 "}" 这种字符\n\n' + _JK),
    ("多段 JSON（只取最后一个）", "正文\n\n{\"a\":1}\n\n" + _JK),
]
for _name, _t in _cases:
    _p = extract_protocol_json(_t)
    check("B:%s ⇒ 抽出 dict" % _name, isinstance(_p, dict), "got=%r" % (_p,))
    if isinstance(_p, dict):
        _s = (validate(_p).get("sanitized") or {})
        check("B:%s ⇒ 三数组非空" % _name,
              bool(_s.get("artifacts")) and bool(_s.get("evidence")) and bool(_s.get("risks")),
              "art=%s ev=%s rk=%s" % (len(_s.get("artifacts") or []),
                                      len(_s.get("evidence") or []),
                                      len(_s.get("risks") or [])))

print("\n=== C. 回落安全（抽不出则行为与改动前一致）===")
for _name, _t in (("纯文本无 JSON", "这就是一段普通回答，没有任何 JSON。"),
                  ("坏 JSON", '正文\n\n{"status": "full", broken}'),
                  ("空串", ""),
                  ("None", None)):
    _p = extract_protocol_json(_t)
    check("C:%s ⇒ 抽出 None" % _name, _p is None, "got=%r" % (_p,))
    check("C:%s ⇒ 回落 normalize_text 可用" % _name,
          bool(normalize_text(_t)["status"]))
check("C5 非 dict 的 JSON（数组）不误认成协议对象",
      extract_protocol_json("[1,2,3]") is None)

print("\n=== D. 物化真落库（临时库）===")
TMP = tempfile.mkdtemp(prefix="verify_proto_art_")
TMP_DB = os.path.join(TMP, "t.db")
try:
    _c = sqlite3.connect(TMP_DB)
    _c.row_factory = sqlite3.Row
    _c.execute("""CREATE TABLE subtask_artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, task_key TEXT,
        artifact_idx INTEGER, kind TEXT, title TEXT, content_json TEXT,
        materialized INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    _c.commit()
    from services.artifact_materializer import store as art_store
    # 复刻 stream.py 的物化（含新的标题字段口径）
    _proto = validate(extract_protocol_json("正文\n\n" + _JK)).get("sanitized") or {}
    _arts = []
    for _k, _kind, _tks in (("artifacts", "artifact", ("title", "ref", "name")),
                             ("evidence", "evidence", ("source", "ref", "title")),
                             ("risks", "risk", ("title", "desc", "advice"))):
        for _it in (_proto.get(_k) or []):
            if isinstance(_it, dict):
                _t = ""
                for _tk in _tks:
                    if str(_it.get(_tk) or "").strip():
                        _t = str(_it.get(_tk))[:80]
                        break
                _arts.append({"kind": _kind, "title": _t, "content_json": _it})
    _refs = art_store(_c, 9999, "t1", _arts)
    _rows = _c.execute("SELECT kind,title FROM subtask_artifacts WHERE run_id=9999").fetchall()
    print("  物化出 %d 行：%s" % (len(_rows),
                                [(r["kind"], r["title"][:16]) for r in _rows]))
    check("D1 三数组都物化进表（此前该表 0 行）", len(_rows) == 3, "got=%d" % len(_rows))
    check("D2 refs 可回引用（subtask:// 协议）",
          len(_refs) == 3 and all(r.startswith("subtask://") for r in _refs))
    _kinds = {r["kind"] for r in _rows}
    check("D3 三类 kind 齐全（artifact/evidence/risk）",
          _kinds == {"artifact", "evidence", "risk"}, str(_kinds))
    _empties = [r["kind"] for r in _rows if not (r["title"] or "").strip()]
    check("D4 evidence/risks **标题不为空**（按各自 schema 的 required 取字段）",
          not _empties, "空标题的 kind：%s" % _empties)
    _c.close()
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n=== E. 真库基线（只读，记录改动前状态）===")
if os.path.exists(PROD_DB):
    _pc = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _sa = _pc.execute("SELECT COUNT(*) FROM subtask_artifacts").fetchone()[0]
    _rows = [r[0] for r in _pc.execute(
        "SELECT summary_json FROM agent_tasks WHERE summary_json NOT IN ('','{}')")]
    _ne = 0
    for _s in _rows:
        try:
            d = json.loads(_s)
        except Exception:
            continue
        if isinstance(d, dict) and (d.get("artifacts") or d.get("evidence") or d.get("risks")):
            _ne += 1
    _pc.close()
    print("  真库：subtask_artifacts %d 行｜三数组非空的 summary_json %d/%d"
          % (_sa, _ne, len(_rows)))
    check("E1 记录到改动前基线为 0（本次修复的目标）", _sa == 0 or _ne == 0,
          "真库已有数据 ⇒ 本门禁的基线描述需更新")
else:
    print("  [SKIP] 未找到真库")

print("\n=== F. 变异自证（解析器退回『只认整段 JSON』）===")
# 精确复原改动前的解析形态：只试 startswith("{")，不做尾部抽取
_old = '            obj = json.loads(t)\n            return obj if isinstance(obj, dict) else None'
if _old not in p_src:
    print("  [VACUOUS] 锚点不在源码里，自证中止")
else:
    # 变异：让 ② 分支（尾部扫描）永远不命中
    _mut = p_src.replace("    end = t.rfind(\"}\")\n    if end < 0:\n        return None",
                         "    end = t.rfind(\"}\")\n    if end >= 0:\n        return None  # MUTATION: 不做尾部扫描", 1)
    _ns = {"__name__": "_mut_proto"}
    try:
        exec(compile(_mut, "<mutant>", "exec"), _ns)
        _mE = _ns["extract_protocol_json"]
        _r1 = _mE(_JK)                       # 纯 JSON：应仍能抽（改动前也支持）
        _r2 = _mE("正文\n\n" + _JK)          # 正文+JSON：改动前**抽不出**
        check("F1 变异体下『纯 JSON』仍可抽（对照组）", isinstance(_r1, dict))
        check("F2 变异体下『正文+尾部 JSON』抽不出（缺陷形态复现）",
              _r2 is None, "变异体竟还能抽出 ⇒ 自证无效")
        check("F3 现行实现能抽出（与变异体形成对照）",
              isinstance(extract_protocol_json("正文\n\n" + _JK), dict))
        _muts.append(("M1", "解析器不做尾部扫描（回到只认整段 JSON）"))
        print("  [SELF-PROOF] M1 解析器退回原形态 —— 正文+JSON 抽不出，"
              "证明第②处改动是承重的（只注入契约不改解析 = 无效）")
    except Exception as e:
        check("F1 变异体执行成功", False, str(e))

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)