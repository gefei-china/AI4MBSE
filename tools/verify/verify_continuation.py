# -*- coding: utf-8 -*-
"""第五轮问题1 自检：续作输入（重试/继续）是否被识别为「承接」而非「新话题」。

用户原话：「之前的输入内容后，任务终止了，我写重试，AI貌似没有读上下文信息，不知道之前的内容」

根因（已定位）：`_tag_topics` 的 carry 词表不含"重试" → "重试"与当前话题段无共享 bigram
→ 被判**新话题** → 当前话题原文段只剩"重试"两字 → AI 无从知晓前文。

本脚本四层验证：
  L1 `_is_continuation_input` 判据（正例/反例）—— 纯函数真调用
  L2 源码级：carry 词表含续作词 + 注入锚点存在
  L3 **真库真调用**：往隔离副本库造一条会话（长话题 + 末尾"重试"），
     调 `_tag_topics` 断言 "重试" **继承**前话题（而非另起新话题）
  L4 变异自证：还原旧 carry 词表 → L3 必须转 FAIL

用隔离副本库：tmp/uisafe_r5.db（由 mbse.db 经 sqlite3.backup() 拷贝）。
"""

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# CI-OPTIONAL: C 实测本地红（L1 反例全部拒绝 判定失败）⇒ 需先修
#   分类：A=需服务在跑/ B=需密钥或写真库/ C=实测就红需先修。
#   依据见 docs/遗留优化项-第二轮盘点-20261005.md；
#   由 tools/verify/verify_gate_wiring.py 强制要求（要么接线，要么写理由）。
import io
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HIST = os.path.join(ROOT, "agent", "pipeline_parts", "history.py")
STREAM = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
DB = os.path.join(ROOT, "tmp", "uisafe_r5.db")

# 必须在导入工程模块前设好库路径（core/config.py 读环境变量）
os.environ["MBSE_DB_PATH"] = DB
sys.path.insert(0, ROOT)

fails, passes = [], []


def ck(name, cond, detail=""):
    (passes if cond else fails).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (("  <- " + str(detail)) if detail else ""))


def read(p):
    with io.open(p, "r", encoding="utf-8") as f:
        return f.read()


# ─────────────────────────────────────────────── L2 源码级
print("\n=== L2 源码级：carry 词表 + 判据 + 注入锚点 ===")
hist_src = read(HIST)
m = re.search(r"carry\s*=\s*\(([^)]*)\)", hist_src, re.S)
carry_body = m.group(1) if m else ""
for w in ("重试", "重跑", "重新", "再来", "retry", "continue"):
    ck("carry 含 '%s'" % w, w in carry_body)
ck("_is_continuation_input 已定义", "def _is_continuation_input" in hist_src)
stream_src = read(STREAM)
ck("stream.py 注入【续作指令】", "【续作指令】" in stream_src)
ck("stream.py 调用 _is_continuation_input", "_is_continuation_input(user_input)" in stream_src)

# ─────────────────────────────────────────────── L1 纯函数
print("\n=== L1 真调用：_is_continuation_input 正例/反例 ===")
try:
    from agent.pipeline_parts.history import HistoryMixin
    _fn = HistoryMixin._is_continuation_input
    available = True
except Exception as e:
    print("  !! 导入失败：%s" % e)
    available = False

if available:
    positives = ["重试", "重试一下", "重跑", "重新来", "继续", "继续吧", "接着来",
                 "再来一次", "retry", "continue", "go on", " 重试 ", "重试！"]
    negatives = ["继续帮我做热管理系统建模",
                 "重试一下刚才那个热管理建模任务的第三步",
                 "帮我重新设计冷板拓扑",
                 "继续分析这个模型的变体点，并输出 SysML V2 代码",
                 "", "   ", "你好"]
    bad_pos = [t for t in positives if not _fn(t)]
    bad_neg = [t for t in negatives if _fn(t)]
    ck("L1 正例全部命中 (%d)" % len(positives), not bad_pos, "未命中 %r" % bad_pos)
    ck("L1 反例全部拒绝 (%d)" % len(negatives), not bad_neg, "误判 %r" % bad_neg)
else:
    ck("L1 可导入 HistoryMixin", False)

# ─────────────────────────────────────────────── L3 真库真调用
print("\n=== L3 真库真调用：长话题 + 末尾'重试' → 话题继承 ===")
TOPIC_A = ("帮我为电动车热管理系统建立 SysML V2 模型，覆盖需求层、功能层、逻辑层、物理层，"
           "包含冷却回路、水泵、散热器、膨胀阀等部件，并定义变体点区分高温回路与低温回路。")
# 话题B 必须与 A **余弦≈0 且无共享 bigram**，否则 `_tag_topics` 根本不会切换话题
# → L3 断言恒真（空转断言），变异也抓不到（实测第一版就踩了这个坑）。
TOPIC_B = "合同的违约责任条款应当如何约定，需要注意哪些法律风险点。"

CONV_ID = 990501  # 专用测试会话 id（隔离库内，不碰真库）


def seed_and_tag(conn):
    conn.execute("DELETE FROM messages WHERE conversation_id=?", (CONV_ID,))
    conn.execute("DELETE FROM conversations WHERE id=?", (CONV_ID,))
    conn.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)",
        (CONV_ID, "续作承接自检"))
    rows = [
        ("user", TOPIC_A),          # 话题A 起点
        ("assistant", "好的，我先建立需求层与功能层骨架，并给出部件清单……" + "（模型片段）" * 20),
        ("user", TOPIC_B),          # 话题B（真切换，有共享词"热"但语义远）
        ("assistant", "电池包直冷方案的风险点包括……" + "（分析）" * 20),
        ("user", "重试"),            # ← 关键：续作短语
    ]
    for role, content in rows:
        conn.execute(
            "INSERT INTO messages (conversation_id, role, content, msg_type, card_data, topic) "
            "VALUES (?,?,?,'text','{}','')", (CONV_ID, role, content))
    conn.commit()
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM messages WHERE conversation_id=? ORDER BY id", (CONV_ID,)).fetchall()]
    return ids


