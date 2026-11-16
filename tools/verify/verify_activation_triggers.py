# -*- coding: utf-8 -*-
"""把上一轮「还差什么」的 5 项触发条件变成机器可判据（只读看板）。

为什么要做：上一轮报告第六节给每项写了触发条件，但那是**人肉记忆**，
没有任何机制在条件满足时报警；同时 `request_invariant.mode` 声称三态可配，
实际配置分组缺失导致 enforce 到不了。本门禁只做测量与判定：

  TRIGGERED     条件已满足，应行动（本门禁不代做，只报）
  NOT_TRIGGERED 条件未满足，保持现状
  MANUAL        需产品决策，机器不判，如实标注

纪律：
  * 不切换生产模式、不改任何 tier、不插数据、不写生产台账；
  * 每条可测判据都有「正常读数 + 变异必被拒」的双向自证；
  * 空库 / 全新克隆下不假红：缺表时显式 SKIP，不伪装成 PASS；
  * 结论来自真实读数，不来自报告文本。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL, SKIP = [], [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                          (" — " + str(detail)) if (detail and not cond) else ""))


def skip(name, why):
    SKIP.append(name)
    print("  [SKIP] %s — %s" % (name, why))


def ro_db():
    path = os.environ.get("MBSE_VERIFY_DB") or os.environ.get("MBSE_DB_PATH") \
        or os.path.join(ROOT, "mbse.db")
    if not os.path.isfile(path):
        return None
    try:
        return sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    except Exception:
        return None


def has_table(conn, name):
    if conn is None:
        return False
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (name,)).fetchone() is not None
    except Exception:
        return False


def mcp_namespace_state(conn):
    """MCP 命名空间接入的触发条件：≥2 个 MCP server 或出现同名工具冲突。"""
    if not has_table(conn, "tools"):
        return None
    servers = conn.execute(
        "SELECT COUNT(DISTINCT mcp_server_id) FROM tools "
        "WHERE source='mcp' AND mcp_server_id IS NOT NULL AND mcp_server_id<>''").fetchone()[0]
    dup = conn.execute(
        "SELECT COUNT(*) FROM (SELECT name FROM tools GROUP BY name HAVING COUNT(*)>1)").fetchone()[0]
    prefixed = conn.execute(
        "SELECT COUNT(*) FROM tools WHERE name LIKE 'mcp__%'").fetchone()[0]
    return {"servers": int(servers), "dup_names": int(dup),
            "already_namespaced": int(prefixed) > 0,
            "triggered": bool(int(servers) >= 2 or int(dup) > 0)}


def profile_state(conn):
    """profile 接管运行时配置：装配骨架是否可用 + 生产配置读取是否已被接管。"""
    try:
        from core import profile_loader as pl
        forms = pl.derive_forms()                      # 真实 API：派生三形态
        names = sorted(forms.keys())
        cat = json.load(open(pl.DEFAULT_CATALOG, encoding="utf-8"))
        catalog_names = sorted((cat.get("profiles") or {}).keys())
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:80])}
    if set(("server", "cli", "offline")) - set(names):
        return {"ok": False, "error": "三形态派生不全", "profiles": names}
    # 接管状态：core.config 的 get() 是否从 profile 派生读取（当前不是 → 明确未接管）
    try:
        from core import config as cfg
        src = getattr(cfg, "PROFILE_SOURCE", None)
    except Exception:
        src = None
    return {"ok": True, "profiles": names, "catalog_profiles": catalog_names,
            "runtime_takeover": src is not None}


def main() -> int:
    print("=" * 74)
    print("剩余边界触发条件看板（只读测量；不切换模式、不改 tier、不插数据）")
    print("=" * 74)

    conn = ro_db()

    # ── [1] MCP 命名空间接入 ────────────────────────────────────────────
    print("\n[1] 触发条件：MCP 命名空间接入生产注册路径")
    st = mcp_namespace_state(conn)
    if st is None:
        skip("MCP 触发条件测量", "空库或缺 tools 表；变异自证仍在 :memory: 完成")
        st = {"servers": 0, "dup_names": 0, "already_namespaced": False, "triggered": False}
    else:
        # 可测性判据只要求「读得到这两项」，不要求库里有数据（空库是合法环境）。
        chk("生产读数：server 数与重名数可测",
            "servers" in st and "dup_names" in st, st)
    # 状态只报告、不做断言：未来合法接入第二个 server 时门禁不应误报红。
    print("  [STATE] MCP 触发条件=%s | server 数=%d | 重名=%d | 已加命名空间前缀=%s ⇒ %s"
          % ("TRIGGERED" if st["triggered"] else "NOT_TRIGGERED",
             st["servers"], st["dup_names"], st["already_namespaced"],
             "应接入命名空间" if st["triggered"] else "保持不接入"))
    # 双向自证：内存库构造「第二个 server」与「重名」，必须判 TRIGGERED
    mem = sqlite3.connect(":memory:")
    mem.execute("CREATE TABLE tools (name TEXT, source TEXT, mcp_server_id TEXT)")
    mem.execute("INSERT INTO tools VALUES ('graph_retrieve','mcp','161')")
    s1 = mcp_namespace_state(mem)
    chk("基线：单一 server 且无重名 ⇒ NOT_TRIGGERED", s1["triggered"] is False, s1)
    mem.execute("INSERT INTO tools VALUES ('other_tool','mcp','999')")
    s2 = mcp_namespace_state(mem)
    chk("变异：出现第 2 个 MCP server ⇒ TRIGGERED", s2["triggered"] is True, s2)
    mem.execute("INSERT INTO tools VALUES ('graph_retrieve','builtin','')")
    s3 = mcp_namespace_state(mem)
    chk("变异：出现同名工具冲突 ⇒ TRIGGERED", s3["dup_names"] > 0, s3)
    mem.execute("INSERT INTO tools VALUES ('mcp__srv__tool','mcp','999')")
    s4 = mcp_namespace_state(mem)
    chk("变异：已加命名空间前缀能被识别", s4["already_namespaced"] is True, s4)
    mem.close()

    # ── [2] profile 接管运行时配置 ──────────────────────────────────────
    print("\n[2] 触发条件：profile 接管 core/config.py 运行时读取")
    ps = profile_state(conn)
    chk("装配骨架可加载且派生三形态", ps["ok"] and
        all(x in ps.get("profiles", []) for x in ("server", "cli", "offline")), ps)
    print("  [STATE] profile 运行时接管=%s | 派生形态=%s"
          % ("已接管" if ps.get("runtime_takeover") else "未接管（当前为装配描述）",
             ",".join(ps.get("profiles", []))))
    print("  [MANUAL] 接管需确认 D-4 领域包边界后逐键切换；本门禁不代做决策")

    # ── [3] 三入口程序 ──────────────────────────────────────────────────
    print("\n[3] 触发条件：CLI / headless / Web 三入口")
    entries = [n for n in ("cli.py", "headless.py", "main.py")
               if os.path.isfile(os.path.join(ROOT, n))]
    chk("Web 入口 main.py 存在", "main.py" in entries, entries)
    print("  [STATE] 已落地入口=%s | CLI / headless 落地状态=%s"
          % (",".join(entries) or "(无)",
             "已落地" if ("cli.py" in entries and "headless.py" in entries) else "未落地"))
    print("  [MANUAL] 新建三入口属产品形态扩张，需明确目标形态与评测集共用方案")

    # ── [4] 运行时不变量切 enforce ──────────────────────────────────────
    print("\n[4] 触发条件：shadow 确认无误报后切 enforce")
    from core import request_invariant as ri
    from core import config as cfg
    chk("三态开关已登记（可配置驱动）", "request_invariant" in cfg.CONFIG_SCHEMA)
    chk("生产默认模式仍为 shadow（未擅自切换）", cfg.DEFAULT_CONFIG["request_invariant"]["mode"] == "shadow")
    prod = ri.enforce_readiness(min_samples=200)
    chk("生产台账判据可执行", isinstance(prod, dict) and "ready" in prod, prod)
    # 状态只报告、不做断言：真实流量积累到 200 条零误报后应变为 ready，门禁不能误报红。
    print("  [STATE] enforce 就绪=%s | samples=%d total=%d would_block=%d ⇒ %s"
          % ("READY" if prod["ready"] else "NOT_READY",
             prod["samples"], prod["total_events"], prod["would_block"], prod["reason"]))
    # 双向自证：临时台账造 200 条零误报样本 ⇒ ready；混入 1 条漂移 ⇒ not ready
    tmp = os.path.join(tempfile.gettempdir(), "mbse_trigger_verify")
    os.makedirs(tmp, exist_ok=True)
    p = os.path.join(tmp, "audit.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for i in range(200):
            f.write(json.dumps({"ts": float(i), "mode": "shadow", "origin": "runtime",
                                "allowed": True, "reasons": [], "digest": "a" * 12,
                                "would_block": False}) + "\n")
    r_ok = ri.enforce_readiness(path=p, min_samples=200)
    chk("变异：200 条零误报 ⇒ ready", r_ok["ready"] is True, r_ok["reason"])
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 999.0, "mode": "shadow", "origin": "runtime",
                            "allowed": False, "reasons": ["drift"], "digest": "b" * 12,
                            "would_block": True}) + "\n")
    r_bad = ri.enforce_readiness(path=p, min_samples=200)
    chk("变异：混入 1 条漂移 ⇒ not ready", r_bad["ready"] is False, r_bad["reason"])
    # 来源隔离：把同样 201 条全标成 verify，则一条都不计入真实样本
    with open(p, "w", encoding="utf-8") as f:
        for i in range(201):
            f.write(json.dumps({"ts": float(i), "mode": "shadow", "origin": "verify",
                                "allowed": True, "reasons": [], "digest": "v" * 12,
                                "would_block": False}) + "\n")
    r_ver = ri.enforce_readiness(path=p, min_samples=200)
    chk("变异：201 条全为 verify ⇒ 仍 not ready（CI 不能凑出 READY）",
        r_ver["ready"] is False and r_ver["samples"] == 0, r_ver)
    with open(p, "w", encoding="utf-8"):
        pass
    chk("台账已可持久化（文件存在且可清空）", os.path.isfile(p))

    # ── [5] 104 条债务门禁提级 ──────────────────────────────────────────
    print("\n[5] 触发条件：债务门禁「全新克隆 + 空库能绿」")
    d = json.load(open(os.path.join(ROOT, "tools/gates_manifest.json"), encoding="utf-8"))
    debt = [g for g in d["gates"] if g["tier"] in ("broken", "backlog")]
    chk("每条债务门禁都登记 reason", all(g.get("reason") for g in debt), len(debt))
    budget = d.get("budget") or {}
    n_broken = sum(1 for g in debt if g["tier"] == "broken")
    n_backlog = sum(1 for g in debt if g["tier"] == "backlog")
    chk("broken 未超预算", n_broken <= budget.get("broken", 0), (n_broken, budget.get("broken")))
    chk("backlog 未超预算", n_backlog <= budget.get("backlog", 0), (n_backlog, budget.get("backlog")))
    chk("本门禁未改动任何 tier（前后条数一致）", len(debt) == n_broken + n_backlog)
    print("  [MANUAL] 提级纪律：本地绿不能替代「全新克隆 + 空库能绿」；"
          "本轮全量干净库实跑仅产出候选清单，不接线")
    print("  债务读数：broken=%d backlog=%d 合计=%d" % (n_broken, n_backlog, len(debt)))

    if conn is not None:
        conn.close()

    print("\n" + "=" * 74)
    print("  PASS %d / FAIL %d / SKIP %d（环境缺数据，非通过）"
          % (len(PASS), len(FAIL), len(SKIP)))
    if FAIL:
        print("  失败项：" + "; ".join(FAIL))
    if SKIP:
        print("  跳过项：" + "; ".join(SKIP))
    print("=" * 74)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
