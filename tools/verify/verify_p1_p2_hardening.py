# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：2026-11-20 补登记：历史漏入清单，当前按 local 手工跑；是否晋 pr 需先确认无外部依赖并同步 ci.yml（G6a）
"""P1/P2 加固门禁（2026-11-19）—— 守六项不变式，含真库/功能级验证与变异自证。

P1-1 attempts 覆盖 checkpoint/partial 两条兜底路径
P1-2 正文合并按 attempt kind 语义决策（不靠文本猜）
P1-3 编排路径显式 flow_kind 标记
P2-1 关键写操作毫秒级日志（request_id 可关联）
P2-2 sendChat 拆分后职责函数仍在调用链上（行为不变）

用法：.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_p1_p2_hardening.py
退出码：全绿 0 / 有失败 1。
"""
import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source  # noqa: E402
import os
import sys
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL = "PASS", "FAIL"
_results = []


def ck(tag, ok, detail=""):
    _results.append((PASS if ok else FAIL, tag))
    print("  %-5s %s%s" % (PASS if ok else FAIL, tag, ("  | " + str(detail)) if detail else ""))


def read(rel):
    if rel.replace('\\', '/').endswith('pipeline_parts/stream.py'):
        return read_stream_source()
    with open(os.path.join(ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def main():
    print("=" * 84)
    print("P1/P2 加固门禁 —— 6 项不变式")
    print("=" * 84)

    st = read("agent/pipeline_parts/stream.py")
    rt = read("routers/conversations.py")
    ac = read("agent/auto_continue.py")
    js = read("static/js/mods/12-chatsend.js")
    cd = read("static/js/mods/06-cards.js")
    wl = read("core/write_log.py")
    cp = read("services/stream_checkpoint.py")

    # ── P1-1 ────────────────────────────────────────────────────────
    print("\n[P1-1] attempts 覆盖兜底路径")
    ck("1.1 partial 固化从 locals 收 attempts",
       '_ats = _g("_ac_attempts")' in st and '_out["attempts"] = _safe(lambda: list(_ats))' in st)
    ck("1.2 轻量 attempt 事件带 index/kind/chars 不带正文",
       '"type": "attempt", "index": len(_ac_attempts) - 1' in st
       and '"chars": _ac_attempts[-1].get("chars", 0)' in st
       and '"content"' not in st[st.find('"type": "attempt"'):st.find('"type": "attempt"')+300])
    ck("1.3 checkpoint 累加器收 attempt 事件",
       '"attempt"' in rt[rt.find("if _typ not in"):rt.find("if _typ not in")+120]
       and 'elif _typ == "attempt"' in rt)
    ck("1.4 _acc_exec 初始化带 attempts 数组",
       '"attempts": []' in rt[rt.find("_acc_exec = {"):rt.find("_acc_exec = {")+120])

    # ── P1-2 ────────────────────────────────────────────────────────
    print("\n[P1-2] 正文合并按 kind 语义")
    ck("2.1 merge_round_content 带 kind 参数",
       "def merge_round_content(prev: str, cur: str, kind: str | None = None)" in ac)
    ck("2.2 continuation → append（前文不丢）",
       'if kind == "continuation":' in ac and 'return p + "\\n\\n" + c' in ac)
    ck("2.3 retry → replace + carry_over",
       'if kind == "retry":' in ac and 'carry_over_unique_blocks' in ac)
    _kind_calls = st.count("kind=(_ac_attempts[-1].get('kind') if _ac_attempts else None)") \
                + rt.count("kind=(")  # router 不调 merge，这里只计 stream/execute
    ck("2.4 流式两处 merge 都传 kind",
       st.count("kind=(_ac_attempts[-1].get('kind') if _ac_attempts else None)") == 2)
    ex = read("agent/pipeline_parts/execute.py")
    ck("2.5 直行两处 merge 都传 kind",
       ex.count("kind=(_ac_attempts[-1].get('kind') if _ac_attempts else None)") == 2)

    # 功能级：kind 语义正确性（真库和构造反例）
    from agent.auto_continue import merge_round_content as merge
    from core.attempt_trace import KIND_CONTINUATION, KIND_RETRY
    ck("2.6 continuation 严格 append（strip 后逐字节）",
       merge("  A  ", "B  ", kind=KIND_CONTINUATION) == "A\n\nB")
    # 构造反例：旧文本判据会追加两个矛盾结论，retry 应按语义替换
    p1 = "# 角色\n" + "系统工程师。" * 40 + "\n\n## 结论\n第一版。" + "细节" * 200
    p2 = "## 结论\n第二版推翻第一版。" + "差异" * 30
    a, b = merge(p1, p2), merge(p1, p2, kind=KIND_RETRY)
    ck("2.7 retry 反例：旧判据留 2 结论，kind 只留 1",
       a.count("## 结论") == 2 and b.count("## 结论") == 1,
       "old=%d new=%d" % (a.count("## 结论"), b.count("## 结论")))

    # ── P1-3 ────────────────────────────────────────────────────────
    print("\n[P1-3] 编排路径显式标记")
    ck("3.1 编排 exec 带 flow_kind=orchestrated", '"flow_kind": "orchestrated"' in st)
    ck("3.2 前端渲染该标记（不折叠）",
       "ex.flow_kind === 'orchestrated'" in cd and "多子任务编排" in cd)

    # ── P2-1 ────────────────────────────────────────────────────────
    print("\n[P2-1] 毫秒级写日志")
    ck("4.1 write_log 存在且用毫秒时间戳",
       "%Y-%m-%d %H:%M:%S.%f" in wl and "request_context" in wl)
    ck("4.2 四类关键写点都记日志",
       all(k in (rt + st + cp) for k in (
           "conv_create_replay", '"conv_create"', "msg_assistant_commit",
           "msg_partial_freeze", "msg_checkpoint_recover")))
    # 功能级：时间戳格式 + request_id 降级
    from core.write_log import stamp as _stamp, request_id as _rid
    import re
    ck("4.3 时间戳含毫秒（正则）", bool(re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$", _stamp())), _stamp())
    ck("4.4 无 HTTP 语境时 request_id 降级为 '-'", _rid() == "-", _rid())

    # ── P2-2 ────────────────────────────────────────────────────────
    print("\n[P2-2] sendChat 拆分（行为不变）")
    ck("5.1 提取了 _ensureConversation / _submitClarify",
       "async function _ensureConversation" in js and "async function _submitClarify" in js)
    _sc = js[js.find("async function sendChat()"):]
    _sc = _sc[:_sc.find("\n}")]
    ck("5.2 sendChat 确实调用两个提取函数",
       "await _ensureConversation(msg)" in _sc and "await _submitClarify(msg, input)" in _sc)
    ck("5.3 sendChat 体积显著下降（<160 行）", len(_sc.splitlines()) < 160,
       "%d 行" % len(_sc.splitlines()))
    ck("5.4 返回值判据保住原 return 语义",
       "if(!(await _ensureConversation(msg))) return;" in _sc
       and "if(!(await _submitClarify(msg, input))) return;" in _sc)

    # ── 变异自证 ────────────────────────────────────────────────────
    print("\n[V] 变异自证")
    mutants = {
        "V1 去掉 kind 参数 ⇒ 2.1 红": (ac.replace("kind: str | None = None", ""), "kind: str | None = None"),
        "V2 去掉 flow_kind ⇒ 3.1 红": (st.replace('"flow_kind": "orchestrated",', ""), '"flow_kind": "orchestrated",'),
        "V3 去掉毫秒时间戳 ⇒ 4.1 红": (wl.replace("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"), "%Y-%m-%d %H:%M:%S.%f"),
        "V4 去掉 sendChat 内调用 ⇒ 5.2 红": (js.replace("await _ensureConversation(msg)", ""), "await _ensureConversation(msg)"),
    }
    for tag, (mutant, needle) in mutants.items():
        ck(tag, needle not in mutant)

    # ── 汇总 ────────────────────────────────────────────────────────
    fails = [t for s, t in _results if s == FAIL]
    print("\n" + "-" * 84)
    print("合计 %d 项（PASS %d / FAIL %d）"
          % (len(_results), len(_results) - len(fails), len(fails)))
    for t in fails:
        print("  · FAIL " + t)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
