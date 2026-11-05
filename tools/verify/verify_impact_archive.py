"""影响分析归档（FR-CIA-3）自检：**共享实现 + 三条路径都接线**。

## 为什么要这条

2026-11-04 核实发现两个真问题：
  ① `execute.py` 有一份**内联副本**，判据只有 `intent == "impact"` ⇒ **CIA 错误引导卡**
     （`{ok: false, code: ...}`）永远不入库；共享实现（`agent/utils.py`）的判据是
     `intent=='impact' or ok is not None or impact_nodes is not None`，失败引导同样要可追溯。
  ② `orchestration.py`（非流式编排收口）**完全没有归档** ⇒ 编排里若含 impact 子任务，
     `impact_analyses` 表查不到，"做过了但无记录"。

## 口径

· 不写生产库：把 `repositories.impact_repo.ImpactRepo` 换成记录器（helper 内部是函数内 import，
  打模块属性即可生效）。
· 被验的是**真实 helper** 的判据 + 三条路径的**源码接线**。
"""

import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from agent.utils import _archive_impact_analysis     # noqa: E402
import repositories.impact_repo as impact_repo       # noqa: E402

PASS = 0
FAIL = 0
FAILED = []


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % label)
    else:
        FAIL += 1
        FAILED.append(label)
        print("  ✗ %s   %s" % (label, detail))


def read_src(rel):
    return io.open(os.path.join(REPO, rel), encoding="utf-8").read()


class Recorder:
    """替身 ImpactRepo：只记调用，不连库。"""
    calls = []
    raise_on_create = False

    def __init__(self, conn=None):
        self.conn = conn

    def create_analysis(self, **kw):
        if Recorder.raise_on_create:
            raise RuntimeError("db down")
        Recorder.calls.append(kw)
        return 1


class _Conn:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True


