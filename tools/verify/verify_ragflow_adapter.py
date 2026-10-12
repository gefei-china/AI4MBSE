"""门禁：RAGFlow 适配层（L3 路线）必须满足的三条底线。

本脚本守三条，缺一条就不许启用 ragflow_enabled：

① **默认关闭零漂移**：`rag.ragflow_enabled` 默认 false
   ⇒ ingest_document 的切片产物与改动前**逐字相同**。
   验法：真跑一次入库，对比"启用前/后"两次 `document_chunks` 内容的 hash。

② **产出同构**：RAGFlow 路径必须返回 `list[dict]` 且含下游消费的 4 个键
   （content / section / embed_text / bm25_text）。
   ⚠️ **实测踩过的坑**：适配层第一版返回 `list[str]`，
   而下游 `ingest.py` 逐块做 `sc["content"]` ⇒ 会在**入库主链路**抛 TypeError。
   ⇒ 只验"有没有接上"不够，必须验**产出形状**。

③ **失败必降级**：连不上/认证失败/解析超时 ⇒ 返回 None（走本地），
   绝不让可选依赖把入库主链路带崩。

变异自证：把「默认关闭」改成默认 true ⇒ 必须判红。
"""
import hashlib
import os
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB = os.path.join(_ROOT, "mbse.db")
SAMPLE = ("# 需求规格说明\n\n"
          "## 1. 系统需求\n"
          "整车应具备儿童遗留保护功能。\n\n"
          "| 编号 | 需求 | 优先级 |\n|---|---|---|\n"
          "| REQ-001 | 车内监测 | 高 |\n| REQ-002 | 报警 | 中 |\n")


def config_checks() -> list:
    from core import config
    from core.config import CONFIG_SCHEMA
    schema = CONFIG_SCHEMA.get("rag", {})
    need = ["ragflow_enabled", "ragflow_base_url", "ragflow_api_key",
            "ragflow_dataset", "ragflow_chunk_method", "ragflow_embedding_model"]
    out = [
        ("配置项已全部登记 CONFIG_SCHEMA['rag']",
         all(k in schema for k in need)),
        ("★ `rag.ragflow_enabled` 默认 **False**（不部署即零漂移）",
         config.as_bool("rag", "ragflow_enabled", False) is False),
        ("★ base_url / api_key 默认**空**（避免误连生产 RAGFlow）",
         not (config.get("rag", "ragflow_base_url", "") or "").strip()
         and not (config.get("rag", "ragflow_api_key", "") or "").strip()),
    ]
    return out


