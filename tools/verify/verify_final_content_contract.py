# -*- coding: utf-8 -*-
"""门禁：最终正文的**形态契约**（2026-11-04 新增，米爸「改了之后还有问题没」）。

── 为什么要有这个门禁 ────────────────────────────────────────────────────
本仓已有 20+ 个 Guard（预算、resume、partial、loop、路由…），但它们守的全是
**数值/流程不变式**；**没有一条**守"最终正文长什么样"。
⇒ 于是这类缺陷能一路穿到用户眼前：

  真库实证（conv 990408 / msg 5761，21432 字）三条同时发生：
    ① **裸 JSON 出现在正文**：协议 JSON 被抽出来存 `summary_json`，但**原文没删**
       ⇒ 用户看到一坨没有结构、没有代码块收敛的 JSON；
    ② **多组「一、二、三」**：汇总输入按 1600 字符头尾裁剪 ⇒ LLM 拿到三份**残缺**
       交付物 ⇒ "整合"退化成**拼接** ⇒ `## 一、结论先行` 出现 2 次；
    ③ **围栏奇偶失配**：正文 13 个 ```（奇数）⇒ 末段代码块没闭合，
       尾部「连接已中断」提示落进代码块内部。

  根因是同一条链上的三处：
    · `services/subtask_protocol.py::extract_protocol_json` 只"抽"不"删"；
    · `workflows/planner.py::_head_tail_clip` 切口会**从中间切开** ``` 围栏；
    · `routers/conversations.py` 追加中断提示前不闭合围栏。

── 本门禁守什么 ─────────────────────────────────────────────────────────
  C1 协议 JSON **不得**出现在最终正文
  C2 剥离**不得**误删正文实质内容
  C3 「抽出来的那份」与「删掉的那段」必须是**同一段**（单一真源，错位比不剥更糟）
  C4 裁剪后 ``` 围栏仍配对
  C5 追加中断提示前必须先闭合围栏
  C6 已配对的正文**一字不动**（呈现层不得无谓改动）
  + 3 组变异自证（每条断言都必须能抓住对应的退化实现）

为什么值得单独成门禁：它守的是**用户看得见的东西**。
前面 20+ 个门禁守的是"系统内部算得对不对"，这一条守"用户看到的是不是人话"。
"""
import os
import re
import sys
import textwrap
import inspect
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from services.subtask_protocol import (          # noqa: E402
    extract_protocol_json, strip_protocol_json, _locate_protocol_json)
from agent.final_content import close_open_fences  # noqa: E402
from workflows.planner import FlowPlannerMixin      # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("  PASS %s" % name)
    else:
        FAIL.append((name, detail))
        print("  FAIL %s  <- %s" % (name, detail))


# ── 真实样本（取自真库 msg 5761 的协议 JSON，结构原样保留）────────────────
_PROTO = json.dumps({
    "schema_version": 1, "status": "partial",
    "summary": "基于知识库梳理出 SysML v2 活动图中 if 只能作为 succession 守卫的 8 条规则",
    "artifacts": [{"kind": "report", "title": "规则条目及示例", "ref": "本回答正文"}],
    "evidence": [{"source": "graph", "ref": "OMG SysML v2 规范 §7.17.5", "basis": "verified"}],
    "risks": [{"level": "mid", "title": "简写合法性存在口径差异", "desc": "未实测",
                "advice": "以贵方校验器实测为准"}],
    "claims": [{"text": "if 只能作为守卫", "kind": "fact", "subject": "if 语句合法位置"}],
    "confidence": 0.82, "missing": ["未获取原始报错代码"]}, ensure_ascii=False)

# 交付物真实形态（真库实测）：正文 + --- + 协议 JSON
_DELIVERABLE = ("## 一、结论先行\n"
                "if 在活动体中只能作为 GuardedSuccession 的守卫使用，不能独立成句。\n"
                "## 二、规则条目\n"
                "1. 守卫必须写在 then 之前；写在 then 之后非法。\n"
                "2. else 不能作为 succession 的兜底分支。\n"
                "3. when 不是 SysML v2 关键字。\n"
                "\n---\n\n" + _PROTO)

print("=" * 62)
print("C 门禁：最终正文形态契约（2026-11-04 新增）")
print("=" * 62)

# ── C1：协议 JSON 不得留在正文 ───────────────────────────────────────────
_clean, _proto = strip_protocol_json(_DELIVERABLE)
check("C1 协议 JSON 已从正文移除", '{"schema_version"' not in _clean,
      "正文仍含 JSON（偏移 %d）" % _clean.find('{"schema_version"'))
