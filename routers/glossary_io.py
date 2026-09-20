"""术语词典导入导出（R2 方案B，2026-09-07）。

能力：
- GET  /api/glossary/export.csv    导出 CSV（10 列，与行业 Termbase/SKOS 对齐）
- GET  /api/glossary/export.skos   导出 SKOS Turtle（与本体 OWL 导出对齐，图谱可直接消费）
- GET  /api/glossary/template.csv  下载导入模板（含示例行）
- POST /api/glossary/import        导入（mode=dry-run 预检 | commit 落库），支持 CSV/XLSX

模板列：术语ID, 名称, 同义词, 英文, 缩写, 定义, 域, 关联概念, 来源, 状态
  术语ID 留空=由名称确定性派生（C-loc_key，改名不变）；关联概念=本体类型名（可空）。
"""
import csv
import io
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from core.deps import db_session
from core.audit import audit

router = APIRouter(tags=["术语导入导出"])

# 前 10 列为 2026-09-07 初版（存量 CSV 可无损导入）；后 5 列按 ISO 704 / ISO 1087 /
# ISO 25964 补充：语境(context)、注(scopeNote)、定义来源(source)、上位概念(broader)、
# 相关概念(related)。追加在末尾而非插入，保证老模板的下标与解析不失效。
COLS = ["术语ID", "名称", "同义词", "英文", "缩写", "定义", "域", "关联概念", "来源", "状态",
        "语境", "注", "定义来源", "上位概念", "相关概念"]
_STATUSES = ("candidate", "approved", "deprecated", "retired")
_KINDS = ("synonym", "alias", "abbr", "hidden")


def _loc_key(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_一-龥]", "_", str(name or "").strip())


def _split(v: str) -> list:
    """分号/逗号/换行分隔的多个值。"""
    return [x.strip() for x in re.split(r"[;；\n\r]+", str(v or "")) if x.strip()]


def _lang(term: str) -> str:
    return "zh" if any("一" <= ch <= "龥" for ch in (term or "")) else "en"


def _csv_bytes(rows: list) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLS)
    w.writerows(rows)
    # utf-8-sig：Excel 直接打开不乱码
    return buf.getvalue().encode("utf-8-sig")


def _concept_rows(conn, domain: str = "", status: str = ""):
    sql = "SELECT * FROM glossary_concepts"
    conds, params = [], []
    if domain and domain != "all":
        conds.append("domain=?"); params.append(domain)
    if status and status != "all":
        conds.append("concept_status=?"); params.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY pref_label"
    out = []
    for c in conn.execute(sql, params).fetchall():
        c = dict(c)
        terms = [dict(t) for t in conn.execute(
            "SELECT * FROM glossary_terms WHERE concept_id=?", (c["concept_id"],)).fetchall()]
        syn = [t["term"] for t in terms
               if t["term_kind"] in ("synonym", "alias") and t["term_kind"] != "preferred"]
        en = [t["term"] for t in terms if t.get("lang") == "en" and t["term_kind"] != "abbr"]
        abbr = [t["term"] for t in terms if t["term_kind"] == "abbr"]
        mapping = (c.get("maps_to_class") or c.get("maps_to_prop") or c.get("maps_to_inst") or "")
        if mapping:
            mapping = mapping.split("#").pop().split("/").pop()
        out.append([
            c["concept_id"], c["pref_label"], "; ".join(syn), "; ".join(en), "; ".join(abbr),
            c.get("definition") or "", c.get("domain") or "unknown", mapping,
            c.get("created_by") or "", c.get("concept_status") or "candidate",
            c.get("context") or "", c.get("note") or "", c.get("source") or "",
            c.get("broader") or "", c.get("related") or "",
        ])
    return out