_orig = impact_repo.ImpactRepo
impact_repo.ImpactRepo = Recorder
try:
    print("=" * 74)
    print("[1] 判据：三类卡片是否该归档")
    print("=" * 74)

    # 1.1 正常 impact 分析
    Recorder.calls = []
    _archive_impact_analysis(_Conn(), 1, 11,
                             json.dumps({"intent": "impact",
                                         "change_source": {"name": "需求REQ-12 变更"},
                                         "params": {"depth": 2}}, ensure_ascii=False),
                             "改了 REQ-12 会影响什么")
    check("1.1 intent=impact（**JSON 字符串**形态）⇒ 归档 1 条",
          len(Recorder.calls) == 1, Recorder.calls)
    check("1.2 标题/来源取自 change_source.name，status=ok",
          Recorder.calls and Recorder.calls[0]["title"] == "需求REQ-12 变更"
          and Recorder.calls[0]["change_source"] == "需求REQ-12 变更"
          and Recorder.calls[0]["status"] == "ok", Recorder.calls[:1])

    # 1.3 CIA 错误引导卡（内联副本漏掉的正是这一类）
    Recorder.calls = []
    _archive_impact_analysis(_Conn(), 2, 22,
                             {"ok": False, "code": "CIA_NO_BASELINE",
                              "message": "未找到变更源"},
                             "随便改点什么")
    check("1.3 CIA 错误引导卡（ok=false + code）⇒ **必须归档**（内联副本漏的类型）",
          len(Recorder.calls) == 1, Recorder.calls)
    check("1.4 失败记录 status=error 且 error_code 落库",
          Recorder.calls and Recorder.calls[0]["status"] == "error"
          and Recorder.calls[0]["error_code"] == "CIA_NO_BASELINE", Recorder.calls[:1])

    # 1.5 只有 impact_nodes（结构形态）也应归档
    Recorder.calls = []
    _archive_impact_analysis(_Conn(), 3, 33, {"impact_nodes": [{"id": "a"}]}, "影响")
    check("1.5 只有 impact_nodes ⇒ 归档（结构形态判据）",
          len(Recorder.calls) == 1, Recorder.calls)

    # 1.6 普通问答卡不应产生垃圾记录
    Recorder.calls = []
    _archive_impact_analysis(_Conn(), 4, 44, {"intent": "chat", "content": "你好"}, "你好")
    check("1.6 普通问答卡（无 impact 语义）⇒ **不归档**（不制造噪音）",
          len(Recorder.calls) == 0, Recorder.calls)

    print()
    print("=" * 74)
    print("[2] 失败静默：归档异常不得影响会话主流程")
    print("=" * 74)
    Recorder.calls = []
    Recorder.raise_on_create = True
    _ok = True
    try:
        _archive_impact_analysis(_Conn(), 5, 55, {"intent": "impact"}, "x")
    except Exception:
        _ok = False
    Recorder.raise_on_create = False
    check("2.1 落库抛异常时不外抛（旁路铁律）", _ok)

    print()
    print("=" * 74)
    print("[3] 接线：三条落库路径都必须调**共享实现**，且不得再有内联副本")
    print("=" * 74)

    _s_exec = read_src("agent/pipeline_parts/execute.py")
    _s_orch = read_src("agent/pipeline_parts/orchestration.py")
    _s_stream = read_src("agent/pipeline_parts/stream.py")
    _s_utils = read_src("agent/utils.py")

    check("3.1 execute.py（单 Agent 路径）调用共享实现",
          "_archive_impact_analysis(conn, conversation_id, msg_id" in _s_exec)
    check("3.2 orchestration.py（非流式编排）调用共享实现 —— 本轮补的缺口",
          "_archive_impact_analysis(conn, conversation_id, msg_id" in _s_orch)
    check("3.3 stream.py（流式主路径）调用共享实现",
          "_archive_impact_analysis(conn, conversation_id, msg_id" in _s_stream)
    check("3.4 execute.py 的内联副本已删除（不再直接 new ImpactRepo）",
          "ImpactRepo(conn).create_analysis" not in _s_exec,
          "残留 %d 处" % _s_exec.count("ImpactRepo(conn).create_analysis"))
    check("3.5 全仓只有 utils.py 一处真正写库（单一实现）",
          sum(1 for s in (_s_exec, _s_orch, _s_stream, _s_utils)
              if "ImpactRepo(conn).create_analysis" in s) == 1)

    print()
    print("=" * 74)
    print("[4] 变异自证：把实现改坏，判据必须翻")
    print("=" * 74)

    def mutate(src, old, new):
        return src.replace(old, new, 1) if old in src else None

    def ns_of(src, name="mutant"):
        ns = {"__name__": name}
        exec(compile(src, "<mutant>", "exec"), ns)
        return ns

    # M1：判据退回"只认 intent==impact"（即被删掉的内联副本口径）⇒ 1.3 必须翻
    _old_judge = ('        if _cd.get("intent") == "impact" or _cd.get("ok") is not None '
                  'or _cd.get("impact_nodes") is not None:')
    _m1 = mutate(_s_utils, _old_judge, '        if _cd.get("intent") == "impact":')
    if _m1:
        _ns1 = ns_of(_m1)
        Recorder.calls = []
        _ns1["_archive_impact_analysis"](_Conn(), 6, 66,
                                         {"ok": False, "code": "CIA_NO_BASELINE"}, "x")
        check("M1 变异「判据退回只认 intent」被抓住（错误引导卡丢失）",
              len(Recorder.calls) == 0, Recorder.calls)
    else:
        check("M1 变异「判据退回只认 intent」被抓住（错误引导卡丢失）", False, "锚点未命中")

    # M2：删掉编排路径的调用 ⇒ 3.2 必须翻
    # ⚠️ 不能只是"注释掉"：注释后原子串仍在源码里，判据会假绿（本次踩过）。
    _m2 = mutate(_s_orch,
                 "_archive_impact_analysis(conn, conversation_id, msg_id, card_data, user_input,",
                 "_REMOVED_CALL(conn, conversation_id, msg_id, card_data, user_input,")
    check("M2 变异「编排路径不归档」被抓住",
          _m2 is not None and "_archive_impact_analysis(conn" not in _m2)
finally:
    impact_repo.ImpactRepo = _orig

print()
print("=" * 74)
print("汇总：PASS = %d / FAIL = %d" % (PASS, FAIL))
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  - %s" % f)
print("口径：ImpactRepo 换记录器，不写生产库；接线判据打在源码上。")
sys.exit(0 if FAIL == 0 else 1)
