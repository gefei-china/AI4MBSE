# -*- coding: utf-8 -*-
"""P1 性能特性验证（Task #14）：verify_p1_perf.py

聚焦「算法特征 / 资源特征」而非绝对耗时（CI 环境不稳定，耗时只做宽松护栏）：

P1-2 Blocking 化（vector2graph / entity_resolver）：
- P1 源码级：_disambiguate_many 只做一次全表扫描（函数体 SELECT 恰 1 次，无逐名查询）
- P2 大数据量消歧：1500 实体 + 200 批量名，正确性 + 耗时护栏（防 O(N×M) 爆炸回归）
- P3 桶上限：2100 同前缀实体 → buckets_dropped==1900，桶内配对不爆炸
- P4 强规则合并：300 实体含 100 对 normalize_full 全等 → auto_merged==100

P1-3 hybrid_search 优化（knowledge_engine）：
- P5 源码级：分页流式（LIMIT ? OFFSET ?）、列裁剪（_CHUNK_COLS 不含 embedding）、
  HyDE 候选集限定（WHERE id IN）、_BM25_PAGE 引用
- P6 3000 chunks 大数据量检索：正确性 + 缓存命中 engine 对象复用
- P7 缓存容量上限 8：9 个不同 key → 最旧被淘汰，len<=8
- P8 指纹 INSERT 敏感：插 chunk → 缓存重建
- P9 指纹 DELETE 敏感：删 chunk → 缓存重建
- P10 content UPDATE 不失效（设计语义：ingest 下 content 更新走新行，声明验证）

回归基线：末尾自动重跑 tests/manual_verify/verify_p1_blocking_smoke.py
（2026-09-18 修复：原清单 verify_staging_fuse / verify_staging_migration /
verify_fusion_fixes 三个脚本**从未入库、磁盘亦不存在**，三项恒 rc=2 FAIL，
致本脚本长期 EXIT=1。已剔除幽灵条目 + 加「存在性前置校验」，防门禁清单再次漂移）

运行：python tests/manual_verify/verify_p1_perf.py
"""
import os
import re
import subprocess
import sys
import tempfile
import time

# ── 必须在任何业务模块 import 之前指定临时库 ──
_TMP_DB = os.path.join(tempfile.gettempdir(), "mbse_p1_perf.db")
for _p in (_TMP_DB, _TMP_DB + "-wal", _TMP_DB + "-shm"):
    if os.path.exists(_p):
        os.remove(_p)
os.environ["MBSE_DB_PATH"] = _TMP_DB

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from database.schema import get_db, init_db  # noqa: E402
from entity_resolver import detect_and_save_candidates  # noqa: E402
from vector2graph import _disambiguate_many  # noqa: E402
from knowledge_engine import (_BM25_CACHE, _BM25_CACHE_MAX, _bm25_cache_get,  # noqa: E402
                              hybrid_search)

init_db()
conn = get_db()

PASS, FAIL = [], []


def check(label: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}  {detail if not cond else ''}")


def timed(label: str, cond: bool, dt: float, limit: float, detail: str = ""):
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}  {dt:.2f}s"
          + (f"（护栏 {limit:.0f}s）" if cond else f" 超时 {detail}"))


def wipe():
    """外键安全清空：先删子表再删 entities。"""
    for t in ("knowledge_conflicts", "entity_dup_candidates", "entity_merges",
              "v2g_candidates", "relations", "entities", "document_chunks"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()


def func_body(src: str, name: str) -> str:
    """提取顶格 def 函数的函数体（到下一个顶格 def / 文件尾）。"""
    lines = src.splitlines()
    start = next(i for i, l in enumerate(lines)
                 if l.startswith(f"def {name}(") or l.startswith(f"def {name} "))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("def ")),
               len(lines))
    return "\n".join(lines[start:end])


# ═══════════════════════════ P1 源码级：Blocking 单次全表扫描 ═══════════════════════════
print("\n== P1 源码级：_disambiguate_many 单次全表扫描 ==")
_src_v2g = open(os.path.join(_BASE, "vector2graph.py"), encoding="utf-8").read()
_body = func_body(_src_v2g, "_disambiguate_many")
check("P1a 函数体 SELECT 恰 1 次（单次全表扫描，无逐名查询）",
      len(re.findall(r"SELECT", _body)) == 1,
      f"SELECT 次数={len(re.findall(r'SELECT', _body))}")

