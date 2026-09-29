# -*- coding: utf-8 -*-
"""LLM 真摘要 + 图片入库兜底 验证（2026-09-29，两大痛点修复的证明脚本）。

背景（实测取证）：
  痛点1 图片解析：OCR 本身可用（质量分 avg 0.89），但 OCR 文本普遍 <50 字且无句末标点，
        被 ingest 分块阶段 min_chunk=50 阈值整份丢弃 → parse_status=failed / error="文本为空"。
        生产库图片文档数为 0 —— 从未成功过。修法：图片文档阈值放宽为 8 + 极短 OCR 兜底单块。
  痛点2 假摘要：preview 的 summary = 前 2 块截断 260 字（meta.py 原_L371），全库无任何 LLM 摘要。
        修法：入库尾部生成 LLM 真摘要写 documents.summary；preview 优先读，空则回退旧行为。

本脚本证明五件事：
  M. 迁移：新库含 summary 列（建表+迁移双路径）；插入缺省默认 ''；最小基表跑真迁移能补列。
  A. 图片：35 字 OCR 正常入库（走放宽阈值路径，chunk 无前缀）；
     极短 OCR（<8 字）走兜底单块（【图片OCR】前缀）；不再出现"文本为空"误失败。
  B. 摘要：入库生成 LLM 摘要落列；preview 优先读（summary_source='llm'）；
     列空回退旧行为（'truncation'）；LLM 挂掉不阻断入库；长文走 map-reduce。
  X. 变异测试 ×4：还原"真实坏行为"（图片修复整体掐断 / 仅兜底掐断 / preview 无视真摘要 /
     入库不写摘要），对应断言必须 FAIL——证明断言非空转。

纪律：夹具建在数据源侧（MBSE_DB_PATH 临时库，子进程 -B 禁 pyc）；变异前后字节级 diff 校验还原；
      pycache 先清后跑（py_compile 会写陈旧 pyc）；子进程锚点命中 != 1 判失败。
"""
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


# ────────────────────────── 子进程侧（--run） ──────────────────────────

def _setup_tmp_db():
    """临时库：必须在任何 app import 之前设置 MBSE_DB_PATH（conftest 同款机制）。"""
    import tempfile
    d = tempfile.mkdtemp(prefix="mbse_verify_sumimg_")
    db = os.path.join(d, "verify.db")
    os.environ["MBSE_DB_PATH"] = db
    return d, db


def _patch_llm(boom=False):
    """as_bool: force_mock=False（让摘要路径真跑）；llm_client = Fake（离线确定性）。"""
    import core.config as cc
    _orig = cc.as_bool
    cc.as_bool = lambda sec, key, dflt=False: (
        False if (sec == "llm" and key == "force_mock") else _orig(sec, key, dflt))
    import llm as llm_mod

    class FakeLLM:
        calls = []

        def chat(self, messages, **kw):
            if boom:
                raise RuntimeError("fake llm down")
            user = messages[1]["content"]
            FakeLLM.calls.append(user[:40])
            if "合并为一段" in user:
                return {"choices": [{"message": {"content": "整篇摘要：热管理系统冷却回路设计规范，涵盖水泵选型与流量要求。"}}]}
            return {"choices": [{"message": {"content": "该文档定义了电池热管理系统的冷却回路设计与流量要求。"}}]}

    llm_mod.llm_client = FakeLLM()
    return llm_mod


