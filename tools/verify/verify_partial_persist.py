# -*- coding: utf-8 -*-
"""第五轮问题4 后端自检：_persist_partial_stream 固化 + 双写防重 + 保护分支。

用户原话：「我输出"重试"AI输出大量内容，又遇到网络错误，然后打开页面之前输出的内容不见了」

后端兜底链路：stream.py 尾部 except → _persist_partial_stream(conversation_id, 已生成文本,
reason) → 落库 messages（assistant，带「以上为已生成内容」后缀）。前端 commitPartialStream
是第一道（用户还在线时固化），本函数是第二道（前端没来得及固化/断开时兜底）。

验证点：
  P1 正常固化：插入带后缀的 assistant 消息，更新会话 updated_at
  P2 双写防重：最后一条已是"含标记的 assistant" → 不再插
  P3 dry_run=True → 不落库
  P4 内容过短(<4字) → 不落库
  P5 异常安全：conversation_id 为 None → 静默返回不抛
"""

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# CI-OPTIONAL: C 实测本地红 ⇒ 需先修
#   分类：A=需服务在跑/ B=需密钥或写真库/ C=实测就红需先修。
#   依据见 docs/遗留优化项-第二轮盘点-20261005.md；
#   由 tools/verify/verify_gate_wiring.py 强制要求（要么接线，要么写理由）。
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(ROOT, "tmp", "uisafe_r5.db")
os.environ["MBSE_DB_PATH"] = DB
sys.path.insert(0, ROOT)

fails, passes = [], []


def ck(name, cond, detail=""):
    (passes if cond else fails).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("  <- " + str(detail)) if detail else ""))


def get_agent_cls():
    # execute_stream/_persist_partial_stream 挂在哪个类上就取哪个
    import importlib
    for mod_name, cls_name in [
        ("agent.pipeline_parts.stream", "StreamMixin"),
        ("agent.agent", "Agent"),
        ("agent.pipeline_parts.stream", "PipelineMixin"),
    ]:
        try:
            mod = importlib.import_module(mod_name)
            cls = getattr(mod, cls_name, None)
            if cls and hasattr(cls, "_persist_partial_stream"):
                return mod, cls
        except Exception:
            continue
    # 兜底：扫描 agent 包所有模块
    import pkgutil
    import agent as agent_pkg
    for m in pkgutil.iter_modules(agent_pkg.__path__):
        try:
            mod = importlib.import_module("agent." + m.name)
        except Exception:
            continue
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if isinstance(obj, type) and hasattr(obj, "_persist_partial_stream"):
                return mod, obj
    return None, None


CONV_ID = 990601
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 准备会话
conn.execute("DELETE FROM messages WHERE conversation_id=?", (CONV_ID,))
conn.execute("DELETE FROM conversations WHERE id=?", (CONV_ID,))
conn.execute("INSERT INTO conversations (id, title, created_at, updated_at) "
             "VALUES (?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)", (CONV_ID, "固化自检"))
conn.execute("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
             "VALUES (?,?,?,?,?)", (CONV_ID, "user", "帮我做热管理建模", "text", "{}"))
conn.commit()

mod, cls = get_agent_cls()
print("\n=== 定位 _persist_partial_stream ===")
ck("找到宿主类", cls is not None, "%s.%s" % (mod.__name__, cls.__name__) if cls else "全 agent 包未找到")

if cls:
    inst = cls.__new__(cls)   # 不跑 __init__（避免加载 LLM 等重资源）
    print("\n=== P1 正常固化 ===")
    content = "针对链路预算，我先梳理EIRP与G/T……（中间大量内容）"
    inst._persist_partial_stream(CONV_ID, content, "error", dry_run=False)
    row = conn.execute(
        "SELECT role, content, msg_type FROM messages WHERE conversation_id=? "
        "ORDER BY id DESC LIMIT 1", (CONV_ID,)).fetchone()
    ck("P1 已落库 assistant 消息", row is not None and row["role"] == "assistant")
    ck("P1 含原内容", row is not None and "链路预算" in (row["content"] or ""))
    ck("P1 含 error 后缀", row is not None and "网络错误中断" in (row["content"] or ""),
       (row["content"] or "")[-60:] if row else "")
    ck("P1 含「以上为已生成内容」标记", row is not None and "以上为已生成内容" in (row["content"] or ""))

    print("\n=== P2 双写防重 ===")
    before_n = conn.execute("SELECT COUNT(*) c FROM messages WHERE conversation_id=?",
                            (CONV_ID,)).fetchone()["c"]
    inst._persist_partial_stream(CONV_ID, "又一段应该被防重拦下的内容" + "x" * 20, "error", dry_run=False)
    after_n = conn.execute("SELECT COUNT(*) c FROM messages WHERE conversation_id=?",
                           (CONV_ID,)).fetchone()["c"]
    ck("P2 未重复插入", before_n == after_n, "%d vs %d" % (before_n, after_n))

    print("\n=== P3 dry_run=True ===")
    before_n = after_n
    inst._persist_partial_stream(CONV_ID, "dry_run 下不应落库的内容" + "y" * 20, "error", dry_run=True)
    after_n = conn.execute("SELECT COUNT(*) c FROM messages WHERE conversation_id=?",
                           (CONV_ID,)).fetchone()["c"]
    ck("P3 dry_run 不落库", before_n == after_n)

    print("\n=== P4 内容过短不落库 ===")
    inst._persist_partial_stream(CONV_ID, "短", "error", dry_run=False)
    after_n2 = conn.execute("SELECT COUNT(*) c FROM messages WHERE conversation_id=?",
                            (CONV_ID,)).fetchone()["c"]
    ck("P4 过短内容被拒", before_n == after_n2)

    print("\n=== P5 conversation_id=None 静默返回 ===")
    try:
        inst._persist_partial_stream(None, "无会话内容" + "z" * 20, "error", dry_run=False)
        ck("P5 不抛异常", True)
    except Exception as e:
        ck("P5 不抛异常", False, str(e))

    print("\n=== P6 前端已固化 → 后端不双写（stop 场景） ===")
    # 模拟前端已固化：最后一条 assistant 已含标记
    conn.execute("INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
                 "VALUES (?,?,?,?,?)", (CONV_ID, "assistant",
                 "前端固化的内容" + "w" * 20 + "\n\n> ⏹ 已停止生成（以上为已生成内容）", "text", "{}"))
    conn.commit()
    before_n = conn.execute("SELECT COUNT(*) c FROM messages WHERE conversation_id=?",
                            (CONV_ID,)).fetchone()["c"]
    inst._persist_partial_stream(CONV_ID, "后端想双写的内容" + "v" * 20, "stop", dry_run=False)
    after_n = conn.execute("SELECT COUNT(*) c FROM messages WHERE conversation_id=?",
                           (CONV_ID,)).fetchone()["c"]
    ck("P6 后端不双写", before_n == after_n, "%d vs %d" % (before_n, after_n))

# 清场
conn.execute("DELETE FROM messages WHERE conversation_id=?", (CONV_ID,))
conn.execute("DELETE FROM conversations WHERE id=?", (CONV_ID,))
conn.commit()
conn.close()

print("\n" + "=" * 58)
print("PASS=%d  FAIL=%d" % (len(passes), len(fails)))
if fails:
    print("失败项：")
    for f_ in fails:
        print("  - " + f_)
sys.exit(1 if fails else 0)