check("C1b 移除后正文不再是空的", len(_clean.strip()) > 60, "len=%d" % len(_clean.strip()))
check("C1c 协议对象仍能取到（抽≠丢）", isinstance(_proto, dict) and _proto.get("schema_version") == 1,
      "proto=%s" % type(_proto).__name__)

# ── C2：不得误删正文实质内容 ─────────────────────────────────────────────
for _frag in ("## 一、结论先行", "GuardedSuccession", "守卫必须写在 then 之前",
              "when 不是 SysML v2 关键字"):
    check("C2 正文实质内容未被误删：%s" % _frag[:14], _frag in _clean, "缺失")

# ── C3：抽与删必须是同一段（单一真源）────────────────────────────────────
_obj, _span = _locate_protocol_json(_DELIVERABLE)
check("C3 定位到协议 span", _span is not None and _span[0] < _span[1], "span=%s" % (_span,))
if _span:
    _seg = _DELIVERABLE.strip()[_span[0]:_span[1]]
    check("C3b span 命中的片段能解析为协议", isinstance(json.loads(_seg), dict), "片段非 JSON")
    check("C3c 剥离后该片段确实不在正文", _seg.strip()[:40] not in _clean, "片段仍在正文")

def _fence_broken_by_mark(text):
    """裁剪结果里是否存在「围栏被省略标记隔开」的破损。

    ⚠️ **不能用"全文 ``` 数为奇数"当判据**（本轮实测踩到）：
       围栏被切成两半时，一半在 head 段、一半在 tail 段 ⇒ 全文 ``` 数**仍是偶数**
       （fences=2）⇒ 奇偶判据放行，但渲染出来是"代码块里混进了省略标记那行散文"。
    ⇒ 正确判据：**按省略标记切段后，每段内部的 ``` 数必须为偶数**
       （偶数 = 该段内围栏自成对；奇数 = 有围栏在段内被切开）。
    """
    for _seg in text.split("…（中间省略"):
        if _seg.count("```") % 2 == 1:
            return True
    return False


# ── C4：裁剪后围栏仍配对 ────────────────────────────────────────────────
#   ⚠️ 样本必须让**围栏跨越切口**，否则两种实现结果相同（围栏整块落在被丢弃的
#      中间段 ⇒ 现行 fences=0、变异体 fences=0）⇒ 断言空转、守不住任何东西。
#      这里的构造：短头(102) + 围栏(约 1722) + 短尾(204)，cap=200 ⇒ head=120 **落在围栏内**。
_ht = FlowPlannerMixin._head_tail_clip
_FENCED = ("开头" + "中" * 100
           + "\n```sysml\npackage P {\n" + "    part def X;\n" * 100 + "}\n```\n"
           + "结尾结论" + "后" * 100)
_cut = _ht(_FENCED, cap=200)
check("C4 裁剪后围栏未被省略标记切开", not _fence_broken_by_mark(_cut),
      "段内围栏奇数=被切开")
check("C4b 裁剪后仍保头尾（围栏对齐不得牺牲结论）",
      _cut.startswith("开头") and _cut.endswith("后"), "len=%d" % len(_cut))
check("C4c 无围栏文本的裁剪行为不变（头尾同留）",
      _ht("前" * 400 + "中" * 2000 + "后" * 400, 200).rstrip().endswith("后"), "")

# ── C5：中断提示前必须闭合围栏 ───────────────────────────────────────────
_open = "```sysml\npackage P {\n    part def X;\n"      # 故意未闭合
_fixed = close_open_fences(_open) + "\n\n> ⏹ 连接已中断（以上为已生成内容）"
check("C5 未闭合围栏被闭合（提示不再落进代码块）",
      _fixed.count("```") % 2 == 0, "fences=%d" % _fixed.count("```"))
check("C5b 闭合后提示在代码块之外",
      _fixed.rstrip().endswith("以上为已生成内容）"), "尾部=%r" % _fixed[-30:])

# ── C6：已配对的正文一字不动 ─────────────────────────────────────────────
_paired = "```sysml\npackage P {}\n```\n\n正文结束。"
check("C6 已配对正文不被 close_open_fences 改动", close_open_fences(_paired) == _paired,
      "被改动了")

# ── C7：中文序号标题归一（只在"回退/重复"时动手）─────────────────────────
from agent.final_content import normalize_ordinal_headings, _int_to_cn, _cn_to_int  # noqa: E402

