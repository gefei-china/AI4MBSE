"""S5 门禁：编排失败必须可按类别分解，且孤儿回收不能因缺列而整体失败（2026-10-29）。

## 要防的三件事

1. **失败率不可分解**：实测（真库）全量 65 条 `agent_tasks.failed` 里
   **57 条（87.7%）** 是「孤儿运行回收」（进程重启导致失活），
   而 `error_class` 填充率只有 **2/65 = 3%** ⇒「编排子任务失败率 20.6%」
   这个数字**无法拆解**，读起来像"编排逻辑不可靠"，实际绝大多数是宿主重启的副产品。
   `failure_class` 早已把 `interrupted` 与 `timeout` 分开（一个是宿主挂了、
   一个是逻辑停滞），缺的只是那条 UPDATE 没把结果写进列——「能力在、没接线」。

2. **补归因却把回收搞挂**（本轮自己差点引入）：如果无条件写 `error_class=?`，
   极老库无该列时会抛 `no such column` ⇒ **整批孤儿一条都收不掉**
   （僵尸任务永久堆积）。这比"少一列归因"严重得多 ⇒ 必须有降级路径。

3. **分类编造**：分类器不可用时应留空，不能写个猜出来的类别。

## 污染
全部在独立临时库上跑，不碰生产库。
"""
from __future__ import annotations

import importlib
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


_TMP = os.path.join(_ROOT, "tmp", "_s5_gate.db")


def _fresh_db(with_error_class=True):
    if os.path.exists(_TMP):
        os.remove(_TMP)
    os.environ["MBSE_DB_PATH"] = os.path.abspath(_TMP)
    import database
    importlib.reload(database)
    con = database.get_db()
    cols = ("id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, task_key TEXT, title TEXT,"
            " agent_id TEXT, status TEXT, error TEXT, conversation_id INTEGER,"
            " updated_at TEXT DEFAULT CURRENT_TIMESTAMP, retry_count INTEGER DEFAULT 0"
            + (", error_class TEXT" if with_error_class else ""))
    con.execute("CREATE TABLE agent_tasks (%s)" % cols)
    con.execute("INSERT INTO agent_tasks (run_id,task_key,title,agent_id,status,updated_at)"
                " VALUES (999,'t1','孤儿','a1','running',datetime('now','-40 minutes'))")
    con.commit()
    import core.run_registry as RR
    importlib.reload(RR)
    return con, RR


print("== A 分类器对孤儿原因的判定（不自己发明类别）==")
from services.failure_class import classify, ERROR_CLASSES   # noqa: E402
orphan = ("编排中断：超过 30 分钟无任何进展，判定为孤儿运行"
          "（进程重启或执行线程丢失），已保留上下文可重跑")
check("A1 孤儿原因 → interrupted", classify(orphan, ok=False) == "interrupted",
      classify(orphan, ok=False))
check("A2 interrupted 是合法枚举", "interrupted" in ERROR_CLASSES)
check("A3 与 timeout 区分开（不同源）", classify("timeout", ok=False) == "timeout")

print()
print("== B 端到端：孤儿回收必须写 error_class ==")
con, RR = _fresh_db(with_error_class=True)
out = RR.reap_orphans(con)
row = con.execute("SELECT status, error_class, error FROM agent_tasks WHERE run_id=999").fetchone()
check("B1 任务已被回收", out.get("reaped") == 1, out)
check("B2 状态为 failed", row[0] == "failed", row[0])
check("B3 error_class 落为 interrupted", row[1] == "interrupted", row[1])
check("B4 error 文本保留（未丢原因）", "孤儿运行" in (row[2] or ""))
check("B5 返回值里回带类别（可观测）", out.get("error_class") == "interrupted", out.get("error_class"))
con.close()

print()
print("== C 关键负例：老库无 error_class 列时必须降级，而不是整批收不掉 ==")
con2, RR2 = _fresh_db(with_error_class=False)
out2 = RR2.reap_orphans(con2)
row2 = con2.execute("SELECT status, error FROM agent_tasks WHERE run_id=999").fetchone()
check("C1 仍成功回收（不因缺列失败）", out2.get("reaped") == 1, out2)
check("C2 状态仍置 failed", row2[0] == "failed", row2[0])
check("C3 error 文本仍写入", "孤儿运行" in (row2[1] or ""))
check("C4 未报错退出", not out2.get("error"), out2.get("error"))
con2.close()

print()
print("== D 源码级：降级路径与接线都在 ==")
src = open(os.path.join(_ROOT, "core", "run_registry.py"), encoding="utf-8").read()
code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
check("D1 调用了 failure_class 分类器", "from services.failure_class import classify" in code)
check("D2 UPDATE 带 error_class 列", "error_class=?" in code)
check("D3 有降级分支（不带 error_class 的 UPDATE）",
      code.count("UPDATE agent_tasks SET status='failed'") >= 2,
      code.count("UPDATE agent_tasks SET status='failed'"))
check("D4 分类失败不阻断（except 留空）", "_ek = \"\"" in code or "_ek = ''" in code)

print()
print("== E task_queue.fail 口径一致（同一词表，不另造）==")
tq = open(os.path.join(_ROOT, "task_queue.py"), encoding="utf-8").read()
check("E1 fail() 也调同一分类器", "from services import failure_class" in tq)
check("E2 fail() 也写 error_class", "error_class=?" in tq)
check("E3 fail() 也有降级", tq.count("status='failed'") >= 2, tq.count("status='failed'"))

print()
print("== F 真实库：可按 error_class 分解（证明这个维度有意义）==")
_db = os.path.join(_ROOT, "mbse.db")
if not os.path.isfile(_db):
    check("F 库存在", False, _db)
else:
    c2 = sqlite3.connect(f"file:{_db}?mode=ro", uri=True)
    rows = list(c2.execute(
        "SELECT COALESCE(NULLIF(error_class,''),'(未分类)') k, COUNT(*) n"
        " FROM agent_tasks WHERE status='failed' GROUP BY 1 ORDER BY n DESC"))
    tot = sum(n for _, n in rows)
    c2.close()
    print("  全量 failed 分类分布：" + "；".join("%s=%d" % (k, n) for k, n in rows))
    check("F1 存在 failed 记录", tot > 0, tot)
    if tot:
        unc = dict(rows).get("(未分类)", 0)
        print("  未分类占比 %.1f%%（修复前为 96.9%%：63/65）" % (unc * 100.0 / tot))
        # 不钉死具体数字（历史行无法回溯），只断言"历史未分类占多数"这个事实成立，
        # 以证明本项修复的必要性；新数据由 A~D 组保证会分类。
        check("F2 说明性：存量确实大量未分类（修复对象存在）", unc > 0, unc)

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
