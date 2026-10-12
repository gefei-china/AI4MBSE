"""RAGFlow 适配层（L3 路线）：让 RAGFlow 只做「解析 + 切片」，其余不动。

═══════════════════════════════════════════════════════════════════════
定位（务必先读，避免误当权威源）
═══════════════════════════════════════════════════════════════════════
本模块**只替换入库链路的"切片"这一段**：
    本工程 ingest_document()  ──原本──> chunk_text_structured() → 向量化 → 落库
    本工程 ingest_document()  ──启用后─> ragflow upload+parse → list_chunks → 落库
**以下一律不动**：检索（agent/rag.py）、图谱（graph_db.py）、四项治理能力
（双时态/审核流/版本对齐/坐标）、权威源（SQLite）。
⇒ RAGFlow 在本架构里是**可弃的解析器**，不是存储、不是权威源。

═══════════════════════════════════════════════════════════════════════
三条安全设计（缺一个就不许启用）
═══════════════════════════════════════════════════════════════════════
1. **默认关闭**：`knowledge.ragflow.enabled` 默认 False ⇒ 行为与改动前**逐字相同**。
2. **失败必降级**：任何异常（连不上/认证失败/解析超时/返回空）→ 自动回落本地
   `chunk_text_structured`，**入库绝不因它失败**（文档管线是主链路，不能被可选依赖拖垮）。
3. **可观测**：`pipeline_detail.ragflow` 记录 {ok, mode, ms, chunks, reason}，
   前端 20-docs.js 能直接看到"这次切片到底走了哪条路"。

═══════════════════════════════════════════════════════════════════════
实测依据（ragflow-main v1.0.0-rc1，Go 重写版）
═══════════════════════════════════════════════════════════════════════
· **无 `api/` 目录**（那是 Python 版的），HTTP 路由在 `internal/router/*_routes.go`，
  统一前缀 `/api/v1/`；官方客户端是 `sdk/python/ragflow_sdk`。
· SDK 依赖**只有 2 个**：`requests>=2.30` + `beartype>=0.20`（Apache-2.0）⇒ 不破铁律 4。
  ⚠️ 但 **SDK 只是客户端**，服务端仍要 Docker + ES + MySQL + Redis + 对象存储。
· 关键方法：`dataset.upload_documents()` → `dataset.parse_documents()`
  → `document.list_chunks()`；检索是 `rag.retrieve()`（本模块**不用**，检索留本工程）。
· ⚠️ SDK 声明 `requires-python = ">=3.13,<3.14"`，而本工程 venv 是 3.13.12 ⇒ 版本匹配。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from core import config

#: SDK 在库内的可选路径（ragflow-main/sdk/python）。不在则视为"未部署 SDK"。
_SDK_CANDIDATES = (
    os.path.join("ragflow-main", "sdk", "python"),
    os.path.join("ragflow", "sdk", "python"),
)


def _cfg(key: str, default: Any = None) -> Any:
    # ★ 组名必须与登记处一致（core/config.py 的 CONFIG_SCHEMA["rag"]）。
    #   实测踩坑：这里原本写 "knowledge"，而配置注册在 "rag" 组
    #   ⇒ as_bool 永远读到默认 False ⇒ **开关调不到**（静默失效）。
    return config.get("rag", key, default)


def is_enabled() -> bool:
    """是否启用 RAGFlow 切片（默认关）。"""
    return bool(config.as_bool("rag", "ragflow_enabled", False))


def sdk_available() -> bool:
    """SDK 是否在库内可import（不触网）。"""
    for p in _SDK_CANDIDATES:
        if os.path.isdir(os.path.join(_ROOT, p)):
            return True
    return False


def _load_sdk():
    """把 SDK 目录加进 sys.path 并返回 RAGFlow 客户端类。**只在启用时调用**。"""
    import sys
    for p in _SDK_CANDIDATES:
        full = os.path.join(_ROOT, p)
        if os.path.isdir(full) and full not in sys.path:
            sys.path.insert(0, full)
    from ragflow_sdk import RAGFlow      # noqa: E402
    return RAGFlow


# ★ 本文件在 knowledge_pipeline/ 子目录，**只需上溯一级**到工程根。
#   实测踩坑：写成 dirname×3（上溯到 mbse_system 的父目录）
#   ⇒ _ROOT 指错 ⇒ os.path.isdir 恒 False ⇒ sdk_available() 恒False
#      ⇒ mode 恒 skip ⇒ **开关怎么开都没反应**（静默失效）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 主入口 ──────────────────────────────────────────────────────────────
def ragflow_chunk(filename: str, file_type: str, content: bytes,
                  *, dataset_name: str = "", size: int = 600,
                  overlap: int = 90, timeout_s: int = 180
                  ) -> dict:
    """用 RAGFlow 解析 + 切片。**永不抛异常**——失败由调用方降级。

    返回 {ok, mode, chunks, ms, reason, chunks_data}
      mode = "ragflow"  成功
           = "skip"    未启用 / SDK 不在库⇒ 调用方走本地
           = "error"   尝试过但失败 ⇒ 调用方走本地（reason 里带原因）
    """
    t0 = time.time()
    if not is_enabled():
        return {"ok": False, "mode": "skip", "chunks": 0,
                "ms": 0, "reason": "未启用（rag.ragflow_enabled=false）",
                "chunks_data": []}
    if not sdk_available():
        return {"ok": False, "mode": "skip", "chunks": 0,
                "ms": 0, "reason": "SDK 不在库内（需 ragflow-main/sdk/python）",
                "chunks_data": []}

    base_url = (_cfg("ragflow_base_url", "") or "").strip()
    api_key = (_cfg("ragflow_api_key", "") or "").strip()
    if not base_url or not api_key:
        return {"ok": False, "mode": "error", "chunks": 0,
                "ms": int((time.time() - t0) * 1000),
                "reason": "缺 rag.ragflow_base_url / rag.ragflow_api_key",
                "chunks_data": []}

    ds_name = dataset_name or (_cfg("ragflow_dataset", "") or "") or "mbse"
    try:
        RagFlow = _load_sdk()
        cli = RagFlow(api_key=api_key, host=base_url)

        # ① 建/取 dataset（SDK 的 create_dataset 幂等：同名已存在会返回既有）
        ds = cli.create_dataset(name=ds_name,
                               chunk_method=_cfg("ragflow_chunk_method", "qna"),
                                embedding_model=_cfg("ragflow_embedding_model", "BAAI/bge-m3"))
        # ⚠️ Go 版 SDK 契约（实测 dataset.py:54）：
        #   upload_documents 收的每项键是 **display_name + blob**（不是 filename/file），
        #   且返回 `list[Document]` 对象（不是 {"data":[...]} dict）。
        docs = ds.upload_documents([{"display_name": filename, "blob": content}]) or []
        if not docs:
            return {"ok": False, "mode": "error", "chunks": 0,
                    "ms": int((time.time() - t0) * 1000),
                    "reason": "上传返回空", "chunks_data": []}
        doc = docs[0]                      # Document 对象（list_chunks 在它身上）
        doc_id = getattr(doc, "id", None)

        # ② 解析（Go版SDK：parse_documents 同步等待 / async_parse_documents 后轮询）
        ds.parse_documents([doc_id])
        # 轮询状态：Document.run_status ∈{0未开始,1进行,2取消,3成功,4失败}
        state, waited = "3", 0
        while waited < timeout_s:
            try:
                got = ds.list_documents(ids=[doc_id]) or []
                if got:
                    st = getattr(got[0], "run_status", None)
                    state = str(st if st is not None else "3")
            except Exception:
                break
            if state in ("3", "DONE"):
                break
            if state in ("4", "FAIL"):
                return {"ok": False, "mode": "error", "chunks": 0,
                        "ms": int((time.time() - t0) * 1000),
                        "reason": f"解析失败（run_status={state}）", "chunks_data": []}
            time.sleep(2)
            waited += 2

        # ③ 取分块 —— ⚠️ Go 版 `Document.list_chunks` 返回 **`list[Chunk]` 对象**
        #   （不是 {"data":[...]} dict；实测 document.py:83）⇒ 必须走属性访问。
        rows = []
        page = 1
        while page <= 20:                      # 上限 20 页，防翻页失控
            items = doc.list_chunks(page=page, page_size=100) or []
            if not items:
                break
            rows.extend(items)
            if len(items) < 100:
                break
            page += 1

        # ★★ **产出必须与本地 `chunk_text_structured` 同构**（实测契约）：
        #    下游 ingest.py 逐块消费 **4 个键**：
        #      content / section / embed_text / bm25_text
        #    —— 尤其 `section` 参与 Title-Enriched 向量化（检索侧靠它给上下文），
        #       缺失会让 ragflow 切出来的块**检索质量显著下降**。
        def _get(o, *names, default=""):
            for n in names:
                if isinstance(o, dict):
                    if n in o:
                        return o[n]
                elif hasattr(o, n):
                    return getattr(o, n)
            return default

        chunks_out = []
        for r in rows:
            c = str(_get(r, "content", default="") or "")
            if not c.strip():
                continue
            qs = _get(r, "questions", default=None) or []
            section = str(qs[0]) if isinstance(qs, list) and qs else ""
            if not section:
                section = _stem(filename)
            chunks_out.append({
                "content": c,
                "section": section,
                # embed_text = 标题 + section + content（与本地一致，Title-Enriched）
                "embed_text": f"{_stem(filename)} {section} {c}".strip(),
                # bm25_text = section + content（与本地一致）
                "bm25_text": f"{section} {c}".strip(),
            })
        return {"ok": bool(chunks_out), "mode": "ragflow", "chunks": len(chunks_out),
                "ms": int((time.time() - t0) * 1000),
                "reason": f"解析 run_status={state}，{len(chunks_out)} 块" if chunks_out
                          else "解析完成但 0 块",
                "chunks_data": chunks_out}
    except Exception as e:                     # ★ 绝不外抛：可选依赖不能拖垮主链路
        return {"ok": False, "mode": "error", "chunks": 0,
                "ms": int((time.time() - t0) * 1000),
                "reason": f"{type(e).__name__}: {str(e)[:160]}",
                "chunks_data": []}
def _stem(filename: str) -> str:
    """去掉扩展名的文件名（作 section 兜底与 embed_text 前缀）。"""
    return os.path.splitext(str(filename or ""))[0]


def ingest_with_ragflow(conn, filename: str, file_type: str, content: bytes,
                        doc_id: int | None = None, **kw) -> list | None:
    """在 ingest_document 内部调用；**返回 None 表示"走本地切片"**。

    ★ 返回类型是 `list[dict]`（与本地 `chunk_text_structured` **同构**，
      含 content/section/embed_text/bm25_text），调用方才能无分支替换。
      若返回 list[str]，下游 `sc["content"]` 会 TypeError —— 且是在**入库主链路**
      上炸，属于最坏的一类失败。
    """
    r = ragflow_chunk(filename, file_type, content,
                      dataset_name=kw.get("dataset_name", ""),
                      size=kw.get("size", 600), overlap=kw.get("overlap", 90))
    # 留痕（与本地切片同一份 pipeline_detail，不新增列）
    if doc_id:
        try:
            row = conn.execute("SELECT pipeline_detail FROM documents WHERE id=?",
                               (doc_id,)).fetchone()
            detail = json.loads((row[0] if row else "") or "{}")
            detail["ragflow"] = {k: r[k] for k in ("ok", "mode", "ms", "chunks", "reason")}
            conn.execute("UPDATE documents SET pipeline_detail=? WHERE id=?",
                         (json.dumps(detail, ensure_ascii=False), doc_id))
        except Exception:
            pass                    # 留痕失败不影响入库
    if r["mode"] == "ragflow" and r["chunks_data"]:
        return list(r["chunks_data"])
    return None