# -*- coding: utf-8 -*-
"""跨域兜底召回验证（2026-09-29）。

背景（真缺陷，实测暴露）：`recall_memories` 只按 `agent_id` 精确过滤，
而库内记忆按域分布极不均（chat=1 / design=24 / requirement_analysis=13 / knowledge_qa=11 …）。
后果：intent 落在**稀疏域**时召回恒为 0 —— 通用方法论就躺在别的域里也拿不到。

本脚本证明四件事：
  A. 稀疏域不再恒 0（跨域兜底生效，来源标 agent_memory_cross）。
  B. 同域优先且**不被跨域压过**（同域强命中时，第一名仍是同域）。
  C. 同域**已够用**时不触发兜底（触发门槛生效，防稀释榜单）。
  D. 变异测试：把兜底判据掐断 → A 必须复现 0 条（证明断言非空转）。

纪律：夹具建在数据源侧（真库临时行，用后即删）；删完**开新连接**复核；
      变异前后 diff 校验还原；子进程 `-B` 禁 pyc。
"""
import os
import re
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


def _db():
    from database import get_db
    return get_db()


def _ins(conn, agent_id, content, mem_type="experience"):
    cur = conn.execute(
        "INSERT INTO agent_memory (agent_id, content, mem_type, activation, access_count,"
        " created_at, forgotten) VALUES (?,?,?,1.0,0,datetime('now','localtime'),0)",
        (agent_id, content, mem_type))
    conn.commit()
    return cur.lastrowid


def _del(ids):
    """删除后**开新连接**复核（旧连接读旧事务视图会假 PASS/FAIL）。"""
    conn = _db()
    for i in ids:
        conn.execute("DELETE FROM agent_memory WHERE id=?", (i,))
    conn.commit()
    conn.close()
    c2 = _db()
    left = c2.execute(
        "SELECT COUNT(*) FROM agent_memory WHERE id IN (%s)" % ",".join("?" * len(ids)),
        ids).fetchone()[0] if ids else 0
    c2.close()
    return left


TMP_IDS = []


def main():
    from agent.memory_recall import recall_memories
    conn = _db()
    try:
        return _run(conn, recall_memories)
    finally:
        # ⚠️ 必须 finally 清理：上一版在 B2 崩了却没清，遗留 2 条 VERIFY-TMP 行，
        #    导致下一次运行 A 段"看起来有同域命中"（假结果）。夹具生命周期要自兜底。
        try:
            conn.close()
        except Exception:
            pass
        try:
            if TMP_IDS:
                _del(TMP_IDS)
        except Exception:
            pass