# C7a 数字互转闭环（自测曾在此抓到 `"" in "一二三"` 恒真的 bug：二十→21）
_bad = [i for i in range(1, 40) if _cn_to_int(_int_to_cn(i)) != i]
check("C7a 中文序号互转 1~39 全对", not _bad, "错配=%s" % _bad[:5])

# C7b 正常单调文档必须**一字不动**
_mono = "## 一、A\n正文\n## 二、B\n正文\n## 三、C\n"
_o, _n = normalize_ordinal_headings(_mono)
check("C7b 单调递增文档一字不动（不误改正常文档）", _n == 0 and _o == _mono,
      "changed=%d 被改=%s" % (_n, _o != _mono))

# C7c 拼接回退必须被重排为连续序号
_joint = ("## 一、结论先行\nA\n## 二、规则条目\nB\n## 六、风险\nC\n"
          "## 二、本次实际做了什么\nD\n## 七、修正后代码\nE\n"
          "## 一、结论先行\nF\n## 三、残余错误\nG\n")
_oj, _nj = normalize_ordinal_headings(_joint)
_nums = [re.findall(r"^##\s([一二三四五六七八九十]+)、", _oj, re.M)]
check("C7c 回退标题被重排为连续序号", _nj > 0, "changed=%d" % _nj)
if _nums and _nums[0]:
    _seq = [_cn_to_int(x) for x in _nums[0]]
    check("C7d 重排后序号严格递增（读者不再看到 二 排在 六 后面）",
          all(_seq[i] > _seq[i - 1] for i in range(1, len(_seq))), "序列=%s" % _seq)

# C7e **只改序号，标题文字与顺序一字不丢**
def _titles(t):
    return [re.sub(r"^#+\s*[一二三四五六七八九十]+[、.]\s*", "", l).strip()
            for l in t.split("\n") if re.match(r"^#{2,6}\s*[一二三四五六七八九十]+[、.]", l)]

check("C7e 标题文字序列完全一致（只动序号）", _titles(_joint) == _titles(_oj),
      "before=%s / after=%s" % (_titles(_joint)[:3], _titles(_oj)[:3]))

# C7f 单标题不动
_single = "## 一、唯一标题\n"
check("C7f 只有一个序号标题时不改动", normalize_ordinal_headings(_single) == (_single, 0),
      "被改动了")

# ── C8：**接线哨兵** —— 函数写好了但没人调用 = 不生效（本仓踩过的坑）──────
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


_w_planner = _read("workflows/planner.py")
_w_stream = _read("agent/pipeline_parts/stream.py")
_w_router = _read("routers/conversations.py")
_w_proto = _read("services/subtask_protocol.py")

check("C8a `_summarize_plan` LLM 出口已接序号归一",
      "normalize_ordinal_headings as _ordfix" in _w_planner
      and "content, _no = _ordfix(content)" in _w_planner, "未接线")
check("C8b `_summarize_plan` 拼接降级出口已接序号归一",
      "_joined, _no = _ordfix(_joined)" in _w_planner, "未接线")
check("C8c 流式收尾（reflection 之后）已接序号归一",
      "normalize_ordinal_headings as _ordfix_fc" in _w_stream
      and "orch_content, _ord_n = _ordfix_fc(orch_content)" in _w_stream, "未接线")
check("C8d 子任务正文已接协议剥离（strip_protocol_json 有真实调用）",
      "strip_protocol_json as _sp_strip" in _w_stream
      and "_sp_strip(_s0)" in _w_stream, "未接线")
check("C8e 中断提示前已接围栏闭合", "close_open_fences as _close_fences" in _w_router,
      "未接线")
check("C8f 协议剥离与抽取共用同一真源（_locate_protocol_json）",
      _w_proto.count("_locate_protocol_json(") >= 3, "未共用")

# ── C9：多段证据包必须**全剥**，但正文里的普通 JSON **一个都不能删** ──────────
_two = "正文A\n\n" + _PROTO + "\n\n正文B\n\n" + _PROTO
_c2, _p2 = strip_protocol_json(_two)
check("C9 多段证据包全部剥离（已拼接成品也能剥干净）",
      _c2.count('{"schema_version"') == 0 and isinstance(_p2, dict),
      "残留=%d" % _c2.count('{"schema_version"'))
check("C9b 多段剥离后正文实质内容仍在", ("正文A" in _c2) and ("正文B" in _c2), "正文被误删")

