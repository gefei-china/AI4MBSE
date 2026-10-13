# -*- coding: utf-8 -*-
"""批次二 §08 门禁：事实/推断分档（evidence.basis）。

**文章要求（§08）**：交付物里要能区分「**已验证事实**」与「**模型推断**」，
读者才知道哪句可以直接信、哪句要复核。

**现状（本条要补的）**：`evidence[]` 只有 `source`（graph/vector/attachment），
答的是"**从哪儿找到的**"，**没有一维答"这是查到的还是想出来的"**。
两者正交：一条 `source=graph` 的依据，既可能是真查了图谱，也可能是模型编的。

**设计上的三个刻意取舍**（写在代码注释里，这里也钉住）：

1. **三档而不是"真/假"二元**：`verified`（有可复核引用）/ `derived`（由已验证事实推出）/
   `asserted`（无引用的直接断言）。
2. **不叫 `inferred`** —— 那会暗示"推理结论因而可靠"，而"无引用的断言"恰恰是**最不可靠**的
   一档。命名不能误导读者。
3. **缺字段兜底到 `asserted`（最保守的一档）**：宁可把"没标"一律当未验证。
   兜到 `verified` 那一侧等于**用一个能高估自己的标签去决定内容可信度** —— 这是本条最危险的
   方向，写反了整条改动就是负收益。

**能力边界（门禁 E 组专门守这条）**：本字段是**模型自报**，服务端**只做枚举校验与截断，
不验证真实性**。因此正确用法是**呈现**（让读者看出哪些无引用），**不是**自动采信/自动过滤。

**本脚本守什么**：
  [A] 枚举与 schema 到位（basis 可选、不列进 required ⇒ 存量不红）
  [B] 归一化：合法值保留；非法值降级 + 告警；**缺失 → asserted（方向不能反）**
  [C] prompt 真的要求模型标，且把枚举值说全
  [D] 汇总段按 basis 分档呈现，且**不是过滤**（asserted 仍出现在文本里）
  [E] **能力边界**：不得出现"按 basis 自动丢弃 asserted"这类逻辑
  [F] 变异自证：把兜底方向改成 verified ⇒ B2 必须 FAIL

**只读**：真库 `mode=ro`；变异在内存副本做，**不写回源文件**。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_fact_inference.py`
"""
import io
import os
import re
import sys
import json
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROTO = os.path.join(ROOT, "services", "subtask_protocol.py")
STREAM = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
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

p_src = io.open(PROTO, encoding="utf-8").read()
s_src = io.open(STREAM, encoding="utf-8").read()

# 合法的最小协议样本（task_key 必须给，否则 ok=False 只是占位告警，干扰判读）
def P(evs, conf="0.8"):
    return json.dumps({
        "schema_version": 1, "task_key": "t1", "status": "full", "summary": "s",
        "artifacts": [], "evidence": evs, "risks": [], "confidence": conf,
    }, ensure_ascii=False)


print("=== A. 枚举与 schema 到位 ===")
check("A1 BASIS_VALUES 三档",
      set(sp.BASIS_VALUES) == {"verified", "derived", "asserted"}, str(sp.BASIS_VALUES))
check("A2 DEFAULT_BASIS 是最保守的 asserted（不是 verified）",
      sp.DEFAULT_BASIS == "asserted", sp.DEFAULT_BASIS)
check("A3 schema 里登记了 basis", '"basis"' in p_src)
check("A4 basis **不列进 evidence.required**（否则存量无该字段 ⇒ 全红）",
      re.search(r'"evidence":.*?"required":\s*\["source",\s*"ref"\]', p_src, re.S) is not None,
      "required 里出现了 basis ⇒ 旧数据全部判红")
check("A5 模块文档写明了能力边界（自报、不验证真实性）",
      "不验证真实性" in p_src or "不验证" in p_src)
check("A6 命名避开了 inferred（会暗示可靠）",
      "inferred" not in "".join(sp.BASIS_VALUES))

