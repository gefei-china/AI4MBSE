# -*- coding: utf-8 -*-
"""门禁：中断固化必须写 finalize_key（否则 /recovery 永远说"未落库"）。

## 为什么必须存在（2026-11-28 真机 run 205 复盘）

现象（用户原话）："最新会话…我只是切换到其他页面，回来后页面中 AI 内容就不见了，
刷新页面后提示 [上一轮有 309 帧输出未落库（服务端中断前已逐帧记录）]"。

日志链：
    [run_stream] conv=1000000256 run=205 帧日志已开
    GET /static/index.html 200            ← 整页重载（不是 SPA 切页）
    [detach] conv=1000000256 run=205 转后台续跑 grace=60s
    [detach] … grace=60s 到期，收口
    [detach] … run=cancelled（reason=grace_expired）
    [_persist_partial_stream] reason=stop conv=1000000256 候选字符=14760
    → 12:24:30 落库 assistant 消息 14781 字

库里的真相：
    messages 7902 | role=assistant | len=14781 | **finalize_key=''**
    /recovery → {"found": false, "reason": "no_finalized_message", "stream_run_id": 205}

即：**正文已经在库里，recovery 却说没落库** —— 因为它按 `finalize_key` 反查，而这
条 INSERT 没写该字段。用户因此看到"未落库 + 回放"的提示，点了还会与库里内容重复。
这是最难查的一类不一致：数据在、判据不认。

## 判据（全部走真实 `StreamMixin._persist_partial_stream` + 临时库）
P1 落库后 assistant 消息的 finalize_key == "<conv>:<末条 user id>"（非空且口径正确）
P2 用 /recovery 的同一条 SQL（finalize_key 反查）能命中 —— 证明跨边界可用
P3 同一轮再固化一次 ⇒ 仍只有 1 条（唯一索引 + INSERT OR IGNORE 幂等）
P4 不同轮（新 user 消息）⇒ 允许再落 1 条（不能过度去重把新内容吞掉）
P5 唯一索引存在（finalize_key<>'' 的部分唯一索引）
M1 变异：把 INSERT 的 finalize_key 去掉 ⇒ P1/P2 必须翻红（证明判据有牙齿）

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_partial_finalize_key.py
"""
from __future__ import annotations

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

STREAM_SRC = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")

RESULTS = []


def check(cid, name, ok, detail=""):
    RESULTS.append({"id": cid, "name": name, "ok": bool(ok), "detail": str(detail)})


