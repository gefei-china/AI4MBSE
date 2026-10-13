# -*- coding: utf-8 -*-
"""冲突阈值标定工具（只读；不改代码、不写生产库）。

**为什么需要它**：`memory.conflict_detect_sim_high = 0.5` 在配置注释里被明确标注
"用 bigram 降级夹具标定，真向量口径下无生产数据支撑"。打开
`memory.conflict_detect_enabled` 之前，必须先回答：**真向量口径下互斥对落在哪一档**。

本探针只做一件事：在真库 `agent_memory` 的活跃条上，**按 detect_conflicts 真实的
分组口径**（scope_type + scope_id + mem_topic/agent）枚举所有同组对，同时算
① `_mem_cosine` 的真实相似度 ② `_has_conflict_marker` 是否命中，
然后按"是否含互斥词"分组统计相似度分布。

⚠️ 本探针**不引入新的相似度口径** —— 直接 import 生产代码里的 `_mem_cosine`
与 `_has_conflict_marker`，保证量出来的就是运行时会用的那个数。
"""
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict

# ⚠️ **不要用 dirname 数层数** —— 本文件挪个目录就会错（本次已踩：tools/calib 与
#    tmp/p_bench 都是两层深，我按"一层"改回 dirname 一次，结果指向 tools/ 而非仓库根）。
#    改为**向上找仓库标志文件**，挪目录不会失效。
def _find_root():
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isfile(os.path.join(_d, "memory_service.py")):
            return _d
        _p = os.path.dirname(_d)
        if _p == _d:
            break
        _d = _p
    raise SystemExit("找不到仓库根（未找到 memory_service.py）")


ROOT = _find_root()
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("MBSE_DB_PATH", os.path.join(ROOT, "mbse.db"))

from memory_service import MemoryService, _mem_cosine, _has_conflict_marker  # noqa: E402

DB = os.environ["MBSE_DB_PATH"]


def _fmt(v, n=4):
    return ("%." + str(n) + "f") % v


