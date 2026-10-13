# -*- coding: utf-8 -*-
"""批次二 §06 门禁：写操作幂等（hil_confirmations 幂等键）。

**背景（真库实证，口径 2026-10-13 只读）**：
`hil_service.py` 里有一句**悬空注释**「# 幂等 ID 命名空间（防生成重复确认）」——
**它描述的常量并不存在**，`grep -n "IDEMPOTENT|_IDEM" hil_service.py` 零命中；
`queue_confirmation` 是**无条件 INSERT**。
⇒ 后果直接可见：真库 `hil_confirmations` 6 行里，**id 13/14 是一模一样的一对**
（`agent_id` / `action` / `payload` 全同、`status` 同为 pending）
⇒ 重复提交已经在产生重复确认单。

**为什么这条不能只靠"tools 表加个幂等列"解决**：本仓写工具是**多入口**的
（`queue_confirmation` 人工确认队列 / `store_write_request` 编排暂存 / 工具直执行），
各自独立落库。**在共用的确认队列这一层做去重**，一处收口，收益最直接、风险最小。

**四个刻意取舍**（写在代码注释里，这里也钉住）：
  ① **不设 UNIQUE 约束**：确认单是"待办"，人工可能隔很久才处理。永久唯一会让
     "上次被拒、今天想重来"被静默吞掉（返回那条已拒绝的旧记录）——
     **那比产生重复确认更糟**（用户永远做不成事且看不出原因）。去重在代码层按窗口判定。
  ② **只对 pending 去重**：已 approved/rejected 的旧记录不再挡新提交。
  ③ **跨 run 不去重**：两个批次都试图建同一个实体，可能是两次独立决策；
     拦掉会让第二个批次"看起来成功但什么都没做"——那是更隐蔽的失败。
  ④ **payload 规范化**：`sort_keys` + 紧凑分隔符，否则 `{"a":1,"b":2}` 与 `{"b":2,"a":1}`
     会算出两个键，而它们显然是同一次操作。

**本脚本守什么**：
  [A] 悬空注释已落实为真实常量与函数（`IDEM_NS_CONFIRM` / `idempotency_key`）
  [B] 键构造：payload 规范化（键序无关）；纳入 agent/action/run/task
  [C] 行为层（临时库真跑）：同参重复提交**被挡住**；键序无关；跨 run 不去重；已审批不挡
  [D] 降级安全：`idem_key` 列缺失时**不启用幂等但不丢确认单**
  [E] DDL/迁移有该列与索引；**刻意无 UNIQUE**（门禁反向守着这条，避免有人"顺手加上"）
  [F] 真库重复现场可复算（作为改动前的基线证据）
  [G] 变异自证：去掉去重判据 ⇒ C1 必须 FAIL

**只读**：临时库（真库备份副本），**不写生产库**；变异在内存副本做。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_idempotency.py`
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
HIL = os.path.join(ROOT, "hil_service.py")
SCHEMA = os.path.join(ROOT, "database", "schema.py")
MIGR = os.path.join(ROOT, "database", "migrations", "columns.py")
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
from hil_service import HILService, idempotency_key, IDEM_NS_CONFIRM, IDEM_WINDOW_HOURS  # noqa: E402

h_src = io.open(HIL, encoding="utf-8").read()
sc_src = io.open(SCHEMA, encoding="utf-8").read()
m_src = io.open(MIGR, encoding="utf-8").read()

print("=== A. 悬空注释已落实为真实常量与函数 ===")
check("A1 IDEM_NS_CONFIRM 是真实常量", isinstance(IDEM_NS_CONFIRM, str) and IDEM_NS_CONFIRM,
      repr(IDEM_NS_CONFIRM))
check("A2 idempotency_key 可用且返回稳定字符串",
      isinstance(idempotency_key("a", "b", {}), str) and len(idempotency_key("a", "b", {})) > 0)
check("A3 同一入参两次调用得同键（稳定性）",
      idempotency_key("A", "act", {"x": 1}, 1, "t1") == idempotency_key("A", "act", {"x": 1}, 1, "t1"))
check("A4 幂等窗口已配置且为正", 0 < IDEM_WINDOW_HOURS <= 24 * 7, str(IDEM_WINDOW_HOURS))
check("A5 模块文档不再只有悬空注释（说明了取舍）",
      "取重" in h_src or "去重" in h_src)

print("\n=== B. 键构造正确性 ===")
check("B1 payload 键序无关",
      idempotency_key("A", "act", {"a": 1, "b": 2}) == idempotency_key("A", "act", {"b": 2, "a": 1}),
      "键序不同算出不同键 ⇒ 同一次操作会被当成两次")
check("B2 payload 值不同 ⇒ 键不同",
      idempotency_key("A", "act", {"x": 1}) != idempotency_key("A", "act", {"x": 2}))
check("B3 agent 不同 ⇒ 键不同（多 Agent 同参调用是不同意图）",
      idempotency_key("A", "act", {}) != idempotency_key("B", "act", {}))
check("B4 action 不同 ⇒ 键不同", idempotency_key("A", "x", {}) != idempotency_key("A", "y", {}))
check("B5 run 不同 ⇒ 键不同（跨批次不去重）",
      idempotency_key("A", "act", {}, 1, "t") != idempotency_key("A", "act", {}, 2, "t"))
check("B6 task_key 不同 ⇒ 键不同",
      idempotency_key("A", "act", {}, 1, "t1") != idempotency_key("A", "act", {}, 1, "t2"))
check("B7 排序规范化已用 sort_keys", 'sort_keys=True' in h_src)

print("\n=== C. 行为层：临时库真跑 ===")
_cur_src = io.open(os.path.abspath(__file__), encoding="utf-8").read()
TMP = tempfile.mkdtemp(prefix="verify_idem_")
TMP_DB = os.path.join(TMP, "t.db")
try:
    _c = sqlite3.connect(TMP_DB)
    _c.row_factory = sqlite3.Row
    _c.execute("""CREATE TABLE hil_confirmations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT DEFAULT '',
        conversation_id INTEGER DEFAULT 0, run_id INTEGER DEFAULT 0,
        action TEXT NOT NULL, payload TEXT DEFAULT '{}', preview TEXT DEFAULT '',
        status TEXT DEFAULT 'pending', decided_by TEXT DEFAULT '', decided_at TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP, task_key TEXT DEFAULT '',
        idem_key TEXT DEFAULT '')""")
    _c.execute("CREATE INDEX idx_hc_idem ON hil_confirmations(idem_key, status)")
    _c.commit()

    def _n():
        return _c.execute("SELECT COUNT(*) FROM hil_confirmations").fetchone()[0]

    _b = _n()
    _r1 = HILService.queue_confirmation(_c, "架构骨架生成Agent", "mbse_pull_ingest", {}, run_id=1, task_key="t1")
    _r2 = HILService.queue_confirmation(_c, "架构骨架生成Agent", "mbse_pull_ingest", {}, run_id=1, task_key="t1")
    check("C0 本脚本按关键字传 run_id/task_key（签名第 4/5 位是 preview/conversation_id）",
          'queue_confirmation(_c, "架构骨架生成Agent", "mbse_pull_ingest", {}, run_id=1, task_key="t1")' in _cur_src,
          "位置传参会把 run/task 传成 preview/conversation_id（本门禁第一版就踩了）")
    check("C1 同一次操作提交两次 ⇒ 只新增 1 行（重复被挡住）", _n() - _b == 1,
          "新增了 %d 行" % (_n() - _b))
    check("C2 第二次返回同一条记录（id 相同）", _r1["id"] == _r2["id"],
          "%s vs %s" % (_r1["id"], _r2["id"]))
    check("C3 idempotent_hit 标记正确（新建=False / 命中=True）",
          _r1.get("idempotent_hit") is False and _r2.get("idempotent_hit") is True)
    _b2 = _n()
    _r3 = HILService.queue_confirmation(_c, "A", "act", {"a": 1, "b": 2}, run_id=2, task_key="t2")
    # ⚠️ 第二次必须传**键序不同但内容相同**的 payload；第一次必然新建 1 行，
    #    所以基线要在**第一次调用之后**取（第一版在调用前取基线 ⇒ 把新建的那行算进"新增"，误判）。
    _r4 = HILService.queue_confirmation(_c, "A", "act", {"b": 2, "a": 1}, run_id=2, task_key="t2")
    check("C4 payload 键序不同仍算同一次（第二次不新增）", _n() == _b2 + 1,
          "第二次又新增了 1 行 ⇒ 键序影响了幂等键（got %d 行）" % (_n() - _b2))
    check("C4b 键序不同返回同一条记录", _r3["id"] == _r4["id"])
    _b3 = _n()
    _r5 = HILService.queue_confirmation(_c, "A", "act", {"x": 1}, run_id=3, task_key="t3")
    _r6 = HILService.queue_confirmation(_c, "A", "act", {"x": 1}, run_id=4, task_key="t3")
    check("C5 跨 run 不去重（两次独立决策都保留）", _n() - _b3 == 2 and _r5["id"] != _r6["id"])
    _r7 = HILService.queue_confirmation(_c, "B", "act", {"y": 1}, run_id=5, task_key="t4")
    _c.execute("UPDATE hil_confirmations SET status='approved' WHERE id=?", (_r7["id"],))
    _c.commit()
    _b4 = _n()
    _r8 = HILService.queue_confirmation(_c, "B", "act", {"y": 1}, run_id=5, task_key="t4")
    check("C6 已审批的旧记录不挡新提交（用户可以重来）", _n() - _b4 == 1,
          "被旧记录挡住了 ⇒ 用户永远做不成事")
    # C7 老库（无 idem_key 列）⇒ 不丢确认单
    _c2 = sqlite3.connect(":memory:")
    _c2.row_factory = sqlite3.Row
    _c2.execute("CREATE TABLE hil_confirmations (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "agent_id TEXT DEFAULT '', conversation_id INTEGER DEFAULT 0, run_id INTEGER DEFAULT 0, "
                "action TEXT NOT NULL, payload TEXT DEFAULT '{}', preview TEXT DEFAULT '', "
                "status TEXT DEFAULT 'pending', decided_by TEXT DEFAULT '', decided_at TEXT DEFAULT '', "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP, task_key TEXT DEFAULT '')")
    _c2.commit()
    _r9 = HILService.queue_confirmation(_c2, "A", "act", {"z": 1})
    _n9 = _c2.execute("SELECT COUNT(*) FROM hil_confirmations").fetchone()[0]
    check("C7 老库无 idem_key 列 ⇒ 仍写入确认单（不丢）",
          _r9 is not None and _n9 == 1, "ret=%r n=%s" % (_r9, _n9))
    _c2.close()
    _c.close()
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("\n=== D. 降级安全 ===")
check("D1 代码对 idem_key 列缺失做了探测",
      "SELECT idem_key FROM hil_confirmations" in h_src,
      "未探测列是否存在 ⇒ 老库会直接抛错")
check("D2 探测失败时降级为不启用幂等", '_idem = ""' in h_src)
check("D3 INSERT 有兜底路径（无 idem_key 列时用旧语句）",
      h_src.count("INSERT INTO hil_confirmations") >= 2,
      "只有一条 INSERT ⇒ 老库上确认单会写不进去")

print("\n=== E. DDL/迁移 ===")
check("E1 schema 有 idem_key", "idem_key TEXT DEFAULT ''" in sc_src)
check("E2 迁移补列有 idem_key",
      re.search(r'_add\("hil_confirmations",\s*"idem_key"', m_src) is not None)
check("E3 有 (idem_key,status) 索引", "idx_hc_idem" in sc_src)
# ★ 反向断言：刻意**不**设 UNIQUE —— 防止有人"顺手加上"而造成永久去重
_tbl = sc_src.split("CREATE TABLE IF NOT EXISTS hil_confirmations")[1].split('""")')[0]
# ⚠️ 判据必须**剥掉注释**再查：DDL 里那段注释本身就写着「不设 UNIQUE 约束」，
#    直接对整段做 "UNIQUE" in seg 会因为**注释里出现这个词**而假红（本门禁第一版就踩了）。
#    ⇒ 凡是「断言某关键字不存在」的门禁，都必须先剥注释。
_tbl_code = re.sub(r"--[^\n]*", "", _tbl)
_tbl_code = re.sub(r"/\*.*?\*/", "", _tbl_code, flags=re.S)
check("E4 ★ 刻意没有 UNIQUE 约束（永久去重会吞掉『被拒后重来」）",
      "UNIQUE" not in _tbl_code.upper(),
      "DDL 代码里出现了 UNIQUE（已剥注释）—— 那会让已拒绝的旧记录永久挡住新提交")
