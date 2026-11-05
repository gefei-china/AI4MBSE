"""S1 门禁：会话级失败必须可归因（2026-10-29）。

## 要防的三件事

1. **失败不带原因**：实测（@75eca26）近 7 天 `conversation` span 有 **73 条 error**，
   而 `meta_json` **全为 `{}`** —— 一条都回答不了"为什么失败"。这类缺口不会让任何功能坏掉，
   所以只能靠门禁盯住。
2. **真异常被记成 ok**（本轮实测发现的语义错误）：旧逻辑用单一哨兵
   `"ok" if _terminal else "error"`，而 `except` 分支会把 `_terminal` 置 True（表示"已通知前端"）
   ⇒ **抛出异常的一轮在 span 里是 ok**。这与「73 条 error 无原因」是同一病灶的两面：
   状态与原因都没有可靠表达。
3. **兜底标记不能冒充根因**：`end_conversation` 在无原因时补 `no_reason_recorded`，
   它必须**明确表示"原因缺失"**，不能写成像个具体原因的样子。

## 判据写法

- 行为级：直接构造 `ConversationTracer` + 内存 SQLite，跑四个场景断言落库的
  `status` 与 `meta_json.reason`（**不 mock，用真实 flush 路径**）。
- 源码级：断言调用侧的优先级链确实存在（防止有人把判定改回单一哨兵）。

只读生产库以外的东西：本脚本自建临时库，不写 mbse.db。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

_PASS, _FAIL = [], []


def check(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# ── 建临时库（只含 orch_run_spans，够用即可）────────────────────────────────
_TMP = os.path.join(_ROOT, "tmp", "_s1_gate.db")
if os.path.exists(_TMP):
    os.remove(_TMP)
con = sqlite3.connect(_TMP)
con.execute("""CREATE TABLE orch_run_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER DEFAULT 0, run_id INTEGER DEFAULT 0,
    kind TEXT DEFAULT '', name TEXT DEFAULT '', parent_id INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running', started_at TEXT DEFAULT '', ended_at TEXT DEFAULT '',
    latency_ms INTEGER DEFAULT 0, meta_json TEXT DEFAULT '', created_at TEXT DEFAULT '')""")
con.commit()
con.close()

from services.orch_span import ConversationTracer   # noqa: E402


def run_case(status, meta, kind_tag):
    """跑一个 tracer 场景，返回落库的 (status, meta_dict)。

    ⚠️ `ConversationTracer.flush(conn)` **必须传连接**才会落库
    （`conn=None` 时直接返回 0，观测数据丢弃且不报错）——首版漏传，
    导致 A~D 四组全部读不到行、断言全红。判据：读回 0 行时先查**是不是没落库**，
    而不是先怀疑实现。
    """
    tr = ConversationTracer(90000 + abs(hash(kind_tag)) % 999, trace_id="t-" + kind_tag,
                            enabled=True)
    tr.begin_conversation(meta={"intent": "chat"})
    tr.end_conversation(status, meta=meta)
    c = sqlite3.connect(_TMP)
    c.row_factory = sqlite3.Row
    n = tr.flush(c)
    c.commit()
    row = c.execute("SELECT status, meta_json FROM orch_run_spans "
                    "WHERE kind='conversation' ORDER BY id DESC LIMIT 1").fetchone()
    c.close()
    if not row or not n:
        return None, {}
    try:
        m = json.loads(row["meta_json"] or "{}")
    except Exception:
        m = {}
    return row["status"], m


print("== A 成功路径：status=ok 且 reason=ok ==")
st, m = run_case("ok", {"intent": "chat", "route": "vector", "reason": "ok"}, "ok")
check("A1 status 落为 ok", st == "ok", st)
check("A2 reason 存在且为 ok", m.get("reason") == "ok", m.get("reason"))
check("A3 原有 meta 字段未被覆盖", m.get("intent") == "chat" and m.get("route") == "vector",
      (m.get("intent"), m.get("route")))

print()
print("== B 失败但调用侧没给原因：必须留可见标记，不能是空对象 ==")
st, m = run_case("error", {}, "noreason")
check("B1 status 落为 error", st == "error", st)
check("B2 reason 非空（不是空 dict 蒙混过去）", bool(str(m.get("reason") or "").strip()),
      m.get("reason"))
check("B3 reason 明确表示「原因缺失」而非冒充根因",
      m.get("reason") == "no_reason_recorded", m.get("reason"))
check("B4 同时给出 note 说明", bool(m.get("note")))

print()
print("== C 失败且给了具体原因：必须原样保留（不被兜底覆盖）==")
st, m = run_case("error", {"reason": "exception:TimeoutError", "last_event": "stage"},
                 "withreason")
check("C1 status=error", st == "error", st)
check("C2 具体原因未被兜底覆盖", m.get("reason") == "exception:TimeoutError", m.get("reason"))
check("C3 归因维度保留", m.get("last_event") == "stage", m.get("last_event"))

print()
print("== D 客户端断开也要能区分（不能与「服务端故障」混为一谈）==")
st, m = run_case("error", {"reason": "client_gone"}, "gone")
check("D1 reason=client_gone 原样保留", m.get("reason") == "client_gone", m.get("reason"))

print()
print("== E 源码级：调用侧优先级链与语义解耦必须存在 ==")
src = open(os.path.join(_ROOT, "routers", "conversations.py"), encoding="utf-8").read()
check("E1 存在 client_gone 分支", '"client_gone"' in src)
check("E2 存在 exception:<类型> 归因", '"exception:%s"' in src)
check("E3 存在 no_terminal_frame 归因", '"no_terminal_frame"' in src)
check("E4 存在「已发 done 才算 ok」的判定", '== "done"' in src)
# 关键：不能再用单一哨兵决定状态（旧写法）。
# ⚠️ 必须**先剥掉注释行**再判：本轮的实现注释里**引用了旧写法作为反例**
#   （"旧逻辑 `ok if _terminal else error` 有两个错误"），直接搜源码会命中注释 ⇒ 假红。
#   判据只应针对**可执行代码**。同类教训：本仓曾因按行号/按整段文本断言而误伤注释。
_code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
_code = "\n".join(_code_lines)
check("E5 已废弃单一哨兵判定（可执行代码里不再出现）",
      '"ok" if _terminal else "error"' not in _code,
      "剥离注释后仍命中" if '"ok" if _terminal else "error"' in _code else "")
check("E6 _fail_reason 与 _terminal 已解耦（两个独立变量都存在）",
      "_fail_reason" in src and "_terminal" in src)
check("E7 三个归因维度都写入 meta",
      all(k in src for k in ('"last_event"', '"sent_any"', '"terminal_sent"')))

print()
print("== F 兜底实现本身不编造（源码级）==")
ssrc = open(os.path.join(_ROOT, "services", "orch_span.py"), encoding="utf-8").read()
check("F1 end_conversation 有兜底", "no_reason_recorded" in ssrc)
check("F2 兜底只在 status!=ok 时触发", '!= "ok"' in ssrc or "!= 'ok'" in ssrc)
check("F3 兜底不覆盖已有 reason（用 setdefault）", 'setdefault("reason"' in ssrc)

# 清理
try:
    os.remove(_TMP)
except Exception:
    pass

print()
print("[RESULT] PASS %d / FAIL %d" % (len(_PASS), len(_FAIL)))
if _FAIL:
    print("[FAILED]", _FAIL)
    sys.exit(1)
print("[ALL GREEN]")
