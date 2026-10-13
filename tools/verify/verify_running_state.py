# -*- coding: utf-8 -*-
"""B1-3 门禁：`running` 态真正写入（claim() 此前零调用点）。

**背景（对标取证）**：`task_queue.py:138` 的 `claim()` 能写 `status='running'`，
但**全仓零调用点**（grep `TaskQueue.claim` 无命中）⇒ 实测
`agent_tasks.status` 库内 `running` **0 行**（222 行只有 done 168 / failed 54）。

**后果（两条，都指向"看不见"）**：
  1. **失去"执行中"可观测性**：进程崩溃时任务行仍停在 `ready`/`planned`，
     resume 无法区分"**从未开始**"与"**跑到一半**"；
  2. **取消链路缺技术前置**：要"取消正在跑的任务"，先得能判断哪个正在跑。

**为什么 resume 侧不用改**：`orch_checkpoint.policy_classify`（`:209-225`）**早已能正确处理
running** —— `run_has_owner=True` ⇒ `owned`（拒绝接管，防双跑写重复实体）；
`False` ⇒ `requeue`。此前只是**这个状态从没被写入过**，整套判据空转。

**本脚本守什么**：
  [A] claim() 有真实调用点，且在**worker 内**（不是主循环批量写——
      那会把"尚未开始"的任务也标成执行中，制造比现状更糟的失真）
  [B] 写入失败不阻断执行（可观测性不是门禁）
  [C] claim 自带前置条件与幂等（并发下不重复标、不报错）
  [D] resume 侧对 running 的判据仍在（policy_classify 的 owned/requeue 两支）
  [E] ready_tasks 不含 running（否则主循环会把正在跑的任务再取一次 ⇒ 重复执行）
  [F] **行为层**：真跑一遍编排主循环片段，确认 running 真的落库
  [G] 变异自证：把 worker 里的 claim 去掉 ⇒ A 必须 FAIL

**只读**：不连生产库。用临时库自建 `agent_tasks` 表（对齐生产列集合，
参照 verify_orch_resume.py 的既有做法），跑完即删。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_running_state.py`
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
TQUEUE = os.path.join(ROOT, "task_queue.py")
CKPT = os.path.join(ROOT, "agent", "orch_checkpoint.py")

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


s_src = io.open(STREAM, encoding="utf-8").read()
t_src = io.open(TQUEUE, encoding="utf-8").read()
c_src = io.open(CKPT, encoding="utf-8").read()

print("=== A. claim() 有调用点且在 worker 内 ===")
# 定位 _worker 函数体（从 def _worker 到下一个同缩进 def）
_m = re.search(r'\n(\s*)def _worker\(tk, task_pool\):', s_src)
if not _m:
    check("A0 能定位 _worker 定义", False, "未找到 _worker")
    _worker_body = ""
    _indent = ""
else:
    _indent = _m.group(1)
    # 截到下一个缩进相同的 def
    _rest = s_src[_m.end():]
    _nxt = re.search(r'\n%sdef ' % re.escape(_indent), _rest)
    _worker_body = _rest[:_nxt.start()] if _nxt else _rest

check("A0 能定位 _worker 函数体", bool(_worker_body))
check("A1 worker 内调用了 TaskQueue.claim", "TaskQueue.claim(" in _worker_body)
check("A2 claim 调用带了 task_id", re.search(r'TaskQueue\.claim\(\s*conn\s*,\s*tk\["id"\]\s*\)', _worker_body) is not None)
# 反向：主循环里**不能**有批量 claim（那会把未开始的任务标 running）
check("A3 主循环无批量 claim（只 worker 内逐个标）",
      s_src.count("TaskQueue.claim(") == 1,
      "全仓 %d 处调用，可能有批量写" % s_src.count("TaskQueue.claim("))

print("\n=== B. 写入失败不阻断执行 ===")
_m2 = re.search(r'(try:\s*\n\s*TaskQueue\.claim\(conn, tk\["id"\]\)\s*\n(\s*)except Exception:)', _worker_body)
check("B1 claim 被 try/except 包住（异常不外溢）", _m2 is not None,
      "裸调用会让 DB 抖动直接打断子任务执行")

print("\n=== C. claim 自带前置条件与幂等 ===")
check("C1 claim 带 status IN ('planned','ready') 前置条件",
      re.search(r"def claim.*?status IN \('planned','ready'\)", t_src, re.S) is not None,
      "无前置条件 ⇒ 并发下可能重复标记已 done 的任务")
_claim_body = ""
if "def claim" in t_src:
    _rest = t_src.split("def claim", 1)[1]
    _nxt_c = re.search(r'\n    @staticmethod', _rest)
    _claim_body = _rest[:_nxt_c.start()] if _nxt_c else _rest[:1200]
check("C2 claim 会更新时间戳（便于观察心跳）", "updated_at=CURRENT_TIMESTAMP" in _claim_body)
# C3 ★ commit 是必需项（2026-10-13 实测踩到）：claim 曾是本类**唯一**不带 commit 的状态推进，
#    而 conn 是主循环与 worker 线程共用 —— 事务悬挂会把主循环心跳拖住，
#    实测打红 verify_orch_reliability 的 G1（活跃产出被误判 "1s 无产出"）。这条必须守住。
check("C3 claim 带 commit（否则悬挂事务拖住主循环心跳）", "conn.commit()" in _claim_body,
      "claim 是本类唯一不带 commit 的状态推进 ⇒ 主循环心跳会被拖住")
# C4 与同类对齐：其余状态推进都带 commit
# ⚠️ 窗口必须够宽：complete/resolve 的签名行较长（带默认参数），
#    400 字符会在 commit 之前就截断 ⇒ 误判成"没带 commit"。用「到下一个 @staticmethod」切段。
for _fn in ("def complete", "def fail", "def retry", "def resolve"):
    _b = ""
    if _fn in t_src:
        _r = t_src.split(_fn, 1)[1]
        _nx = re.search(r'\n    @staticmethod', _r)
        _b = _r[:_nx.start()] if _nx else _r[:900]
    check("C4 %s 也带 commit（同类一致性）" % _fn.replace("def ", ""),
          "conn.commit()" in _b,
          "未找到 commit —— 同类状态推进应一致")

print("\n=== D. resume 侧对 running 的判据仍在 ===")
_pc = c_src.split("def policy_classify")[1][:900] if "def policy_classify" in c_src else ""
check("D1 policy_classify 仍有 running 分支", 'if st == "running"' in _pc)
check("D2 有主时判 owned（防双跑）", '"owned" if run_has_owner else "requeue"' in _pc)
check("D3 ALIVE_STATUSES 含 running", '"running"' in
      c_src.split("ALIVE_STATUSES = ")[1][:80] if "ALIVE_STATUSES = " in c_src else False)

print("\n=== E. ready_tasks 不含 running（防重复执行）===")
_rt = t_src.split("def ready_tasks")[1][:700] if "def ready_tasks" in t_src else ""
check("E1 ready_tasks 只取 planned/ready（不含 running）",
      "status IN ('planned','ready')" in _rt)
check("E2 ready_tasks 有注释说明（可读性）", "就绪" in _rt)

print("\n=== F. 行为层：claim 真能落库 + 前置条件生效 ===")
TMP = tempfile.mkdtemp(prefix="verify_running_")
TMP_DB = os.path.join(TMP, "t.db")
try:
    _c = sqlite3.connect(TMP_DB)
    # ⚠️ 列集合对齐生产 agent_tasks（含 claim/update 用到的列）
    _c.execute("""CREATE TABLE agent_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER, task_key TEXT, title TEXT, agent_id TEXT,
        task_type TEXT, config TEXT, deps TEXT, status TEXT DEFAULT 'planned',
        result TEXT, metadata TEXT, error TEXT, assigned_by TEXT,
        seq INTEGER DEFAULT 0, latency_ms INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    _c.execute("INSERT INTO agent_tasks (run_id,task_key,status,deps) VALUES (1,'t1','ready','[]')")
    _c.execute("INSERT INTO agent_tasks (run_id,task_key,status,deps) VALUES (1,'t2','planned','[\"t1\"]')")
    _c.execute("INSERT INTO agent_tasks (run_id,task_key,status,deps) VALUES (1,'t3','done','[]')")
    _c.commit()
    _c.close()

    sys.path.insert(0, ROOT)
    from task_queue import TaskQueue

    _c = sqlite3.connect(TMP_DB)
    _c.row_factory = sqlite3.Row

    # F1 planned → running
    TaskQueue.claim(_c, 1)
    check("F1 ready 任务被 claim 成 running",
          _c.execute("SELECT status FROM agent_tasks WHERE id=1").fetchone()["status"] == "running",
          "got=%s" % _c.execute("SELECT status FROM agent_tasks WHERE id=1").fetchone()["status"])

    # F2 幂等：重复 claim 不报错、状态不变
    try:
        TaskQueue.claim(_c, 1)
        TaskQueue.claim(_c, 1)
        check("F2 重复 claim 幂等不抛异常", True)
    except Exception as e:
        check("F2 重复 claim 幂等不抛异常", False, str(e))

    # F3 前置条件生效：done 任务不被重新标 running（这是最关键的防呆）
    TaskQueue.claim(_c, 3)
    check("F3 done 任务不被 claim 成 running（前置条件承重）",
          _c.execute("SELECT status FROM agent_tasks WHERE id=3").fetchone()["status"] == "done",
          "got=%s" % _c.execute("SELECT status FROM agent_tasks WHERE id=3").fetchone()["status"])

    # F4 running 不进 ready_tasks（否则主循环会重复取到正在跑的任务）
    _ready = TaskQueue.ready_tasks(_c, 1)
    _rkeys = [r["task_key"] for r in _ready]
    check("F4 running 任务不出现在 ready_tasks（防重复执行）",
          "t1" not in _rkeys, "ready=%s" % _rkeys)
    # ⚠️ t2 依赖 t1，而 t1 此刻已被 claim 成 running（**不是** done）
    #    ⇒ ready_tasks 正确地不返回 t2。这不是缺陷，是依赖语义在起作用。
    check("F4b 依赖项未 done 时下游不被放出（依赖语义仍承重）",
          "t2" not in _rkeys, "ready=%s" % _rkeys)

    # F5 独立验证「依赖已满足的 planned 能被取出」：把 t1 置 done 后 t2 应出现
    _c.execute("UPDATE agent_tasks SET status='done' WHERE id=1")
    _c.commit()
    _rkeys2 = [r["task_key"] for r in TaskQueue.ready_tasks(_c, 1)]
    check("F5 依赖满足后下游任务被放出（ready_tasks 本身正常）",
          "t2" in _rkeys2, "ready=%s" % _rkeys2)

    # F6 pending_count 把 running 算作未完成（否则主循环会提前收尾）
    # ⚠️ 当前状态：t1=done（F5 里置的）/ t2=planned / t3=done ⇒ 未完成 1 个
    check("F6 pending_count 与实际未完成数一致",
          TaskQueue.pending_count(_c, 1) == 1,
          "got=%s（应为 1：仅 t2 未完成）" % TaskQueue.pending_count(_c, 1))
    # F6b 关键：把 t2 标成 running 后 pending_count 仍应为 1（**不得**因 running 被排除而变 0）
    _c.execute("UPDATE agent_tasks SET status='running' WHERE id=2")
    _c.commit()
    check("F6b running 计入 pending_count（否则主循环误判批次已结束）",
          TaskQueue.pending_count(_c, 1) == 1,
          "got=%s（running 若被排除，主循环会提前 break，剩下的任务永远不执行）"
          % TaskQueue.pending_count(_c, 1))

    _c.close()
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n=== G. 变异自证（去掉 worker 里的 claim，判据必须失效）===")
_m3 = re.search(r'\n(\s*)def _worker\(tk, task_pool\):', s_src)
_rest = s_src[_m3.end():]
_nxt = re.search(r'\n%sdef ' % re.escape(_m3.group(1)), _rest)
_body = _rest[:_nxt.start()] if _nxt else _rest
_mut = _body.replace("TaskQueue.claim(conn, tk[\"id\"])", "pass  # MUTATION: 不写 running")
if "TaskQueue.claim(conn, tk[\"id\"])" not in _body:
    print("  [VACUOUS] 锚点不在 worker 内，自证中止")
elif _mut == _body:
    print("  [VACUOUS] 变异未生效")
else:
    check("G1 变异体上 A1 必须 FAIL（worker 内无 claim 调用）",
          "TaskQueue.claim(" not in _mut)
    check("G2 变异体上 A3 必须 FAIL（全仓 0 处调用）",
          (s_src.replace(_body, _mut)).count("TaskQueue.claim(") == 0,
          "变异后仍有其它调用点，A3 抓不住变异")
    _muts.append(("M1", "worker 内不调用 claim()"))
    print("  [SELF-PROOF] M1 worker 内去掉 claim —— 全仓回到零调用点的原始形态")

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)