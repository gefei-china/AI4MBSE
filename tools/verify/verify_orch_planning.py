# -*- coding: utf-8 -*-
"""B1-4 门禁：主路径 planner prompt 补齐 `expected_output`（任务契约 0.5% 填充率）。

**背景（对标取证）**：实测 `agent_tasks.expected_output` **222 行里只有 1 行有值（0.5%）**，
而**整条消费链都在**：
  · DDL 有列（`schema.py:883`）
  · `task_queue.py::create_plan` 会落库（`:36,42`）
  · `workflows/planner.py::build_subtask_context` 会把它作为「期望产出」注入子任务 prompt（`:32-34`）
  · `orch_checkpoint.py:278` / `orchestration.py:489` 也会读
⇒ **唯一缺的是"让 LLM 产出它"** —— 而主路径 `stream.py` 的 `plan_prompt`
**只列了 5 个字段**（key/title/agent/deps/task_type/context），
非流式的 `workflows/planner.py:227-240` 列了 8 个 ⇒ **两套 prompt 漂移**，
而主路径是前端唯一入口 ⇒ 99.5% 的任务没有完成标准。

**本脚本守什么**：
  [A] 主路径 prompt 含 expected_output（且措辞与非流式版对齐）
  [B] 顺带补齐 tools/skills（同样只在非流式版有，主路径缺）
  [C] 两套 prompt 的**字段集合一致**（防再次漂移）—— 这是本门禁的核心价值
  [D] 解析不丢字段（`plan` 是 dict 直传，非白名单取值）
  [E] 落库链路会写 expected_output
  [F] 变异自证：从主路径 prompt 删掉 expected_output ⇒ A/C 必须 FAIL

**只读**：只读源码 + 只读真库，**不改任何生产文件**（变异在内存副本上做）。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_orch_planning.py`
"""
import io
import os
import re
import sys
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STREAM = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
PLANNER = os.path.join(ROOT, "workflows", "planner.py")
TQUEUE = os.path.join(ROOT, "task_queue.py")
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


def task_fields(prompt_src):
    """从一段 planner prompt 源码文本里抽出它列出的 JSON 字段名集合。

    做法：找形如 `"key": "t1", "title": ...` 的**字段声明段**（prompt 里给 LLM 看的
    格式样例），把其中的键名收齐。用启发式：定位 `'{"tasks":` 到对应闭合的片段。
    ⚠️ 刻意**不**去扫全文所有 `"xxx":`，否则会把 Python 字典字面量的键也算进来。
    """
    m = re.search(r'\{"tasks":\s*\[', prompt_src)
    if not m:
        return set()
    seg = prompt_src[m.start(): m.start() + 1200]
    # 截到该片段里最后一个引号结束，避免吃到后面的代码
    return set(re.findall(r'"\s*([a-z_]+)\s*"\s*:', seg))


print("=== A. 主路径 prompt 含 expected_output ===")
s_src = io.open(STREAM, encoding="utf-8").read()
p_src = io.open(PLANNER, encoding="utf-8").read()

check("A1 stream.py 出现 expected_output 字段声明",
      '"expected_output"' in s_src,
      "主路径 planner prompt 仍无该字段")
check("A2 要求每个任务都写（不是可空）",
      "**每个任务都要写 expected_output**" in s_src,
      "未强调必填，LLM 仍会大量留空")
check("A3 说明了用途（下游注入子任务）",
      "下游据此把完成标准注入子任务" in s_src)

print("\n=== B. 顺带补齐 tools/skills ===")
check("B1 主路径含 tools 白名单字段", '"tools": ["允许的工具白名单' in s_src)
check("B2 主路径含 skills 字段", '"skills": ["指定技能' in s_src)

print("\n=== C. 两套 prompt 字段集合一致（核心价值：防漂移）===")
_sf = task_fields(s_src)
_pf = task_fields(p_src)
print("  主路径(stream.py) 字段: %s" % sorted(_sf))
print("  非流式(planner.py) 字段: %s" % sorted(_pf))
# 非流式版用了双花括号转义（{{ }}），抽出后应当一致
_missing_in_stream = _pf - _sf
_missing_in_planner = _sf - _pf
check("C1 主路径不缺非流式已声明的字段",
      not _missing_in_stream, "缺: %s" % sorted(_missing_in_stream))
check("C2 非流式不缺主路径已声明的字段",
      not _missing_in_planner, "缺: %s" % sorted(_missing_in_planner))
check("C3 两套字段集合完全相同（无漂移）",
      _sf == _pf, "stream-only=%s planner-only=%s"
      % (sorted(_sf - _pf), sorted(_pf - _sf)))
