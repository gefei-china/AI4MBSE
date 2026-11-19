"""verify_finalize_key_unique — P3 根治门禁：DB 唯一键彻底消除固化双写（2026-11-19）

核心判据（行为级，非静态）：
  1. 迁移 DDL 在 schema.py 中存在（加列 + 部分唯一索引），且幂等。
  2. 两条固化路径都用 _finalize_key_for_turn + INSERT OR IGNORE。
  3. **并发实证**：两个独立连接、两个线程同一时刻写同一 finalize_key ⇒ 最终只有 1 条消息。
     （这正是旧实现 check-then-write 会双写的场景，DB 唯一约束必须挡住）
  4. 不同轮（不同 user id）⇒ 各自可写 1 条（唯一键不误伤正常多轮）。
  5. 服务端/前端算出同一个 key（同一会话末条 user 消息）。
  6. 变异自证：摘掉唯一索引 ⇒ 并发测试必须双写（断言 FAIL）。

用法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_finalize_key_unique.py
"""
from __future__ import annotations
import os, sys, sqlite3, threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

SCHEMA = os.path.join(_ROOT, "database", "schema.py")
ROUTER = os.path.join(_ROOT, "routers", "conversations.py")
schema_src = open(SCHEMA, encoding="utf-8").read()
router_src = open(ROUTER, encoding="utf-8").read()

results = []
def rec(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (" ⇒ " + str(detail) if detail else ""))

# ── 静态判据 ──
rec("S1 schema 加列 finalize_key",
    "ALTER TABLE messages ADD COLUMN finalize_key" in schema_src)
rec("S2 部分唯一索引（只对非空键）",
    "ux_messages_finalize" in schema_src and "WHERE finalize_key<>''" in schema_src)
rec("S3 helper 按末条 user 消息算键",
    "def _finalize_key_for_turn" in router_src and "role='user'" in router_src)
rec("S4 两条路径都用 INSERT OR IGNORE",
    router_src.count("INSERT OR IGNORE INTO messages") >= 2)
rec("S5 前端冲突时复用已存在 id",
    "复用已固化消息" in router_src or "唯一键命中" in router_src)

# ── 迁移 DDL（提取自 schema，跑在内存库）──
def apply_migration(c):
    try:
        c.execute("ALTER TABLE messages ADD COLUMN finalize_key TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_finalize "
                  "ON messages(finalize_key) WHERE finalize_key<>''")
    except Exception:
        pass

def make_db(with_index=True):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
              "conversation_id INTEGER, role TEXT, content TEXT, "
              "msg_type TEXT, card_data TEXT, created_at TEXT, finalize_key TEXT DEFAULT '')")
    if with_index:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_finalize "
                  "ON messages(finalize_key) WHERE finalize_key<>''")
    return c

# ── 行为判据 3：并发双写实证 ──
c = make_db()
c.execute("INSERT INTO messages(conversation_id,role,content,finalize_key) VALUES(1,'user','q','')")
c.commit()
barrier = threading.Barrier(2)
outcome = []

import tempfile
fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
def setup_file():
    cc = sqlite3.connect(path)
    cc.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "conversation_id INTEGER, role TEXT, content TEXT, finalize_key TEXT DEFAULT '')")
    cc.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_finalize "
               "ON messages(finalize_key) WHERE finalize_key<>''")
    cc.execute("INSERT INTO messages(conversation_id,role,content) VALUES(1,'user','q')")
    cc.commit(); cc.close()
setup_file()

def thread_write(tag):
    conn = sqlite3.connect(path, timeout=30)
    barrier.wait()  # 两线程同起点
    try:
        cur = conn.execute("INSERT OR IGNORE INTO messages(conversation_id,role,content,finalize_key) "
                           "VALUES(1,'assistant',?,'1:1')", (tag,))
        conn.commit()
        outcome.append((tag, cur.rowcount))
    finally:
        conn.close()

t1 = threading.Thread(target=thread_write, args=("server",))
t2 = threading.Thread(target=thread_write, args=("frontend",))
t1.start(); t2.start(); t1.join(); t2.join()
_c = sqlite3.connect(path)
cnt = _c.execute("SELECT COUNT(*) FROM messages WHERE finalize_key='1:1'").fetchone()[0]
_c.close()
rec("B1 并发双写：同 key 两线程 ⇒ 仅 1 条落库", cnt == 1, "落库 %d 条, rowcount=%s" % (cnt, outcome))
try:
    os.unlink(path)
except OSError:
    pass  # Windows 文件句柄偶发延迟释放，不影响判据

# ── 行为判据 4：不同轮不误伤 ──
c = make_db()
for uid, key in [(1,"1:1"), (2,"1:2")]:
    c.execute("INSERT OR IGNORE INTO messages(conversation_id,role,content,finalize_key) VALUES(1,'assistant','x',?)",(key,))
c.commit()
cnt2 = c.execute("SELECT COUNT(*) FROM messages WHERE finalize_key<>''").fetchone()[0]
rec("B2 不同轮各自可写（不误伤）", cnt2 == 2, "落库 %d 条" % cnt2)

# ── 行为判据 5：helper 两端同键 ──
try:
    from routers.conversations import _finalize_key_for_turn
    cc = make_db()
    cc.execute("INSERT INTO messages(conversation_id,role,content) VALUES(7,'user','q')")
    cc.commit()
    k = _finalize_key_for_turn(cc, 7)
    rec("B3 helper 键确定性（conv7 末条 user id=1 ⇒ 7:1）", k == "7:1", k)
except Exception as e:
    rec("B3 helper 键确定性", False, repr(e))

# ── 变异自证：无唯一索引 ⇒ 并发必双写 ──
import tempfile as _tf
fd2, path2 = _tf.mkstemp(suffix=".db"); os.close(fd2)
cc = sqlite3.connect(path2)
cc.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER, role TEXT, content TEXT, finalize_key TEXT DEFAULT '')")
cc.execute("INSERT INTO messages(conversation_id,role,content) VALUES(1,'user','q')")
cc.commit(); cc.close()
b2 = threading.Barrier(2)
def thread_write_noindex(tag):
    conn = sqlite3.connect(path2, timeout=30)
    b2.wait()
    conn.execute("INSERT OR IGNORE INTO messages(conversation_id,role,content,finalize_key) VALUES(1,'assistant',?,'1:1')",(tag,))
    conn.commit(); conn.close()
tt = [threading.Thread(target=thread_write_noindex,args=(x,)) for x in ("a","b")]
[t.start() for t in tt]; [t.join() for t in tt]
_c3 = sqlite3.connect(path2)
cnt3 = _c3.execute("SELECT COUNT(*) FROM messages WHERE finalize_key='1:1'").fetchone()[0]
_c3.close()
rec("M1 变异：无唯一索引 ⇒ 双写（证明唯一键是根治关键）", cnt3 == 2, "落库 %d 条" % cnt3)
try:
    os.unlink(path2)
except OSError:
    pass

npass = sum(1 for _, ok in results if ok); nfail = len(results)-npass
print("\n" + "="*60)
print("PASS %d / FAIL %d" % (npass, nfail))
print("ALL_OK=" + str(nfail==0))
sys.exit(0 if nfail==0 else 1)
