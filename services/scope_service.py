# -*- coding: utf-8 -*-
"""建模范围服务：范围 CRUD + 自然语言圈定 + 预览/展开。
文件管理 = 全局数据（不分分支）；范围片段文本在保存时快照固化，重向量化不漂移。
"""
import json
import re


_WHOLE = ("整篇", "全文", "整个文档", "全部", "所有文档", "whole")
_SECTION_KW = ("第", "节", "章节", "部分", "段", "section")


def _load_json(v, default):
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v or '{}') if v else default
    except Exception:
        return default


def list_scopes(conn):
    try:
        rows = conn.execute("SELECT * FROM modeling_scopes ORDER BY id DESC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["doc_names"] = [x for x in _load_json(d.get("doc_names"), []) if x]
            d["doc_ids"] = _load_json(d.get("doc_ids"), [])
            d["chunk_ids"] = _load_json(d.get("chunk_ids"), [])
            out.append(d)
        return out
    except Exception:
        return []


def get_scope(conn, scope_id):
    try:
        row = conn.execute("SELECT * FROM modeling_scopes WHERE id=?", (scope_id,)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


def save_scope(conn, name, mode="doc", doc_names=None, fragment_text="", chunk_ids=None, meta=None, created_by="系统"):
    """幂等保存（同名覆盖）。doc_names：命中文档的 filename/source_doc 清单。"""
    name = (name or "").strip() or "未命名范围"
    doc_names = [d for d in (doc_names or []) if d and str(d).strip()]
    doc_ids = []
    if doc_names:
        try:
            dh = {r[0]: r[1] for r in conn.execute("SELECT id, filename FROM documents").fetchall()}
            inv = {v: k for k, v in dh.items()}
            doc_ids = [inv.get(n) for n in doc_names if inv.get(n) is not None]
        except Exception:
            doc_ids = []
    existing = conn.execute("SELECT id FROM modeling_scopes WHERE name=?", (name,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE modeling_scopes SET mode=?, doc_ids=?, doc_names=?, fragment_text=?, chunk_ids=?, meta=?, created_by=? WHERE id=?",
            (mode, json.dumps(doc_ids, ensure_ascii=False), json.dumps(doc_names, ensure_ascii=False),
             fragment_text or "", json.dumps(chunk_ids or [], ensure_ascii=False),
             json.dumps(meta or {}, ensure_ascii=False), created_by, existing["id"]))
        conn.commit()
        sid = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO modeling_scopes (name, mode, doc_ids, doc_names, fragment_text, chunk_ids, meta, created_by)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (name, mode, json.dumps(doc_ids, ensure_ascii=False), json.dumps(doc_names, ensure_ascii=False),
             fragment_text or "", json.dumps(chunk_ids or [], ensure_ascii=False),
             json.dumps(meta or {}, ensure_ascii=False), created_by))
        conn.commit()
        sid = cur.lastrowid
    return {"id": sid, "name": name, "mode": mode}


def delete_scope(conn, scope_id):
    conn.execute("DELETE FROM modeling_scopes WHERE id=?", (scope_id,))
    conn.commit()
    return {"ok": True}


def resolve_scope(conn, scope_id=None, scope=None):
    """展开为检索可消费结构 {ok, mode, docs, fragments, chunk_ids}。
    scope_id 引用已保存范围；scope 为内联 dict。G2/G3 片段文本补齐到 fragments。"""
    if scope_id:
        obj = get_scope(conn, scope_id) or {}
        if not obj:
            return {"ok": False, "error": "范围不存在或已删除", "mode": None, "docs": [], "fragments": "", "chunk_ids": []}
    else:
        obj = scope or {}
    mode = obj.get("mode", "doc")
    doc_names = [d for d in _load_json(obj.get("doc_names"), []) if d]
    chunk_ids = _load_json(obj.get("chunk_ids"), [])
    fragments = obj.get("fragment_text") or ""
    if mode == "chunk" and chunk_ids and not fragments:
        parts = []
        for c in chunk_ids:
            did = c.get("document_id") if isinstance(c, dict) else None
            cix = c.get("chunk_index") if isinstance(c, dict) else None
            try:
                row = conn.execute(
                    "SELECT content FROM document_chunks WHERE document_id=? AND chunk_index=?", (did, cix)).fetchone()
                if row and row[0]:
                    parts.append(row[0])
            except Exception:
                pass
        fragments = "\n\n".join(parts)
    return {"ok": True, "mode": mode, "docs": doc_names, "fragments": fragments, "chunk_ids": chunk_ids}


def scope_from_text(text, conn):
    """自然语言圈定（预览草稿，不落库）。
    - 命中规则（由强到弱）：
      ① 《文件名》 / #文件名 / 文件名子串 / 去扩展名文件名 / 文档标题；
      ② 书名号内容（或整句）的中文双字词元与文档名/标题共享（容错匹配，如《宽带载荷》→「变更影响分析-宽带载荷参数调整.docx」）。
    - 提到 整篇/全文 → G1(全部)；提到 第N节/段 → G2(fragment_group)。"""
    def _toks(s):
        _stop = set("的了是在和与或及把被让对从向为以于就都也很而但并如果因为所以这些那些我们您请帮我测试范围宽度")
        cjk = re.sub(r"[^一-鿿]", "", s or "")
        out = set()
        for i in range(len(cjk) - 1):
            w = cjk[i:i + 2]
            if not all(ch in _stop for ch in w):
                out.add(w)
        return out

    t = (text or "").strip()
    if not t:
        return {"ok": True, "mode": "doc", "doc_names": [], "fragment_text": "", "reason": "empty"}
    docs = conn.execute(
        "SELECT d.id, d.filename, m.title FROM documents d LEFT JOIN doc_metadata m ON m.document_id=d.id WHERE d.parse_status='completed' ORDER BY d.id").fetchall()
    all_names = [d["filename"] for d in docs]
    quoted = "".join(re.findall(r"《([^》]*)》", t))
    qt = _toks(quoted) if quoted else _toks(t)
    picked = set()
    for d in docs:
        name = d["filename"] or ""
        title = (d["title"] or "").strip()
        base = re.sub(r"(?:\.[A-Za-z0-9]+)$", "", name)
        hit = (bool(re.search("《" + re.escape(name) + "》", t)) or ("#" + name) in t
               or (name and name in t) or (base and base in t) or (title and title in t))
        if not hit and qt and name:
            shared = _toks(name) & qt
            if any(len(w) >= 2 for w in shared):
                hit = True
        if hit:
            picked.add(name)
    whole = any(w in t for w in _WHOLE)
    has_section = any(w in t for w in _SECTION_KW)
    if not picked and whole:
        picked = set(all_names)
    mode = "doc"
    if picked and not whole and has_section:
        mode = "fragment_group"
    reason = "命中 %d 篇" % len(picked)
    if picked:
        reason += "（%s）" % ("按全部已解析文档" if whole else ("按节/段" if has_section else "整篇"))
    return {"ok": True, "mode": mode, "doc_names": sorted(picked), "fragment_text": "",
            "reason": reason}


def scope_preview(conn, scope_id=None, scope=None):
    res = resolve_scope(conn, scope_id, scope)
    if not res["ok"]:
        return res
    docs, mode = res["docs"], res["mode"]
    chunk_count = 0
    samples = []
    for d in docs:
        cc = conn.execute("SELECT COUNT(*) AS n FROM document_chunks WHERE source_doc=?", (d,)).fetchone()
        chunk_count += (cc["n"] if cc else 0)
        for ch in conn.execute(
                "SELECT section, content FROM document_chunks WHERE source_doc=? ORDER BY chunk_index LIMIT 3", (d,)):
            samples.append({"doc": d, "section": ch["section"], "content": (ch["content"] or "")[:120]})
    return {"ok": True, "mode": mode, "docs": docs, "doc_count": len(docs),
            "chunk_count": chunk_count, "samples": samples}


def doc_sections(conn, doc_id):
    try:
        rows = conn.execute(
            "SELECT chunk_index, section, content FROM document_chunks WHERE document_id=? ORDER BY chunk_index", (doc_id,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