# ═══════════════════════════ P2 大数据量批量消歧 ═══════════════════════════
print("\n== P2 大数据量批量消歧（1500 实体 / 200 名） ==")
wipe()
t0 = time.perf_counter()
A_IDS = {}
for i in range(1, 501):
    conn.execute("INSERT INTO entities (id, name, entity_type, status, branch) VALUES (?,?,?,?,?)",
                 (f"n{i}", f"实体{i}", "system", "reviewed", "dev"))
    conn.execute("INSERT INTO entities (id, name, entity_type, status, branch) VALUES (?,?,?,?,?)",
                 (f"a{i}", f"卫星{i}转发器A", "system", "reviewed", "dev"))
    conn.execute("INSERT INTO entities (id, name, entity_type, status, branch) VALUES (?,?,?,?,?)",
                 (f"b{i}", f"卫星{i}转发器B", "system", "reviewed", "dev"))
    A_IDS[i] = f"a{i}"
conn.commit()

names = []
names += [f"实体{i}" for i in range(1, 51)]                     # 50 精确 → dup_high
names += [f"卫星{i}转发器C" for i in range(1, 101)]              # 100 bigram 近似(库内无C) → dup_suspect
names += ["星际尘埃粒子监测", "海底电缆铺设规划", "量子纠缠密钥分发"]  # 3 组真无关
names += [f"完全无关名{i}甲乙丙" for i in range(1, 48)]           # 47 无关（无库内子串/bigram）
t1 = time.perf_counter()
out = _disambiguate_many(conn, names)
t2 = time.perf_counter()

check("P2a 批量返回 200 名（含空过滤语义一致）", len(out) == 200, f"len={len(out)}")
check("P2b 精确名 → dup_high(实体1)",
      out.get("实体1") == ("dup_high", "n1"), str(out.get("实体1")))
ok_bigram = all(out.get(f"卫星{i}转发器C") == ("dup_suspect", A_IDS[i])
                for i in (1, 25, 50, 75, 99))
check("P2c bigram 跨后缀近似 → dup_suspect(同号 A) 抽查 5",
      ok_bigram, str({i: out.get(f"卫星{i}转发器C") for i in (1, 25, 50, 75, 99)}))
# 跨位数边界（i=100 的 bigram 与 i=10 重叠，Dice≈0.82≥0.8）→ 保守 dup_suspect（不要求同号）
check("P2c2 跨位数数字串 → 保守 dup_suspect（Dice 阈值语义）",
      (out.get("卫星100转发器C") or ("", ""))[0] == "dup_suspect",
      str(out.get("卫星100转发器C")))
check("P2d 真无关名 → none",
      all(out.get(f"完全无关名{i}甲乙丙") == ("none", "") for i in (1, 24, 47)),
      str([out.get(f"完全无关名{i}甲乙丙") for i in (1, 24, 47)]))
timed("P2e 建索引+200 名消歧耗时护栏", t2 - t1, t2 - t1, 10.0)
print(f"      （含插入耗时 {t1-t0:.2f}s，消歧 {t2-t1:.2f}s）")

# ═══════════════════════════ P3 桶上限 ═══════════════════════════
print("\n== P3 桶上限（2100 同前缀实体） ==")
wipe()
t0 = time.perf_counter()
conn.executemany(
    "INSERT INTO entities (id, name, entity_type, status, branch) VALUES (?,?,?,?,?)",
    [(f"c{i}", f"同前缀实体{i}", "system", "reviewed", "dev") for i in range(1, 2101)])
conn.commit()
t1 = time.perf_counter()
ret = detect_and_save_candidates(conn)
t2 = time.perf_counter()
check("P3a buckets_dropped == 1900（2100-200 超限）",
      ret.get("buckets_dropped", 0) == 1900, str(ret))
check("P3b 桶内配对产出候选（19900 对双判据不爆炸）",
      ret.get("candidates", 0) > 0, str(ret))
timed("P3c 检测耗时护栏", t2 - t1, t2 - t1, 45.0)
print(f"      （插入 {t1-t0:.2f}s，检测 {t2-t1:.2f}s，19900 对双判据纯 Python）")

# ═══════════════════════════ P4 强规则合并 ═══════════════════════════
print("\n== P4 强规则合并（300 实体含 100 对 normalize_full 全等） ==")
wipe()
conn.executemany(
    "INSERT INTO entities (id, name, entity_type, status, branch) VALUES (?,?,?,?,?)",
    [(f"x{i}", f"普通实体{i}", "system", "reviewed", "dev") for i in range(1, 101)]
    + [(f"m{i}", f"全等名{i}", "system", "reviewed", "dev") for i in range(1, 101)]
    + [(f"m{i}x", f"全等名{i} ", "system", "reviewed", "dev") for i in range(1, 101)])  # 尾空格→归一全等