print("\n=== B. 归一化与兜底方向 ===")
_r = sp.validate(json.loads(P([{"source": "graph", "ref": "e1", "basis": "verified"}])))
check("B1 合法 verified 保留",
      _r["sanitized"]["evidence"][0]["basis"] == "verified", str(_r["sanitized"]["evidence"]))
_r = sp.validate(json.loads(P([{"source": "graph", "ref": "e1", "basis": "derived"}])))
check("B2 合法 derived 保留", _r["sanitized"]["evidence"][0]["basis"] == "derived")
_r = sp.validate(json.loads(P([{"source": "graph", "ref": "e1"}])))
check("B3 ★ 缺 basis ⇒ asserted（兜底往更不可靠的一档）",
      _r["sanitized"]["evidence"][0]["basis"] == "asserted",
      "got=%s" % _r["sanitized"]["evidence"][0]["basis"])
_r = sp.validate(json.loads(P([{"source": "graph", "ref": "e1", "basis": "very_sure"}])))
check("B4 非法值降级为 asserted 且留告警",
      _r["sanitized"]["evidence"][0]["basis"] == "asserted"
      and any("basis" in w for w in _r["warnings"]),
      "basis=%s warnings=%s" % (_r["sanitized"]["evidence"][0]["basis"], _r["warnings"]))
_r = sp.validate(json.loads(P([{"source": "graph", "ref": "e1", "basis": None}])))
check("B5 basis=null 也按 asserted（等价于缺失）",
      _r["sanitized"]["evidence"][0]["basis"] == "asserted",
      "got=%s" % _r["sanitized"]["evidence"][0]["basis"])
# 旧数据兼容：存量 evidence 无 basis 字段，必须仍判 ok（不因新字段而红）
check("B6 旧格式（无 basis）仍能正常校验（不判红）",
      sp.validate(json.loads(P([{"source": "graph", "ref": "e1"}])))["ok"] is True)

print("\n=== C. prompt 真的要求标 ===")
_ev_tpl = re.search(r'"evidence":\[\{[^}]*\}[^\]]*\]', s_src, re.S)
_ev_txt = _ev_tpl.group(0) if _ev_tpl else ""
check("C1 prompt 的 evidence 模板含 basis", '"basis"' in _ev_txt, _ev_txt[:80])
for _b in sp.BASIS_VALUES:
    check("C2 prompt 列出了 %s" % _b, _b in _ev_txt)
check("C3 prompt 要求 source 用枚举（不是占位「来源」）",
      "来源" not in _ev_txt, "仍是占位词 ⇒ 模型会自由发挥")
check("C4 prompt 说明了『没查证过就写 asserted』",
      "一律写 asserted" in s_src or "没查证过" in s_src)

print("\n=== D. 汇总段按 basis 分档呈现（不是过滤）===")
_seg = ""
_m = re.search(r'_evs = _sm\.get\("evidence"\) or \[\]', s_src)
if _m:
    _nxt = s_src.find("_rks = _sm.get", _m.start())
    _seg = s_src[_m.start():_nxt if _nxt > 0 else _m.start() + 1200]
check("D1 汇总段引入了 basis 排序", "_ORD" in _seg and "sorted(" in _seg)
check("D2 排序把 verified 放前", '"verified": 0' in _seg)
check("D3 **asserted 仍出现在文本里**（分档≠过滤）",
      "for e in _ev_sorted" in _seg or "_ev_sorted[:10]" in _seg)
check("D4 全部无引用时给出显式警示",
      "asserted" in _seg and ("勿直接作为验收依据" in _seg or "模型断言" in _seg))
check("D5 部分无引用时给出计数", "无引用断言" in _seg)

print("\n=== E. 能力边界：不得按 basis 自动丢弃 ===")
# 本条的核心纪律：basis 是模型自报、未经服务端验证 ⇒ 拿它去过滤内容 = 用未验证标签决定去留
_bad = []
if re.search(r"if\s+.*basis.*==\s*[\"']asserted[\"']\s*:", _seg):
    _bad.append("汇总段出现「按 basis==asserted 判定」的分支")