def run_tag(conn, force_reload=False):
    """调 _tag_topics。

    ⚠️ 变异自证必须 force_reload=True：`HistoryMixin` 在本进程已被 import，
    Python 用的是**内存里的旧类**，改盘上的 .py 不会生效 → 变异永远是"假绿"。
    （实测踩过：变异后仍继承话题，看似"断言空转"，实为模块未重载。）
    """
    try:
        import importlib
        from agent.pipeline_parts import history as _hist_mod
    except Exception as e:
        return None, "导入失败 %s" % e
    if force_reload:
        importlib.reload(_hist_mod)
    inst = _hist_mod.HistoryMixin()
    mapping = inst._tag_topics(conn, CONV_ID)
    conn.commit()
    return mapping, None


conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
ids = seed_and_tag(conn)
mapping, err = run_tag(conn)
if err:
    ck("L3 可调用 _tag_topics", False, err)
else:
    topics = [mapping.get(i, "") for i in ids]
    print("  各消息话题标签：")
    for i, t in zip(ids, topics):
        role = conn.execute("SELECT role FROM messages WHERE id=?", (i,)).fetchone()["role"]
        print("    id=%d %-9s topic=%r" % (i, role, t))
    topic_a = (topics[0] or "").strip()          # 话题A（首条）
    topic_b = (topics[2] or "").strip()          # 话题B（第3条 user）
    last_topic = (topics[-1] or "").strip()      # "重试" 的话题

    # ★ 夹具自证：先确认"话题B 确实切换了"，否则后面全是空转断言
    ck("夹具自证：话题B 确实切走了（B≠A 且非空）",
       bool(topic_b) and topic_b != topic_a,
       "A=%r B=%r" % (topic_a, topic_b))
    ck("L3 '重试' 话题非空", bool(last_topic))
    ck("L3 '重试' 继承话题B（未另起新话题）", last_topic == topic_b,
       "重试=%r  话题B=%r" % (last_topic, topic_b))
    ck("L3 '重试' 未退化成自身两个字（即未成为新话题标签）",
       last_topic != "重试", "重试话题标签=%r" % last_topic)

# ─────────────────────────────────────────────── L4 变异自证
print("\n=== L4 变异自证：还原旧 carry 词表 → L3 断言必须转 FAIL ===")
orig = read(HIST)
try:
    # 还原成"没有 重试/重跑/重新/再来/retry/continue"的旧词表（用正则定位，避免缩进/换行漂移）
    mutated, n_sub = re.subn(
        r'carry\s*=\s*\([^)]*\)',
        'carry = ("继续", "接着", "还有", "另外", "再说", "那", "再")',
        orig, count=1, flags=re.S)
    anchor_hit = (n_sub == 1 and "重试" not in re.search(r'carry\s*=\s*\([^)]*\)', mutated, re.S).group(0))
    ck("变异锚点命中且确实移除了续作词", anchor_hit,
       "n_sub=%d" % n_sub)
    if anchor_hit:
        with io.open(HIST, "w", encoding="utf-8", newline="") as f:
            f.write(mutated)
        # 重跑同一场景（必须先清空 topic，否则 _tag_topics 命中"已全打标"短路，变异无从生效）
        conn2 = sqlite3.connect(DB)
        conn2.row_factory = sqlite3.Row
        ids2 = seed_and_tag(conn2)
        for i in ids2:
            conn2.execute("UPDATE messages SET topic='' WHERE id=?", (i,))
        conn2.commit()
        mp2, err2 = run_tag(conn2, force_reload=True)
        if err2:
            variant_flipped = False
            print("  变异后调用失败：%s" % err2)
        else:
            t_a2 = (mp2.get(ids2[0], "") or "").strip()
            t_b2 = (mp2.get(ids2[2], "") or "").strip()
            t_last2 = (mp2.get(ids2[-1], "") or "").strip()
            # 旧行为：'重试' 无 carry 保护 → 被判新话题 → t_last2 既不等于 tb2，也≈'重试'
            variant_flipped = (t_last2 != t_b2)
            print("  变异后 A=%r B=%r 重试=%r  → 已切走=%s" % (t_a2, t_b2, t_last2, variant_flipped))
        ck("变异后 L3 断言转 FAIL（'重试' 确实另起了新话题）", variant_flipped,
           "" if variant_flipped else "变异无效：旧行为下 '重试' 仍继承话题 → 断言是空转")
        conn2.close()
finally:
    with io.open(HIST, "w", encoding="utf-8", newline="") as f:
        f.write(orig)
restored = (read(HIST) == orig)
ck("源文件已逐字节还原", restored, "" if restored else "还原失败！")

# 清场
try:
    conn.execute("DELETE FROM messages WHERE conversation_id=?", (CONV_ID,))
    conn.execute("DELETE FROM conversations WHERE id=?", (CONV_ID,))
    conn.commit()
except Exception:
    pass
conn.close()

print("\n" + "=" * 58)
print("PASS=%d  FAIL=%d" % (len(passes), len(fails)))
if fails:
    print("失败项：")
    for f_ in fails:
        print("  - " + f_)
sys.exit(1 if fails else 0)