def phase_migration():
    d, db = _setup_tmp_db()
    try:
        from database.schema import init_db
        from database.connection import get_db
        init_db()
        conn = get_db()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()]
        ck("M1_summary_col_in_fresh_init", "summary" in cols, cols[:5])
        conn.execute("INSERT INTO documents (filename) VALUES ('缺省插入.docx')")
        v = conn.execute("SELECT summary FROM documents WHERE filename='缺省插入.docx'").fetchone()[0]
        ck("M2_insert_without_summary_defaults_empty", v == "", repr(v))
        conn.close()

        # 最小基表（模拟老库形状）+ 跑真迁移 —— 不手抄 _add 逻辑
        import sqlite3
        db2 = os.path.join(d, "old.db")
        c2 = sqlite3.connect(db2)
        c2.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, filename TEXT)")
        from database.migrations.columns import _migrate_columns
        _migrate_columns(c2)
        cols2 = [r[1] for r in c2.execute("PRAGMA table_info(documents)").fetchall()]
        ck("M3_min_base_table_real_migration_adds_col", "summary" in cols2, cols2)
        c2.execute("INSERT INTO documents (filename) VALUES ('x')")
        v2 = c2.execute("SELECT summary FROM documents WHERE filename='x'").fetchone()[0]
        ck("M4_insert_after_migration_defaults_empty", v2 == "", repr(v2))
        c2.close()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def phase_image():
    d, db = _setup_tmp_db()
    try:
        from database.schema import init_db
        from database.connection import get_db
        init_db()
        import knowledge_pipeline.ingest as ing
        ing._save_source_copy = lambda *a, **k: None          # 不往仓库 data/uploads 落文件
        _patch_llm(boom=True)                                  # 图片阶段摘要必须离线自愈（不触真 LLM）
        # mock extract：35 字 OCR（>=8 且 <50 → 走"放宽阈值"路径）
        OCR35 = "电池热管理系统的冷却回路主要由水泵三通阀与散热器组成箭头标示水流方向"
        meta_img = {"ok": True, "kind": "image", "reason": "",
                    "ocr": {"enabled": True, "available": True, "pages_total": 1,
                            "pages_ocr": 1, "chars_ocr": len(OCR35), "elapsed": 0.1, "note": "mock"}}
        ing.extract_text_ex = lambda fn, content: (OCR35, dict(meta_img)) if "回路" in fn \
            else ("登录界面", {"ok": True, "kind": "image", "reason": "", "ocr": dict(meta_img["ocr"], chars_ocr=4)})

        conn = get_db()
        r1 = ing.ingest_document(conn, "回路图.png", "png", b"\x89PNG-fake")
        ck("A1_img35_completed", r1.get("parse_status") == "completed", r1.get("error"))
        row = conn.execute("SELECT content FROM document_chunks WHERE document_id=? ORDER BY chunk_index",
                           (r1["doc_id"],)).fetchall()
        ck("A2_img35_no_prefix_relax_path",
           len(row) >= 1 and row[0][0] == OCR35,
           (row[0][0][:30] + "…") if row else "no-chunks")

        # 极短 OCR（4 字 < 8）→ 全部片段被丢 → 兜底单块（【图片OCR】前缀）
        r2 = ing.ingest_document(conn, "登录页.png", "png", b"\x89PNG-fake2")
        ck("A3_img6_fallback_single_chunk",
           r2.get("parse_status") == "completed",
           r2.get("error"))
        row2 = conn.execute("SELECT content FROM document_chunks WHERE document_id=? ORDER BY chunk_index",
                            (r2["doc_id"],)).fetchall()
        ck("A4_fallback_wrapped_with_prefix",
           len(row2) == 1 and row2[0][0].startswith("【图片OCR】登录页.png"),
           row2[0][0][:40] if row2 else "no-chunks")
        conn.close()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def phase_summary():
    d, db = _setup_tmp_db()
    try:
        from database.schema import init_db
        from database.connection import get_db
        init_db()
        import knowledge_pipeline.ingest as ing
        ing._save_source_copy = lambda *a, **k: None
        llm_mod = _patch_llm(boom=False)
        CANNED = "该文档定义了电池热管理系统的冷却回路设计与流量要求。"

        conn = get_db()
        md_text = "# 热管理系统冷却回路设计规范\n\n" + \
            "冷却回路需保证电芯温差不超过5摄氏度，水泵流量按40L/min设计，" \
            "散热器迎风面积与风道阻力按整机热平衡校核。\n" * 9
        r1 = ing.ingest_document(conn, "冷却回路规范.md", "md", md_text.encode("utf-8"))
        ck("B1_ingest_completed", r1.get("parse_status") == "completed", r1.get("error"))
        ck("B2_summary_generated_true", r1.get("summary_generated") is True, r1.get("summary_generated"))
        dbsum = conn.execute("SELECT summary FROM documents WHERE id=?", (r1["doc_id"],)).fetchone()[0]
        ck("B3_db_summary_is_llm_text", dbsum == CANNED, dbsum[:40])

        from routers.meta import get_document_preview
        out = get_document_preview(r1["doc_id"], conn=conn)
        ck("B4_preview_reads_llm_summary",
           out.get("summary") == CANNED and out.get("summary_source") == "llm",
           (out.get("summary", "")[:30], out.get("summary_source")))

        # 旧文档形态：列空 → 回退「前2块截断」旧行为
        conn.execute("UPDATE documents SET summary='' WHERE id=?", (r1["doc_id"],))
        conn.commit()
        out2 = get_document_preview(r1["doc_id"], conn=conn)
        ck("B5_preview_fallback_when_empty",
           out2.get("summary_source") == "truncation" and 0 < len(out2.get("summary", "")) <= 261
           and out2.get("summary") != CANNED,
           (out2.get("summary_source"), len(out2.get("summary", ""))))

        # LLM 挂掉：入库不受阻，summary 留空，preview 自动回退
        class BoomLLM:
            def chat(self, messages, **kw):
                raise RuntimeError("fake llm down")
        llm_mod.llm_client = BoomLLM()
        text2 = "# 备件清单\n\n" + "水泵备件包括机械密封与叶轮，更换周期按运行小时数确定。\n" * 5
        r2 = ing.ingest_document(conn, "备件清单.md", "md", text2.encode("utf-8"))
        db2 = conn.execute("SELECT summary FROM documents WHERE id=?", (r2["doc_id"],)).fetchone()[0]
        ck("B6_llm_fail_ingest_ok_summary_empty",
           r2.get("parse_status") == "completed" and r2.get("summary_generated") is False and db2 == "",
           (r2.get("parse_status"), r2.get("summary_generated"), db2[:20]))
        out3 = get_document_preview(r2["doc_id"], conn=conn)
        ck("B6b_preview_fallback_after_fail", out3.get("summary_source") == "truncation", out3.get("summary_source"))

        # 长文 map-reduce：清洗后 >4000 字 → 4 段 map + 1 次 reduce
        calls = []

        class CountLLM:
            def chat(self, messages, **kw):
                user = messages[1]["content"]
                calls.append(user[:40])
                if "合并为一段" in user:
                    return {"choices": [{"message": {"content": "整篇摘要：热管理系统冷却回路设计规范，涵盖水泵选型与流量要求。"}}]}
                return {"choices": [{"message": {"content": "该文档定义了电池热管理系统的冷却回路设计与流量要求。"}}]}

        llm_mod.llm_client = CountLLM()
        base = "电池包浸没式冷却相比冷板方案的均温性提升约30%，但成本与维护复杂度显著上升，需要综合评估量产可行性。"
        long_text = "# 浸没式冷却评估\n\n" + base * 120      # ~5300 字
        r3 = ing.ingest_document(conn, "浸没式评估.md", "md", long_text.encode("utf-8"))
        db3 = conn.execute("SELECT summary FROM documents WHERE id=?", (r3["doc_id"],)).fetchone()[0]
        ck("B7_long_doc_map_reduce_runs",
           r3.get("summary_generated") is True and len(calls) >= 2
           and any("/4 段" in c for c in calls) and db3.startswith("整篇摘要"),
           (len(calls), db3[:30]))
        conn.close()
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ────────────────────────── 编排侧（变异测试） ──────────────────────────