@router.get("/api/glossary/export.csv")
def export_csv(domain: str = "", status: str = "", conn=Depends(db_session)):
    rows = _concept_rows(conn, domain, status)
    return StreamingResponse(
        io.BytesIO(_csv_bytes(rows)),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=glossary.csv"})


@router.get("/api/glossary/template.csv")
def export_template():
    demo = [
        ["", "功率放大器", "功放; 放大器", "Power Amplifier", "PA",
         "将输入信号功率放大的部件", "satellite_comms", "TWTA", "人工录入", "approved"],
        ["C-转发器", "转发器", "转发器分系统", "Transponder", "",
         "接收、变频并放大转发信号的设备", "satellite_comms", "", "GJB 标准", "approved"],
    ]
    return StreamingResponse(
        io.BytesIO(_csv_bytes(demo)),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=glossary_template.csv"})


@router.get("/api/glossary/export.skos")
def export_skos(domain: str = "", conn=Depends(db_session)):
    """SKOS Turtle 导出：概念层 → skos:Concept，映射 → skos:closeMatch（本体类 IRI）。"""
    rows = _concept_rows(conn, domain)
    lines = [
        "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .",
        "@prefix dct: <http://purl.org/dc/terms/> .",
        "@prefix mbse: <http://www.xingwang.mbse/> .",
        "",
        "mbse:glossary a skos:ConceptScheme ;",
        '\tskos:prefLabel "AI4MBSE 术语词典"@zh .',
        "",
    ]
    for cid, label, syn, en, abbr, definition, dom, mapping, src, status, \
            ctx, note, defsrc, broader, related in rows:
        lines.append(f"mbse:{cid} a skos:Concept ;")
        lines.append(f"\tskos:inScheme mbse:glossary ;")
        lines.append(f'\tskos:prefLabel "{label}"@{_lang(label)} ;')
        for t in _split(syn):
            lines.append(f'\tskos:altLabel "{t}"@{_lang(t)} ;')
        for t in _split(en):
            lines.append(f'\tskos:altLabel "{t}"@en ;')
        for t in _split(abbr):
            lines.append(f'\tskos:altLabel "{t}"@{_lang(t)} ;')
        if definition:
            lines.append(f'\tskos:definition "{definition}"@zh ;')
        if ctx:      # skos:example = ISO 704 语境
            lines.append(f'\tskos:example "{ctx}"@zh ;')
        if note:     # skos:scopeNote = ISO 1087 注
            lines.append(f'\tskos:scopeNote "{note}"@zh ;')
        if mapping:
            lines.append(f"\tskos:closeMatch mbse:class/{_loc_key(mapping)} ;")
        if broader:  # 层级关系
            lines.append(f"\tskos:broader mbse:{broader} ;")
        for rid in _split(related):   # 关联关系
            lines.append(f"\tskos:related mbse:{rid} ;")
        lines.append(f'\tdct:identifier "{cid}" ;')
        lines.append(f'\tdct:subject "{dom}" ;')
        if defsrc:   # 定义溯源
            lines.append(f'\tdct:source "{defsrc}" ;')
        lines.append(f'\tskos:historyNote "{status}" .')
        lines.append("")
    return PlainTextResponse("\n".join(lines), media_type="text/turtle; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=glossary.ttl"})


def _parse_file(content: bytes, filename: str) -> list:
    """CSV/XLSX → 行字典列表。编码优先 utf-8-sig，回退 gbk。"""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        ws = wb[wb.sheetnames[0]]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        header = [str(h or "").strip() for h in rows[0]]
        out = []
        for r in rows[1:]:
            if not any(x is not None and str(x).strip() for x in r):
                continue
            out.append({header[i]: ("" if r[i] is None else str(r[i]).strip())
                        for i in range(min(len(header), len(r)))})
        return out
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = content.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))
    return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in reader]


def _precheck(conn, rows: list) -> dict:
    """预检：必填/ID 冲突/同形异义/映射悬空/状态合法。"""
    issues, preview = [], []
    onto = {r[0] for r in conn.execute("SELECT name FROM ontology_types").fetchall()}
    onto |= {r[0] for r in conn.execute("SELECT pref_label FROM glossary_concepts").fetchall()}
    seen_cid, seen_label = {}, {}
    term_owner = {}
    for t in conn.execute("SELECT term, concept_id FROM glossary_terms").fetchall():
        term_owner.setdefault(t["term"].lower(), set()).add(t["concept_id"])
    for i, r in enumerate(rows, start=2):  # 表头=第1行
        label = (r.get("名称") or "").strip()
        if not label:
            issues.append({"row": i, "level": "error", "msg": "名称为空，跳过该行"})
            continue
        cid = (r.get("术语ID") or "").strip() or ("C-" + _loc_key(label))
        exists = bool(conn.execute("SELECT 1 FROM glossary_concepts WHERE concept_id=?",
                                   (cid,)).fetchone())
        if cid in seen_cid:
            issues.append({"row": i, "level": "error",
                           "msg": f"术语ID 在文件内重复：{cid}（与第 {seen_cid[cid]} 行）"})
        seen_cid[cid] = i
        if label.lower() in seen_label:
            issues.append({"row": i, "level": "warn",
                           "msg": f"名称重复：{label}（与第 {seen_label[label.lower()]} 行，将合并术语）"})
        seen_label[label.lower()] = i
        mapping = (r.get("关联概念") or "").strip()
        if mapping and onto and mapping not in onto:
            issues.append({"row": i, "level": "warn",
                           "msg": f"关联概念「{mapping}」在本体类型中不存在（映射悬空）"})
        st = (r.get("状态") or "approved").strip() or "approved"
        if st not in _STATUSES:
            issues.append({"row": i, "level": "warn",
                           "msg": f"状态「{st}」非法，回落 approved"})
        for t in _split(r.get("同义词", "")) + _split(r.get("英文", "")) + _split(r.get("缩写", "")):
            owners = term_owner.get(t.lower())
            if owners and cid not in owners:
                issues.append({"row": i, "level": "warn",
                               "msg": f"同形异义：术语「{t}」已属于其他概念 {sorted(owners)[:2]}"})
        preview.append({"row": i, "concept_id": cid, "label": label,
                        "action": "更新" if exists else "新建",
                        "terms": len(_split(r.get("同义词", ""))) + len(_split(r.get("英文", "")))
                                 + len(_split(r.get("缩写", "")))})
    return {"issues": issues, "preview": preview}


