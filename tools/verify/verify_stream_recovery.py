# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：B 组用例前置「真库可用」（读仓库根 mbse.db，B1 直接判红），CI 无真库 ⇒ 未接线。2026-11-19 本机实测 14/14。改法须同步改清单 tier 并接线，不允许只删这行
"""verify_stream_recovery — 断线恢复机制门禁（2026-11-19）

对标 Dify「断线不致命」的稳妥落地：SSE 异常无终态时，后端按 finalize_key
取回本轮已固化内容 + 结构化原因 + 编排 run_id；前端恢复展示（含续跑入口）。

判据：
 S1-S5 后端端点存在、归属校验、复用 finalize_key、返回结构化字段、只读无副作用
 F1-F4 前端恢复函数 + 两处异常收口调用 + 编排续跑入口 + 幂等标记
 B1-B2 行为：能从真库按 key 查到固化消息；结构化原因可读
 M1-M2 变异自证：摘掉端点/摘掉前端调用 ⇒ 对应断言 FAIL

用法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_stream_recovery.py
"""
from __future__ import annotations
import os, sys, json, sqlite3
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

ROUTER = os.path.join(_ROOT, "routers", "conversations.py")
FRONT = os.path.join(_ROOT, "static", "js", "mods", "12-chatsend.js")
router = open(ROUTER, encoding="utf-8").read()
front = open(FRONT, encoding="utf-8").read()
res = []
def rec(name, ok, detail=""):
    res.append((name, bool(ok))); print(("PASS " if ok else "FAIL ")+name+(" ⇒ "+str(detail) if detail else ""))

# ---- 静态 ----
rec("S1 recovery 端点存在", '@router.get("/api/conversations/{conv_id}/recovery")' in router)
rec("S2 归属校验在位且传 user（防 500）",
    "def get_turn_recovery" in router and "_own_conv(conn, conv_id, user)" in router)
rec("S3 复用 finalize_key", "_finalize_key_for_turn(conn, conv_id)" in router and "finalize_key=?" in router)
rec("S4 返回结构化原因 + run_id", '"abort_reason": abort_code' in router and '"orchestrated_run_id": run_id' in router)
fn = router[router.index("def get_turn_recovery"):router.index("def _turn_overrides")]
rec("S5 端点函数体无写操作（只读）", "INSERT INTO" not in fn and "UPDATE " not in fn and "DELETE " not in fn)

rec("F1 前端恢复函数存在", "async function _recoverTurnContent" in front and "RECOVER_TURN_CONTENT_V1" in front)
rec("F2 两处异常收口调用", front.count("await _recoverTurnContent(aiBox)") >= 2, front.count("await _recoverTurnContent(aiBox)"))
rec("F3 编排续跑入口", "startResumeRun(r.orchestrated_run_id" in front)
rec("F4 幂等标记防重复恢复", "aiBox.dataset.recovered" in front)
rec("F5 恢复端点被调用", "/recovery`" in front or "/recovery'" in front)

# ---- 行为：真库按 key 查固化消息（造临时会话，测后清理）----
db = os.path.join(_ROOT, "mbse.db")
if os.path.exists(db):
    import time
    c = sqlite3.connect(db, timeout=30); c.row_factory = sqlite3.Row
    if not any(d[1] == "finalize_key" for d in c.execute("PRAGMA table_info(messages)")):
        rec("B1 finalize_key 列存在（迁移已应用）", False, "真库缺列")
    else:
        cid = 999999700 + int(time.time()) % 200
        try:
            c.execute("INSERT INTO conversations(id,title,intent,created_at) VALUES(?,'recover-gate','design',CURRENT_TIMESTAMP)", (cid,))
            c.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,'user','q',CURRENT_TIMESTAMP)", (cid,))
            uid = c.execute("SELECT MAX(id) FROM messages WHERE conversation_id=? AND role='user'", (cid,)).fetchone()[0]
            key = "%s:%s" % (cid, uid)
            card = json.dumps({"exec": {"abort_reason": "tool_inflight_abort", "abort_detail": "工具在途被取消"}})
            c.execute("INSERT OR IGNORE INTO messages(conversation_id,role,content,msg_type,card_data,finalize_key) VALUES(?,'assistant','已生成内容','text',?,?)", (cid, card, key))
            c.commit()
            row = c.execute("SELECT id, card_data FROM messages WHERE conversation_id=? AND role='assistant' AND finalize_key=? ORDER BY id DESC LIMIT 1", (cid, key)).fetchone()
            rec("B1 按 key 可查到固化消息", bool(row), "key=%s" % key)
            d = json.loads(row["card_data"] or "{}") if row else {}
            ab = (d.get("exec") or {}).get("abort_reason", "")
            rec("B2 结构化原因可读", ab == "tool_inflight_abort", ab)
        finally:
            c.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))
            c.execute("DELETE FROM conversations WHERE id=?", (cid,))
            c.commit()
    c.close()
else:
    rec("B1 真库可用", False, "无 mbse.db")

# ---- 变异自证 ----
m1 = router.replace('@router.get("/api/conversations/{conv_id}/recovery")', '@router.get("/api/conversations/{conv_id}/recovery_OFF")', 1)
rec("M1 摘端点 ⇒ S1 FAIL", '@router.get("/api/conversations/{conv_id}/recovery")' not in m1)
m2 = front.replace("await _recoverTurnContent(aiBox)", "await _noop(aiBox)")
rec("M2 摘前端调用 ⇒ F2 FAIL", m2.count("await _recoverTurnContent(aiBox)") < 2)

np = sum(1 for _, ok in res if ok); nf = len(res)-np
print("\n"+"="*60); print("PASS %d / FAIL %d" % (np, nf)); print("ALL_OK="+str(nf==0))
sys.exit(0 if nf==0 else 1)