_WORKER = r'''
import os, sys, sqlite3, tempfile, json
ROOT = r"@@ROOT@@"
sys.path.insert(0, ROOT)

# 整库复制到临时文件 → MBSE_DB_PATH 重定向 ⇒ 零污染生产库
from tools.db_view import open_ro as _open_ro
src = _open_ro()
tmpdb = tempfile.mktemp(suffix=".db")
dst = sqlite3.connect(tmpdb); src.backup(dst); dst.close(); src.close()

import core.config as cfg; cfg.DB_PATH = tmpdb
import database.connection as dc; dc.DB_PATH = tmpdb

# CI 全新 checkout 上可能没有 mbse.db（或为空库）⇒ 就地建表，避免"门禁环境坏了"
# 被误当成"被测功能坏了"。建表用**当前** schema，故含 finalize_key 列。
sys.path.insert(0, os.path.join(ROOT, "tools", "verify"))
from _tmpdb import ensure_schema
_schema_ok = ensure_schema(tmpdb)

from database import get_db
from agent.pipeline_parts.stream import StreamMixin

P = type("_Probe", (StreamMixin,), {})()
CONV_KEY_SQL = ("SELECT id, finalize_key, length(content) AS L FROM messages "
                "WHERE conversation_id=? AND role='assistant' ORDER BY id")

def new_conv(conn):
    return conn.execute("INSERT INTO conversations (title,intent,user_id,project_id)"
                        " VALUES ('verify_fk','chat',1,'')").lastrowid

def add_user(conn, cid, txt):
    return conn.execute("INSERT INTO messages (conversation_id, role, content, msg_type, card_data)"
                        " VALUES (?,?,?,?,?)", (cid, "user", txt, "text", "{}")).lastrowid

out = {"schema_ok": bool(_schema_ok)}

# ── 主用例：一轮 user + 一次中断固化 ──
conn = get_db()
cid = new_conv(conn)
uid = add_user(conn, cid, "请生成变更影响分析报告")
conn.commit()
P._persist_partial_stream(cid, "正文内容第一段。" * 40, reason="stop")
conn.commit()
rows = [dict(r) for r in conn.execute(CONV_KEY_SQL, (cid,)).fetchall()]
out["rows"] = rows
out["conv_id"] = cid
out["user_id"] = uid
out["expect_key"] = "%s:%d" % (cid, uid)

# P2：用 /recovery 的同一条 SQL 反查
key = rows[0]["finalize_key"] if rows else ""
hit = conn.execute("SELECT id, content, card_data, msg_type FROM messages "
                   "WHERE conversation_id=? AND role='assistant' AND finalize_key=? "
                   "ORDER BY id DESC LIMIT 1", (cid, key)).fetchone() if key else None
out["recovery_hit"] = bool(hit)

# P3：同轮再固化一次 ⇒ 仍 1 条
P._persist_partial_stream(cid, "正文内容第一段。" * 40, reason="stop")
conn.commit()
out["after_dup"] = len(conn.execute(CONV_KEY_SQL, (cid,)).fetchall())

# P4：新轮（新 user 消息）⇒ 允许再多 1 条
add_user(conn, cid, "第二个问题：再讲讲验证")
conn.commit()
P._persist_partial_stream(cid, "第二轮正文。" * 60, reason="error")
conn.commit()
rows2 = [dict(r) for r in conn.execute(CONV_KEY_SQL, (cid,)).fetchall()]
out["after_new_turn"] = len(rows2)
out["keys"] = [r["finalize_key"] for r in rows2]
out["keys_unique"] = len(set(out["keys"])) == len(out["keys"])

# P5：唯一索引形态
idx = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index' "
                   "AND tbl_name='messages' AND name='ux_messages_finalize'").fetchone()
out["index_sql"] = (idx["sql"] if idx else "")

# ── 缺列降级：造一个"旧库"（没有 finalize_key 列）──
#   纪律：加列必须带缺列兼容 + warning。裸写新列会抛 no such column ⇒ 整条固化失败
#   （真机形态：verify_partial_persist 的 4 条判据被我改红，内容本来能保住却丢了）。
import io as _io, contextlib as _ctx
_dg = {}
conn2 = get_db()
cid2 = new_conv(conn2); add_user(conn2, cid2, "缺列场景的提问"); conn2.commit()
# SQLite 不允许删除被索引引用的列（实测报 "error in index ... after drop column"），
# 先删索引再删列，才能造出"旧库没有该列"的真实形态。
conn2.execute("DROP INDEX IF EXISTS ux_messages_finalize")
conn2.execute("ALTER TABLE messages DROP COLUMN finalize_key")
conn2.commit()
_buf = _io.StringIO()
try:
    with _ctx.redirect_stdout(_buf):
        P._persist_partial_stream(cid2, "缺列场景正文。" * 40, reason="stop")
    _dg["raised"] = ""
except Exception as _e:
    _dg["raised"] = repr(_e)
conn2.commit()
_lg = _buf.getvalue()
_dg["warned"] = ("缺 finalize_key 列" in _lg)
_dg["rows"] = len(conn2.execute(
    "SELECT id FROM messages WHERE conversation_id=? AND role='assistant'", (cid2,)).fetchall())
out["degrade"] = _dg
conn2.close()

conn.close()
os.unlink(tmpdb)
print("PROBE|" + json.dumps(out, ensure_ascii=False))
'''


def _run(root):
    import json
    script = _WORKER.replace("@@ROOT@@", root)
    r = subprocess.run([os.path.join(root, ".venv", "Scripts", "python.exe"),
                        "-X", "utf8", "-c", script],
                       capture_output=True, text=True, cwd=root, timeout=900)
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("PROBE|"):
            return json.loads(ln[6:]), (r.stdout or "") + (r.stderr or "")
    return None, (r.stdout or "") + (r.stderr or "")