def _commit(conn, rows: list) -> dict:
    stats = {"created": 0, "updated": 0, "terms": 0, "skipped": 0}
    onto_class = {r[0] for r in conn.execute(
        "SELECT name FROM ontology_types WHERE type_kind='entity'").fetchall()}
    onto_prop = {r[0] for r in conn.execute(
        "SELECT name FROM ontology_types WHERE type_kind='attribute'").fetchall()}
    for r in rows:
        label = (r.get("名称") or "").strip()
        if not label:
            stats["skipped"] += 1
            continue
        cid = (r.get("术语ID") or "").strip() or ("C-" + _loc_key(label))
        definition = (r.get("定义") or "").strip()
        domain = (r.get("域") or "").strip() or "unknown"
        status = (r.get("状态") or "approved").strip() or "approved"
        if status not in _STATUSES:
            status = "approved"
        mapping = (r.get("关联概念") or "").strip()
        mc = mp = mi = ""
        if mapping:
            if mapping in onto_class:
                mc = mapping
            elif mapping in onto_prop:
                mp = mapping
            else:
                mi = mapping
        # ISO 704 / ISO 25964 扩展字段（老模板无此列时为空串，不覆盖既有值）
        _ctx = (r.get("语境") or "").strip()
        _note = (r.get("注") or "").strip()
        _src = (r.get("定义来源") or "").strip()
        _broader = (r.get("上位概念") or "").strip()
        _related = "; ".join(_split(r.get("相关概念", "")))
        cur = conn.execute("SELECT * FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone()
        if cur:
            cur = dict(cur)
            conn.execute(
                "UPDATE glossary_concepts SET pref_label=?, definition=?, domain=?, "
                "concept_status=?, maps_to_class=?, maps_to_prop=?, maps_to_inst=?, "
                "context=?, note=?, source=?, broader=?, related=?, "
                "version=version+1, updated_at=CURRENT_TIMESTAMP WHERE concept_id=?",
                (label, definition or cur.get("definition") or "", domain, status,
                 mc or cur.get("maps_to_class") or "", mp or cur.get("maps_to_prop") or "",
                 mi or cur.get("maps_to_inst") or "",
                 _ctx or cur.get("context") or "", _note or cur.get("note") or "",
                 _src or cur.get("source") or "",
                 _broader or cur.get("broader") or "", _related or cur.get("related") or "",
                 cid))
            stats["updated"] += 1
        else:
            conn.execute(
                "INSERT INTO glossary_concepts (concept_id, pref_label, definition, domain, "
                "concept_status, maps_to_class, maps_to_prop, maps_to_inst, created_by, "
                "context, note, source, broader, related) "
                "VALUES (?,?,?,?,?,?,?,?, 'import',?,?,?,?,?)",
                (cid, label, definition, domain, status, mc, mp, mi,
                 _ctx, _note, _src, _broader, _related))
            stats["created"] += 1
        conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, term_kind, "
                     "term_status, source, reliability) VALUES (?,?,?,?,?,?,?)",
                     (cid, label, _lang(label), "preferred", "preferred", "import", 7))
        for kind, col in (("synonym", "同义词"), ("abbr", "缩写")):
            for t in _split(r.get(col, "")):
                cur = conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, "
                                   "term_kind, term_status, source, reliability) "
                                   "VALUES (?,?,?,?,?,?,?)",
                                   (cid, t, _lang(t), kind, "admitted", "import", 6))
                stats["terms"] += cur.rowcount or 0
        for t in _split(r.get("英文", "")):
            cur = conn.execute("INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, "
                               "term_kind, term_status, source, reliability) "
                               "VALUES (?,?,?,?,?,?,?)",
                               (cid, t, "en", "synonym", "admitted", "import", 6))
            stats["terms"] += cur.rowcount or 0
    conn.commit()
    return stats