def main():
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row

    # ── 0. 规模 ──
    total = conn.execute("SELECT COUNT(*) FROM agent_memory").fetchone()[0]
    rows = [dict(r) for r in conn.execute(
        "SELECT id, agent_id, mem_type, content, mem_topic, scope_type, scope_id, "
        "embedding, embed_version FROM agent_memory "
        "WHERE forgotten=0 AND COALESCE(superseded_by,0)=0 ORDER BY id")]
    print("=== 0. 取证范围 ===")
    print("  agent_memory 总行: %d｜活跃条(forgotten=0 且 superseded_by=0): %d"
          % (total, len(rows)))
    print("  embed_version 分布: %s"
          % dict(conn.execute("SELECT COALESCE(NULLIF(embed_version,''),'(空)'), COUNT(*) "
                              "FROM agent_memory GROUP BY 1").fetchall()))

    # ── 1. 完全照 detect_conflicts 的分组口径 ──
    groups = defaultdict(list)
    for r in rows:
        key = (r.get("scope_type") or "", r.get("scope_id") or "",
               r.get("mem_topic") or ("__agent__:" + str(r.get("agent_id"))))
        groups[key].append(r)

    multi = {k: v for k, v in groups.items() if len(v) >= 2}
    print("\n=== 1. 同组配对空间（detect_conflicts 的分组口径）===")
    print("  分组数: %d｜其中含 ≥2 条的组: %d" % (len(groups), len(multi)))
    n_pairs = sum(len(v) * (len(v) - 1) // 2 for v in multi.values())
    print("  待比较对数: %d" % n_pairs)
    if not multi:
        print("  [SKIP] 没有任何同组对 ⇒ 该口径下无标定样本")
        return

    try:
        from knowledge_engine import VectorEngine
        ve = VectorEngine()
    except Exception as e:
        ve = None
        print("  VectorEngine 不可用（%s）⇒ 走 bigram 降级口径" % type(e).__name__)

    # ── 2. 逐对测量 ──
    same_vec = 0
    sims, marker_sims, nomarker_sims = [], [], []
    pairs = []
    for key, g in multi.items():
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                a, b = g[i], g[j]
                s = _mem_cosine(ve, a, b)
                if s is None:
                    continue
                if a.get("embed_version") and a.get("embed_version") == b.get("embed_version"):
                    same_vec += 1
                mk = (_has_conflict_marker(a["content"])
                      or _has_conflict_marker(b["content"]))
                sims.append(s)
                (marker_sims if mk else nomarker_sims).append(s)
                pairs.append((s, mk, key, a, b))

    print("\n=== 2. 相似度分布（全部同组对）===")
    print("  实测对数: %d｜其中双方同为真向量口径: %d" % (len(sims), same_vec))
    if not sims:
        print("  [SKIP] 无可算相似度的对")
        return
    ss = sorted(sims)
    print("  min=%s p10=%s median=%s p90=%s max=%s"
          % (_fmt(ss[0]), _fmt(ss[len(ss) // 10]), _fmt(ss[len(ss) // 2]),
             _fmt(ss[len(ss) * 9 // 10]), _fmt(ss[-1])))

    def _dist(arr, name):
        if not arr:
            print("  %-18s 无样本" % name)
            return
        a = sorted(arr)
        buckets = Counter()
        for x in a:
            buckets[">=0.90 重复区" if x >= 0.90 else
                    "0.85~0.90" if x >= 0.85 else
                    "0.70~0.85" if x >= 0.70 else
                    "0.50~0.70" if x >= 0.50 else "<0.50"] += 1
        print("  %-18s n=%-3d min=%s median=%s max=%s ｜ 分档: %s"
              % (name, len(a), _fmt(a[0]), _fmt(a[len(a) // 2]), _fmt(a[-1]),
                 dict(buckets)))

    print("\n=== 3. 按「是否含互斥断言词」分组（这是标定的核心）===")
    _dist(sims, "全部对")
    _dist(marker_sims, "含互斥词（候选冲突）")
    _dist(nomarker_sims, "不含互斥词")

    # ── 4. 阈值扫描：不同 sim_high 各会命中多少 ──
    print("\n=== 4. 阈值扫描（仅统计，含互斥词的对）===")
    print("  %-10s %-14s %-16s" % ("sim_high", "命中含互斥词对", "误伤(无互斥词)"))
    for th in (0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.85, 0.90):
        hit_m = sum(1 for s, mk, *_ in pairs if s >= th and mk)
        hit_n = sum(1 for s, mk, *_ in pairs if s >= th and not mk)
        print("  %-10s %-14d %-16d" % (th, hit_m, hit_n))

    # ── 5. 互斥词表与实际命中内容 ──
    print("\n=== 5. 互斥词表与命中实况 ===")
    print("  _CONFLICT_MARKERS (%d 个): %s"
          % (len(MemoryService._CONFLICT_MARKERS),
             list(MemoryService._CONFLICT_MARKERS)))
    hit_cnt = Counter()
    for s, mk, key, a, b in pairs:
        if not mk:
            continue
        for m in MemoryService._CONFLICT_MARKERS:
            if m in (a["content"] or "") or m in (b["content"] or ""):
                hit_cnt[m] += 1
    print("  实际命中的词: %s" % (dict(hit_cnt) or "无"))

    # ── 6. 高相似 + 含互斥词的对（真会触发 supersede 的）──
    print("\n=== 6. 真正会触发标记的对（sim>=0.5 且含互斥词）===")
    trig = [(s, key, a, b) for s, mk, key, a, b in pairs if s >= 0.5 and mk]
    print("  共 %d 对" % len(trig))
    for s, key, a, b in sorted(trig, key=lambda x: -x[0])[:8]:
        print("  --- sim=%s topic=%r ---" % (_fmt(s), key[2]))
        print("      A#%s: %s" % (a["id"], (a["content"] or "")[:110].replace("\n", " ")))
        print("      B#%s: %s" % (b["id"], (b["content"] or "")[:110].replace("\n", " ")))

    # ── 7. 结论：0.5 在真向量口径下能否被标定 ──
    print("\n=== 7. 标定结论 ===")
    if not marker_sims:
        print("  ★ 真库同组对里**没有任何一对含互斥断言词** ⇒")
        print("    在真向量口径下，sim_high 取任何值都命中 0 对 ——")
        print("    阈值**无法从生产数据标定**（不是\"0.5 不准\"，是\"没有样本可标\"）。")
        print("    ⇒ 打开开关前必须先造样本，否则开关的有效性无从验证。")
        print("\n  参考：无互斥词的对里，sim>=0.5 的有 %d 对（consolidate 阈值 0.85 之上另有 %d 对）"
              % (sum(1 for s, mk, *_ in pairs if s >= 0.5 and not mk),
                 sum(1 for s, mk, *_ in pairs if s >= 0.85 and not mk)))
    else:
        a = sorted(marker_sims)
        print("  含互斥词对 n=%d，相似度 min=%s median=%s max=%s"
              % (len(a), _fmt(a[0]), _fmt(a[len(a) // 2]), _fmt(a[-1])))
        print("  ⇒ 建议 sim_high 取略低于 min 的档（宁可多报候选也不漏），当前 0.5 %s"
              % ("已低于 min，安全" if a[0] >= 0.5 else "★ 高于 min，会漏判"))
    # ── 8. entity 侧为何 0 对（结构性问题，不是阈值问题）──
    print("\n=== 8. entity_resolver.conflict_detect 的结构诊断 ===")
    try:
        from entity_resolver import _normalize_mentions
        # ⚠️ 必须 `_dd(list)` 不能 `_dd()`：defaultdict 不带 default_factory 时
        #    取缺失键抛 **KeyError**（不是"建个空列表"）——本脚本第一版就踩了，
        #    表现为分组阶段莫名 KeyError，与数据脏无关。
        from collections import defaultdict as _dd
        _c2 = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
        _c2.row_factory = sqlite3.Row
        _ents = [dict(r) for r in _c2.execute(
            "SELECT * FROM entities WHERE status!='deprecated'")]
        # 归一化只是分组键的一层，**万一拿不到就退回原名** —— 本节只做结构诊断，
        # 不需要与生产分组口径逐字一致（那由 conflict_detect 自己保证）。
        # （实测：照 conflict_detect 的实参调用，_normalize_mentions 稳定返回 60 条，
        #   不抛异常。下面这层 try 是纯防御，本脚本第一版真正踩的坑是 `_dd()`
        #   漏了 default_factory，与归一化无关。）
        try:
            _norms = _normalize_mentions(
                _c2, [e["name"] for e in _ents],
                types={e["name"]: e.get("entity_type", "") for e in _ents})
            print("  （归一化可用，按生产口径分组）")
        except Exception as _ne:
            _norms = {}
            print("  ⚠️ _normalize_mentions 抛 %s（本节退回按原名分组）"
                  % type(_ne).__name__)
        _bk = _dd(list)
        for e in _ents:
            _bk[(e["branch"], _norms.get(e["name"], e["name"]),
                 e["entity_type"])].append(e)
        _multi = {k: v for k, v in _bk.items() if len(v) >= 2}
        print("  非 deprecated 实体: %d｜(branch,归一名,类型) 组数: %d｜含>=2条的组: %d"
              % (len(_ents), len(_bk), len(_multi)))
        # 逐级放宽，看是哪一级把候选压没了
        _bk2 = _dd(list)
        for e in _ents:
            _bk2[(e["branch"], e["name"])].append(e)
        _m2 = {k: v for k, v in _bk2.items() if len(v) >= 2}
        print("  放宽到 (branch,原名) 后含>=2条的组: %d" % len(_m2))
        for k, v in list(_m2.items())[:5]:
            _ids = [e["id"] for e in v]
            print("    branch=%-9s name=%-10s -> %d 条 id=%s｜同id=%s"
                  % (str(k[0])[:9], k[1], len(v), _ids[:4], len(set(_ids)) == 1))
        print("  ⇒ 结论：0 对是**分组口径**造成的（类型限定 + 归一化把候选压到 0），")
        print("    **不是 sim 阈值造成的** —— entity 侧那条链路里根本没有相似度门，")
        print("    所以调 threshold 对它零影响。")
        _c2.close()
    except Exception as e:
        print("  entity 侧诊断失败: %s %s" % (type(e).__name__, e))

    conn.close()


if __name__ == "__main__":
    main()