if re.search(r"_evs\s*=\s*\[e\s+for\s+e.*basis", s_src):
    _bad.append("出现按 basis 过滤 evidence 的列表推导")
if re.search(r"(drop|skip|filter|丢弃|过滤)[^\n]*basis", s_src):
    _bad.append("出现含 basis 的丢弃/过滤逻辑")
check("E1 没有把 asserted 自动丢弃（能力边界：只能呈现不能过滤）",
      not _bad, str(_bad))
check("E2 模块注释写明『不用于自动采信/自动过滤』",
      "自动采信" in p_src or "不是**自动采信" in p_src or "不判" in p_src)

print("\n=== F. 真库现状（只读，记录基线）===")
if os.path.exists(PROD_DB):
    _c = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _rows = [r[0] for r in _c.execute(
        "SELECT summary_json FROM agent_tasks WHERE summary_json NOT IN ('','{}')")]
    _c.close()
    _has = 0
    for _s in _rows:
        try:
            _d = json.loads(_s)
        except Exception:
            continue
        if isinstance(_d, dict):
            for _e in (_d.get("evidence") or []):
                if isinstance(_e, dict) and _e.get("basis"):
                    _has += 1
    print("  真库 summary_json %d 行；带 basis 的 evidence 条目 %d 条（改动前必然为 0）"
          % (len(_rows), _has))
    check("F1 记录到改动前基线（evidence 本就全空，basis 无从谈起）",
          _has == 0, "真库已有 %d 条带 basis ⇒ 基线描述需更新" % _has)
else:
    print("  [SKIP] 未找到真库")

print("\n=== G. 变异自证（兜底方向反转为 verified）===")
_anchor = '"basis": str(_basis) if _basis is not None else DEFAULT_BASIS,'
if _anchor not in p_src:
    print("  [VACUOUS] 锚点不在源码里，自证中止")
else:
    # 变异：把「缺失兜底」改成「缺失即 verified」—— 这正是本条最危险的写法
    _mut = p_src.replace('DEFAULT_BASIS = "asserted"', 'DEFAULT_BASIS = "verified"', 1)
    # ⚠️ 变异体必须在**干净命名空间**里 exec：本脚本顶部已 `from services import
    #    subtask_protocol as sp`，同名模块对象已在 sys.modules 里；只 exec 源码片段时，
    #    其内部的 `import json` 之类没问题，但若变异后的模块级常量没被替换到实际运行对象上，
    #    变异就"看似生效、实则没变"—— 第一版正是这样导致 B3 抓不住变异的假失败。
    #    做法：把变异体注册成一个**新的模块名**再 import，确保拿到的是新常量。
    import types
    _mod = types.ModuleType("_mut_proto_mod")
    _mod.__file__ = "<mutant>"
    try:
        exec(compile(_mut, "<mutant>", "exec"), _mod.__dict__)
        _mB = _mod.validate(json.loads(
            P([{"source": "graph", "ref": "e1"}])))["sanitized"]["evidence"][0]["basis"]
        check("G1 变异体（缺失→verified）上 B3 必须 FAIL", _mB == "verified",
              "变异体返回 %r —— B3 抓不住变异，自证无效" % (_mB,))
        # G1b 反向确认：现行实现的兜底确实与 DEFAULT_BASIS 解耦（改常量不影响行为）
        check("G1b 变异体里 DEFAULT_BASIS 确已改成 verified",
              _mod.DEFAULT_BASIS == "verified", "常量替换未生效")
        check("G2 现行实现缺失时是 asserted", sp.validate(json.loads(
            P([{"source": "graph", "ref": "e1"}])))["sanitized"]["evidence"][0]["basis"] == "asserted")
        _muts.append(("M1", "缺失兜底方向反转为 verified（用未验证标签高估可信度）"))
        print("  [SELF-PROOF] M1 兜底方向反转 —— 缺 basis 的证据被当成已验证，"
              "证明『兜底必须往更不可靠的一档’这条不可动")
    except Exception as e:
        check("G1 变异体执行成功", False, str(e))

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)