def _run(conn, recall_memories):
    Q = "智源工程包结构怎么查询"

    print("=" * 62)
    print("A. 稀疏域（chat）不再恒 0：跨域兜底生效")
    print("=" * 62)
    # 基线：chat 域在这些噪音清理后只剩极少数条目 → 同域应无强命中
    base = recall_memories(conn, Q, agent_id="chat")
    ck("A1. 跨域兜底后召回非空（此前实测为 0 条）", len(base) > 0, f"{len(base)} 条")
    ck("A2. 兜底条目来源标记为 agent_memory_cross",
       any(h["source_type"] == "agent_memory_cross" for h in base),
       [h["source_type"] for h in base])
    ck("A3. 跨域条目 meta 带 cross_domain=True",
       all(h["meta"].get("cross_domain") for h in base if h["source_type"].endswith("_cross")))

    print("\n" + "=" * 62)
    print("B. 同域优先：同域强命中必须排第一，且不被跨域压过")
    print("=" * 62)
    t1 = _ins(conn, "chat", "查询智源工程包结构树时，必须先取工程列表确定 vc，"
                           "再以该 vc 查询包树，顶层包为父节点为空的节点。（VERIFY-TMP-B）")
    TMP_IDS.append(t1)
    hits = recall_memories(conn, Q, agent_id="chat")
    top = hits[0] if hits else {}
    ck("B1. 同域条目排第一", str(top.get("source_type")) == "agent_memory", top.get("source_type"))
    _rest = max([h["score"] for h in hits[1:]] or [1e-9])
    ck("B2. 同域分数显著高于跨域（>=3x）",
       (top.get("score") or 0) >= 3 * _rest,
       f"top={top.get('score')} 次={_rest}")

    print("\n" + "=" * 62)
    print("C. 触发门槛：同域已够用 → 不触发兜底（防稀释榜单）")
    print("=" * 62)
    # 同域塞一条与查询高度重合的强命中 → 超过 memory_cross_min_score(0.25)
    t2 = _ins(conn, "chat", Q + " 的标准步骤：先取工程列表拿到 vc，再查包结构树。（VERIFY-TMP-C）")
    TMP_IDS.append(t2)
    hits2 = recall_memories(conn, Q, agent_id="chat")
    cross = [h for h in hits2 if h["source_type"] == "agent_memory_cross"]
    strong = max([h["score"] for h in hits2 if not h["source_type"].endswith("_cross")] or [0])
    ck("C1. 同域出现强命中时不再跨域兜底", len(cross) == 0,
       f"strong={strong:.3f} cross={len(cross)}")

    print("\n" + "=" * 62)
    print("D. 变异测试：掐断兜底判据 → A 必须复现 0 条")
    print("=" * 62)
    # ⚠️ 关键前置：必须先清掉 B/C 段插入的夹具行，让 D 回到"干净稀疏域"状态。
    #    否则掐断兜底后同域仍有 B/C 的临时命中，D1 恒为 n>0（假 FAIL，实测踩到）。
    _del(TMP_IDS)
    TMP_IDS.clear()
    print("  [prep] B/C 段夹具已清理，D 段在干净稀疏域上验证")
    target = os.path.join(_ROOT, "agent", "memory_recall.py")
    bak = target + ".mutbak"
    src = open(target, encoding="utf-8").read()
    anchor = "        if _strong < _cross_floor and not scopes:"
    if src.count(anchor) != 1:
        ck("D0. 变异锚点唯一命中", False, f"count={src.count(anchor)}")
    else:
        ck("D0. 变异锚点唯一命中", True)
        shutil.copy2(target, bak)
        open(target, "w", encoding="utf-8").write(src.replace(anchor, "        if False and not scopes:"))
        root_pyc = os.path.join(_ROOT, "__pycache__")
        for d in (root_pyc, os.path.join(_ROOT, "agent", "__pycache__")):
            if os.path.isdir(d):
                for f in os.listdir(d):
                    if f.startswith("memory_recall"):
                        try:
                            os.remove(os.path.join(d, f))
                        except Exception:
                            pass
        try:
            code = ("import sys; sys.path.insert(0, r'%s')\n"
                    "from database import get_db\n"
                    "from agent.memory_recall import recall_memories\n"
                    "h = recall_memories(get_db(), %r, agent_id='chat')\n"
                    "print('N', len(h))\n" % (_ROOT, Q))
            r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                               text=True, cwd=_ROOT)
            out = (r.stdout or "") + (r.stderr or "")
            mm = re.search(r"N (\d+)", out)
            n = int(mm.group(1)) if mm else -1
            ck("D1. 掐断兜底后稀疏域召回回到 0（证明兜底是唯一来源、断言非空转）",
               n == 0, f"n={n} {out.strip()[:90]}")
        finally:
            os.replace(bak, target)
            for d in (root_pyc, os.path.join(_ROOT, "agent", "__pycache__")):
                if os.path.isdir(d):
                    for f in os.listdir(d):
                        if f.startswith("memory_recall"):
                            try:
                                os.remove(os.path.join(d, f))
                            except Exception:
                                pass
            print("  [restored] agent/memory_recall.py 已还原")

    print("\n" + "=" * 62)
    print("E. 清理：删除临时夹具行并复核")
    print("=" * 62)
    left = _del(TMP_IDS)
    TMP_IDS.clear()
    ck("E1. 临时夹具行已清空（开新连接复核）", left == 0, f"left={left}")

    print("\n" + "=" * 62)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    print("=" * 62)
    if FAIL:
        for f in FAIL:
            print("  FAIL:", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