@router.post("/api/glossary/import")
async def glossary_import(file: UploadFile = File(...), mode: str = Form("dry-run"),
                          conn=Depends(db_session)):
    """导入术语。mode=dry-run 仅预检；mode=commit 落库（幂等 upsert）。

    返回 {total, issues:[{row,level,msg}], preview:[...], stats?}
    """
    content = await file.read()
    if not content:
        return JSONResponse({"error": "文件为空"}, 400)
    try:
        rows = _parse_file(content, file.filename or "")
    except Exception as e:
        return JSONResponse({"error": f"解析失败：{e}"}, 400)
    if not rows:
        return JSONResponse({"error": "未解析到数据行（请确认表头含「名称」列）"}, 400)
    if "名称" not in rows[0]:
        return JSONResponse(
            {"error": f"缺少「名称」列，当前列：{list(rows[0].keys())[:8]}"}, 400)
    res = _precheck(conn, rows)
    out = {"total": len(rows), **res}
    if mode == "commit":
        out["stats"] = _commit(conn, rows)
        audit("王工", "glossary_import",
              f"导入术语：{out['stats']['created']} 新建 / {out['stats']['updated']} 更新 / "
              f"{out['stats']['terms']} 术语", conn=conn)
    return out


@router.get("/api/glossary/export.tbx")
def glossary_export_tbx(conn=Depends(db_session)):
    """P1：TBX (ISO 30042) 术语交换格式导出——与 CAT/术语管理工具互通的标准格式。

    结构：martif > text > body > termEntry(概念) > descrip(subjectField/definition/status)
          + langSet(zh/en) > tig(term + termNote termType)。
    """
    from fastapi.responses import Response
    from xml.sax.saxutils import escape as _xesc
    rows = conn.execute("SELECT * FROM glossary_concepts ORDER BY pref_label").fetchall()
    entries = []
    for c in rows:
        c = dict(c)
        terms = [dict(t) for t in conn.execute(
            "SELECT * FROM glossary_terms WHERE concept_id=? ORDER BY term_kind, term",
            (c["concept_id"],)).fetchall()]
        langsets = {}
        for t in terms:
            langsets.setdefault(t["lang"] or "zh", []).append(t)
        ls_xml = ""
        for lang, ts in langsets.items():
            tigs = ""
            for t in ts:
                tt = {"preferred": "fullForm", "synonym": "synonym", "abbr": "abbreviation",
                      "alias": "synonym", "hidden": "deprecatedTerm"}.get(t["term_kind"], "synonym")
                note = f'<termNote type="administrativeStatus">{_xesc(t["term_status"] or "admitted")}</termNote>' if (t["term_status"] and t["term_status"] != "admitted") else ""
                tigs += (f'<tig><term>{_xesc(t["term"])}</term>'
                         f'<termNote type="termType">{tt}</termNote>{note}</tig>')
            ls_xml += f'<langSet xml:lang="{_xesc(lang)}">{tigs}</langSet>'
        desc = f'<descrip type="definition">{_xesc(c["definition"] or "")}</descrip>' if c["definition"] else ""
        subj = f'<descrip type="subjectField">{_xesc(c["domain"] or "")}</descrip>' if c["domain"] else ""
        status = f'<descrip type="administrativeStatus">{_xesc(c["concept_status"])}</descrip>'
        ctx = f'<descrip type="context">{_xesc(c["context"])}</descrip>' if c["context"] else ""
        src_d = f'<descrip type="source">{_xesc(c["source"])}</descrip>' if c["source"] else ""
        entries.append(
            f'<termEntry id="{_xesc(c["concept_id"])}">{status}{subj}{desc}{ctx}{src_d}{ls_xml}</termEntry>')
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<martif type="TBX" xml:lang="zh">\n<text>\n<body>\n'
           + "\n".join(entries) + "\n</body>\n</text>\n</martif>")
    return Response(xml, media_type="application/xml",
                    headers={"Content-Disposition": 'attachment; filename="glossary.tbx"'})
