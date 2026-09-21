# -*- coding: utf-8 -*-
"""评测前置自检（preflight）—— 在跑任何检索评测之前必须先过这一关。

为什么需要它（2026-09-21 实测踩到）：
  本机存在**两个 python**：项目 `.venv`（装了 httpx，能真调 embedding/LLM）
  与 WorkBuddy 自带的 managed python（**没有 httpx**）。
  用后者跑检索时，链路会**静默降级**：embedding 回落 bigram、LLM 重排回落 Mock，
  但 `retrieve()` 照样返回 200 风格的结果 —— **不报错、只是测的是另一条链路**。
  实测：managed python 下 5 条 query 全部 conf=0.0 且路由退化。
  若不先发现这点，后面所有"评测分数"都是降级路径的分数，对外毫无意义。

本脚本做四件事：
  A. 环境自检：解释器 / httpx / 实际生效的 embedding provider 与 LLM provider
  B. **降级探测**：真跑一次 retrieve()，在输出里抓降级标记（"降级 bigram" / "回落 Mock"）
  C. **语料快照指纹**：documents/chunks/entities/relations 计数 + branch/version + embedding 模型与维度
     —— 评测集对语料敏感（同一 query 在不同时点的 route 可能不同），必须把指纹写进分数旁边
  D. **确定性/噪声底线**：同一 query 重复 N 轮，报 route 是否稳定、latency 波动
     —— 配置改动带来的差异若小于噪声带宽，就不能算改善

产出：tmp/p21/eval_preflight.json（含 ok 判定）
用法：
    "<repo>/.venv/Scripts/python.exe" -X utf8 tools/eval/preflight.py
    QUERIES="a,b" ROUNDS=3 "<repo>/.venv/Scripts/python.exe" -X utf8 tools/eval/preflight.py
"""
import io
import json
import os
import re
import sqlite3
import statistics
import sys
import time
from contextlib import redirect_stdout, redirect_stderr

REPO = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
DB = os.path.join(REPO, "mbse.db")
OUT = os.path.join(REPO, "tmp", "p21", "eval_preflight.json")
sys.path.insert(0, REPO)
os.chdir(REPO)

DEFAULT_QUERIES = [
    "测控分系统与地面控制站的关系",          # 期望图谱域
    "卫星通信系统包含哪些分系统",            # 期望图谱域
    "OMG 规范里 requirement 的写法",         # 期望文档域
    "属性定义与量的区别",                    # 期望文档域
    "SysML_V2 端口定义语法",                 # 期望文档域
]
QUERIES = [q for q in (os.environ.get("QUERIES") or "").split(",") if q.strip()] or DEFAULT_QUERIES
ROUNDS = int(os.environ.get("ROUNDS", "3"))

# 降级标记：出现在 retrieve() 的输出里即视为链路已降级
DEGRADE_MARKERS = [
    ("embedding→bigram", r"降级\s*bigram"),
    ("LLM→Mock", r"回落\s*Mock"),
    ("embedding 失败", r"embedding\s*API\s*调用失败"),
]

L = []


def w(s=""):
    L.append(s)
    print(s)
    sys.stdout.flush()


def head(t):
    w()
    w("=" * 78)
    w(t)
    w("=" * 78)


# ── A. 环境自检 ──────────────────────────────────────────────────────────────
head("A. 环境自检")
w("解释器 : %s" % sys.executable)
w("版本   : %s" % sys.version.split()[0])
in_venv = os.path.normcase(os.path.join(REPO, ".venv")) in os.path.normcase(sys.executable)
w("在项目 venv 内 : %s%s" % (in_venv, "" if in_venv else "   ⚠️ 极可能缺少 httpx → 链路会降级"))

deps = {}
for mod in ("httpx", "numpy", "fastapi"):
    try:
        __import__(mod)
        deps[mod] = True
    except Exception as e:
        deps[mod] = "%s: %s" % (type(e).__name__, e)
for k, v in deps.items():
    w("依赖 %-8s : %s" % (k, v))

# ── C. 语料快照指纹（读真库，只读）────────────────────────────────────────────
head("C. 语料快照指纹")
conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
conn.row_factory = sqlite3.Row


def one(sql, default=None):
    try:
        r = conn.execute(sql).fetchone()
        return r[0] if r else default
    except Exception as e:
        return "ERR:%s" % e


finger = {
    "db_file": os.path.basename(DB),
    "db_bytes": os.path.getsize(DB),
    "documents": one("select count(*) from documents"),
    "chunks": one("select count(*) from document_chunks"),
    "entities": one("select count(*) from entities"),
    "relations": one("select count(*) from relations"),
    "ontology_active": one("select version_label from ontology_versions "
                           "where active=1 order by id desc limit 1"),
    "doc_branches": [dict(r) for r in conn.execute(
        "select branch, count(*) n from documents group by branch")],
    "chunk_branches": [dict(r) for r in conn.execute(
        "select branch, count(*) n from document_chunks group by branch")],
    "chunk_domains": [dict(r) for r in conn.execute(
        "select domain, count(*) n from document_chunks group by domain")],
    "embed_version": one("select embed_version from document_chunks "
                         "where embed_version is not null limit 1"),
}
for k in ("documents", "chunks", "entities", "relations", "ontology_active",
          "embed_version", "db_bytes"):
    w("%-16s : %s" % (k, finger[k]))