check("E5 索引是普通索引而非唯一索引",
      re.search(r"CREATE INDEX IF NOT EXISTS idx_hc_idem ON hil_confirmations\(idem_key, status\)",
                sc_src) is not None
      # ⚠️ 只能在**本索引这一行**上判 UNIQUE，不能对整个 schema.py 搜
      #    `CREATE UNIQUE INDEX` —— 别的表有唯一索引是正常的（那是它们的正确设计）。
      and not re.search(r"CREATE UNIQUE INDEX[^\n]*idx_hc_idem", sc_src)
      and not re.search(r"CREATE UNIQUE INDEX[^\n]*hil_confirmations", sc_src))

print("\n=== F. 真库重复现场（改动前基线）===")
if os.path.exists(PROD_DB):
    _pc = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _dups = _pc.execute(
        "SELECT agent_id, action, payload, COUNT(*) n FROM hil_confirmations "
        "GROUP BY agent_id, action, payload HAVING n > 1").fetchall()
    _tot = _pc.execute("SELECT COUNT(*) FROM hil_confirmations").fetchone()[0]
    _pc.close()
    print("  真库 hil_confirmations %d 行；重复组: %s" % (_tot, _dups if _dups else "无"))
    check("F1 记录到改动前确有重复现场（本条修复的由来）", bool(_dups),
          "真库已无重复 ⇒ 基线描述需更新")