_PURGE_MODULES = [
    ("knowledge_pipeline", "ingest"),
    ("routers", "meta"),
    ("database", "schema"),
    (os.path.join("database", "migrations"), "columns"),
]


def _purge_pycache():
    """只清被改模块的陈旧 .pyc（子进程 -B 不写 pyc；py_compile 会写）。
    不整目录 rmtree：目录文件数会触发批量删除守卫，且逐文件清除已足够。"""
    for pkg, mod in _PURGE_MODULES:
        pc = os.path.join(_ROOT, pkg, "__pycache__")
        if not os.path.isdir(pc):
            continue
        for fn in os.listdir(pc):
            if fn.startswith(mod + ".") and fn.endswith(".pyc"):
                try:
                    os.remove(os.path.join(pc, fn))
                except OSError:
                    pass


def _run_child(phase):
    import subprocess
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("MBSE_DB_PATH", None)  # 子进程自设
    r = subprocess.run([sys.executable, "-B", os.path.abspath(__file__), "--run", phase],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=_ROOT, env=env, timeout=600)
    fails = re.findall(r"\[FAIL\] (\S+)", r.stdout or "")
    passes = re.findall(r"\[PASS\] (\S+)", r.stdout or "")
    return fails, passes, r


_MUTS = [
    dict(tag="X1_图片修复整体掐断", file="knowledge_pipeline/ingest.py",
         old='_is_image_doc = (parse_meta.get("kind") == "image")',
         new="_is_image_doc = False",
         phase="image", expect=["A1", "A3"], keep=[]),
    dict(tag="X2_仅兜底单块掐断", file="knowledge_pipeline/ingest.py",
         old="if not structured and _is_image_doc and text.strip():",
         new="if False and _is_image_doc and text.strip():",
         phase="image", expect=["A3"], keep=["A1", "A2"]),
    dict(tag="X3_preview无视真摘要", file="routers/meta.py",
         old='_real = (row["summary"] or "").strip()',
         new='_real = ""',
         phase="summary", expect=["B4"], keep=[]),
    dict(tag="X4_入库不写摘要", file="knowledge_pipeline/ingest.py",
         old="_summary = _gen_llm_summary(text, filename)",
         new='_summary = ""',
         phase="summary", expect=["B2", "B3"], keep=[]),
]


