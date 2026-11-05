"""多方案选型闭环（FR-MG-6 收口）自检：决策记录 + 卡片回显 + 校验与接线。

## 为什么要有这条

对比卡此前只到「推荐 / 并列 / 数据不足」，**人的选型决定没有落点**——
选了哪个、谁选的、当时得分矩阵，事后查不到。本条锁住：
  ① 端点校验（不许选矩阵里没有的方案）；
  ② 决策落 `alternative_selections`（append-only，改选可查）；
  ③ 卡片回写 `alternatives.selected`（历史回放可见）；
  ④ 前端两条渲染路径都消费 `selected`。

## 口径

直连调用路由函数（绕过 FastAPI 依赖注入，conn 用真库、user=None ⇒ decided_by=未登录）；
临时 conversation_id=990401，跑完清理（messages / alternative_selections）。
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from database import get_db                       # noqa: E402
from routers.alternatives import SelectRequest, select_alternative, list_selections  # noqa: E402

PASS = 0
FAIL = 0
FAILED = []
CONV = 990401


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % label)
    else:
        FAIL += 1
        FAILED.append(label)
        print("  ✗ %s   %s" % (label, detail))


def read_src(rel):
    return io.open(os.path.join(REPO, rel), encoding="utf-8").read() if (io := __import__("io")) else ""


def cleanup(conn):
    conn.execute("DELETE FROM messages WHERE conversation_id=?", (CONV,))
    conn.execute("DELETE FROM alternative_selections WHERE conversation_id=?", (CONV,))
    conn.execute("DELETE FROM conversations WHERE id=?", (CONV,))
    conn.commit()


def make_conv(conn):
    """临时会话（messages 有 FK ⇒ 必须先建会话）。只填无默认值的必填列。"""
    conn.execute(
        "INSERT INTO conversations (id, title, intent, status, phase, created_at, updated_at, "
        "project_id, current_intent, last_slots, pending_clarify, pinned_at, main_agent, llm_overrides) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (CONV, "选型闭环自检", "chat", "active", "chat",
         "2026-01-01 00:00:00", "2026-01-01 00:00:00",
         "0", "chat", "{}", "", "", "", "{}"))
    conn.commit()


def card_json():
    """一张三候选对比卡（含推荐与并列标记）。"""
    return json.dumps({
        "alternatives": {
            "recommended": "方案A", "tie": False,
            "columns": [
                {"name": "方案A", "total": 0.72, "rank": 1, "recommended": True, "coverage": 1.0},
                {"name": "方案B", "total": 0.65, "rank": 2, "recommended": False, "coverage": 1.0},
                {"name": "方案C", "total": 0.40, "rank": 3, "recommended": False,
                 "coverage": 0.6, "eligible": False, "missing_dimensions": ["cost", "risk"]},
            ],
            "rows": [{"key": "performance", "label": "性能"}],
        }
    }, ensure_ascii=False)


conn = get_db()
cleanup(conn)
make_conv(conn)

print("=" * 74)
print("[1] 正常选型：定位最新对比卡 → 审计行 + 卡片回写")
print("=" * 74)

conn.execute(
    "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) VALUES (?,?,?,?,?)",
    (CONV, "assistant", "对比结论", "text", card_json()))
conn.commit()

r1 = select_alternative(SelectRequest(conversation_id=CONV, selected_name="方案B",
                                      reason="成本可接受且扩展性好"), conn=conn, user=None)
check("1.1 选型成功（ok=True，回传 message_id 与 selection）",
      isinstance(r1, dict) and r1.get("ok") and r1.get("selection", {}).get("name") == "方案B", r1)

_row = conn.execute("SELECT card_data FROM messages WHERE id=?", (r1["message_id"],)).fetchone()
_cd = json.loads(_row["card_data"])
_sel = (_cd.get("alternatives") or {}).get("selected") or {}
check("1.2 卡片回写 selected（历史回放能渲染'已选定'）",
      _sel.get("name") == "方案B" and _sel.get("by"), _sel)
check("1.3 selected 带理由与决策人",
      "成本可接受" in str(_sel.get("reason") or "") and bool(_sel.get("by")), _sel)

_row2 = conn.execute(
    "SELECT selected_name, alternatives_snapshot FROM alternative_selections "
    "WHERE conversation_id=? ORDER BY id DESC LIMIT 1", (CONV,)).fetchone()
_snap = json.loads(_row2["alternatives_snapshot"] or "{}")
check("1.4 审计行落 `alternative_selections`（含快照）",
      _row2 and _row2["selected_name"] == "方案B"
      and _snap.get("recommended") == "方案A" and len(_snap.get("columns") or []) == 3,
      dict(_row2) if _row2 else None)

print()
print("=" * 74)
print("[2] 校验：不许选矩阵里没有的方案")
print("=" * 74)

r2 = select_alternative(SelectRequest(conversation_id=CONV, selected_name="方案Z"),
                        conn=conn, user=None)
# ⚠️ JSONResponse.body 是 UTF-8 字节，不能拿 str 直接 `in` —— 先解码再断言
_err2 = json.loads(r2.body).get("error", "") if hasattr(r2, "body") else ""
check("2.1 未入选候选名 ⇒ 400 且报出可选集",
      getattr(r2, "status_code", 200) == 400 and "方案A" in _err2 and "方案C" in _err2,
      _err2[:80])

print()
print("=" * 74)
print("[3] 自动定位 + 改选（append-only）")
print("=" * 74)

r3 = select_alternative(SelectRequest(conversation_id=CONV, selected_name="方案A",
                                      reason="评分最高"), conn=conn, user=None)
check("3.1 message_id=0 ⇒ 自动定位到那张卡", r3.get("ok") and r3.get("message_id") == r1["message_id"], r3)

_n = conn.execute("SELECT COUNT(*) FROM alternative_selections WHERE conversation_id=?",
                  (CONV,)).fetchone()[0]
check("3.2 改选 = **追加**新审计行（历史可查'改选过'，不覆盖）", _n == 2, _n)

_ls = list_selections(conversation_id=CONV, conn=conn)
check("3.3 GET selections 倒序返回（最新在前）",
      _ls.get("items") and _ls["items"][0]["selected_name"] == "方案A",
      [i.get("selected_name") for i in _ls.get("items", [])])

_row3 = conn.execute("SELECT card_data FROM messages WHERE id=?", (r1["message_id"],)).fetchone()
_sel3 = (json.loads(_row3["card_data"]).get("alternatives") or {}).get("selected") or {}
check("3.4 卡片回显的是**最新**决策（方案A）", _sel3.get("name") == "方案A", _sel3)

print()
print("=" * 74)
print("[4] 前端接线：两条渲染路径都消费 selected，控件存在")
print("=" * 74)

_js = read_src("static/js/mods/58-alternatives.js")
check("4.1 历史回放路径合并 card_data.alternatives.selected（否则永远看不到'已选过'）",
      "cd.alternatives.selected" in _js)
check("4.2 已选定态渲染（✅ + 决策人）", "已选定" in _js and "sel.by" in _js)
check("4.3 未选定态有选型控件与提交函数", "alt-sel-name" in _js and "altSubmitSelect" in _js)
check("4.4 提交走选型端点", "/api/alternatives/select" in _js)
check("4.5 会话 id 在点击时读取（不是渲染时固化）",
      "typeof currentConvId !== 'undefined'" in _js)

cleanup(conn)
conn.close()

print()
print("=" * 74)
print("汇总：PASS = %d / FAIL = %d" % (PASS, FAIL))
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  - %s" % f)
print("口径：直连路由函数（绕过依赖注入），真库 + 临时会话 990401，跑完清理。")
sys.exit(0 if FAIL == 0 else 1)
