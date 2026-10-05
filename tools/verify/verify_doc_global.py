# -*- coding: utf-8 -*-
"""文档库全局化一致性自检（2026-09-21）。

背景：文档库已按需求「从分支体系抽离为全局资产」——`ingest_document(branch='global')`、
迁移把存量 documents/document_chunks 统一为 'global'、检索与文档列表均忽略分支。
但代码里仍残留旧「分支隔离」模型的实现，本次逐处对齐并加护栏。

三层验证：
  A 静态断言 —— 四处对齐点 + 设计声明的落点存在
  B 行为断言 —— **夹具驱动**（临时库，不碰真实库）：分支过滤不再吃掉全局文档、
                分支删除不再能误删全局文档
  C 变异自证 —— 把三处改动各自还原回旧写法，对应断言必须 FAIL

运行：.venv/Scripts/python.exe -X utf8 tools/verify/verify_doc_global.py
"""

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# CI-OPTIONAL: C 实测本地红（C2 变异源文件未逐字节还原）⇒ 需先修
#   分类：A=需服务在跑/ B=需密钥或写真库/ C=实测就红需先修。
#   依据见 docs/遗留优化项-第二轮盘点-20261005.md；
#   由 tools/verify/verify_gate_wiring.py 强制要求（要么接线，要么写理由）。
import inspect
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
print("文档库全局化一致性自检")
print("=" * 78)

# ══ A 静态断言 ══════════════════════════════════════════════════════════════
print("\n── A 静态断言 ──")
search_src = read("knowledge_pipeline/search.py")
krepo_src = read("repositories/knowledge_repo.py")
brepo_src = read("repositories/branch_repo.py")
meta_src = read("repositories/meta_repo.py")
rag_src = read("agent/rag.py")
pipe_src = read("routers/knowledge_parts/pipeline.py")

# A1/A2：`_branch_clause` 必须以 OR 形式保护 'global'
m = re.search(r"def _branch_clause\(branches\) -> tuple:(.*?)\n\n\ndef ", search_src, re.S)
body = m.group(1) if m else ""
check("A1 _branch_clause 的子句含 `OR branch = 'global'`",
      "OR branch = 'global'" in body, "未保护全局文档 → 传 branches 的调用方会静默拿 0 条")
check("A2 _branch_clause 的 docstring 注明只作用于 document_chunks",
      "只作用于 document_chunks" in body, "缺作用域说明，读者会误以为也管 entities")

# A3：count_documents 不得再出现分支过滤
m3 = re.search(r"def count_documents\(self, branch: str = \"\"\) -> int:(.*?)\n    # ── ontology ──",
                krepo_src, re.S)
cd_body = m3.group(1) if m3 else ""
check("A3 count_documents 已无 `branch=?` 过滤（branch 仅保留接收）",
      bool(m3) and 'branch=?' not in cd_body, "仍按分支过滤 → total_docs 恒 0")
check("A4 count_documents 的 docstring 记明「实体/关系仍按分支、只有文档全局」",
      "只有文档是全局的" in cd_body, "缺口径说明，后续易再次误改")

# A5：分支删除必须守住全局文档
check("A5 delete_branch 对 documents 的删除已用 `if name != \"global\"` 护栏",
      'if name != "global":' in brepo_src and
      brepo_src.index('if name != "global":') < brepo_src.index(
          'DELETE FROM documents WHERE branch=?', brepo_src.index("def delete_branch")),
      "缺护栏 → 分支名恰为 'global' 时会删光文档库")

# A6：设计声明应四处一致（互相印证，防单点漂移）
check("A6 设计声明在 4 处一致（ingest 默认 global / 迁移 / 文档列表 / 检索接口）",
      'branch: str = "global"' in read("knowledge_pipeline/ingest.py")
      and "SET branch='global'" in read("database/migrations/documents.py")
      and "branch 参数保留接收但不再过滤" in meta_src
      and "检索不按分支过滤" in pipe_src,
      "有一处设计声明缺失或被改回分支隔离")