w("doc_branches    : %s" % finger["doc_branches"])
w("chunk_domains   : %s" % finger["chunk_domains"])

# embedding 维度（抽样 1 条，不反序列化全部）
try:
    row = conn.execute("select embedding from document_chunks "
                       "where embedding is not null limit 1").fetchone()
    emb = row[0] if row else None
    if isinstance(emb, (bytes, bytearray)):
        finger["embed_dim_bytes"] = len(emb)
        w("embedding 存储  : BLOB %d bytes（%s）" % (len(emb),
                              "float32×%d" % (len(emb) // 4) if len(emb) % 4 == 0 else "未知"))
    elif isinstance(emb, str):
        import json as _j
        v = _j.loads(emb)
        finger["embed_dim"] = len(v)
        w("embedding 存储  : JSON 文本 dim=%d" % len(v))
except Exception as e:
    w("embedding 读取  : ERR %s" % e)

# ── B + D. 降级探测 + 确定性/噪声底线 ─────────────────────────────────────────
head("B+D. 降级探测 + 确定性（%d 条 query × %d 轮）" % (len(QUERIES), ROUNDS))
from agent.rag import GraphRAG                                    # noqa: E402
from core import config as _cfg_mod                               # noqa: E402

for k in ("recall_k", "top_k", "inject_k", "rerank_enabled", "rerank_max_candidates",
          "route_threshold", "hyde_enabled"):
    w("生效 rag.%-22s = %s" % (k, _cfg_mod.get("rag", k)))

rag = GraphRAG()
rows, degrade_seen = [], set()
for q in QUERIES:
    for i in range(ROUNDS):
        buf = io.StringIO()
        t0 = time.time()
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                r = rag.retrieve(q)
            err = ""
        except Exception as e:
            r, err = {}, "%s: %s" % (type(e).__name__, e)
        ms = int((time.time() - t0) * 1000)
        log = buf.getvalue()
        for name, pat in DEGRADE_MARKERS:
            if re.search(pat, log):
                degrade_seen.add(name)
        rows.append({
            "q": q, "round": i, "err": err,
            "route": r.get("route"), "reason": r.get("route_reason"),
            "conf": round(float(r.get("confidence") or 0), 4),
            "g": r.get("graph_count"), "v": r.get("vector_count"),
            "chunk_hits": len(r.get("chunk_hits") or []),
            "ms": ms,
        })

for q in QUERIES:
    sub = [x for x in rows if x["q"] == q]
    routes = sorted({x["route"] for x in sub})
    confs = sorted({x["conf"] for x in sub})
    lats = [x["ms"] for x in sub]
    span = (max(lats) - min(lats)) / max(statistics.median(lats), 1) * 100
    w("%-30s route=%-22s conf=%-16s lat=%d..%dms(波动 %.0f%%)"
      % (q[:30], ",".join(str(x) for x in routes), ",".join(str(x) for x in confs),
         min(lats), max(lats), span))

stable = sum(1 for q in QUERIES if len({x["route"] for x in rows if x["q"] == q}) == 1)
lat_all = [x["ms"] for x in rows]
noise = {
    "route_stable": "%d/%d" % (stable, len(QUERIES)),
    "latency_p50_ms": int(statistics.median(lat_all)),
    "latency_min_ms": min(lat_all), "latency_max_ms": max(lat_all),
    "latency_spread_pct": round((max(lat_all) - min(lat_all)) / max(statistics.median(lat_all), 1) * 100, 1),
    "chunks_returned": sorted({x["chunk_hits"] for x in rows}),
    "errors": [x["err"] for x in rows if x["err"]][:3],
}

head("结论")
httpx_ok = deps.get("httpx") is True
degraded = bool(degrade_seen) or not httpx_ok
w("httpx 可用 : %s" % httpx_ok)
w("降级标记   : %s" % (sorted(degrade_seen) or "无"))
w("route 稳定 : %s" % noise["route_stable"])
w("延迟噪声   : p50=%dms  波动 %.1f%%（调参差异小于此带宽即不可判）"
  % (noise["latency_p50_ms"], noise["latency_spread_pct"]))
w("返回分块数 : %s" % noise["chunks_returned"])
if degraded:
    w()
    w("⛔ 本次链路已降级 → **不可用于评测**。请用项目 venv 重跑：")
    w('   "%s\\.venv\\Scripts\\python.exe" -X utf8 tools/eval/preflight.py' % REPO)
else:
    w()
    w("✅ 链路完好（真 embedding + 真 LLM），可用于评测。")

res = {"ok": not degraded, "degraded_markers": sorted(degrade_seen),
       "deps": deps, "fingerprint": finger, "noise_floor": noise,
       "rounds": ROUNDS, "queries": QUERIES, "rows": rows,
       "when": time.strftime("%Y-%m-%d %H:%M:%S")}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)
w("→ %s" % OUT)
with io.open(os.path.splitext(OUT)[0] + ".txt", "w", encoding="utf-8") as f:
    f.write("\n".join(L))
sys.exit(0 if not degraded else 1)
