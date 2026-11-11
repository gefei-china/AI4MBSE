# -*- coding: utf-8 -*-
"""文档上传去重（2026-11-10）——「同名/同内容」在上传时就提示，而不是入库后才发现。

## 为什么需要它（真实账单，不是理论风险）
真库实测（2026-11-09）：`documents` 14 → 16，其中 id **851 / 852 同名**
`1_800_SysML-v2-AI建模知识文档.md`，**各 270 chunk**（相隔 18 分钟两次上传）。
后果不是"多一行数据"，而是：
  · 检索结果里同一段话出现两遍（占掉注入槽位，挤掉别的来源）；
  · 540 个重复 chunk 进向量库，`verify_vec_cache` 因此对不上（差 540 行）；
  · 而当时库里**只有 filename + file_size 可比** ⇒ "同名同内容误传"与
    "同名新版"在数据层完全无法区分，"要不要拦"只能靠猜。

## 两类重复，处置必须不同（这是本模块的核心判断）
| 情形 | 语义 | 默认动作 | 为什么 |
|---|---|---|---|
| **同 sha256**（字节完全相同） | 误操作 / 重复导入 | `skip`（复用已有文档，不入库） | 重复入库零价值，且成倍放大检索噪声 |
| **同名不同 sha** | 可能是真·新版 | `warn`（照常入库，但回包带提示） | 拦掉会让"升级同名文档"这件事做不到 |

⚠️ **同名同内容**时按内容判据走 `skip`（不是 `warn`）：否则用户会看到"同名已存在"的提示，
但真正的信息是"这份文件已经在库里了，不用再传"。

## 为什么判定在**上传入口**而不是入库管道里
入库管道（`ingest_document`）是"已经决定要入库"之后的事，它没有"要不要"的语义；
而提示必须发生在**用户还在等回包**的那一刻（上传对话框）。故本模块由两个上传入口调用：
  · `routers/meta.py::upload_document`（资料库上传，同步/异步两条分支共用同一判定）
  · 前端在收到 `duplicate.fatal` 时用确认框询问，用户确认后带 `force=1` 重发。

## 三态策略（配置化，不硬编码）
`upload.dup_content_policy` / `upload.dup_name_policy` ∈ `skip | warn | block`
  · `warn`  ⇒ 照常入库 + 回包 `duplicate`（现状提示）
  · `block` ⇒ 409 拒绝，除非请求带 `force=1`
  · `skip`  ⇒ 不入库，直接返回已有文档（同内容默认，避免重复 chunk）
总开关 `upload.dup_check_enabled=False` ⇒ **逐字节回退到改动前**（不做任何查询）。

## 口径纪律
  · `content_sha256` 为空串的存量行 = **未知**，不参与内容判据（只按文件名判），
    **不回填**（回填要重读全部源副本，且"当时是否重复"应由人确认）。
  · 判定只读，不写库；写明 `duplicate` 的 `existing` 字段供前端展示（id/标题/块数/时间）。
"""
from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger("mbse.knowledge.dedup")

__all__ = ["content_sha256", "stamp_content_hash", "check_duplicate",
           "duplicate_groups", "source_copy_sha"]

_POLICIES = ("skip", "warn", "block")


def _cfg(group: str, key: str, default):
    try:
        from core import config as _c
        return _c.get(group, key, default)
    except Exception:
        return default


def _policy(key: str, default: str) -> str:
    v = str(_cfg("upload", key, default) or default).strip().lower()
    return v if v in _POLICIES else default


def content_sha256(content: bytes) -> str:
    """字节级指纹。空内容返回空串（**不是** sha256(b'') —— 那会让所有空文件互相判重）。"""
    if not content:
        return ""
    return hashlib.sha256(content).hexdigest()


def stamp_content_hash(conn, doc_id: int, content: bytes) -> str:
    """给刚登记的文档写内容指纹。**失败不抛**（去重是增强，不得成为上传的故障源）。

    为什么要单独一步而不是塞进那条 INSERT：`documents` 的插入语句在
    `ingest_document` / `stage_upload_document` 两处各有一份（同步/异步），
    把它们都改一遍等于给"两处必然漂移"埋点；而指纹只是**附加信息**，
    写成一次旁路 UPDATE 更安全（列不存在时静默跳过，老库不受影响）。
    """
    sha = content_sha256(content)
    if not doc_id or not sha:
        return ""
    try:
        conn.execute("UPDATE documents SET content_sha256=? WHERE id=?", (sha, int(doc_id)))
        conn.commit()
    except Exception as e:
        logger.warning("内容指纹写入失败（不阻断入库）: %s", e)
        return ""
    return sha