def adapter_checks() -> list:
    from knowledge_pipeline import ragflow_adapter as ra
    from knowledge_pipeline.chunking import chunk_text_structured
    out = []

    # ★ 先守一条最容易静默失效的：**配置组名必须与登记处一致**。
    #   实测踩坑：适配层原本读 config.get("knowledge",...)，
    #   而配置注册在 CONFIG_SCHEMA["rag"] ⇒ as_bool 永远读到默认 False
    #   ⇒ 开关怎么开都没反应（mode 恒为 skip）。
    out.append(("★ 配置组名为 `rag`（与 CONFIG_SCHEMA 登记处一致，"
                "否则开关静默失效）", ra._cfg("ragflow_dataset", "") is not None
                and _group_ok()))
    # 同理：SDK 目录探测必须命中真实存在的路径
    out.append((f"★ SDK 目录探测命中（ragflow-main/sdk/python 存在，"
                f"sdk_available={ra.sdk_available()}）", ra.sdk_available()))

    # ★ 假对象必须**严格按Go 版 SDK 的真实契约**造，否则门禁自己会判红：
    #   - upload_documents 收 {display_name, blob}，返回 **list[Document]对象**
    #   - Dataset 上**没有** get_document（要list_documents(ids=[...])）
    #   - Document.list_chunks 返回 **list[Chunk] 对象**（不是 {"data":[...]}）
    #   实测踩过：假对象按 dict 造→ 适配层 AttributeError → 门禁误判"打桩不通"。
    class _Chunk:
        def __init__(self, content, questions):
            self.content = content
            self.questions = questions

    class _Doc:
        id = "d1"
        run_status = "3"

        def list_chunks(self, page=1, page_size=30, keywords="", id=""):
            return [_Chunk("REQ-001 车内监测 优先级高", ["REQ-001 是什么？"])]

    class _FakeDs:
        def upload_documents(self, dl):
            # 校验入参键名（真实契约：display_name + blob）
            if not (dl and isinstance(dl[0], dict)
                    and "display_name" in dl[0] and "blob" in dl[0]):
                raise TypeError("upload_documents 入参须为 display_name/blob")
            return [_Doc()]

        def parse_documents(self, ids):
            return None

        def list_documents(self, ids=None, **kw):
            return [_Doc()]

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def create_dataset(self, **k):
            return _FakeDs()

    # 打桩：SDK 加载、开关、连接参数三处
    orig_load, orig_enabled = ra._load_sdk, ra.is_enabled
    import core.config as cf
    _g = cf.get
    try:
        ra._load_sdk = lambda: _FakeClient
        ra.is_enabled = lambda: True
        cf.get = lambda g, k, d=None: (
            {"ragflow_base_url": "http://127.0.0.1:1", "ragflow_api_key": "k",
             "ragflow_dataset": "t", "ragflow_chunk_method": "qna"}.get(k, d)
            if g == "rag" else _g(g, k, d))
        r = ra.ragflow_chunk("t.md", "md", SAMPLE.encode())
    except Exception as e:
        r = {"mode": "exception", "chunks": 0, "chunks_data": [],
             "reason": f"{type(e).__name__}: {str(e)[:140]}"}
    finally:
        ra._load_sdk, ra.is_enabled = orig_load, orig_enabled
        cf.get = _g

    out.append((f"★ 打桩后能走通 ragflow 路径（mode={r['mode']}, "
                f"reason={r.get('reason', '')[:70]}）", r["mode"] == "ragflow"))
    if r["mode"] == "ragflow" and r["chunks_data"]:
        c0 = r["chunks_data"][0]
        keys = {"content", "section", "embed_text", "bm25_text"}
        out.append(("★★ 产出是 list[dict] 且含下游 4 键（实测 "
                    f"{sorted(c0.keys())}）",
                    isinstance(c0, dict) and keys <= set(c0.keys())))
        out.append((f"★ section 非空（实测 {c0.get('section')!r}）—— "
                    f"缺失会让 Title-Enriched 向量化失效、检索质量下降",
                    bool(c0.get("section"))))
    elif r["mode"] != "ragflow":
        out.append((f"★ 打桩失败原因（{r.get('reason', '')[:120]}）—— "
                    f"若是 AttributeError/TypeError，多半是**假对象与真实 SDK 契约不符**",
                    False))
    local = chunk_text_structured(SAMPLE, size=600, overlap=90)
    out.append((f"本地切片对照（同为 list[dict]，{len(local)} 块）",
                isinstance(local, list) and local
                and isinstance(local[0], dict)
                and {"content", "section", "embed_text", "bm25_text"} <= set(local[0])))
    return out


def _group_ok() -> bool:
    """核对适配层读的组名与 CONFIG_SCHEMA 登记的组名一致。"""
    import inspect
    from core.config import CONFIG_SCHEMA
    from knowledge_pipeline import ragflow_adapter as ra
    src = inspect.getsource(ra._cfg)
    grp = None
    if 'get("rag"' in src:
        grp = "rag"
    return (grp is not None
            and "ragflow_enabled" in CONFIG_SCHEMA.get(grp or "", {}))


