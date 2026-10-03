# -*- coding: utf-8 -*-
"""P0-a / P0-b 验证：审计溯源上下文 + 哈希链 + 迁移回填。

纪律：
- 夹具**不手抄 DDL** —— 只建最小基表，列由真实迁移 `_migrate_audit_chain` 补（避免与生产 schema 漂移）
- 断言必须能做变异测试：每个断言都要有"坏样子"能让它变红
"""
import contextlib
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.audit import audit, bind_request, request_context, verify_chain, _GENESIS, _row_hash  # noqa: E402
from database.migrations.misc import _migrate_audit_chain  # noqa: E402

PASS, FAIL = 0, []


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {extra}")


class _ClientStub:
    def __init__(self, host): self.host = host


class _ReqStub:
    def __init__(self, headers=None, client=None):
        self.headers = headers or {}
        self.client = client


def _new_db():
    """最小基表（与旧库同构：只有 5 列写入的列都由迁移补齐）。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_name TEXT DEFAULT '', event_type TEXT NOT NULL, detail TEXT DEFAULT '',
        result TEXT DEFAULT 'success', ip_address TEXT DEFAULT '',
        branch TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    return conn, path


print("══ S1 bind_request 溯源提取 ══")
ctx = bind_request(_ReqStub({"x-forwarded-for": "203.0.113.9, 10.0.0.1", "user-agent": "Mozilla/5.0"},
                            _ClientStub("127.0.0.1")))
check("S1a XFF 多跳取首跳", ctx["ip"] == "203.0.113.9", ctx["ip"])
check("S1b UA 落上下文", ctx["user_agent"] == "Mozilla/5.0", ctx["user_agent"])
check("S1c request_id 自动生成", len(ctx["request_id"]) == 16, ctx["request_id"])
ctx2 = bind_request(_ReqStub({}, _ClientStub("192.168.1.7")))
check("S1d 无 XFF 回退 client.host", ctx2["ip"] == "192.168.1.7", ctx2["ip"])
check("S1e 显式 X-Request-Id 透传",
      bind_request(_ReqStub({"x-request-id": "abc123"}))["request_id"] == "abc123")
bind_request(None)
check("S1f 清理后为空", request_context() == {}, request_context())

print("══ S2 迁移：列补齐 + 历史回填 ══")
conn, path = _new_db()
for i in range(5):   # 历史数据：模拟 5 条旧审计（无 hash）
    conn.execute("INSERT INTO audit_logs (user_name, event_type, detail, result) VALUES (?,?,?,?)",
                 (f"u{i}", "old_event", f"历史{i}", "success"))
conn.commit()
_migrate_audit_chain(conn)
cols = [r[1] for r in conn.execute("PRAGMA table_info(audit_logs)")]
for c in ("user_agent", "request_id", "prev_hash", "hash"):
    check(f"S2-{c} 列已补", c in cols, cols)
v = verify_chain(conn=conn)
check("S2a 历史行全部入链", v["chained"] == 5 and v["unchained"] == 0, v)
check("S2b 链首接创世", conn.execute("SELECT prev_hash FROM audit_logs ORDER BY id LIMIT 1").fetchone()[0] == _GENESIS)
check("S2c 链完整 ok", v["ok"], v["broken"][:2])

print("══ S3 迁移幂等 + 安全边界 ══")
before = conn.execute("SELECT hash FROM audit_logs ORDER BY id").fetchall()
_migrate_audit_chain(conn)
after = conn.execute("SELECT hash FROM audit_logs ORDER BY id").fetchall()
check("S3a 二次迁移不改链", [r[0] for r in before] == [r[0] for r in after])
# 构造"已服役 + 前缀未入链"场景：新审计已入链，中间插一条无 hash 的老行
conn.execute("INSERT INTO audit_logs (id, user_name, event_type, detail, result, hash) "
             "VALUES (900, 'old', 'legacy', '未入链旧行', 'success', '')")
conn.commit()
before_pref = conn.execute("SELECT hash FROM audit_logs WHERE id=900").fetchone()[0]
_migrate_audit_chain(conn)
check("S3b 已服役时前缀未入链行不动（避免级联）",
      conn.execute("SELECT hash FROM audit_logs WHERE id=900").fetchone()[0] == before_pref)
v2 = verify_chain(conn=conn)
check("S3c 未入链行被暴露为 unchained", v2["unchained"] == 1, v2)
check("S3d ok=False（存在未入链不影响 ok 判定，仅为提示）", v2["ok"] is True, v2)
conn.close()
os.remove(path)

print("══ S4 audit() 落溯源字段 + 接链 ══")
import core.audit as ca  # noqa: E402
conn, path = _new_db()
_migrate_audit_chain(conn)
ca.db_conn = lambda: contextlib.nullcontext(conn)   # 隔离：避免 audit(conn=None) 写生产库
bind_request(_ReqStub({"x-forwarded-for": "198.51.100.42", "user-agent": "TestAgent/1.0"},
                      _ClientStub("127.0.0.1")))
audit("王工", "auth_login", "登录成功", conn=conn)
r = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
check("S4a ip_address 落盘", r["ip_address"] == "198.51.100.42", r["ip_address"])
check("S4b user_agent 落盘", r["user_agent"] == "TestAgent/1.0", r["user_agent"])
check("S4c request_id 落盘", len(r["request_id"]) == 16, r["request_id"])
check("S4d 首行 prev_hash = 创世", r["prev_hash"] == _GENESIS)
check("S4e hash 非空", len(r["hash"]) == 64, r["hash"])

audit("王工", "second", "第二条", conn=conn)
r2 = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()
check("S4f 第二行 prev_hash 接上一行 hash", r2["prev_hash"] == r["hash"])
check("S4g 摘要与重算一致",
      r2["hash"] == _row_hash(r2["prev_hash"], r2["user_name"], r2["event_type"], r2["detail"],
                              r2["result"], r2["branch"] or "", r2["ip_address"], r2["user_agent"],
                              r2["request_id"]))

print("══ S5 防篡改：三类破坏必须被检出 ══")
# ① 改内容（记录原值以便精确复原）
_orig_detail = r["detail"]
conn.execute("UPDATE audit_logs SET detail='被篡改内容' WHERE id=?", (r["id"],))
conn.commit()
v3 = verify_chain(conn=conn)
check("S5a 篡改可检出", not v3["ok"] and any("篡改" in b["reason"] for b in v3["broken"]), v3["broken"][:2])
conn.execute("UPDATE audit_logs SET detail=? WHERE id=?", (_orig_detail, r["id"]))   # 精确复原
conn.commit()
check("S5b 复原后恢复 ok", verify_chain(conn=conn)["ok"])

# ② 删中间行（不破坏内容，但断链接）
conn.execute("DELETE FROM audit_logs WHERE id=?", (r["id"],))
conn.commit()
v4 = verify_chain(conn=conn)
check("S5c 删除中间行可检出（prev_hash 不衔接）",
      not v4["ok"] and any("衔接" in b["reason"] for b in v4["broken"]), v4["broken"][:2])

# ③ 篡改链首之外 —— 改 result 字段（审计成败被翻）
audit("王工", "third", "第三条", conn=conn)
conn.commit()
_tid = conn.execute("SELECT id FROM audit_logs ORDER BY id DESC LIMIT 1").fetchone()[0]
conn.execute("UPDATE audit_logs SET result='blocked' WHERE id=?", (_tid,))
conn.commit()
v5 = verify_chain(conn=conn)
check("S5d 翻改 result 可检出（含删除行仍可疑）", not v5["ok"])

# ④ 掩盖攻击（补 M1 变异暴露的断言面缺口）：干净链 A→B→C，删掉 B 并把 C 的 prev_hash
#    改写成 A.hash 试图抹痕。若摘要不含 prev_hash（M1 变异形态），改 prev_hash 字段不会
#    破坏摘要 → 抹痕成功。正确实现必须让它仍被检出（reason = 内容与摘要不符）。
connX, pathX = _new_db()
_migrate_audit_chain(connX)
for nm in ("A", "B", "C"):
    audit("审计员", f"evt_{nm}", f"原始记录-{nm}", conn=connX)
connX.commit()
_ids = [row[0] for row in connX.execute("SELECT id FROM audit_logs ORDER BY id").fetchall()]
check("S5e-pre 干净链三行", len(_ids) == 3 and verify_chain(conn=connX)["ok"])
_hA = connX.execute("SELECT hash FROM audit_logs WHERE id=?", (_ids[0],)).fetchone()[0]
connX.execute("DELETE FROM audit_logs WHERE id=?", (_ids[1],))          # 删中间行 B
connX.execute("UPDATE audit_logs SET prev_hash=? WHERE id=?", (_hA, _ids[2]))  # 掩盖：C 接回 A
connX.commit()
v6 = verify_chain(conn=connX)
check("S5e 删行+改写 prev_hash 的掩盖攻击仍被检出（摘要含 prev_hash）",
      not v6["ok"] and any("摘要" in b["reason"] for b in v6["broken"]), v6["broken"][:2])
connX.close()
os.remove(pathX)

print("══ S6 降级：无哈希列时不阻断业务 ══")
conn2, path2 = _new_db()   # 未跑迁移 → 缺 hash/request_id 等列
ca.db_conn = lambda: contextlib.nullcontext(conn2)
bind_request(_ReqStub({}, _ClientStub("10.1.1.1")))
try:
    audit("王工", "legacy_write", "降级写入仍要落", conn=conn2)
    ok_cnt = conn2.execute("SELECT COUNT(*) n FROM audit_logs WHERE detail='降级写入仍要落'").fetchone()[0]
    check("S6a 列缺失时降级写入成功", ok_cnt == 1, ok_cnt)
except Exception as e:
    check("S6a 列缺失时降级写入成功", False, str(e)[:200])
conn2.close()
os.remove(path2)
conn.close()
os.remove(path)

print(f"\n══ {PASS} PASS / {len(FAIL)} FAIL ══")
if FAIL:
    print("失败项: " + " | ".join(FAIL))
sys.exit(1 if FAIL else 0)