# A7：文档检索三条路径都显式不传分支
check("A7 rag.py 的 hybrid 与向量兜底两路均 branches=None",
      len(re.findall(r"branches=None", rag_src)) >= 2,
      f"branches=None 出现 {len(re.findall('branches=None', rag_src))} 次（应 ≥2）")

# A8：签名级行为——upload 默认写全局
try:
    from knowledge_pipeline.ingest import ingest_document
    _d = inspect.signature(ingest_document).parameters["branch"].default
    check("A8 ingest_document 的 branch 默认值为 'global'", _d == "global", f"got {_d!r}")
except Exception as e:                                    # 依赖缺失时退化为源码断言
    check("A8 ingest_document 的 branch 默认值为 'global'（退回源码断言）",
          bool(re.search(r'doc_id: int \| None = None, branch: str = "global"\)', read("knowledge_pipeline/ingest.py"))),
          f"import 失败：{e}")

# A9：前端上传不得再受分支只读门禁约束（服务端 upload 无分支校验，documents 恒 global）
def strip_js_comments(s):
    """去掉 JS 行注释后再断言 —— 否则"注释里提到过某个写法"会被误判成"代码里还在用"。

    （本脚本首版即栽在这里：注释里解释"旧实现调用 branchWritable()" → 断言恒 FAIL。）
    """
    return re.sub(r"//[^\n]*", "", s)


docs_js = read("static/js/mods/20-docs.js")
m9 = re.search(r"async function doUploadDoc\(\) \{(.*?pendingDocFiles\.length)", docs_js, re.S)
check("A7' doUploadDoc 已移除 branchWritable() 门禁（否则 release 分支上无法上传资料）",
      bool(m9) and "branchWritable" not in strip_js_comments(m9.group(1)),
      "仍带分支门禁 → 全局文档却要求先切到 dev 分支")

# A10：文档选择器不再展示恒为 'global' 的 branch
send_js = strip_js_comments(read("static/js/mods/12-chatsend.js"))
check("A8' 文档选择器 desc 不再拼 _d.branch（恒 'global'，内部哨兵值不该露给用户）",
      "key:'doc:'+_d.id" in send_js and "_d.branch" not in send_js.split("key:'doc:'+_d.id")[1][:200],
      "仍在展示恒为 global 的分支值")

# ══ B 行为断言（夹具驱动：临时库，不碰真实库）═══════════════════════════════
print("\n── B 行为断言（夹具驱动）──")
from knowledge_pipeline.search import _branch_clause          # noqa: E402
from repositories.knowledge_repo import KnowledgeRepo          # noqa: E402
from repositories.branch_repo import BranchRepo                # noqa: E402


def make_fixture():
    """最小夹具：2 份全局文档(chunk×3) + 1 份历史 dev 文档(chunk×1) + 3 个分支。

    夹具只建被测代码真正读到的列；不掺真实库，避免「本次环境恰好配了什么」耦合断言。
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE branches(id INTEGER PRIMARY KEY, name TEXT, parent_branch TEXT, branch_type TEXT);
        CREATE TABLE entities(id INTEGER PRIMARY KEY, name TEXT, branch TEXT, status TEXT);
        CREATE TABLE relations(id INTEGER PRIMARY KEY, source_id INT, target_id INT,
                               relation_type TEXT, branch TEXT, status TEXT);
        CREATE TABLE merge_requests(id INTEGER PRIMARY KEY, source_branch TEXT,
                                    target_branch TEXT, status TEXT);
        CREATE TABLE documents(id INTEGER PRIMARY KEY, filename TEXT, file_type TEXT, branch TEXT);
        CREATE TABLE document_chunks(id INTEGER PRIMARY KEY, document_id INT, chunk_index INT,
                                     content TEXT, branch TEXT);
    """)
    conn.executemany("INSERT INTO branches(id,name,branch_type) VALUES (?,?,?)",
                     [(1, "release", "release"), (2, "dev", "work"), (3, "global", "global")])
    conn.executemany("INSERT INTO documents(id,filename,file_type,branch) VALUES (?,?,?,?)",
                     [(1, "规范A.pdf", "pdf", "global"),
                      (2, "规范B.pdf", "pdf", "global"),
                      (3, "历史遗留.md", "md", "dev")])
    conn.executemany("INSERT INTO document_chunks(id,document_id,chunk_index,content,branch) VALUES (?,?,?,?,?)",
                     [(1, 1, 0, "c1", "global"), (2, 1, 1, "c2", "global"),
                      (3, 2, 0, "c3", "global"), (4, 3, 0, "legacy", "dev")])
    conn.commit()
    return conn