else:
    print("  [SKIP] 未找到真库")

print("\n=== G. 变异自证（去掉去重判据）===")
_anchor = 'if dup:'
if _anchor not in h_src:
    print("  [VACUOUS] 锚点不在源码里，自证中止")
else:
    _mut = h_src.replace(_anchor, "if False and dup:  # MUTATION: 不做去重", 1)
    import types
    _mod = types.ModuleType("_mut_hil")
    _mod.__file__ = "<mutant>"
    try:
        exec(compile(_mut, "<mutant>", "exec"), _mod.__dict__)
        _T2 = tempfile.mkdtemp(prefix="verify_idem_mut_")
        _D2 = os.path.join(_T2, "m.db")
        _mc = sqlite3.connect(_D2)
        _mc.row_factory = sqlite3.Row
        _mc.execute("""CREATE TABLE hil_confirmations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT DEFAULT '',
            conversation_id INTEGER DEFAULT 0, run_id INTEGER DEFAULT 0,
            action TEXT NOT NULL, payload TEXT DEFAULT '{}', preview TEXT DEFAULT '',
            status TEXT DEFAULT 'pending', decided_by TEXT DEFAULT '', decided_at TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, task_key TEXT DEFAULT '',
            idem_key TEXT DEFAULT '')""")
        _mc.commit()
        _mod.HILService.queue_confirmation(_mc, "A", "act", {}, run_id=1, task_key="t1")
        _mod.HILService.queue_confirmation(_mc, "A", "act", {}, run_id=1, task_key="t1")
        _mn = _mc.execute("SELECT COUNT(*) FROM hil_confirmations").fetchone()[0]
        check("G1 变异体（不去重）上 C1 必须 FAIL（会写 2 行）", _mn == 2,
              "变异体只写了 %d 行 ⇒ C1 抓不住变异，自证无效" % _mn)
        _mc.close()
        shutil.rmtree(_T2, ignore_errors=True)
        _muts.append(("M1", "queue_confirmation 不做幂等去重（回到改动前形态）"))
        print("  [SELF-PROOF] M1 去掉去重 —— 同参提交写出 2 行，"
              "证明该判据承重（真库里 id 13/14 就是这么来的）")
    except Exception as e:
        check("G1 变异体执行成功", False, str(e))

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)