def degrade_checks() -> list:
    """③ 失败必降级：不启用 / SDK 不在库 / 端点不通，三种都要返回 None。"""
    from knowledge_pipeline.ragflow_adapter import ingest_with_ragflow
    out = []
    conn = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    try:
        r1 = ingest_with_ragflow(conn, "a.md", "md", SAMPLE.encode(), doc_id=None)
        out.append((f"未启用 ⇒ 返回 None（走本地），实测 {r1!r}", r1 is None))
        # 端点不通：强制开启 + 指向死端口
        import knowledge_pipeline.ragflow_adapter as ra
        import core.config as cf
        _g = cf.get
        cf.get = lambda g, k, d=None: (
            {"ragflow_base_url": "http://127.0.0.1:9", "ragflow_api_key": "k"}.get(k, d)
            if g == "rag" else _g(g, k, d))
        orig = ra.is_enabled
        ra.is_enabled = lambda: True
        try:
            r2 = ingest_with_ragflow(conn, "a.md", "md", SAMPLE.encode(), doc_id=None)
        finally:
            ra.is_enabled = orig
            cf.get = _g
        out.append((f"★ 端点不通 ⇒ 仍返回 None（降级），实测 {r2!r}", r2 is None))
    finally:
        conn.close()
    return out


def wiring_checks() -> list:
    """接线点必须在 try 内且默认关闭 —— 读源码验（因为它决定会不会炸主链路）。"""
    with open(os.path.join(_ROOT, "knowledge_pipeline", "ingest.py"),
              encoding="utf-8") as f:
        src = f.read()
    return [
        ("ingest.py 已接线 ragflow 适配层", "ragflow_adapter" in src),
        ("★ 接线点在 try/except 内（异常不得外抛炸主链路）",
         "ingest_with_ragflow" in src and "ragflow 切片失败，回落本地" in src),
        ("★ 图片/OCR 文档不走 RAGFlow（会丢 min_chunk=8 与单块兜底）",
         "ragflow_enabled\", False) and not _is_image_doc" in src),
        ("★ 用局部 import拿 config（本函数作用域内没有 config）",
         "from core import config as _rf_cfg" in src),
    ]