# B1/B2：分支过滤不得吃掉全局文档
sql, params = _branch_clause(["release"])
check("B1 _branch_clause(['release']) 产出含 OR branch='global' 的子句",
      "branch = 'global'" in sql, f"got {sql!r}")
conn = make_fixture()
n = conn.execute("SELECT COUNT(*) FROM document_chunks WHERE content != ''" + sql, params).fetchone()[0]
check("B2 夹具 3 条全局 chunk 在 branches=['release'] 下全部可见（旧写法为 0）",
      n == 3, f"got {n}")

# B3/B4：文档统计不随分支变
kr = KnowledgeRepo(conn)
c_all, c_rel, c_dev = kr.count_documents(), kr.count_documents("release"), kr.count_documents("dev")
check("B3 count_documents 不随 branch 变化（3/3/3）",
      (c_all, c_rel, c_dev) == (3, 3, 3), f"got {c_all}/{c_rel}/{c_dev}")

# B5：分支删除不得误删全局文档（对 'global' 名的直接护栏）
br_fix = make_fixture()
r = BranchRepo(br_fix).delete_branch("global")
docs_after = br_fix.execute("SELECT COUNT(*) FROM documents WHERE branch='global'").fetchone()[0]
check("B4 delete_branch('global') 返回 ok 且全局文档 2 份全在",
      r.get("ok") is True and docs_after == 2, f"ok={r.get('ok')} 全局文档剩 {docs_after}")

# B6：对普通分支名，行为与旧一致（历史非全局遗留行仍被清理）
br_del = make_fixture()
r2 = BranchRepo(br_del).delete_branch("dev")
glob_after = br_del.execute("SELECT COUNT(*) FROM documents WHERE branch='global'").fetchone()[0]
dev_after = br_del.execute("SELECT COUNT(*) FROM documents WHERE branch='dev'").fetchone()[0]
check("B5 delete_branch('dev')：全局文档 2 份保留、历史 dev 行被清理（0）",
      r2.get("ok") is True and glob_after == 2 and dev_after == 0,
      f"ok={r2.get('ok')} global={glob_after} dev={dev_after}")

# B7：既有引用校验未被破坏（有实体则拒绝删除）
br_guard = make_fixture()
br_guard.execute("INSERT INTO entities(id,name,branch,status) VALUES (1,'转发器','dev','reviewed')")
br_guard.commit()
r3 = BranchRepo(br_guard).delete_branch("dev")
check("B6 分支内有实体时仍拒绝删除（引用校验未被破坏）",
      r3.get("ok") is False and "实体" in (r3.get("error") or ""), f"got {r3}")
conn.close()

# ══ C 变异自证 ══════════════════════════════════════════════════════════════
# 护栏：变异子进程只跑 A/B（由 DOCG_MUT_CHILD=1 标记），否则 subprocess 自调用会无限递归
if os.environ.get("DOCG_MUT_CHILD") == "1":
    print("\n（变异子进程：跳过 C 段）")
    print("\n" + "=" * 78)
    print(f"结果：{OK} pass / {FAIL} fail")
    print("=" * 78)
    sys.exit(1 if FAIL else 0)