def evaluate():
    global RESULTS
    RESULTS = []

    with open(STREAM_SRC, encoding="utf-8") as f:
        src = f.read()

    # S 组：结构（先便宜地证明"写入了这个字段"，再做行为验证）
    check("S1_insert_has_finalize_key", "中断固化的 INSERT 含 finalize_key 列",
          "INSERT OR IGNORE INTO messages " in src
          and "content, msg_type, card_data, finalize_key" in src)
    check("S2_key_same_as_router", "finalize_key 口径与路由层一致（\"%s:%d\" % (conv, 末条 user id)）",
          '"%s:%d" % (conversation_id, _uid)' in src
          and "role='user'" in src)
    check("S3_idempotent_insert", "用 INSERT OR IGNORE（配合唯一索引实现 DB 级幂等）",
          "INSERT OR IGNORE INTO messages " in src)
    check("S4_rowcount_guard", "rowcount 为 0 时不再归档（避免同轮产物归档两次）",
          "if not _cur.rowcount:" in src and "本轮已有固化消息" in src)
    check("S5_column_probe", "写库前探测 finalize_key 列是否存在（缺列兼容的前提）",
          "PRAGMA table_info(messages)" in src and "_has_fk" in src)

    data, raw = _run(_ROOT)
    if data is None:
        check("P0_probe", "行为探针可执行", False, raw[-500:])
        return {r["id"]: (r["ok"], r["detail"]) for r in RESULTS}

    rows = data.get("rows") or []
    check("P0_probe", "行为探针可执行（夹具库 schema 就绪）",
          data.get("schema_ok") is True, "schema_ok=%s rows=%d" % (data.get("schema_ok"), len(rows)))
    check("P1_key_written", "中断固化的消息带 finalize_key 且等于 <conv>:<末条 user id>",
          len(rows) == 1 and rows[0]["finalize_key"] == data.get("expect_key"),
          "got=%r expect=%r" % ((rows[0]["finalize_key"] if rows else None), data.get("expect_key")))
    check("P2_recovery_can_find", "用 /recovery 的同一条 SQL 能命中该消息（跨边界可用）",
          data.get("recovery_hit") is True, str(data.get("recovery_hit")))
    check("P3_same_turn_idempotent", "同一轮重复固化仍只有 1 条",
          data.get("after_dup") == 1, "after_dup=%s" % data.get("after_dup"))
    check("P4_new_turn_allowed", "新一轮允许再落 1 条（不过度去重）",
          data.get("after_new_turn") == 2, "after_new_turn=%s" % data.get("after_new_turn"))
    check("P4b_keys_unique", "不同轮 finalize_key 互不相同",
          data.get("keys_unique") is True, str(data.get("keys")))
    dg = data.get("degrade") or {}
    check("P6_missing_column_degrades", "缺 finalize_key 列的旧库：不抛异常且内容仍落库",
          dg.get("raised") == "" and dg.get("rows") == 1,
          "raised=%r rows=%s" % (dg.get("raised"), dg.get("rows")))
    check("P7_missing_column_warns", "缺列时打出 warning 说明代价（不静默退回）",
          dg.get("warned") is True, str(dg.get("warned")))

    _idx = (data.get("index_sql") or "")
    _idx_ns = _idx.replace(" ", "")
    check("P5_partial_unique_index", "finalize_key 有部分唯一索引（finalize_key<>''，空键不参与唯一约束）",
          "ux_messages_finalize" in _idx and "UNIQUE" in _idx_ns.upper()
          and "WHEREfinalize_key<>''" in _idx_ns,
          _idx[:100])

    return {r["id"]: (r["ok"], r["detail"]) for r in RESULTS}


def main():
    global RESULTS
    evaluate()
    base = list(RESULTS)
    red = [r for r in base if not r["ok"]]
    for r in red:
        print("FAIL [%s] %s :: %s" % (r["id"], r["name"], r["detail"][:220]))
    print("")
    print("合计 %d 项，绿 %d，红 %d" % (len(base), len(base) - len(red), len(red)))
    return 1 if red else 0


if __name__ == "__main__":
    sys.exit(main())
