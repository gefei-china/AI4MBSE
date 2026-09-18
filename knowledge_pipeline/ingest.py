"""文档入库管道：extract_doc_title/_save_source_copy/_generate_hyde_questions/chunking_params/ingest_document。"""
import json
import logging
import os
import re
import time
import uuid
from .extract import extract_text
from .chunking import chunk_text_structured, _is_complete_sentence
from .embedder import Embedder

logger = logging.getLogger(__name__)

# 项目根目录（拆包后 __file__ 位于 knowledge_pipeline/ 子目录，须上溯一级）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def extract_doc_title(filename: str, text: str) -> str:
    """自动提取文档自身标题（用户需求：标题应是文件自己的标题，非固定值）。

    优先级：Markdown 首个 # 标题 → 文档首行（非空、长度<60）→ 空（前端回落文件名）。
    """
    text = (text or "").strip()
    if not text:
        return ""
    for line in text.split("\n")[:20]:
        line = line.strip()
        if line.startswith("# "):
            return line.lstrip("# ").strip()[:80]
    first = text.split("\n")[0].strip()
    if first and len(first) <= 60 and not first.startswith(("=", "-", "*", "|")):
        return first[:80]
    return ""


def _save_source_copy(doc_id: int, filename: str, content) -> None:
    """保存源文件副本（供预览）到 data/uploads/（失败不阻断主流程）。"""
    try:
        import os
        if isinstance(content, str):
            content = content.encode("utf-8", errors="replace")
        base = os.path.join(_PROJECT_ROOT, "data", "uploads")
        os.makedirs(base, exist_ok=True)
        safe = re.sub(r"[^\w.\-\u4e00-\u9fa5]", "_", filename) or "doc"
        path = os.path.join(base, f"{doc_id}_{safe}")
        with open(path, "wb") as f:
            f.write(content)
    except Exception as e:
        logger.warning("源文件副本保存失败（不阻断主流程）: %s", e)


def _generate_hyde_questions(content: str, section: str = "") -> list:
    """P2-1 Reverse HyDE：为 chunk 生成假设问题（用户可能怎么问这段话）→ 查询时问题对问题匹配。

    规则启发式（零外部依赖，确定性）：
    - 提取核心术语（SysML 关键字/领域词/章节词）
    - 组装 2-3 种问法（是什么/怎么做/定义是）
    索引期开销低，查询期无 LLM 调用（对齐 Anyscale Reverse HyDE 的索引侧方案）。
    v2：长度门槛 20→10，术语表扩充（MBSE 领域），section 参与度提高（无术语时用 section+首句兜底）。
    """
    if not content or len(content) < 10:
        return []
    text = content[:120]
    questions = []
    terms = []
    # SysML 关键字 + MBSE 领域词（v2 扩充）
    for kw in ("part def", "requirement", "package", "attribute", "连接", "约束", "需求",
               "部件", "功能", "导入", "端口", "状态", "动作", "SysML", "语法",
               "V波段", "宽带", "转发器", "载荷", "热管理", "温度", "设计", "规范", "错误", "修正",
               "接口", "系统", "验证", "模型", "活动", "场景", "验收", "指标", "参数", "性能",
               "可靠性", "安全性", "架构", "权衡", "基线", "配置项", "追溯", "覆盖率",
               "测试用例", "仿真", "试验", "质量", "成本", "风险", "标准", "需求规格",
               "变体", "variant", "上下文", "用例", "活动图", "状态机", "序", "模块"):
        if kw.lower() in content.lower() and kw not in terms:
            terms.append(kw)
        if len(terms) >= 3:
            break
    if section:
        terms.insert(0, section.strip()[:30])
    if terms:
        questions.append(f"什么是{terms[0]}？")
        if len(terms) > 1:
            questions.append(f"{terms[0]}和{terms[1]}有什么关系？")
        questions.append(f"{terms[0]}怎么使用？")
    # 无术语兜底：直接问内容首句（v2：优先取首个完整句，而非裸截断）
    if not questions:
        m = re.search(r"^(.{8,60}?[。！？；])", text)
        questions.append((m.group(1) if m else text[:40]) + "？")
    return questions[:3]


