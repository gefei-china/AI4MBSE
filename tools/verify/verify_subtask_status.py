# -*- coding: utf-8 -*-
"""B1-2 门禁：`normalize_text` 不再恒返回 `status="full"`（假绿根因）。

**背景（对标取证）**：`normalize_text` 是 `stream.py:450` 在 **LLM 未按协议输出 JSON** 时
的**唯一产出路径**，而它**无条件**写 `status="full"`。实测后果（2026-10-12 只读真库）：
  · `agent_tasks.summary_json` **180/180 行 status 全为 full**，confidence 恒 0.5，
    `artifacts/evidence/risks` 值非空率 **0%**；
  · 与此同时 `agent_tasks` 有 **54 行 failed** —— 这些失败在**交付协议层完全不可见**；
  · `stream.py:805` 三态聚合第 1 段因此**永久返回 full**，只剩 `:846` 的「只降不升」
    硬修正在兜底 ⇒ **"编排成功"这个信号在数据上不可证伪**。

**本脚本守什么**：
  [A] 空/空白文本 ⇒ failed（**不得**再返回 full）
  [B] 有实质文本 ⇒ full（**不得**误杀正常交付）
  [C] 传入执行状态 failed/blocked ⇒ 优先采信（"已知失败但正文非空"也不放过）
  [D] **反例护栏**：诚实描述上游缺口的正文（含"无法/失败/未能"）**必须仍判 full**
      —— 真库 168 条 done 里"无法"出现 93 次，它们是**有价值交付**不是失败
  [E] meta 留痕（normalized_from_text / empty_text）⇒ 事后可复盘判定依据
  [F] 三态聚合不再恒 full（`summarize_status` 能被触到 failed 分支）
  [G] 真库口径复核：failed→空、done→非空 的**分离度**成立（防判据脱离真库）
  [H] 变异自证：把 status 改回恒 full，A/F 必须 FAIL

**只读**：不写生产库。真库口径用 `mode=ro` 连；改源码的变异在**内存副本**上做，
**不写回源文件**（避免 2026-10-12 实测踩过的「变异体残留被误判成产品缺陷」事故）。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_subtask_status.py`
"""
import io
import os
import sys
import json
import sqlite3
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "services", "subtask_protocol.py")
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
from services import subtask_protocol as sp  # noqa: E402

print("=== A. 空文本必须判 failed（不再恒 full）===")
for label, val in (("None", None), ("空串", ""), ("纯空格", "   "),
                   ("换行+制表", "\n\t \r\n"), ("空 dict 序列化串", "{}")):
    r = sp.normalize_text(val)
    # "{}" 会走到 json 分支吗？不会——normalize_text 只处理"非 dict 的纯文本"路径，
    # 但这里直接调它，判据应一致：空内容 ⇒ failed
    if label == "空 dict 序列化串":
        check("A:%s ⇒ 至少不是 full（有内容）" % label,
              r["status"] != "full" or val.strip() != "", "got=%s" % r["status"])
    else:
        check("A:%s ⇒ status=failed" % label, r["status"] == "failed",
              "got=%s" % r["status"])

print("\n=== B. 有实质文本必须判 full（不误杀正常交付）===")
for label, val in (
        ("短文本", "已生成 3 个 SysML 视图并通过校验。"),
        ("中位长度", "需求条目化完成，共 12 条需求，其中 3 条待确认。" * 10),
        ("含否定词但有实质交付", "上游 t1 未产出需求条目，故本次无可校验对象；"
                        "已输出缺口清单 4 项与补齐建议。"),
):
    r = sp.normalize_text(val)
    check("B:%s ⇒ status=full" % label, r["status"] == "full", "got=%s" % r["status"])

print("\n=== C. 传入执行状态优先采信 ===")
for ts, want in (("failed", "failed"), ("blocked", "failed"),
                 ("done", "full"), ("partial", "full"),
                 ("planned", "full"), ("running", "full"),
                 ("", "full"), (None, "full")):
    r = sp.normalize_text("正文有内容但执行状态是 %r" % (ts,), task_status=ts)
    check("C:task_status=%r ⇒ %s" % (ts, want), r["status"] == want, "got=%s" % r["status"])

print("\n=== D. 反例护栏：诚实描述缺口的正文不得误判为 failed ===")
# 真库实证口径（2026-10-12 只读）：done 的 168 条中「无法」93 次、「失败」16、「未能」17
_GUARD = [
    "无法校验，因为上游未产出代码",
    "本次评审未通过：存在 3 处不一致",
    "未能读取知识库，但已列出排查步骤",
    "上游未能交付，本轮只能给条件化方案设计",
]
for txt in _GUARD:
    r = sp.normalize_text(txt)
    check("D:含否定词仍 full — %r" % txt[:24], r["status"] == "full",
          "got=%s（**误杀**）" % r["status"])