def orchestrate():
    import filecmp
    import shutil

    print("== 基线（migration + image + summary）==")
    _purge_pycache()
    all_fails = []
    for ph in ("migration", "image", "summary"):
        fails, passes, r = _run_child(ph)
        all_fails += fails
        print("  phase %-9s PASS=%d FAIL=%d" % (ph, len(passes), len(fails)))
        if r.returncode != 0 and not fails:
            print("  [FAIL] 子进程异常退出 rc=%s\n%s\n%s" % (r.returncode, r.stdout[-800:], r.stderr[-800:]))
            all_fails.append("child_crash_" + ph)
    base_ok = not all_fails
    print("基线结果:", "全绿" if base_ok else ("FAIL: %s" % all_fails))

    mut_results = []
    for m in _MUTS:
        src = os.path.join(_ROOT, m["file"])
        with open(src, "rb") as f:
            orig_bytes = f.read()
        orig_text = orig_bytes.decode("utf-8")
        n_hit = orig_text.count(m["old"])
        if n_hit != 1:
            mut_results.append((m["tag"], False, "锚点命中 %d 次（必须=1）" % n_hit))
            continue
        bak = src + ".mutbak"
        shutil.copy2(src, bak)
        try:
            with open(src, "w", encoding="utf-8", newline="") as f:
                f.write(orig_text.replace(m["old"], m["new"]))
            _purge_pycache()
            fails, passes, r = _run_child(m["phase"])
            got_fail = [x for x in m["expect"] if any(f.startswith(x) for f in fails)]
            kept = [x for x in m["keep"] if any(p.startswith(x) for p in passes)]
            ok = (len(got_fail) == len(m["expect"])) and (len(kept) == len(m["keep"]))
            detail = "expect_fail=%s got=%s keep_pass=%s got=%s" % (m["expect"], got_fail, m["keep"], kept)
            mut_results.append((m["tag"], ok, detail))
            print("  变异 %-22s %s | %s" % (m["tag"], "CAUGHT" if ok else "LEAKED!!!", detail))
        finally:
            os.replace(bak, src)
            with open(src, "rb") as f:
                restored = f.read()
            if restored != orig_bytes:
                print("  [FATAL] 还原校验失败: %s —— 请从 git 恢复该文件！" % m["file"])
                mut_results.append((m["tag"] + "_RESTORE", False, "字节不一致"))
            _purge_pycache()

    print("\n== 总结 ==")
    ok_all = base_ok and all(ok for _, ok, _ in mut_results)
    print("基线:", "PASS" if base_ok else "FAIL", "| 变异:",
          ", ".join("%s=%s" % (t.split("_")[0], "CAUGHT" if ok else "LEAK") for t, ok, _ in mut_results))
    return 0 if ok_all else 1


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--run":
        ph = sys.argv[2]
        {"migration": phase_migration, "image": phase_image, "summary": phase_summary}[ph]()
        print("\n[%s] PASS=%d FAIL=%d" % (ph, len(PASS), len(FAIL)))
        for n in FAIL:
            print("  FAIL-detail:", n)
        return 0 if not FAIL else 1
    return orchestrate()


if __name__ == "__main__":
    sys.exit(main())