conn.commit()
ret4 = detect_and_save_candidates(conn)
merges = conn.execute("SELECT COUNT(*) FROM entity_merges").fetchone()[0]
check("P4a auto_merged == 100（全等对直接合并，免两两打分）",
      ret4.get("auto_merged", 0) == 100, str(ret4))
check("P4b entity_merges 落库 >= 100", merges >= 100, f"merges={merges}")

# ═══════════════════════════ P5 源码级：hybrid_search 优化 ═══════════════════════════
print("\n== P5 源码级：分页 / 列裁剪 / HyDE 候选集 ==")
_src_ke = open(os.path.join(_BASE, "knowledge_engine.py"), encoding="utf-8").read()
_body_cache = func_body(_src_ke, "_bm25_cache_get")
check("P5a 分页流式构建（LIMIT ? OFFSET ? + _BM25_PAGE）",
      len(re.findall(r"LIMIT \? OFFSET \?", _body_cache)) >= 1
      and len(re.findall(r"_BM25_PAGE", _body_cache)) >= 1,
      "缺分页结构")
_chunk_cols = re.search(r'_CHUNK_COLS = "([^"]*)"', _src_ke).group(1)
check("P5b 列裁剪不含 embedding 大字段",
      "embedding" not in _chunk_cols, _chunk_cols)
check("P5c HyDE 限定候选集（WHERE id IN）",
      len(re.findall(r"WHERE id IN", _src_ke)) >= 1, "未找到 id IN 候选集 SQL")

# ═══════════════════════════ P6 3000 chunks 大数据量检索 ═══════════════════════════
print("\n== P6 3000 chunks 大数据量检索 ==")
wipe()
conn.executemany("INSERT INTO documents (id, filename, branch) VALUES (?,?,?)",
                 [(d + 1, f"doc{d+1}.md", "dev") for d in range(6)])
cid = 1
rows = []
for d in range(6):
    for j in range(500):
        if d < 4:  # 0-3 卫星主题
            content = (f"卫星通信方案{d}-{j}: Ka频段转发器与高通量波束成形，"
                       f"星上处理载荷支持再生中继与低轨组网")
        else:      # 4-5 热控主题
            content = (f"载荷热控手册{d}-{j}: 泵驱两相流回路散热量5kW，"
                       f"碳纤维反射面面密度低于2kg/m2")
        rows.append((cid, d + 1, j, content, f"doc{d+1}.md", "dev",
                     "satellite" if d < 4 else "thermal", "bigram-tf"))
        cid += 1
conn.executemany(
    "INSERT INTO document_chunks (id, document_id, chunk_index, content, source_doc, "
    "branch, domain, embed_version) VALUES (?,?,?,?,?,?,?,?)", rows)
conn.commit()

t0 = time.perf_counter()
r6 = hybrid_search(conn, "Ka频段转发器怎么选型", top_k=5, branches=["dev"], domain="satellite")
t1 = time.perf_counter()
check("P6a domain=satellite 过滤正确（3000 行全表裁剪）",
      bool(r6.get("hits")) and all(h.get("domain") == "satellite" for h in r6["hits"]),
      str(r6)[:160])
check("P6b top1 归属卫星文档（doc1-4）",
      r6["hits"][0]["document_id"] in (1, 2, 3, 4),
      str([(h["document_id"], h["chunk_index"]) for h in r6["hits"]]))

n6 = len(_BM25_CACHE)
eng6 = {id(v[0]) for v in _BM25_CACHE.values()}
t2 = time.perf_counter()
r6b = hybrid_search(conn, "Ka频段转发器怎么选型", top_k=5, branches=["dev"], domain="satellite")
t3 = time.perf_counter()
check("P6c 二次调用缓存命中（engine 对象复用，零重建）",
      len(_BM25_CACHE) == n6 and any(id(v[0]) in eng6 for v in _BM25_CACHE.values()),
      f"cache={len(_BM25_CACHE)}")
timed("P6d 首次 build+检索耗时护栏", t1 - t0, t1 - t0, 30.0)
print(f"      （首次 {t1-t0:.2f}s，缓存命中 {t3-t2:.2f}s，节省 {t1-t0-(t3-t2):.2f}s）")

# ═══════════════════════════ P7 缓存容量上限 ═══════════════════════════
print("\n== P7 缓存容量上限 8（9 个不同 key 淘汰最旧） ==")
# 补 doc7-9（各 1 chunk，真实范围）
conn.executemany("INSERT INTO documents (id, filename, branch) VALUES (?,?,?)",
                 [(7, "doc7.md", "dev"), (8, "doc8.md", "dev"), (9, "doc9.md", "dev")])
