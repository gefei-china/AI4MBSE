# -*- coding: utf-8 -*-
"""文档库「基于文件的管理」实施自检（2026-09-21）。

对应方案 `docs/文档库基于文件的管理-评估与优化方案-20260921.md` §6 的 V1–V8 验收口径。
三层结构（与本仓既有自检脚本同款）：
  A 静态断言 —— 关键防线是否真的写进源码（含"防漏召回"的指纹这类隐蔽点）
  B 行为断言 —— **夹具驱动**（`:memory:` 临时库，不碰真实库、不耦合"本机恰好配了什么"）
  C 变异自证 —— 把每处改动还原回旧写法，对应断言**必须 FAIL**（否则就是空转断言）

运行：
    .venv/Scripts/python.exe -X utf8 tools/verify/verify_doc_folders.py
"""
import contextlib
import io
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.chdir(REPO)
sys.path.insert(0, str(REPO))

OK = FAIL = 0
RESULTS = []


def check(tag, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        RESULTS.append(("PASS", tag, ""))
        print(f"[PASS] {tag}")
    else:
        FAIL += 1
        RESULTS.append(("FAIL", tag, detail))
        print(f"[FAIL] {tag} —— {detail}")


def read(p):
    return (REPO / p).read_text(encoding="utf-8")


print("=" * 78)
print("文档库「基于文件的管理」实施自检")
print("=" * 78)

# ══ A 静态断言 ══════════════════════════════════════════════════════════════
print("\n── A 静态断言 ──")
search_src = read("knowledge_pipeline/search.py")
ke_src = read("knowledge_engine.py")
rag_src = read("agent/rag.py")
brepo_src = read("repositories/branch_repo.py")
mig_src = read("database/migrations/documents.py")
schema_src = read("database/schema.py")
docs_js = read("static/js/mods/20-docs.js")
folder_js = read("static/js/mods/40-docfolders.js")
html_src = read("static/index.html")

check("A1 `_lifecycle_clause` 已定义且默认排除 deprecated",
      "def _lifecycle_clause(" in search_src and "lifecycle_status NOT IN ('deprecated')" in search_src,
      "缺生命周期子句 → 已废弃文档仍会被检索召回")
_lc_body = re.search(r"def _lifecycle_clause\(.*?(?=\n\ndef |\Z)", search_src, re.S)
_lc_body = _lc_body.group(0) if _lc_body else ""
check("A2 `_lifecycle_clause` 用 `IS NULL OR NOT IN` 写法（防老库 NULL 行被整批吃掉）",
      "lifecycle_status IS NULL" in _lc_body and "NOT IN ('deprecated')" in _lc_body,
      "写成 `!= 'deprecated'` 时 NULL 行会被静默排除（该列为 NULL 的老库行整批消失）")

# A3/A4：子句必须真正接进三路，且签名可显式包含
check("A3 hybrid_search / _bm25_cache_get / search_chunks 均带 include_deprecated",
      all(f"{fn}" in ke_src and "include_deprecated" in ke_src for fn in ("hybrid_search", "_bm25_cache_get"))
      and "include_deprecated: bool = False" in search_src,
      "子句没接进函数签名 → 过滤只能靠默认值碰运气")
# A5 是本轮最隐蔽的一条：子句加了但缓存指纹不变 → 缓存存活期内仍漏召回
bm = ke_src.split("def _bm25_cache_get(")[1][:2600]
check("A4 BM25 写水位指纹含「已下线条数」（否则 UPDATE lifecycle_status 不使缓存失效）",
      "SUM(CASE WHEN lifecycle_status" in bm and "fp[2]" in bm,
      "指纹只用 (COUNT,MAX(id)) → 刚废弃的文档在缓存存活期内仍会被 BM25 召回")

_rb = brepo_src.split("def rollback_merge(")[1]
check("A5 rollback_merge 按快照内容决定是否删文档（使「删而不还」不可达）",
      'if snapshot.get("documents"):' in _rb
      and _rb.index('if snapshot.get("documents"):') < _rb.index('DELETE FROM documents WHERE branch=?'),
      "无条件删文档 → 快照不含文档时静默丢文档（只加 `if tgt != \"global\"` 挡不住 release 遗留行）")
check("A6 `_merge_commit` 的文档快照已标注为死代码（防后人误以为回滚能恢复文档）",
      "恒为空数组" in brepo_src.split("if is_release:")[0] or "恒为空数组**（死代码）" in brepo_src,
      "未标注 → 后人会依据它写'回滚恢复文档'的逻辑")

check("A7 `_migrate_docs_global` 去重已收窄到「同名且跨分支」",
      "HAVING COUNT(*)>1 AND COUNT(DISTINCT branch)>1" in mig_src,
      "仍按 filename 无条件去重 → 同分支同名（版本链形态）会被物理删除")
check("A8 documents.branch 的 DDL 默认值已对齐 'global'",
      "branch TEXT DEFAULT 'global'" in schema_src,
      "默认值仍是 'dev' → 绕过 ingest_document 的裸 INSERT 落到 dev，可能被分支回滚删除")

check("A9 doc_folders 用哨兵 parent_id NOT NULL DEFAULT 0（不用 NULL）+ 表达式唯一索引",
      "parent_id   INTEGER NOT NULL DEFAULT 0" in mig_src
      and "ux_doc_folders_name_parent" in mig_src and "IFNULL(parent_id, 0)" in mig_src,
      "D1 未修 → 根级可建无限个同名目录")
check("A10 documents.folder_id 用 0 表示未归类（NOT NULL DEFAULT 0）",
      '"INTEGER NOT NULL DEFAULT 0"' in mig_src and 'folder_id' in mig_src,
      "用 NULL 表示未归类 → 会重蹈 UNIQUE/等值判断在 NULL 上静默漏判的覆辙")

check("A11 白名单自愈（_resolve_scope_docs）已排除已下线文档",
      " AND (lifecycle_status IS NULL OR lifecycle_status NOT IN ('deprecated'))" in
      rag_src.split("def _resolve_scope_docs(")[1][:2800]
      and "include_deprecated" in rag_src.split("def _resolve_scope_docs(")[1][:2800],
      "自愈会把已废弃文档判为'白名单有效' → 自愈形同虚设")

check("A12 删除文档同步清理源副本（D8）",
      "remove_source_copy" in read("repositories/meta_repo.py"),
      "删除路径不留副本 → 孤儿副本继续累积")
check("A13 副本路径规则已收敛为单点（全仓只有 ingest 一份正则）",
      read("knowledge_pipeline/ingest.py").count(r"[^\w.\-\u4e00-\u9fa5]") == 1
      and r"[^\w.\-\u4e00-\u9fa5]" not in read("routers/meta.py")
      and read("knowledge_pipeline/search.py").count(r"[^\w.\-\u4e00-\u9fa5]") == 0,
      "同一规则多处各写一遍 → 定位不到副本且失败静默")

check("A14 前端左栏分「结构目录 / 智能视图」两组（智能视图只读）",
      "结构目录" in folder_js and "智能视图" in folder_js and "只读" in folder_js,
      "两组混在一起 → 用户会把'未归类'当目录往里拖文件")
check("A15 目录树用事件委托（不给节点拼 inline onclick —— 目录名含引号会崩）",
      "addEventListener('click'" in folder_js and "data-act" in folder_js
      and not re.search(r'onclick="docFolder\w+\(\s*\'\$\{', folder_js),
      "用 inline onclick 拼用户输入的名字 → 名字带引号直接语法崩")
check("A16 index.html 已就位左栏/树容器/面包屑，并加载新模块",
      'id="doc-left"' in html_src and 'id="doc-folders"' in html_src
      and 'id="doc-crumb"' in html_src and "40-docfolders.js" in html_src,
      "前端容器或模块缺失")
check("A17 上传落点 = 当前选中目录（folder_id 随表单上报）",
      "fd.append('folder_id'" in docs_js and "folder_id: str = Form(" in read("routers/meta.py"),
      "上传不落当前目录 → 「上传到该目录」的语义是假的")
def _strip_module_docstring(src: str) -> str:
    """去掉模块级 docstring。

    ⚠️ 检查「数据层不含 branch」时**必须**先剥 docstring：本模块的 docstring 正解释
    "本域任何表/查询都不带 branch"，把设计声明当成违规命中会让断言恒 FAIL（第一版就如此）。
    """
    m = re.match(r'\s*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', src, re.S)
    return src[m.end():] if m else src


_df_code = re.sub(r"(?m)#.*$", "", _strip_module_docstring(read("repositories/doc_folder_repo.py")))
check("A18 数据层不含 branch 维度（目录树不按分支切分）",
      "branch" not in _df_code,
      "目录树带入 branch → 与「文档是全局资产」冲突")
check("A19 路由层同理（目录 API 不带 branch）",
      "branch" not in re.sub(r"(?m)#.*$", "", _strip_module_docstring(read("routers/doc_folders.py"))),
      "目录 API 带入 branch → 前端要凭空多一个恒 'global' 的参数")

# ══ B 行为断言（夹具驱动）═══════════════════════════════════════════════════
print("\n── B 行为断言（夹具驱动：临时库，不碰真实库）──")
from repositories.doc_folder_repo import DocFolderRepo, normalize_folder   # noqa: E402
from repositories.branch_repo import BranchRepo                            # noqa: E402
from repositories.meta_repo import MetaRepo                                # noqa: E402
from knowledge_pipeline.search import (                                    # noqa: E402
    _lifecycle_clause, _search_chunks_bigram, vector_search_embed, search_chunks)
from knowledge_engine import hybrid_search, _BM25_CACHE                    # noqa: E402
from database.migrations import (_migrate_doc_folders,                     # noqa: E402
                                 _migrate_document_lifecycle, _migrate_artifact_ingest,
                                 _migrate_docs_global)

DOCS_DDL = """
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL,
    file_type TEXT DEFAULT '', file_size INTEGER DEFAULT 0,
    parse_status TEXT DEFAULT 'pending', chunk_count INTEGER DEFAULT 0,
    entity_count INTEGER DEFAULT 0, quality_score REAL DEFAULT 0,
    uploaded_by TEXT DEFAULT '', branch TEXT DEFAULT 'global',
    knowledge_category TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE document_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
    chunk_index INTEGER DEFAULT 0, content TEXT NOT NULL,
    source_doc TEXT DEFAULT '', section TEXT DEFAULT '',
    embedding TEXT DEFAULT '[]', embed_version TEXT DEFAULT '',
    hyde_questions TEXT DEFAULT '[]', hyde_embedding TEXT DEFAULT '',
    branch TEXT DEFAULT 'global', domain TEXT DEFAULT 'unknown',
    origin TEXT DEFAULT 'upload', lifecycle_status TEXT DEFAULT 'stored',
    linked_entity_ids TEXT DEFAULT '[]', bm25_text TEXT DEFAULT '');
CREATE TABLE doc_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL,
    title TEXT DEFAULT '', author TEXT DEFAULT '', version TEXT DEFAULT '',
    tags TEXT DEFAULT '', source TEXT DEFAULT 'upload', extra TEXT DEFAULT '{}',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP, superseded_by INTEGER DEFAULT 0);
CREATE TABLE domain_review_queue (id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL, status TEXT DEFAULT 'pending');
CREATE TABLE branches (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    parent_branch TEXT DEFAULT '', branch_type TEXT DEFAULT 'work');
CREATE TABLE entities (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT,
    branch TEXT DEFAULT 'dev', status TEXT DEFAULT 'reviewed', properties TEXT DEFAULT '{}',
    entity_type TEXT DEFAULT '');
CREATE TABLE relations (id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER,
    target_id INTEGER, relation_type TEXT DEFAULT '', branch TEXT DEFAULT 'dev',
    status TEXT DEFAULT 'reviewed', properties TEXT DEFAULT '{}');
CREATE TABLE merge_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, source_branch TEXT,
    target_branch TEXT, status TEXT DEFAULT 'draft', prev_release_snapshot TEXT DEFAULT '{}');
CREATE TABLE knowledge_publish_logs (id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER, name TEXT, branch TEXT, published_at TEXT,
    version INTEGER DEFAULT 1, merged_from TEXT DEFAULT '', action TEXT DEFAULT 'publish');
"""


def make_fixture(docs=None, chunks=None):
    """最小夹具：只建被测代码真正读到的表/列，再跑**真实迁移**建 doc_folders/folder_id。

    用真实迁移而不是手抄 DDL —— 这样 D1 哨兵写的变异能穿透到夹具（否则变异只改源码、
    夹具还是老结构，断言不会 FAIL，等于没有变异自证）。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DOCS_DDL)
    with contextlib.redirect_stdout(io.StringIO()):   # 迁移的 print 是给人看的，别污染断言输出
        _migrate_document_lifecycle(conn)
        _migrate_artifact_ingest(conn)
        _migrate_doc_folders(conn)
    for d in (docs or []):
        conn.execute("INSERT INTO documents (id, filename, branch, parse_status) VALUES (?,?,?,?)", d)
    for c in (chunks or []):
        conn.execute("INSERT INTO document_chunks (id, document_id, content, source_doc, "
                     "lifecycle_status, branch, domain) VALUES (?,?,?,?,?,?,?)", c)
    conn.commit()
    return conn


# ── V1：根级/子级同名目录第二次创建必须失败 ──
R = DocFolderRepo(make_fixture())
root1 = R.create("规范", 0)
dup_root = R.create("规范", 0)
check("B1 V1 根级同名目录第二次创建被拒（哨兵 0 生效）",
      root1.get("ok") is True and dup_root.get("ok") is False
      and "已存在同名" in (dup_root.get("error") or ""),
      f"第一次={root1} 第二次={dup_root}")
child1 = R.create("GB", root1.get("id"))
dup_child = R.create("GB", root1.get("id"))
check("B2 V1 子级同名目录第二次创建被拒",
      child1.get("ok") is True and dup_child.get("ok") is False,
      f"第一次={child1} 第二次={dup_child}")
# 裸 DDL 层复验（证明拦截来自数据库约束，不是应用层的名称预检）
def _raw_insert_twice(conn, parent_sql):
    """按给定 parent_id 字面量连插两条同名根目录，返回第二次是否被数据库拒绝。"""
    try:
        conn.execute(f"INSERT INTO doc_folders (name, parent_id, path) VALUES ('规范',{parent_sql},'/规范/')")
        conn.execute(f"INSERT INTO doc_folders (name, parent_id, path) VALUES ('规范',{parent_sql},'/规范/')")
        return False
    except sqlite3.IntegrityError:
        return True


check("B3 V1 裸 DDL 层（parent_id=0）根级同名触发 IntegrityError", _raw_insert_twice(make_fixture(), "0"),
      "约束没落到数据库 → 任何绕过仓储的写入都能造出同名根目录")
check("B3b V1 第二道闸：parent_id 被写成 NULL 时同名仍被表达式索引拦住",
      _raw_insert_twice(make_fixture(), "NULL"),
      "表达式索引缺失 → 手写 SQL / 历史数据把 parent_id 写成 NULL 时，根级同名会再次穿透")
# 反向对照：不带约束的等价表，NULL 父子级同名**可以**并存（这就是 D1 的原始缺陷形态）
_c_nc = sqlite3.connect(":memory:")
_c_nc.execute("CREATE TABLE f(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, parent_id INTEGER, "
              "UNIQUE(parent_id, name))")
_dup_ok = False
try:
    _c_nc.execute("INSERT INTO f(name, parent_id) VALUES ('规范', NULL)")
    _c_nc.execute("INSERT INTO f(name, parent_id) VALUES ('规范', NULL)")
    _dup_ok = True
except sqlite3.IntegrityError:
    pass
check("B3c 对照：无表达式索引时 NULL 根级同名确实拦不住（证明 B3b 不是空转）", _dup_ok,
      "对照组居然被拦住了 → 说明 B3b 的机制与我的理解不符，需重新核对 D1 结论")

# ── V2/V2b：已废弃文档在三路检索中 0 召回 ──
Q = "Ka频段转发器选型约束"
fx = make_fixture(
    docs=[(1, "规范A.md", "global", "completed"), (2, "规范B.md", "global", "completed")],
    chunks=[(1, 1, Q + "：转发器选型需满足 Ka 频段约束", "规范A.md", "stored", "global", "thermal"),
            (2, 2, Q + "：转发器选型需满足 Ka 频段约束", "规范B.md", "stored", "global", "thermal")])


def _set_deprecated(conn, cid):
    conn.execute("UPDATE document_chunks SET lifecycle_status='deprecated' WHERE id=?", (cid,))
    conn.execute("UPDATE documents SET lifecycle_status='deprecated' WHERE id=?",
                 (conn.execute("SELECT document_id FROM document_chunks WHERE id=?", (cid,)).fetchone()[0],))
    conn.commit()


def _recall_all(conn, include_deprecated):
    """三条路各打一次，返回 (bigram 命中数, 向量路命中数, 混合路命中数)。"""
    _BM25_CACHE.clear()
    bg = _search_chunks_bigram(conn, Q, top_k=5, include_deprecated=include_deprecated)
    ve = vector_search_embed(conn, Q, top_k=5, include_deprecated=include_deprecated)
    hy = hybrid_search(conn, Q, top_k=5, include_deprecated=include_deprecated)
    sc = search_chunks(conn, Q, top_k=5, include_deprecated=include_deprecated)
    _BM25_CACHE.clear()
    return len(bg), len(ve), len(hy["hits"]), len(sc)


_set_deprecated(fx, 2)
b_bg, b_ve, b_hy, b_sc = _recall_all(fx, include_deprecated=False)
check("B4 V2 已废弃文档在三路检索中 0 召回（bigram/向量/混合/入口）",
      (b_bg, b_ve, b_hy, b_sc) == (1, 1, 1, 1),
      f"期望各 1 条（只应命中未废弃那份），实际 {b_bg}/{b_ve}/{b_hy}/{b_sc} —— >1 即废弃件仍被召回")
a_bg, a_ve, a_hy, a_sc = _recall_all(fx, include_deprecated=True)
check("B5 显式 include_deprecated=True 时管理侧能看到废弃件（开关有效，非死开关）",
      (a_bg, a_ve, a_hy, a_sc) == (2, 2, 2, 2),
      f"实际 {a_bg}/{a_ve}/{a_hy}/{a_sc} —— 开关没生效则回收站/版本列表看不到自己的内容")

# ── V2c：BM25 缓存指纹必须对 lifecycle_status 的 UPDATE 敏感 ──
fx2 = make_fixture(docs=[(1, "规范A.md", "global", "completed")],
                   chunks=[(1, 1, Q + "：转发器选型需满足 Ka 频段约束", "规范A.md", "stored", "global", "thermal")])
_BM25_CACHE.clear()
hybrid_search(fx2, Q, top_k=5)                       # 先建缓存（此时未废弃）
_set_deprecated(fx2, 1)                              # 再废弃（走 UPDATE，行数/MAX(id) 都不变）
n_cached = len(hybrid_search(fx2, Q, top_k=5)["hits"])
_BM25_CACHE.clear()
check("B6 缓存建立后废弃该文档 → 必须 0 召回（指纹含已下线条数）", n_cached == 0,
      f"实际 {n_cached} 条 —— 缓存未失效：子句加了也白加，这是最隐蔽的漏召回形态")

# ── V3：rollback_merge 不得删文档 ──
fx3 = make_fixture(docs=[(1, "全局规范.md", "global", "completed")])
fx3.execute("INSERT INTO branches (name, branch_type) VALUES ('release','release')")
fx3.execute("INSERT INTO documents (id, filename, branch, parse_status) "
            "VALUES (2,'历史release遗留.md','release','completed')")
fx3.execute("INSERT INTO entities (id, name, branch, status) VALUES (1,'转发器','release','reviewed')")
fx3.execute("INSERT INTO merge_requests (id, source_branch, target_branch, status, prev_release_snapshot) "
            "VALUES (1,'dev','release','merged',?)",
            ('{"entities":[{"id":1,"name":"转发器","branch":"release","status":"reviewed","entity_type":"","properties":"{}"}],"relations":[]}',))
fx3.commit()
rb = BranchRepo(fx3).rollback_merge(1)
left_release = fx3.execute("SELECT COUNT(*) FROM documents WHERE id=2").fetchone()[0]
left_global = fx3.execute("SELECT COUNT(*) FROM documents WHERE id=1").fetchone()[0]
check("B7 V3 rollback_merge 后非全局文档行仍在（护栏生效，不静默丢文档）",
      rb.get("ok") is True and left_release == 1 and left_global == 1,
      f"rollback={rb} release 文档剩 {left_release} 全局文档剩 {left_global}（旧写法会删掉且不还原）")

# ── V6/V8：移动文档、重命名目录都不得触碰 chunks/向量与磁盘副本 ──
fx4 = make_fixture(docs=[(1, "规范A.md", "global", "completed")],
                   chunks=[(1, 1, "热管理约束内容", "规范A.md", "stored", "global", "thermal")])
fx4.execute("UPDATE document_chunks SET embedding='[0.1,0.2]', embed_version='test-dim2' WHERE id=1")
fx4.commit()
writes = []
_raw_exec = fx4.execute


class _TraceConn:
    """包装 execute：记录对 document_chunks 的**写**语句（移动/改名不得产生任何一条）。"""
    def __init__(self, c):
        self._c = c

    def execute(self, sql, *a):
        s = " ".join(str(sql).split())
        if re.match(r"^(UPDATE|DELETE|INSERT)\s+document_chunks", s, re.I):
            writes.append(s[:90])
        return self._c.execute(sql, *a)

    def __getattr__(self, k):
        return getattr(self._c, k)


R4 = DocFolderRepo(_TraceConn(fx4))
fid = R4.create("规范", 0)["id"]
R4.move_document(1, fid)
emb_after_move = fx4.execute("SELECT embedding FROM document_chunks WHERE id=1").fetchone()[0]
R4.rename(fid, "标准规范")
emb_after_rename = fx4.execute("SELECT embedding FROM document_chunks WHERE id=1").fetchone()[0]
check("B8 V6/V8 移动与重命名全程未产生任何 document_chunks 写语句、向量逐字节不变",
      not writes and emb_after_move == "[0.1,0.2]" and emb_after_rename == "[0.1,0.2]",
      f"chunks 写语句={writes} 移动后={emb_after_move} 改名后={emb_after_rename}")

# ── 删除目录：文档回未归类，绝不级联删文档 ──
R4.move_document(1, fid)
R4.move_document(1, 0)          # 先复原
R4.move_document(1, fid)
before_docs = fx4.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
dr = R4.delete(fid)
after_docs = fx4.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
fid_after = fx4.execute("SELECT folder_id FROM documents WHERE id=1").fetchone()[0]
check("B9 删除目录后文档回到「未归类」且文档行数不变（绝不级联删文档）",
      dr.get("ok") is True and after_docs == before_docs == 1 and fid_after == 0,
      f"删除={dr} 文档数 {before_docs}->{after_docs} folder_id={fid_after}")

# ── V7：删除文档同步清理源副本 ──
import tempfile                                                     # noqa: E402
import knowledge_pipeline.ingest as _ing                            # noqa: E402
from repositories.meta_repo import MetaRepo as _MR                  # noqa: E402

_fake_dir = tempfile.mkdtemp(prefix="docf_orphan_")
_orig_dir = _ing.source_copy_dir
_ing.source_copy_dir = lambda: _fake_dir
try:
    fx5 = make_fixture(docs=[(1, "待删.md", "global", "completed")])
    copy_path = os.path.join(_fake_dir, "1_待删.md")
    with open(copy_path, "wb") as f:
        f.write(b"x")
    existed_before = os.path.exists(copy_path)
    _MR(fx5).delete_document(1)
    gone_after = not os.path.exists(copy_path)
finally:
    _ing.source_copy_dir = _orig_dir
check("B10 V7 删除文档同步清理源副本（孤儿副本不再累积）",
      existed_before and gone_after,
      f"删除前存在={existed_before} 删除后已清={gone_after}")

# ── V5'/G5：迁移的同名去重不得吃掉"版本链形态"的同名行 ──
fx6 = make_fixture()
fx6.executemany("INSERT INTO documents (id, filename, branch, parse_status) VALUES (?,?,?,?)",
                [(1, "设计规范.md", "dev", "completed"),
                 (2, "设计规范.md", "dev", "completed"),      # 同分支同名 = 版本链形态，必须保留
                 (3, "热管理规范.md", "dev", "completed"),
                 (4, "热管理规范.md", "release", "completed")])  # 跨分支同名 = 真发布快照，可去重
fx6.commit()
_migrate_docs_global(fx6)
n_same_branch = fx6.execute("SELECT COUNT(*) FROM documents WHERE filename='设计规范.md'").fetchone()[0]
n_cross_branch = fx6.execute("SELECT COUNT(*) FROM documents WHERE filename='热管理规范.md'").fetchone()[0]
check("B11 G5 迁移去重只吃「跨分支发布快照」（2→1），不动「同分支同名」（保持 2）",
      n_same_branch == 2 and n_cross_branch == 1,
      f"同分支同名剩 {n_same_branch}（应为 2，被删=版本历史被物理删除）；跨分支剩 {n_cross_branch}（应为 1）")

# ── 目录维过滤（仅自身 / 含子目录）──
fx7 = make_fixture(docs=[(1, "a.md", "global", "completed"), (2, "b.md", "global", "completed"),
                         (3, "c.md", "global", "completed")])
R7 = DocFolderRepo(fx7)
top = R7.create("规范", 0)["id"]
sub = R7.create("GB", top)["id"]
R7.move_document(1, top)
R7.move_document(2, sub)
m = MetaRepo(fx7)
n_self = len(m.list_documents_with_meta(folder_ids=[top]))
n_sub = len(m.list_documents_with_meta(folder_ids=R7._descendant_ids(top)))
n_unc = len(m.list_documents_with_meta(folder_ids=[0]))
check("B12 目录过滤：仅当前目录=1、含子目录=2、未归类=1",
      (n_self, n_sub, n_unc) == (1, 2, 1), f"实际 self={n_self} subtree={n_sub} unc={n_unc}")

# ── 归一化：前端各种空值形状都收敛到哨兵 0 ──
check("B13 目录/父目录归一：None/''/'/'/'0'/负数/np 均 → 0",
      all(normalize_folder(v) == 0 for v in (None, "", "/", "0", 0, -3, "abc")),
      f"got {[normalize_folder(v) for v in (None,'','/','0',0,-3,'abc')]}")

# ── G2：白名单自愈对「已下线」文档的口径 ──
from agent.rag import GraphRAG                                                # noqa: E402
fx8 = make_fixture(docs=[(1, "下线规范.md", "global", "completed")])
fx8.execute("UPDATE documents SET lifecycle_status='deprecated' WHERE id=1")
fx8.commit()
eff, warn = GraphRAG._resolve_scope_docs(fx8, ["下线规范.md"])
check("B14 G2 白名单里全是已下线文档 → 自愈判为失效（effective 空 + 计入 missing）",
      eff == [] and warn is not None and "下线规范.md" in (warn.get("missing") or []),
      f"effective={eff} warn={warn} —— 判成'有效'时三路过滤同时归零且全程静默")
# 对照：未下线时同名文档是「有效项」（证明 B14 不是恒真）
fx9 = make_fixture(docs=[(1, "在线规范.md", "global", "completed")])
eff2, warn2 = GraphRAG._resolve_scope_docs(fx9, ["在线规范.md"])
check("B15 G2 对照：未下线文档仍是有效白名单项（自愈没被改坏）",
      eff2 == ["在线规范.md"] and warn2 is None, f"effective={eff2} warn={warn2}")

# ── B16/B17：批量出站（deprecated → committed）必须把 chunks 同步回 stored ──
# 现场（2026-09-21 端到端验证暴露）：`/api/documents/batch` 的 restore 走
# `MetaRepo.batch_transition`，而它的判据原为 `chunk_sync=(to_status == "deprecated")`
# —— 只覆盖「下线」，漏了「出站」。后果：文档在管理界面显示已恢复、chunks 仍 deprecated，
# 于是 G1 的 `NOT IN ('deprecated')` 把它**永久挡在检索外**（界面正常、搜索不到，全程静默）。
# 单文档 `/restore` 显式传了 chunk_sync=True 所以是对的 → 典型「两条路径不一致」。
from repositories.meta_repo import MetaRepo                                      # noqa: E402
fx10 = make_fixture(docs=[(1, "出站用例.md", "global", "completed")],
                    chunks=[(1, 1, Q + "：转发器选型需满足 Ka 频段约束",
                             "出站用例.md", "stored", "global", "thermal")])
MR = MetaRepo(fx10)
r_in = MR.batch_transition([1], "deprecated", "tester", reason="B16 进站")
n_in = fx10.execute("SELECT COUNT(*) FROM document_chunks WHERE document_id=1 "
                    "AND lifecycle_status='deprecated'").fetchone()[0]
check("B16 批量进站：chunks 同步置 deprecated（原有正确行为，作为对照基线）",
      r_in["ok"] == 1 and n_in == 1, f"r_in={r_in} chunks_dep={n_in}")
r_out = MR.batch_transition([1], "committed", "tester", reason="B17 出站",
                            allow_from=["deprecated"])
doc_st = fx10.execute("SELECT lifecycle_status FROM documents WHERE id=1").fetchone()[0]
chunk_st = set(x[0] for x in fx10.execute(
    "SELECT DISTINCT lifecycle_status FROM document_chunks WHERE document_id=1"))
check("B17 ★批量出站：chunks 必须同步回 stored（否则检索侧永久不可见）",
      r_out["ok"] == 1 and doc_st == "committed" and chunk_st == {"stored"},
      f"r_out={r_out} documents={doc_st} chunks={chunk_st} ← chunks 仍是 deprecated 即漏同步")
# ⚠️ 这里**故意不写**「出站后四路检索能召回」的断言：实测它会与 MUT 第一条
# （`_lifecycle_clause` 还原为「不过滤」）**交叉抵消** —— 组合变异下过滤整体失效，
# 该断言期望值 (1,1,1,1) 与变异后实测值相同，于是在变异子进程里假绿（=空转）。
# 该跨层效果由 `tmp/e2e/e2e_doc_folders.py` 的 E33 在真实链路上把关
# （E33 实测抓到了本缺陷：restore 后 791 仍未被召回）。留一条假绿断言只会污染结论。

# ══ C 变异自证 ══════════════════════════════════════════════════════════════
if os.environ.get("DOCF_MUT_CHILD") == "1":
    print("\n（变异子进程：跳过 C 段）")
    print("\n" + "=" * 78)
    print(f"结果：{OK} pass / {FAIL} fail")
    print("=" * 78)
    sys.exit(1 if FAIL else 0)

print("\n── C 变异自证（把改动还原 → 断言必须 FAIL）──")
# ⚠️ 方法论坑（2026-09-21 实测）：本段是**一次性施加全部变异**再跑一遍 A/B。
# 因此某条断言的期望值可能与被「另一条变异」改写后的实测值**恰好相同**，从而假绿。
# 实测：一条依赖 G1 过滤开关的检索断言，在「`_lifecycle_clause` 还原为不过滤」那条变异下
# 期望值与实测值重合 → 被判成"空转断言"。它其实不是没价值，而是**交叉抵消**。
# 规则：跨层断言要么只用**不受其他变异影响**的判据（如直接查表状态），
#       要么下沉到端到端脚本里用真实链路把关。
MUT = [
    ("还原 _lifecycle_clause 为「不过滤」", "knowledge_pipeline/search.py",
     """    if include_deprecated:
        return "", []
    return " AND (lifecycle_status IS NULL OR lifecycle_status NOT IN ('deprecated'))", []""",
     """    if include_deprecated:
        return "", []
    return "", []""",
     ["A1 `_lifecycle_clause` 已定义且默认排除 deprecated",
      "B4 V2 已废弃文档在三路检索中 0 召回（bigram/向量/混合/入口）"]),
    ("还原 BM25 指纹为 (COUNT,MAX(id))", "knowledge_engine.py",
     """        "SELECT COUNT(*), COALESCE(MAX(id),0), "
        "COALESCE(SUM(CASE WHEN lifecycle_status IS NULL "
        "OR lifecycle_status NOT IN ('deprecated') THEN 0 ELSE 1 END),0) "
        "FROM document_chunks").fetchone()""",
     """        "SELECT COUNT(*), COALESCE(MAX(id),0), 0 FROM document_chunks").fetchone()""",
     ["A4 BM25 写水位指纹含「已下线条数」（否则 UPDATE lifecycle_status 不使缓存失效）",
      "B6 缓存建立后废弃该文档 → 必须 0 召回（指纹含已下线条数）"]),
    ("撤掉 rollback_merge 的「按快照决定是否删文档」护栏", "repositories/branch_repo.py",
     """        if snapshot.get("documents"):
            self.execute("DELETE FROM document_chunks WHERE branch=?", (tgt,))
            self.execute("DELETE FROM documents WHERE branch=?", (tgt,))""",
     """        if True:
            self.execute("DELETE FROM document_chunks WHERE branch=?", (tgt,))
            self.execute("DELETE FROM documents WHERE branch=?", (tgt,))""",
     ["A5 rollback_merge 按快照内容决定是否删文档（使「删而不还」不可达）",
      "B7 V3 rollback_merge 后非全局文档行仍在（护栏生效，不静默丢文档）"]),
    ("还原 D1-a：去掉 parent_id 的哨兵默认与 NOT NULL", "database/migrations/documents.py",
     "        parent_id   INTEGER NOT NULL DEFAULT 0,     -- 0 = 根（D1：不用 NULL，否则 UNIQUE 失效）",
     "        parent_id   INTEGER,                        -- 变异：允许 NULL（D1 原始缺陷形态）",
     ["A9 doc_folders 用哨兵 parent_id NOT NULL DEFAULT 0（不用 NULL）+ 表达式唯一索引"]),
    ("还原 D1-b：去掉表达式唯一索引这道第二道闸", "database/migrations/documents.py",
     """    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_doc_folders_name_parent "
              "ON doc_folders(name, IFNULL(parent_id, 0))")""",
     """    pass   # 变异：第二道闸缺失（D1 原始缺陷形态）""",
     ["A9 doc_folders 用哨兵 parent_id NOT NULL DEFAULT 0（不用 NULL）+ 表达式唯一索引",
      "B3b V1 第二道闸：parent_id 被写成 NULL 时同名仍被表达式索引拦住"]),
    ("还原 D1-c：去掉表级 UNIQUE(parent_id, name)", "database/migrations/documents.py",
     """        created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(parent_id, name)                     -- ⚠️ 仅因 parent_id NOT NULL 才有效（见 1）
    )\"\"\")""",
     """        created_at  TEXT DEFAULT CURRENT_TIMESTAMP
    )\"\"\")""",
     ["B1 V1 根级同名目录第二次创建被拒（哨兵 0 生效）",
      "B2 V1 子级同名目录第二次创建被拒",
      "B3 V1 裸 DDL 层（parent_id=0）根级同名触发 IntegrityError"]),
    ("还原迁移去重为「按 filename 无条件去重」", "database/migrations/documents.py",
     '"GROUP BY filename HAVING COUNT(*)>1 AND COUNT(DISTINCT branch)>1").fetchall()',
     '"GROUP BY filename HAVING COUNT(*)>1").fetchall()',
     ["A7 `_migrate_docs_global` 去重已收窄到「同名且跨分支」",
      "B11 G5 迁移去重只吃「跨分支发布快照」（2→1），不动「同分支同名」（保持 2）"]),
    ("撤掉白名单自愈的生命周期过滤", "agent/rag.py",
     """                f"SELECT DISTINCT filename FROM documents WHERE filename IN ({ph})"
                + ("" if include_deprecated else
                   " AND (lifecycle_status IS NULL OR lifecycle_status NOT IN ('deprecated'))"),""",
     """                f"SELECT DISTINCT filename FROM documents WHERE filename IN ({ph})"
                + "",  # 变异：不过滤已下线""",
     ["A11 白名单自愈（_resolve_scope_docs）已排除已下线文档",
      "B14 G2 白名单里全是已下线文档 → 自愈判为失效（effective 空 + 计入 missing）"]),
    ("还原 batch_transition 的 chunk_sync 判据为「只看下线」", "repositories/meta_repo.py",
     '            chunk_sync=(to_status == "deprecated" or cur["lifecycle_status"] == "deprecated")',
     '            chunk_sync=(to_status == "deprecated")',
     ["B17 ★批量出站：chunks 必须同步回 stored（否则检索侧永久不可见）"]),
]
MUT_LABELS = [t for _, _, _, _, tags in MUT for t in tags]
mutation_failed = []
backup = {}


