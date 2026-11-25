# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：2026-11-20 补登记：历史漏入清单，当前按 local 手工跑；是否晋 pr 需先确认无外部依赖并同步 ci.yml（G6a）
"""verify_three_issue_fixes — 三问题修复门禁（2026-11-19）

P1 问题1：思考文案软化 + card 检索计数按引用登记簿回填（stream.py）
P2 问题2：中断固化文案带结构化原因码，不再写死「连接已中断」
P3 问题3：服务端兜底固化写入前同连接二次复查，消除竞态双写

每块含静态断言 + 变异自证（摘掉补丁标志块 ⇒ 对应断言须 FAIL）。
用法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_three_issue_fixes.py
"""
from __future__ import annotations
import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source  # noqa: E402
import os, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONV = os.path.join(_ROOT, "routers", "conversations.py")
STREAM = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")
conv = open(CONV, encoding="utf-8").read()
stream = read_stream_source()

results = []
def rec(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (" ⇒ " + str(detail) if detail else ""))

# ── P2 问题2 ────────────────────────────────────────────────
rec("P2-1 原因分类前移到终端块（有正文分支也能用）",
    "_ABORT_REASON_IN_TAIL_V1" in conv and
    conv.index("_ab_code, _ab_txt = _classify_abort") < conv.index("_ABORT_REASON_IN_TAIL_V1"))
rec("P2-2 固化文案带原因码占位 %s",
    "原因[%s]：%s" in conv and "_ab_code, _ab_txt" in conv)
rec("P2-3 写死的「连接已中断（以上为已生成内容，可直接重新发送这条消息继续）」文案已移除",
    "连接已中断（以上为已生成内容，" not in conv)
rec("P2-4 exec 原因复用前移结果（不再二次分类）",
    '_exec["abort_reason"] = _ab_code' in conv and
    conv.count("_ab_code, _ab_txt = _classify_abort") == 1)

# ── P1 问题1 ────────────────────────────────────────────────
rec("P1-1 思考文案软化（不再绝对化地说未检索）",
    "初始未走知识库检索" in stream and "本次未触发知识检索（纯问答/无 # 标签）" not in stream)
rec("P1-2 检索计数回填标志存在",
    "RETRIEVE_COUNT_RECONCILE_V1" in stream)
rec("P1-3 回填以引用登记簿为准（_citation_registry + source=tool_retrieve）",
    "_reg_now = getattr(self, \"_citation_registry\", None)" in stream and
    'retrieval["source"] = "tool_retrieve"' in stream)
rec("P1-4 回填在 card_data 构造之前",
    stream.index("RETRIEVE_COUNT_RECONCILE_V1") < stream.index("card_data = json.dumps({", stream.index("RETRIEVE_COUNT_RECONCILE_V1")))

# ── P3 问题3 ────────────────────────────────────────────────
rec("P3-1 写入前二次复查标志存在",
    "FINALIZE_WRITE_RECHECK_V1" in conv)
rec("P3-2 复查用写入同一连接 _wc（消除跨连接竞态）",
    "_last2 = _wc.execute(" in conv and "if not _recheck_skip:" in conv)
rec("P3-3 复查判据含本轮新消息 + 固化标记",
    '_is_new = (_start_msg_id <= 0 or int(_last2["id"]) > _start_msg_id)' in conv and
    '"以上为已生成内容" in ((_last2["content"] or ""))' in conv)
rec("P3-4 跳过时留痕（可观测；根治后文案并入唯一键冲突提示）",
    "未写入：唯一键冲突或二次复查跳过" in conv or "跳过写入：同轮已固化(二次复查)" in conv)

# ── 变异自证：把每个标志块的核心判据替换掉，对应断言须 FAIL ──
def mutation_check(label, src, needle, replacement, check_fn):
    mutated = src.replace(needle, replacement, 1)
    assert mutated != src, "变异未生效（anchor 不存在）: " + needle[:40]
    return (label, not check_fn(mutated), "变异后断言应 FAIL")

mv1 = mutation_check("M-P1 计数回填摘除 ⇒ P1-3 须FAIL", stream,
    'retrieval["source"] = "tool_retrieve"', 'retrieval["source"] = "none"',
    lambda s: '_reg_now = getattr(self, "_citation_registry", None)' in s and 'retrieval["source"] = "tool_retrieve"' in s)
rec(*mv1)

mv2 = mutation_check("M-P2 原因码占位摘除 ⇒ P2-2 须FAIL", conv,
    "原因[%s]：%s", "原因：未知",
    lambda s: "原因[%s]：%s" in s)
rec(*mv2)

mv3 = mutation_check("M-P3 二次复查摘除 ⇒ P3-2 须FAIL", conv,
    "_last2 = _wc.execute(", "_last2 = _noop.execute(",
    lambda s: "_last2 = _wc.execute(" in s and "if not _recheck_skip:" in s)
rec(*mv3)

npass = sum(1 for _, ok, _ in results if ok)
nfail = len(results) - npass
print("\n" + "=" * 60)
print("PASS %d / FAIL %d" % (npass, nfail))
print("ALL_OK=" + str(nfail == 0))
sys.exit(0 if nfail == 0 else 1)