def _row_info(r) -> dict:
    d = dict(r) if hasattr(r, "keys") else {}
    return {
        "doc_id": int(d.get("id") or 0),
        "filename": d.get("filename") or "",
        "file_size": int(d.get("file_size") or 0),
        "chunk_count": int(d.get("chunk_count") or 0),
        "parse_status": d.get("parse_status") or "",
        "created_at": d.get("created_at") or "",
        "title": d.get("title") or "",
    }


def check_duplicate(conn, filename: str, content: bytes, *, exclude_id: int = 0) -> dict:
    """上传前的重复判定（只读）。

    :returns: {
        "checked": bool,          # 总开关关 ⇒ False（调用方据此完全走旧路径）
        "kind": "" | "content" | "name",
        "action": "proceed" | "warn" | "skip" | "block",
        "policy": "skip" | "warn" | "block" | "",
        "existing": {...} | None, # 命中的已有文档（前端直接展示）
        "same_name_count": int,   # 同名文档数（>1 说明库里已有重名，值得提示）
        "hint": str,              # 一句人读的话（前端 toast / 章节标题）
    }

    ⚠️ 判据顺序是**内容优先**：同内容但改了文件名，也仍然是一次重复导入。
    """
    out = {"checked": False, "kind": "", "action": "proceed", "policy": "",
           "existing": None, "same_name_count": 0, "hint": ""}
    if not bool(_cfg("upload", "dup_check_enabled", True)):
        return out
    out["checked"] = True

    sha = content_sha256(content)
    fn = (filename or "").strip()
    try:
        # ① 内容相同（任何文件名）—— 只在 sha 非空、且库里有同 sha 时成立
        if sha:
            r = conn.execute(
                "SELECT d.id, d.filename, d.file_size, d.chunk_count, d.parse_status, "
                "       d.created_at, COALESCE(m.title,'') AS title "
                "FROM documents d LEFT JOIN doc_metadata m ON m.document_id=d.id "
                "WHERE d.content_sha256=? AND d.id<>? "
                "ORDER BY d.id DESC LIMIT 1", (sha, int(exclude_id or 0))).fetchone()
            if r:
                info = _row_info(r)
                pol = _policy("dup_content_policy", "skip")
                out.update({
                    "kind": "content", "policy": pol,
                    "action": "block" if pol == "block" else ("skip" if pol == "skip" else "warn"),
                    "existing": info,
                    "hint": "库里已有**完全相同**的文档 #%d《%s》（%d 块，%s 入库），"
                            "本次未重复入库。" % (info["doc_id"], info["title"] or info["filename"],
                                                info["chunk_count"], info["created_at"][:16]),
                })
                return out
        # ② 同名（内容不同或存量未知）
        if fn:
            rows = conn.execute(
                "SELECT d.id, d.filename, d.file_size, d.chunk_count, d.parse_status, "
                "       d.created_at, COALESCE(m.title,'') AS title "
                "FROM documents d LEFT JOIN doc_metadata m ON m.document_id=d.id "
                "WHERE d.filename=? AND d.id<>? ORDER BY d.id DESC", (fn, int(exclude_id or 0))
            ).fetchall()
            if rows:
                info = _row_info(rows[0])
                pol = _policy("dup_name_policy", "warn")
                out.update({
                    "kind": "name", "policy": pol,
                    "action": "block" if pol == "block" else ("skip" if pol == "skip" else "warn"),
                    "existing": info, "same_name_count": len(rows),
                    "hint": "已有同名文档 #%d《%s》（%d 块，%s 入库）%s。"
                            "若这是新版，继续上传即可；若是误传，请先删除旧文档。"
                            % (info["doc_id"], info["title"] or info["filename"],
                               info["chunk_count"], info["created_at"][:16],
                               ("；当前共 %d 份同名" % len(rows)) if len(rows) > 1 else ""),
                })
                return out
    except Exception as e:
        # 判定失败不得阻断上传（那是"因为查不动所以不能传"的坏设计）；但要留痕
        logger.warning("重复判定失败（按非重复处理）: %s", e)
        out.update({"checked": True, "kind": "", "action": "proceed",
                    "hint": ""})
        return out
    return out