def _mutate(path, old, new):
    raw = (REPO / path).read_bytes()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    assert old in text, f"变异锚点不存在（脚本自身错）：{path}"
    (REPO / path).write_bytes(text.replace(old, new, 1).replace("\n", nl).encode("utf-8"))


try:
    for _, p, old, new, _ in MUT:
        if p not in backup:
            backup[p] = (REPO / p).read_bytes()
        _mutate(p, old, new)
    print(f"  已施加 {len(MUT)} 处变异，重跑 A/B 断言集…")
    r = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve())],
                       cwd=str(REPO), capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "DOCF_MUT_CHILD": "1"})
    out = (r.stdout or "") + (r.stderr or "")
    # ⚠️ 元断言（第一版就栽在这里）：变异若让子进程**中途崩掉**（例如把 SQL 改出语法错），
    # 下半段断言根本不会执行 —— 而"没看到 [FAIL]"会被误读成"断言仍然通过"，
    # 于是全部 B 段断言伪装成空转，C 段给出"变异无效"的假结论。
    # 所以必须先确认子进程完整跑完，再逐条判定。
    crashed = "Traceback (most recent call last)" in out
    check("C0 变异子进程完整跑完 A/B（无 traceback；否则下面的判定全部无效）", not crashed,
          "子进程异常中断 → 未执行的断言会被误判为空转断言"
          + ("；错误尾部：" + out.strip().splitlines()[-1][:160] if crashed else ""))
    uniq = sorted(set(MUT_LABELS))
    for tag in uniq:
        ran = re.search(r"\[(?:PASS|FAIL)\] " + re.escape(tag), out)
        if not ran:
            print(f"    !! 变异后该断言**未执行**（子进程没跑到）：{tag}")
        elif re.search(r"\[FAIL\] " + re.escape(tag), out):
            mutation_failed.append(tag)
        else:
            print(f"    !! 变异后该断言仍通过（=空转断言）：{tag}")
    check(f"C1 变异后全部 {len(uniq)} 条相关断言转为 FAIL",
          len(mutation_failed) == len(uniq),
          f"仅 {len(mutation_failed)}/{len(uniq)}：{sorted(set(uniq) - set(mutation_failed))}")
finally:
    for p, b in backup.items():
        (REPO / p).write_bytes(b)
    print("  变异已还原；逐文件 diff 校验…")
    bad = [p for p, b in backup.items() if (REPO / p).read_bytes() != b]
    check("C2 变异源文件已逐字节还原", not bad, f"未还原：{bad}")

print("\n" + "=" * 78)
print(f"结果：{OK} pass / {FAIL} fail")
print("=" * 78)
sys.exit(1 if FAIL else 0)