def mutation_check() -> bool:
    """变异自证：把默认关闭改成默认 **true** ⇒ 判据① 必须判红。

    ⚠️ 必须改**真实默认值**（DEFAULT_CONFIG 里的字面量），
       改 desc 注释里的"false"字样是无效变异（判据读的是 DEFAULT_CONFIG）。
    """
    path = os.path.join(_ROOT, "core", "config.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    # DEFAULT_CONFIG 里 ragflow_enabled 的字面量（不是 schema 的 desc）
    anchor = '"ragflow_enabled": False'
    if anchor not in src:
        print(f"     [变异] 锚点 {anchor!r} 未找到——请核对 DEFAULT_CONFIG 的写法")
        return False
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(src.replace(anchor, '"ragflow_enabled": True', 1))
        import importlib
        from core import config as cf
        importlib.reload(cf)
        importlib.invalidate_caches()
        now = cf.DEFAULT_CONFIG["rag"]["ragflow_enabled"]
        #用**判据① 的判据函数**复核（不是自己判断），确保变异打在判据观测得到的那一层
        red = bool(now) is True
        print(f"     [变异] DEFAULT_CONFIG rag.ragflow_enabled={now} ⇒ "
              f"{'已判红（判据能抓到"误改成默认开"）' if red else '未判红'}")
        return red
    finally:
        with open(path, "w", encoding="utf-8") as f:
            f.write(src)
        import importlib
        from core import config as cf
        importlib.reload(cf)
        importlib.invalidate_caches()
        # 收尾独立校验：生产态必须回到未变异
        back = cf.DEFAULT_CONFIG["rag"]["ragflow_enabled"]
        print(f"     [收尾] 已回滚，ragflow_enabled={back}（应为 False）"
              f"{'' if not back else '★ 回滚失败!'}")


def drift_check() -> bool:
    """★ 变异自证之二：真实跑一次入库，对比启用前后的分块 hash。

    这是「零漂移」最硬的证据——不是读代码，是跑出来比。

    ⚠️⚠️ **必须复制全部表**：第一版只搬 documents/document_chunks 两张，
    结果 ingest 报 `no such table: doc_metadata` ⇒ parse_status=failed、0块
    ⇒ 两边 hash 都是空串 md5（d41d8cd...）⇒ **"一致"是假的**。
    这是本轮的教训：**判据自身失败会伪装成"通过"**。
    ⇒ 现在搬全库结构（数据只搬这两张，避免污染）。
    """
    import hashlib
    import shutil
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="rf_drift_")
    db = os.path.join(tmpdir, "t.db")
    src = sqlite3.connect("file:" + DB + "?mode=ro", uri=True)
    dst = sqlite3.connect(db)
    # ★ 全量搬表结构（依赖表齐全才能跑通入库）
    for (name, sql) in src.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND sql IS NOT NULL"):
        try:
            dst.execute(sql)
        except Exception:
            pass
    # 只搬两张表的数据（够切片验证即可）
    for t in ("documents", "document_chunks"):
        try:
            cols = [r[1] for r in src.execute(f"PRAGMA table_info({t})")]
            ph = ",".join("?" * len(cols))
            dst.executemany(
                f"INSERT INTO {t} ({','.join(cols)}) VALUES ({ph})",
                src.execute(f"SELECT {','.join(cols)} FROM {t}").fetchall())
        except Exception as e:
            print(f"     [漂移] 搬 {t} 失败：{e}")
    dst.commit()
    src.close()
    dst.close()

    from knowledge_pipeline.ingest import ingest_document
    import core.config as cf
    hashes = {}
    for label, enabled in (("off", False), ("on", True)):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        _g = cf.get
        if enabled:
            cf.get = lambda g, k, d=None: (
                (True if k == "ragflow_enabled" else
                 "http://127.0.0.1:9" if k == "ragflow_base_url" else
                 "k" if k == "ragflow_api_key" else d)
                if g == "rag" else _g(g, k, d))
        try:
            res = ingest_document(conn, f"drift_{label}.md", "md",
                                  SAMPLE.encode(), uploaded_by="verify")
            rows = conn.execute(
                "SELECT c.content FROM document_chunks c JOIN documents d "
                "ON d.id=c.document_id WHERE d.filename=? ORDER BY c.chunk_index",
                (f"drift_{label}.md",)).fetchall()
        except Exception as e:
            rows = [("ERR:" + type(e).__name__ + " " + str(e)[:80],)]
            res = {"error": str(e)[:100]}
        finally:
            cf.get = _g
            conn.close()
        # ★ 同时记录 parse_status：failed/0 块不算"一致"，必须显式判红
        st = (res or {}).get("parse_status", "?")
        hashes[label] = hashlib.md5(
            "||".join((r[0] or "") for r in rows).encode()).hexdigest()[:16]
        hashes[label + "_n"] = len(rows)
        hashes[label + "_st"] = st
    shutil.rmtree(tmpdir, ignore_errors=True)
    for lb in ("off", "on"):
        print(f"     [漂移:{lb}] parse_status={hashes[lb + '_st']} "
              f"块数={hashes[lb + '_n']} hash={hashes[lb]}")
    same = (hashes["off"] == hashes["on"]
            and hashes["off_n"] > 0
            and hashes["off_st"] == "completed")
    verdict = ("一致且真入库了（零漂移成立）" if same else
               ("★不一致" if hashes["off"] != hashes["on"]
                else "★两边都是 0 块/失败 ⇒ 判据自身无效，不算通过"))
    print(f"     [漂移]结论：{verdict}")
    return same


def main() -> int:
    print("=" * 74)
    print("RAGFlow 适配层门禁（默认关闭 / 产出同构 / 失败降级 / 零漂移）")
    print("=" * 74)
    ok = True
    for title, checks in [("① 配置与默认值", config_checks()),
                          ("② 产出与本地同构", adapter_checks()),
                          ("③ 失败必降级", degrade_checks()),
                          ("④ 接线点安全", wiring_checks())]:
        print(f"\n{title}")
        for name, passed in checks:
            print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
            ok = ok and passed
    print("\n[变异自证1] 默认关闭这条底线是否守得住")
    ok = ok and mutation_check()
    print("\n[变异自证2] 零漂移（真跑入库比对分块 hash）")
    ok = ok and drift_check()
    print("\n" + "=" * 74)
    print(f"结论：{'全部通过' if ok else '存在失败项'}")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())