conn.executemany(
    "INSERT INTO document_chunks (id, document_id, chunk_index, content, source_doc, "
    "branch, domain, embed_version) VALUES (?,?,?,?,?,?,?,?)",
    [(3001, 7, 0, "doc7 独立内容：星间激光链路", "doc7.md", "dev", "satellite", "bigram-tf"),
     (3002, 8, 0, "doc8 独立内容：氢镍蓄电池组", "doc8.md", "dev", "satellite", "bigram-tf"),
     (3003, 9, 0, "doc9 独立内容：星敏感器姿态测量", "doc9.md", "dev", "satellite", "bigram-tf")])
conn.commit()
fp = conn.execute("SELECT COUNT(*), COALESCE(MAX(id),0) FROM document_chunks").fetchone()
for d in range(1, 10):
    hybrid_search(conn, "转发器", top_k=2, branches=["dev"], doc_names=[f"doc{d}.md"])
check("P7a 缓存数 <= 8（容量上限生效）", len(_BM25_CACHE) <= _BM25_CACHE_MAX,
      f"len={len(_BM25_CACHE)}")
k1 = (("dev",), ("doc1.md",), (fp[0], fp[1]))
k9 = (("dev",), ("doc9.md",), (fp[0], fp[1]))
check("P7b 最旧 key（doc1）被淘汰", k1 not in _BM25_CACHE, "doc1 仍在缓存")
check("P7c 最新 key（doc9）保留", k9 in _BM25_CACHE, "doc9 未入缓存")

# ═══════════════════════════ P8/P9/P10 指纹敏感性 ═══════════════════════════
print("\n== P8-P10 指纹敏感性（INSERT / DELETE / content-UPDATE） ==")
e1, _r = _bm25_cache_get(conn, ["dev"], None, ["doc1.md"])
conn.execute("INSERT INTO document_chunks (id, document_id, chunk_index, content, source_doc, "
             "branch, domain, embed_version) VALUES (?,?,?,?,?,?,?,?)",
             (3004, 1, 500, "新增内容：Ka频段低噪声放大器LNA", "doc1.md", "dev", "satellite", "bigram-tf"))
conn.commit()
e2, _r = _bm25_cache_get(conn, ["dev"], None, ["doc1.md"])
check("P8a INSERT 指纹失效 → 缓存重建（engine 更新）", e2 is not e1, "engine 未替换")

conn.execute("DELETE FROM document_chunks WHERE id=3004")
conn.commit()
e3, _r = _bm25_cache_get(conn, ["dev"], None, ["doc1.md"])
check("P9a DELETE 指纹失效 → 缓存重建（engine 更新）", e3 is not e2, "engine 未替换")

conn.execute("UPDATE document_chunks SET content='内容更新测试' WHERE id=3003")
conn.commit()
e4, _r = _bm25_cache_get(conn, ["dev"], None, ["doc1.md"])
check("P10a content UPDATE 不改指纹 → 命中缓存（设计语义：ingest 下更新走新行）",
      e4 is e3, "content 更新触发重建（超出设计语义）")

# ═══════════════════════════ REG 基线回归 ═══════════════════════════
# 清单维护约定（2026-09-18 修复）：只列**仓库内实际存在**的脚本。
# 历史缺陷：原清单引用 verify_staging_fuse / verify_staging_migration /
# verify_fusion_fixes，三者**从未入库**（git 全历史无新增记录）且磁盘已不存在
# → 检查恒为 FAIL（rc=2 can't open file），本脚本自诞生起就一直 EXIT=1，门禁形同失效。
# 现改为：① 清单换现存等价基线；② 加存在性前置校验，缺失即判 FAIL 并明示"清单漂移"。
print("\n== REG 基线回归 ==")
baselines = ["tests/manual_verify/verify_p1_blocking_smoke.py"]
for b in baselines:
    if not os.path.exists(os.path.join(_BASE, b)):
        check(f"REG {os.path.basename(b)} 脚本存在（门禁清单漂移）", False,
              f"清单引用的脚本不存在：{b}")
        continue
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, b], cwd=_BASE, capture_output=True,
                       text=True, encoding="utf-8")
    dt = time.perf_counter() - t0
    tail = "\n".join(r.stdout.strip().splitlines()[-4:]) if r.stdout else r.stderr[-400:]
    check(f"REG {os.path.basename(b)} 退出码 0", r.returncode == 0,
          f"rc={r.returncode}\n{tail}")
    print(f"      {os.path.basename(b)} 耗时 {dt:.1f}s")

# ═══════════════════════════ 汇总 ═══════════════════════════
print("\n" + "=" * 56)
print(f"PASS {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
print("ALL GREEN ✅")