def chunking_params(filename: str = "") -> dict:
    """读取 chunking 配置并解析出本文件的切片参数（按扩展名分派 size）。

    返回 {size, overlap, min_chunk, sentence_overlap, table_max_rows,
          code_block_chunk, title_enriched}
    """
    try:
        from core import config
        ck = config.get("chunking") or {}
    except Exception:
        ck = {}
    ext = os.path.splitext(filename or "")[1].lower() if filename else ""
    size_by_type = ck.get("size_by_type") or {}
    size = size_by_type.get(ext) or ck.get("default_size") or 600
    return {
        "size": int(size),
        "overlap": int(ck.get("overlap") or max(int(size) // 7, 60)),
        "min_chunk": int(ck.get("min_chunk") or 50),
        "sentence_overlap": bool(ck.get("sentence_overlap", True)),
        "table_max_rows": int(ck.get("table_max_rows") or 15),
        "code_block_chunk": bool(ck.get("code_block_chunk", True)),
        "title_enriched": bool(ck.get("title_enriched", True)),
    }


def ingest_document(conn, filename: str, file_type: str, content: bytes,
                    uploaded_by: str = "王工", metadata: dict | None = None,
                    doc_id: int | None = None, branch: str = "global") -> dict:
    """上传文档 → 状态机入库：解析 → 结构感知分块 → 向量化 → document_chunks + 元数据。

    metadata（KB-P0）：{title, author, version, tags, source, extra}
    doc_id：重试复用——传入时 UPDATE 原文档（不新建），清空旧 chunks 由调用方负责
    branch：全局资产——文档从分支体系抽离（默认 'global'，不随分支变化；实体/关系仍按分支）
    管道阶段明细：documents.pipeline_detail = {parse,chunk,embed,insert} 各阶段 done/failed
    返回 {doc_id, parse_status, chunk_count, error?, pipeline}
    """
    try:
        # 1) 登记文档（parsing 状态）——新建或复用
        if doc_id is None:
            cur = conn.execute(
                "INSERT INTO documents (filename, file_type, file_size, parse_status, uploaded_by, branch, pipeline_detail) "
                "VALUES (?,?,?, 'parsing', ?, ?, '{}')",
                (filename, file_type, len(content), uploaded_by, branch))
            doc_id = cur.lastrowid
        else:
            conn.execute(
                "UPDATE documents SET parse_status='parsing', chunk_count=0, error_msg='', pipeline_detail='{}' "
                "WHERE id=?", (doc_id,))

        def _stage(stage: str, status: str, error: str = ""):
            import json as _j
            try:
                cur_d = _j.loads(conn.execute("SELECT pipeline_detail FROM documents WHERE id=?", (doc_id,)).fetchone()[0] or "{}")
            except Exception:
                cur_d = {}
            cur_d[stage] = status
            conn.execute(
                "UPDATE documents SET pipeline_detail=?, error_msg=? WHERE id=?",
                (_j.dumps(cur_d, ensure_ascii=False), error or conn.execute(
                    "SELECT error_msg FROM documents WHERE id=?", (doc_id,)).fetchone()[0] or "", doc_id))

        # 1.3) 保存源文件副本（源文件预览 + 失败重试）
        _save_source_copy(doc_id, filename, content)

        # 1.5) 元数据落库（KB-P0）——标题自动提取（用户未填时用文档自身标题）
        md = metadata or {}
        text0 = extract_text(filename, content)
        detected_title = md.get("title", "").strip()
        if not detected_title:
            detected_title = extract_doc_title(filename, text0)
        conn.execute(
            "INSERT INTO doc_metadata (document_id, title, author, version, tags, source, extra) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(document_id) DO UPDATE SET title=excluded.title, author=excluded.author, "
            "version=excluded.version, tags=excluded.tags, extra=excluded.extra",
            (doc_id, detected_title, md.get("author", ""),
             md.get("version", "v1.0"),
             json.dumps(md.get("tags", []), ensure_ascii=False),
             md.get("source", "upload"),
             json.dumps(md.get("extra", {}), ensure_ascii=False)))

        # 2) 解析文本（阶段：parse）
        text = text0
        if not text.strip():
            _stage("parse", "failed", "无法从该格式提取文本（支持 txt/md/pdf/docx/csv/json）")
            conn.execute("UPDATE documents SET parse_status='failed', chunk_count=0 WHERE id=?", (doc_id,))
            conn.commit()
            return {"doc_id": doc_id, "parse_status": "failed", "chunk_count": 0,
                    "error": "无法从该格式提取文本（支持 txt/md/pdf/docx/csv/json）", "pipeline": "parse"}
        _stage("parse", "done")

        # 3) 结构感知分块（v2：config 参数 + 表格/代码块分派 + title-enriched，阶段：chunk）
        ck = chunking_params(filename)
        structured = chunk_text_structured(
            text, size=ck["size"], overlap=ck["overlap"], min_chunk=ck["min_chunk"],
            sentence_overlap=ck["sentence_overlap"], table_max_rows=ck["table_max_rows"],
            code_block_chunk=ck["code_block_chunk"], doc_title=detected_title or filename)
        # P0-3 数据卫生：按 content hash 去重 + 长度阈值（防重复拼接文档污染向量库，如 113/116）
        # 长度阈值与 chunk_text 一致：<min_chunk 且非完整句才丢弃（完整句豁免，防误杀短文本文档）
        _seen_hashes = set()
        _deduped = []
        for sc in structured:
            c = sc["content"]
            c_len = len(c.strip())
            if c_len < ck["min_chunk"] and not (c_len >= 10 and _is_complete_sentence(c)):
                continue
            import hashlib
            h = hashlib.md5(c.encode("utf-8")).hexdigest()
            if h in _seen_hashes:
                continue
            _seen_hashes.add(h)
            _deduped.append(sc)
        structured = _deduped
        chunks = [c["content"] for c in structured]
        if not chunks:
            _stage("chunk", "failed", "文本为空")
            conn.execute("UPDATE documents SET parse_status='failed' WHERE id=?", (doc_id,))
            conn.commit()
            return {"doc_id": doc_id, "parse_status": "failed", "chunk_count": 0, "error": "文本为空",
                    "pipeline": "chunk"}
        _stage("chunk", "done")

        # 4) 向量化（v2：Title-Enriched——embed_text = 标题+section+content，检索侧有章节上下文；
        #    记录实际使用的版本：真 embedding API 或 bigram 降级，阶段：embed）
        embedder = Embedder(conn)
        if ck["title_enriched"]:
            embed_texts = [sc.get("embed_text") or sc["content"] for sc in structured]
        else:
            embed_texts = chunks
        vectors, embed_version = embedder.embed_with_version(embed_texts)
        _stage("embed", "done")

        # P0-2 域打标：按文件名/内容自动分类（高置信直接写库，低置信进 review 队列）
        from glossary import infer_domain, log_domain_review
        domain, domain_conf = infer_domain(filename=filename, content=text0, return_score=True)
        if domain_conf < 0.9:
            log_domain_review(conn, doc_id, filename, domain, domain_conf, "入库自动分类置信度不足")
        conn.execute(
            "UPDATE documents SET domain=?, domain_confidence=? WHERE id=?",
            (domain, domain_conf, doc_id))

        # P2-1 Reverse HyDE：为每个 chunk 生成假设问题（v2 放宽门槛 + 扩充术语表）
        # 真向量模式下同步向量化问题 → hyde_embedding（检索侧问题对问题同版本余弦）
        _hyde_questions = []
        _hyde_vecs = []
        for sc in structured:
            qs = _generate_hyde_questions(sc["content"], sc.get("section", ""))
            _hyde_questions.append(qs)
            _hyde_vecs.append(None)
        if embed_version != "bigram-tf" and any(q for q in _hyde_questions):
            try:
                hyde_vecs, _ = embedder.embed_with_version(
                    ["；".join(q) for q in _hyde_questions if q] or [], batch_size=0)
                _it = iter(hyde_vecs)
                _hyde_vecs = [next(_it) if q else None for q in _hyde_questions]
            except Exception:
                _hyde_vecs = [None] * len(_hyde_questions)

        # 5) 落库 chunks（含 section / bm25_text / domain / hyde，阶段：insert）
        # P2-1 落库前向量归一化为单位向量（幂等，检索矩阵加载时免重复 sqrt）
        from vector_index import normalize_vector
        for i, (sc, vec) in enumerate(zip(structured, vectors)):
            conn.execute(
                "INSERT INTO document_chunks (document_id, chunk_index, content, embedding, embed_version, source_doc, section, bm25_text, branch, domain, hyde_questions, hyde_embedding) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (doc_id, i, sc["content"], json.dumps(normalize_vector(vec), ensure_ascii=False),
                 embed_version, filename, sc.get("section", ""), sc.get("bm25_text", ""), branch,
                 domain, json.dumps(_hyde_questions[i], ensure_ascii=False),
                 json.dumps(normalize_vector(_hyde_vecs[i]), ensure_ascii=False) if _hyde_vecs[i] else "[]"))
        # 数据变更：向量索引失效，下次检索重建
        try:
            from vector_index import ChunkVectorIndex
            ChunkVectorIndex.invalidate()
        except Exception as e:
            logger.warning("向量索引失效标记失败（下次检索自动重建）: %s", e)
        _stage("insert", "done")

        # 6) 完成状态
        # 2026-09-10 米爸澄清：入库（committed）= 内容已进图库，必须人工切换。
        # 解析完成只到 stored（向量就绪/待入库），且不覆盖已 committed/archived/deprecated 的文档。
        conn.execute(
            "UPDATE documents SET parse_status='completed', chunk_count=?, "
            "lifecycle_status=CASE WHEN lifecycle_status IN ('uploaded','processing') "
            "THEN 'stored' ELSE lifecycle_status END WHERE id=?",
            (len(chunks), doc_id))
        conn.commit()
        return {"doc_id": doc_id, "parse_status": "completed", "chunk_count": len(chunks),
                "embed_version": embed_version, "detected_title": detected_title,
                "pipeline": {"parse": "done", "chunk": "done", "embed": "done", "insert": "done"}}
    except Exception as e:
        try:
            if doc_id:
                conn.execute("UPDATE documents SET parse_status='failed', error_msg=? WHERE id=?",
                             (str(e)[:300], doc_id))

                conn.commit()
        except Exception as e:
            logger.warning("失败状态回写失败: %s", e)
        return {"doc_id": doc_id, "parse_status": "failed", "chunk_count": 0, "error": str(e)[:200]}


def ingest_upload_document(conn, filename: str, file_type: str, content: bytes,
                           uploaded_by: str = "王工", metadata: dict | None = None,
                           branch: str = "global") -> dict:
    """文档上传统一入口：ingest_document 全管道（解析→分块→向量化→落库）+ 上传即抽取。

    供「文件管理上传」（meta.py upload_document）与「AI 建模会话附件上传」
    （conversations.py upload_file）共用同一套逻辑，避免两处行为漂移：
    - 解析失败 → parse_status='failed'，自动抽取跳过（不降级）；
    - 自动抽取受 settings.file_auto_extract_enabled 开关控制（默认关闭），
      开启且候选来源为 SysML 时命中模型实体/关系候选（conf 0.9/0.85，dup_high）。
    返回 {doc_id, filename, size, parse_status, chunk_count, embed_version,
          detected_title, auto_extract, error?}
    """
    import json as _j
    result = ingest_document(conn, filename, file_type, content,
                             uploaded_by=uploaded_by, metadata=metadata, branch=branch)
    doc_id = result.get("doc_id")
    auto_meta = {}
    if result.get("parse_status") == "failed":
        return {"doc_id": doc_id, "filename": filename, "size": len(content),
                "parse_status": "failed", "chunk_count": 0, "embed_version": "",
                "detected_title": "", "auto_extract": auto_meta, "error": result.get("error", "")}
    try:
        _sw = {r["key"]: r["value"] for r in conn.execute(
            "SELECT key, value FROM settings").fetchall()}
        extract_enabled = (_sw.get("file_auto_extract_enabled") or "0") == "1"
        cur_d = _j.loads(conn.execute(
            "SELECT pipeline_detail FROM documents WHERE id=?", (doc_id,)).fetchone()[0] or "{}")
        if not extract_enabled:
            # 开关关闭：跳过自动抽取（管道抽取阶段标记 disabled，前端隐藏补抽入口）
            cur_d["extraction"] = "disabled"
            conn.execute("UPDATE documents SET pipeline_detail=? WHERE id=?",
                         (_j.dumps(cur_d, ensure_ascii=False), doc_id))
            conn.commit()
            auto_meta = {"enabled": False, "reason": "文件管理实体抽取开关未开启"}
        else:
            from vector2graph import extract_candidates
            query = (result.get("detected_title") or filename or "")[:80]
            ext = extract_candidates(conn, query, doc_id=doc_id, top_k=5)
            cur_d["extraction"] = "done"
            cur_d["extract_candidates"] = len(ext.get("candidates", []))
            conn.execute("UPDATE documents SET pipeline_detail=? WHERE id=?",
                         (_j.dumps(cur_d, ensure_ascii=False), doc_id))
            conn.commit()
            auto_meta = {
                "enabled": True,
                "extracted": len(ext.get("candidates", [])),
                "node_count": ext.get("node_count", 0),
                "edge_count": ext.get("edge_count", 0),
                "batch_id": ext.get("batch_id", ""),
                "rejected": ext.get("rejected", []),
                # 候选摘要（前端直接渲染，避免重复调用抽取）；关系候选带两端对象
                "candidates": [{
                    "name": c.get("name", ""),
                    "entity_type": c.get("entity_type", ""),
                    "rel_type": c.get("rel_type", ""),
                    "rel_source": c.get("rel_source", ""),
                    "rel_target": c.get("rel_target", ""),
                } for c in (ext.get("candidates") or [])][:12],
            }
    except Exception as e:
        auto_meta = {"extracted": 0, "error": str(e)[:120]}
    return {"doc_id": doc_id, "filename": filename, "size": len(content),
            "parse_status": result.get("parse_status"), "chunk_count": result.get("chunk_count", 0),
            "embed_version": result.get("embed_version", ""),
            "detected_title": result.get("detected_title", ""),
            "auto_extract": auto_meta}

