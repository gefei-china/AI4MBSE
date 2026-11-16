# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
"""真实 HTTP 入口的改选回流端到端验证（2026-11-07 米爸第 15 轮）。

不直接调 record_override，而是走 `POST /api/conversations/{id}/chat/stream`
并带 `intent_override` —— 与前端 pickIntent() 完全同路径。
判据：样本池出现 source='override' / status='suggested' 的记录，
且 note 含真实系统原判（不是空字符串）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
BASE = "http://127.0.0.1:8000"
DB = os.path.join(ROOT, "mbse.db")
TEXT = "请帮我评估一下这个无人机地面站架构的变更影响范围"
# ⚠️ 改选值必须与系统原判**不同**：这句话原判是 impact，若也传 impact，
#    `record_override` 会按设计拦掉（「改选==原判」不是纠错信号，见 D5 判据）。
#    首版 E2E 用了 impact ⇒ 假红，是验证脚本自己的用例错，不是产品缺陷。
OVERRIDE = "review"


def post(path, payload, timeout=180):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def sse(path, payload, timeout=220):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json"})
    evs = 0
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                evs += 1
                if '"type":"done"' in line or '"type": "done"' in line:
                    break
    return evs


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("DELETE FROM intent_samples WHERE text=?", (TEXT,))
    conn.commit()
    try:
        cid = post("/api/conversations", {"title": "改选回流验证"})["id"]
        print("cid", cid, flush=True)
        t0 = time.time()
        n = sse("/api/conversations/%d/chat/stream" % cid,
                {"message": TEXT, "intent_override": OVERRIDE})
        print("SSE 事件 %d 个，耗时 %.1fs（改选值=%s，与原判 impact 不同）"
              % (n, time.time() - t0, OVERRIDE), flush=True)
        time.sleep(1)
        rows = conn.execute(
            "SELECT id,text,status,source,note,hit_intent,hit_route FROM intent_samples "
            "WHERE text=?", (TEXT,)).fetchall()
        print("样本池命中 %d 行" % len(rows), flush=True)
        ok = False
        for r in rows:
            print("  ", dict(r), flush=True)
            note = json.loads(r["note"] or "{}")
            if (r["status"] == "suggested" and r["source"] == "override"
                    and note.get("override") == OVERRIDE
                    and note.get("detected")):   # 必须是真实系统原判，不能空
                ok = True
        print("ALL_OK=%s  （真实 HTTP 改选回流写 suggested）" % ok, flush=True)
        return 0 if ok else 1
    finally:
        conn.execute("DELETE FROM intent_samples WHERE text=?", (TEXT,))
        conn.commit()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
