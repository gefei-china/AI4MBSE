"""缺口A：知识库真管道——文档解析 → 分块 → Embedding → 入库。

对齐 Dify 知识库管道：
1. extract_text：按格式抽取纯文本（txt/md 直读、pdf 用 pdfplumber、docx 用 python-docx）
2. chunk_text：段落感知分块（中文友好，保留段落边界 + 重叠）
3. embed：可插拔 Embedding——优先 OpenAI-compatible /embeddings（真向量），无 key 降级 bigram TF
4. ingest_document：上传 → 解析 → 分块 → 向量化 → 落 document_chunks 表（parse_status 状态机）

解耦拆分（2026-08）：原单体 993 行 → 包结构，按管道阶段拆分，对外导入契约不变：
- extract.py    文本抽取（extract_text/extract_text_ex/_extract_*）
- ocr.py        OCR 兜底：图片与扫描版 PDF（离线 ONNX，模型内置包内、零网络下载）
- chunking.py   分块（chunk_text/chunk_text_structured/_is_complete_sentence...）
- embedder.py   Embedding（Embedder）
- ingest.py     入库管道（ingest_document/extract_doc_title/chunking_params...）
- search.py     检索（search_chunks/vector_search_embed/retry_document/reindex_document...）
"""
# 文本抽取
from .extract import (
    extract_text,
    extract_text_ex,      # 2026-09-21：带诊断 meta（失败原因 / OCR 参与明细）
    _extract_csv_table,
    _extract_xlsx,
    _extract_pptx,
    _extract_docx,
    _extract_pdf,
)
# OCR 兜底（2026-09-21：扫描版 PDF / 图片；模型内置、零网络下载）
from . import ocr
# 分块
from .chunking import (
    _is_complete_sentence,
    _tail_sentence,
    chunk_text,
    _chunk_table_rows,
    chunk_text_structured,
)
# Embedding
from .embedder import Embedder
# 入库管道
from .ingest import (
    extract_doc_title,
    _save_source_copy,
    source_copy_dir,
    source_copy_path,
    safe_doc_name,
    remove_source_copy,
    _generate_hyde_questions,
    chunking_params,
    ingest_document,
    ingest_upload_document,
)
# 检索
from .search import (
    _branch_clause,
    _doc_clause,
    _ai_clause,
    _lifecycle_clause,
    _search_chunks_bigram,
    vector_search_embed,
    search_chunks,
    retry_document,
    doc_meta_title,
    get_source_text,
    reindex_document,
)