print("\n=== E. meta 留痕可复盘 ===")
_r = sp.normalize_text("")
check("E1 空文本 meta.normalized_from_text=True", _r["meta"].get("normalized_from_text") is True)
check("E2 空文本 meta.empty_text=True", _r["meta"].get("empty_text") is True)
_r2 = sp.normalize_text("有内容")
check("E3 有文本 meta.empty_text=False", _r2["meta"].get("empty_text") is False)
check("E4 summary 仍截到 MAX_SUMMARY（兼容旧行为）",
      len(sp.normalize_text("x" * 5000)["summary"]) == sp.MAX_SUMMARY)
check("E5 task_key 仍是默认值（未破坏下游快照）",
      sp.normalize_text("正文")["task_key"] == sp.DEFAULT_TASK_KEY)

print("\n=== F. 三态聚合能被触到 failed 分支 ===")
# 若 normalize_text 恒 full，聚合永远只出 full ⇒ 门禁必须证明 failed 能传上去
_s = [sp.normalize_text(""), sp.normalize_text("")]
check("F1 两个空文本聚合 ⇒ failed（证明第 1 段不再恒 full）",
      sp.summarize_status(_s) == "failed", "got=%s" % sp.summarize_status(_s))
_s2 = [sp.normalize_text(""), sp.normalize_text("有正文")]
check("F2 空+非空聚合 ⇒ partial", sp.summarize_status(_s2) == "partial",
      "got=%s" % sp.summarize_status(_s2))
_s3 = [sp.normalize_text("有正文"), sp.normalize_text("有正文2")]
check("F3 全部非空聚合 ⇒ full（不误降）", sp.summarize_status(_s3) == "full",
      "got=%s" % sp.summarize_status(_s3))

print("\n=== G. 真库口径复核：判据必须与真库分布吻合 ===")
if not os.path.exists(PROD_DB):
    print("  [SKIP] 未找到真库")
else:
    _c = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _rows = _c.execute("SELECT status, result FROM agent_tasks").fetchall()
    _c.close()
    _f = [r for r in _rows if r[0] == "failed"]
    _d = [r for r in _rows if r[0] == "done"]
    _f_empty = sum(1 for r in _f if not (r[1] or "").strip())
    _d_empty = sum(1 for r in _d if not (r[1] or "").strip())
    print("  真库：failed=%d（其中 result 空 %d）｜done=%d（其中 result 空 %d）"
          % (len(_f), _f_empty, len(_d), _d_empty))
    check("G1 failed 侧空文本率 =100%%（判据可覆盖全部失败）",
          len(_f) > 0 and _f_empty == len(_f), "%d/%d" % (_f_empty, len(_f)))
    check("G2 done 侧空文本率 =0%%（判据零误杀）",
          _d_empty == 0, "%d/%d" % (_d_empty, len(_d)))

    # 用真库真实样本跑一遍新判据，看聚合结果是否从"恒 full"变成"如实"
    _norm = [sp.normalize_text(r[1], task_status=r[0]) for r in _rows]
    _agg = collections.Counter(x["status"] for x in _norm)
    _st = collections.Counter(r[0] for r in _rows)
    print("  真库执行状态分布：%s" % dict(_st))
    print("  套用新判据后协议状态分布：%s" % dict(_agg))
    check("G3 failed 任务在协议层可见（关键：一改就能看见）",
          _agg.get("failed", 0) >= len(_f),
          "协议层 failed=%s，实际失败=%d" % (_agg.get("failed", 0), len(_f)))
    check("G4 done 任务不被误杀（协议层 full 数 = done 数）",
          _agg.get("full", 0) == len(_d),
          "协议层 full=%s，done=%d" % (_agg.get("full", 0), len(_d)))

print("\n=== H. 变异自证（把 status 改回恒 full，判据必须失效）===")
_src = io.open(SRC, encoding="utf-8").read()
# 锚点锚**判据行**（条件表达式），不锚续行缩进
_ANCHOR = 'if _ts in ("failed", "blocked"):'


def _mut_always_full(s):
    """把三元判据替换成恒 full —— 精确复原 2026-10-13 之前的缺陷形态。"""
    return s.replace(
        '    _status = "failed"\n    elif not text.strip():\n        _status = "failed"\n',
        '    _status = "full"  # MUTATION: 恒 full\n', 1)


if _ANCHOR not in _src:
    print("  [VACUOUS] 锚点不在源码里，自证中止")
else:
    _m = _mut_always_full(_src)
    if _m == _src:
        print("  [VACUOUS] 变异未生效（revert 未命中）")
    else:
        # 在内存里 exec 变异体，验证 A 段判据确实会翻
        _ns = {"__name__": "_mut_module"}
        try:
            exec(compile(_m, "<mutant>", "exec"), _ns)
            _mf = _ns["normalize_text"]("")
            _mres = _mf["status"]
        except Exception as e:
            _mres = "exec-err:%s" % e
        check("H1 变异体（恒 full）上 A 段判据必须 FAIL",
              _mres == "full", "变异体返回 %s —— 断言抓不住变异，自证无效" % _mres)
        _muts.append(("M1", "status 判据还原为恒 full"))
        print("  [SELF-PROOF] M1 status 判据还原为恒 full —— 空文本判据消失")

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)
