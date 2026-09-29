# -*- coding: utf-8 -*-
"""会话摘要接口回归：GET /api/conversations/{id}/summary

覆盖：正常会话（source/摘要质量）、空会话（source=empty）、不存在（404）、
     缓存命中（同一消息数两次结果一致）、字段完整性（updated_at/intent/msg_count）。
以及质量断言：不得出现 `结论：。` 类标记误命中、不得带客套开场、无 markdown 残留。

用法：python tools/verify/verify_conv_summary_api.py
"""
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.environ.get("MBSE_BASE", "http://127.0.0.1:8000")
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(ROOT, "mbse.db")

# ⚠️ 本机设了 http_proxy（实测 127.0.0.1:59906），urllib 默认会走代理 →
# 访问本机服务得到 502 "upstream connect failed"，看起来像服务挂了，实为代理拦截。
# 必须显式禁用代理（空 ProxyHandler）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

OK, FAIL = [], []


def chk(name, cond, detail=""):
    (OK if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("   " + str(detail)) if detail and not cond else ""))


def get(path):
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body}


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    ids = [r["conversation_id"] for r in conn.execute(
        "SELECT conversation_id, COUNT(*) c FROM messages GROUP BY conversation_id "
        "HAVING c>=2 ORDER BY c DESC LIMIT 6")]
    empty_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM conversations WHERE id NOT IN "
        "(SELECT DISTINCT conversation_id FROM messages) LIMIT 3")]
    conn.close()

    print("== 1) 正常会话 ==")
    for cid in ids:
        st, d = get("/api/conversations/%d/summary" % cid)
        if st != 200:
            chk("conv %s 返回 200" % cid, False, "status=%s body=%s" % (st, d))
            continue
        need = {"summary", "source", "msg_count", "updated_at", "intent"}
        chk("conv %s 字段完整(%s)" % (cid, ",".join(sorted(need))),
            need.issubset(set(d)), "missing=%s" % (need - set(d)))
        chk("conv %s source 合法" % cid, d.get("source") in ("llm", "structured", "empty"), d.get("source"))
        chk("conv %s msg_count 为正" % cid, (d.get("msg_count") or 0) > 0, d.get("msg_count"))
        s = d.get("summary") or ""
        chk("conv %s 摘要非空" % cid, bool(s.strip()), repr(s[:40]))
        chk("conv %s 无 `结论：。` 误命中" % cid,
            not re.search(r"结论[：:]\s*[。！？；）)】\]”’…]", s), repr(s[:70]))
        chk("conv %s 无 markdown 残留" % cid,
            not re.search(r"```|!?\[[^\]]*\]\(|\*\*", s), repr(s[:70]))
        chk("conv %s 未带客套开场" % cid,
            not s.startswith(("直接回答", "好的，", "明白了，", "很高兴")), repr(s[:40]))
        print("        → [%s] %s" % (d.get("source"), s[:88]))

    print("== 2) 空会话（无消息）==")
    for cid in empty_ids:
        st, d = get("/api/conversations/%d/summary" % cid)
        chk("空会话 %s 返回 200" % cid, st == 200, st)
        if st == 200:
            chk("空会话 %s source=empty 且摘要为空" % cid,
                d.get("source") == "empty" and not (d.get("summary") or "").strip(),
                "%s / %r" % (d.get("source"), d.get("summary")))

    print("== 3) 不存在的会话 ==")
    st, d = get("/api/conversations/999999999/summary")
    chk("不存在会话返回 404", st == 404, "status=%s body=%s" % (st, d))
    chk("404 带 error 字段", isinstance(d, dict) and bool(d.get("error")), d)

    print("== 4) 缓存命中（同 msg_count 两次一致）==")
    if ids:
        cid = ids[0]
        _, d1 = get("/api/conversations/%d/summary" % cid)
        _, d2 = get("/api/conversations/%d/summary" % cid)
        chk("两次调用结果一致（缓存生效）",
            d1.get("summary") == d2.get("summary") and d1.get("source") == d2.get("source"),
            "%r vs %r" % (d1.get("summary", "")[:40], d2.get("summary", "")[:40]))

    print("\n== 汇总: %d PASS / %d FAIL ==" % (len(OK), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