def source_copy_sha(doc_id: int, filename: str = "") -> str:
    """读 `data/uploads/` 里的**源副本**算 sha256（只读）。取不到返回 ''。

    为什么需要它：存量文档的 `content_sha256` 全是空串（迁移不回填），
    于是"同名多份"到底是**误传**还是**版本迭代**，光看 `documents` 表判不出来。
    源副本就是当时那份字节 —— 直接哈希即可定论，与"上传时用 sha 判重"是同一判据。
    """
    import glob
    import os
    try:
        from core import config as _c
        root = str(getattr(_c, "BASE_DIR", "") or os.getcwd())
    except Exception:
        root = os.getcwd()
    pat = os.path.join(root, "data", "uploads", "%d_*" % int(doc_id))
    try:
        fs = [f for f in glob.glob(pat) if os.path.isfile(f)]
        if not fs:
            return ""
        want = os.path.basename(str(filename or ""))
        exact = [f for f in fs if f.endswith("_" + want)] if want else []
        target = (exact or sorted(fs, key=len))[0]
        with open(target, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except Exception:
        return ""


def duplicate_groups(conn, limit: int = 50, verify_bytes: bool = False) -> list:
    """库内**已存在**的重复清单（只读盘点，供管理端与清理脚本使用）。

    两种分组：
      · `same_content` —— content_sha256 相同的文档（真重复，删掉多余的零风险）
      · `same_name`    —— 同 filename 多份（可能是版本迭代，**必须人工判断**）
    返回 [{kind, key, count, docs:[{doc_id,filename,file_size,chunk_count,created_at}]}]
    """
    out = []
    try:
        rows = conn.execute(
            "SELECT content_sha256 AS k, COUNT(*) AS n, SUM(chunk_count) AS ck, "
            "       SUM(file_size) AS sz FROM documents "
            "WHERE COALESCE(content_sha256,'')<>'' GROUP BY content_sha256 "
            "HAVING COUNT(*)>1 ORDER BY n DESC LIMIT ?", (int(limit),)).fetchall()
        for r in rows:
            d = dict(r) if hasattr(r, "keys") else {"k": r[0], "n": r[1]}
            docs = [dict(x) for x in conn.execute(
                "SELECT id AS doc_id, filename, file_size, chunk_count, created_at "
                "FROM documents WHERE content_sha256=? ORDER BY id", (d["k"],)).fetchall()]
            out.append({"kind": "same_content", "key": str(d["k"])[:16],
                        "count": int(d["n"] or 0), "chunks": int(d.get("ck") or 0),
                        "bytes": int(d.get("sz") or 0), "docs": docs})
        rows = conn.execute(
            "SELECT filename AS k, COUNT(*) AS n, SUM(chunk_count) AS ck, "
            "       SUM(file_size) AS sz FROM documents "
            "WHERE COALESCE(filename,'')<>'' GROUP BY filename "
            "HAVING COUNT(*)>1 ORDER BY n DESC LIMIT ?", (int(limit),)).fetchall()
        for r in rows:
            d = dict(r) if hasattr(r, "keys") else {"k": r[0], "n": r[1]}
            docs = [dict(x) for x in conn.execute(
                "SELECT id AS doc_id, filename, file_size, chunk_count, created_at "
                "FROM documents WHERE filename=? ORDER BY id", (d["k"],)).fetchall()]
            grp = {"kind": "same_name", "key": str(d["k"]),
                   "count": int(d["n"] or 0), "chunks": int(d.get("ck") or 0),
                   "bytes": int(d.get("sz") or 0), "docs": docs}
            if verify_bytes and len(docs) >= 2:
                # 显式要求时才读源副本：这是 IO，不该成为默认成本
                shas = [source_copy_sha(x["doc_id"], x.get("filename")) for x in docs]
                grp["sha_verified"] = bool(all(shas)) and len(set(shas)) == 1
                grp["sha_prefix"] = [v[:12] for v in shas]
            out.append(grp)
    except Exception as e:
        logger.warning("重复清单查询失败: %s", e)
    return out