# C9c **误删防护**：正文里的示例 JSON（无协议身份）必须原样保留
_body_json = ('说明如下：\n\n```json\n{"name": "转发器", "status": "ok"}\n```\n\n'
              '以及行内 {"schema_version_note": "这不是证据包"}\n\n' + _PROTO)
_c9c, _p9c = strip_protocol_json(_body_json)
check("C9c 正文示例 JSON 不被误删（只删有协议身份的）",
      '{"name": "转发器"' in _c9c and 'schema_version_note' in _c9c,
      "示例 JSON 被误删")
check("C9d 误删防护下真证据包仍被剥离", _c9c.count('"status":"partial"') == 0,
      "证据包残留")

# ── 变异自证：每条断言都必须能抓住对应的退化实现 ─────────────────────────
print()
print("── 变异自证 ──")

# M1：还原成"只抽不删"（抽出 JSON 但正文留着）
_orig_extract = extract_protocol_json


def _mut_no_strip(text):
    obj = extract_protocol_json(text)
    return text, obj


_r1 = _mut_no_strip(_DELIVERABLE)[0]
check("M1 变异自证：只抽不删 ⇒ 正文仍含 JSON（C1 能抓住）",
      '{"schema_version"' in _r1 and '{"schema_version"' not in _clean, "变异无效")

# M2：还原成不对齐围栏的裁剪（切口从中间切）
def _mut_no_align(text, cap):
    mark = "\n…（中间省略…）…\n"
    head = int(cap * 0.6)
    tail = cap - head
    return text[:head] + mark + text[-tail:]


_r2 = _mut_no_align(_FENCED, 200)
check("M2 变异自证：不对齐围栏 ⇒ 围栏被切开（C4 能抓住）",
      _fence_broken_by_mark(_r2) and not _fence_broken_by_mark(_cut),
      "变异体 broken=%s / 现行 broken=%s" % (
          _fence_broken_by_mark(_r2), _fence_broken_by_mark(_cut)))

# M3：去掉闭合逻辑
_r3 = _open + "\n\n> ⏹ 连接已中断（以上为已生成内容）"
check("M3 变异自证：不闭合围栏 ⇒ 提示落进代码块（C5 能抓住）",
      _r3.count("```") % 2 == 1 and _fixed.count("```") % 2 == 0, "变异无效")

# M4：序号归一变成"什么都不做"（函数在、没效果）
def _mut_ord_noop(text):
    return text, 0


_o4, _n4 = _mut_ord_noop(_joint)
check("M4 变异自证：序号归一空转 ⇒ C7c 能抓住（回退仍存在）",
      _n4 == 0 and normalize_ordinal_headings(_joint)[1] > 0, "变异无效")

# M5：序号归一**过度处理**（连正常单调文档也重排）—— 这条守的是"不误改正常文档"
def _mut_ord_aggressive(text):
    """把所有 ## 中文序号标题无条件重排为 1..n（不管是否回退）。"""
    ms = list(re.finditer(r"^(##)[ \t]*([一二三四五六七八九十]+)([、.])", text, re.M))
    if not ms:
        return text, 0
    out, pos = [], 0
    for i, m in enumerate(ms, start=1):
        out.append(text[pos:m.start(2)])
        out.append(_int_to_cn(i))
        pos = m.end(2)
    out.append(text[pos:])
    return "".join(out), len(ms)


_mo, _mn = _mut_ord_aggressive(_mono)
# ⚠️ 这里必须用一个"**从 二 起且单调**"的样本才具区分度：
#   若样本是 一/二/三，激进重排结果**仍是**一/二/三 ⇒ 与原文字节相同 ⇒ 变异不可见（本轮踩过）。
_mono_offset = "## 二、A\n正文\n## 三、B\n正文\n## 四、C\n"
_mo2, _mn2 = _mut_ord_aggressive(_mono_offset)
check("M5 变异自证：过度重排 ⇒ C7b 能抓住（单调文档被误改）",
      _mn2 > 0 and _mo2 != _mono_offset and normalize_ordinal_headings(_mono_offset)[1] == 0,
      "变异体 changed=%d 且文字改变=%s ／ 现行 changed=%d" % (
          _mn2, _mo2 != _mono_offset, normalize_ordinal_headings(_mono_offset)[1]))

print()
print("=" * 62)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for _n, _d in FAIL:
    print("  FAIL:", _n, "<-", _d)
print("=" * 62)
raise SystemExit(1 if FAIL else 0)