for _must in ("key", "title", "agent", "deps", "task_type", "context",
              "expected_output", "tools", "skills"):
    check("C4.%s 两套都有" % _must, _must in _sf and _must in _pf,
          "stream=%s planner=%s" % (_must in _sf, _must in _pf))

print("\n=== D. 解析不丢字段（plan 是 dict 直传）===")
# 主路径解析：plan = [t for t in ((data or {}).get("tasks") or []) if isinstance(t, dict) ...]
check("D1 解析为 dict 直传（非白名单取值）",
      re.search(r'plan\s*=\s*\[t\s+for\s+t\s+in', s_src) is not None,
      "解析形态变了，需重新确认是否丢字段")
check("D2 过滤条件只要求 key 存在（不裁字段）",
      'isinstance(t, dict) and t.get("key")' in s_src)

print("\n=== E. 落库链路会写 expected_output ===")
t_src = io.open(TQUEUE, encoding="utf-8").read()
check("E1 INSERT 语句含 expected_output 列", "expected_output" in t_src.split("INSERT INTO agent_tasks")[1][:400]
      if "INSERT INTO agent_tasks" in t_src else False)
check("E2 取值来自 t.get(\"expected_output\")", 't.get("expected_output")' in t_src)
check("E3 build_subtask_context 会注入「期望产出」",
      '期望产出' in p_src)

print("\n=== F. 真库口径：填充率现状（说明改动前的基线）===")
if os.path.exists(PROD_DB):
    _c = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _tot = _c.execute("SELECT COUNT(*) FROM agent_tasks").fetchone()[0]
    _n = _c.execute("SELECT COUNT(*) FROM agent_tasks"
                    " WHERE expected_output IS NOT NULL AND TRIM(expected_output)!=''").fetchone()[0]
    _ctx = _c.execute("SELECT COUNT(*) FROM agent_tasks"
                      " WHERE context IS NOT NULL AND TRIM(context)!=''").fetchone()[0]
    _c.close()
    _rate = 100.0 * _n / max(_tot, 1)
    print("  expected_output %d/%d = %.1f%%｜context %d/%d = %.1f%%"
          % (_n, _tot, _rate, _ctx, _tot, 100.0 * _ctx / max(_tot, 1)))
    # 2026-10-21 重标定（原判据 `_n <= 5`，按门禁自带指引更新）：
    #   原判据写于修复上线当天（1/222=0.5%）。此后真库自然积累到 10/253=4.0%
    #   （run 538/539/541，均为修复上线后的新计划）——**这是修复生效的证据，
    #   不是退化**。绝对行数判据会把"功能开始工作"误报成红。
    #   新判据改**比率上限 40%**：仍防两种真退化 —— ① 历史行被批量回填
    #   （比率会瞬间冲到接近 100%）；② 修复被悄悄撤掉（新行不再有值，
    #   比率随新任务积累持续走低，由 C 段字段漂移门禁另一路兜底）。
    check("E4 真库 expected_output 填充率未异常（≤40%；防批量回填/伪造基线）",
          _rate <= 40.0, "%.1f%%（%d/%d 行）" % (_rate, _n, _tot))
else:
    print("  [SKIP] 未找到真库")

print("\n=== G. 变异自证（从主路径 prompt 删掉 expected_output，判据必须失效）===")


def _mut_drop_expected(s):
    """精确复原 2026-10-13 之前的形态：从主路径字段声明里去掉 expected_output。"""
    return s.replace(
        ' \' "expected_output": "该子任务的完成标准（可验证的交付物描述，50字内）",\'\n',
        "", 1)


if '"expected_output"' in s_src:
    _m = _mut_drop_expected(s_src)
    if _m == s_src:
        print("  [VACUOUS] 变异未生效（revert 未命中）")
    else:
        _mf = task_fields(_m)
        check("G1 变异体（删字段）上 C1 必须 FAIL",
              ("expected_output" not in _mf),
              "变异体里字段仍在 = 断言抓不住变异，自证无效（fields=%s）" % sorted(_mf))
        check("G2 变异体（删字段）上 C3 字段集不再相同",
              _mf != _pf, "变异体字段集仍与非流式相同，自证无效")
        _muts.append(("M1", "主路径 planner prompt 删掉 expected_output 字段声明"))
        print("  [SELF-PROOF] M1 主路径 prompt 删字段 —— 字段集合随即与非流式漂移")
else:
    print("  [VACUOUS] 锚点不在源码里，自证中止")

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)