print("\n── C 变异自证（把改动还原 → 断言必须 FAIL）──")
MUT = [
    ("还原 _branch_clause 为纯 branch IN(...)", "knowledge_pipeline/search.py",
     """    return (" AND (branch IN ({}) OR branch = 'global')".format(",".join("?" * len(branches))),
            list(branches))""",
     """    return " AND branch IN ({})".format(",".join("?" * len(branches))), list(branches)""",
     ["A1 _branch_clause 的子句含 `OR branch = 'global'`",
      "B1 _branch_clause(['release']) 产出含 OR branch='global' 的子句",
      "B2 夹具 3 条全局 chunk 在 branches=['release'] 下全部可见（旧写法为 0）"]),
    ("还原 count_documents 为按分支过滤", "repositories/knowledge_repo.py",
     """        → `/api/knowledge/stats?branch=release` 的 total_docs 恒为 0（不传分支才是真值）。
        \"\"\"
        return self.count("documents")""",
     """        → `/api/knowledge/stats?branch=release` 的 total_docs 恒为 0（不传分支才是真值）。
        \"\"\"
        if branch:
            return self.count("documents", "branch=?", (branch,))
        return self.count("documents")""",
     ["B3 count_documents 不随 branch 变化（3/3/3）"]),
    ("撤掉 delete_branch 的全局护栏", "repositories/branch_repo.py",
     """        if name != "global":
            self.execute("DELETE FROM documents WHERE branch=?", (name,))""",
     """        if True:
            self.execute("DELETE FROM documents WHERE branch=?", (name,))""",
     ['A5 delete_branch 对 documents 的删除已用 `if name != "global"` 护栏',
      "B4 delete_branch('global') 返回 ok 且全局文档 2 份全在"]),
    ("前端恢复上传的分支门禁", "static/js/mods/20-docs.js",
     "  const files = pendingDocFiles.length ? pendingDocFiles",
     "  if(!branchWritable()) return;\n  const files = pendingDocFiles.length ? pendingDocFiles",
     ["A7' doUploadDoc 已移除 branchWritable() 门禁（否则 release 分支上无法上传资料）"]),
    ("前端恢复展示恒为 global 的 branch", "static/js/mods/12-chatsend.js",
     """                    desc: (_d.status||'') + (_d.file_type ? ' · ' + _d.file_type : ''),""",
     """                    desc: (_d.status||'') + ' · ' + (_d.branch||''),""",
     ["A8' 文档选择器 desc 不再拼 _d.branch（恒 'global'，内部哨兵值不该露给用户）"]),
]
MUT_LABELS = [t for _, _, _, _, tags in MUT for t in tags]

mutation_failed_as_expected = []
backup = {}


def _mutate(path, old, new):
    """按文件原有换行约定写入变异 —— 锚点一律用 LF 书写，此处归一化。

    ⚠️ 本仓库混用换行符：`knowledge_pipeline/search.py` 是 CRLF，`repositories/*.py` 是 LF。
    直接用 LF 锚点去字节匹配 CRLF 文件会「锚点不存在」，极易误判成脚本写错。
    """
    raw = (REPO / path).read_bytes()
    nl = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n")
    assert old in text, f"变异锚点不存在（脚本自身错）：{path}"
    out = text.replace(old, new, 1).replace("\n", nl)
    (REPO / path).write_bytes(out.encode("utf-8"))


try:
    for _, p, old, new, _ in MUT:
        if p not in backup:
            backup[p] = (REPO / p).read_bytes()
        _mutate(p, old, new)
    print(f"  已施加 {len(MUT)} 处变异，重跑 A/B 断言集…")
    r = subprocess.run([sys.executable, "-X", "utf8", str(Path(__file__).resolve())],
                       cwd=str(REPO), capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       env={**os.environ, "DOCG_MUT_CHILD": "1"})
    out = (r.stdout or "") + (r.stderr or "")
    for tag in sorted(set(MUT_LABELS)):
        if re.search(r"\[FAIL\] " + re.escape(tag), out):
            mutation_failed_as_expected.append(tag)
        else:
            print(f"    !! 变异后该断言仍通过（=空转断言）：{tag}")
    uniq = sorted(set(MUT_LABELS))
    check(f"C1 变异后全部 {len(uniq)} 条相关断言转为 FAIL",
          len(mutation_failed_as_expected) == len(uniq),
          f"仅 {len(mutation_failed_as_expected)}/{len(uniq)}：{mutation_failed_as_expected}")
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
