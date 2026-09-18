"""P2-1 本体蓝图（Schema 冷启动统一入口）：业务文本 / SysML Profile → 本体草案 → 人工确认 → ontology_types。

对标知识图谱平台 v2.0 Step 0「LLM 辅助蓝图」+ docs/本体管理模块调研与优化方案.md P2-8。
2026-08-16 整合：原独立「导入 SysML Profile」并入本体蓝图作为第二种输入源——
文本走 LLM 抽取（规则兜底）；Profile 走结构化解析（复用 sysml_profile.parse_profile +
profile_to_candidates）。两者统一产出 ontology_drafts（pending），统一「勾选 → 应用」。

流程：draft_from_text / draft_from_profile → ontology_drafts（pending）→
前端勾选确认 → apply_drafts（校验后写 ontology_types，标记 applied）。

规则兜底（Mock/无 key 确定性，可回归）：
- "X 是 Y 的子类" → 实体 X（父=Y）；"X 是 Y 的父类" → 实体 Y（父=X）
- "定义 X 类型/类" → 实体 X
"""
import json
import re
import uuid

_MOCK_PATTERNS = [
    re.compile(r"([\u4e00-\u9fffA-Za-z0-9_-]{2,24})\s*是\s*([\u4e00-\u9fffA-Za-z0-9_-]{2,24})\s*的\s*(子类|父类)"),
    re.compile(r"定义\s*([\u4e00-\u9fffA-Za-z0-9_-]{2,24})\s*(类型|类|实体)"),
    re.compile(r"新增\s*(?:一个)?\s*([\u4e00-\u9fffA-Za-z0-9_-]{2,24})\s*(类型|类|实体)"),
]


def _mock_draft(text: str) -> list:
    """规则兜底：确定性抽取本体草案候选（可回归）。"""
    out = []
    seen = set()
    for pat in _MOCK_PATTERNS:
        for m in re.finditer(pat, text):
            if pat is _MOCK_PATTERNS[0]:
                sub, sup, kind = m.group(1), m.group(2), m.group(3)
                cands = [{"name": sub, "parent_name": sup if kind == "子类" else ""}]
                if kind == "父类":
                    cands.append({"name": sup, "parent_name": sub})
            else:
                cands = [{"name": m.group(1), "parent_name": ""}]
            for c in cands:
                key = c["name"]
                if key in seen or len(key) < 2:
                    continue
                seen.add(key)
                out.append({
                    "type_kind": "entity",
                    "name": c["name"],
                    "parent_name": c["parent_name"],
                    "properties": {},
                    "evidence": m.group(0)[:160],
                })
    return out


def draft_from_text(conn, text: str, operator: str = "知识工程师") -> dict:
    """文档/文本 → 本体草案候选（ontology_drafts pending，不入库）。

    LLM 模式：system prompt 要求输出 JSON
      [{"type_kind":"entity|relation|attribute","name","parent_name","properties":{},"relation_src","relation_tgt"}]
    严格按本体类型语义（relation 需 src/tgt 类型名）。
    Mock/失败 → _mock_draft 规则兜底。
    返回 {batch_id, drafts: [...], error}
    """
    text = (text or "").strip()
    if not text:
        return {"batch_id": "", "drafts": [], "error": "输入文本为空"}
    candidates = None
    try:
        from core import config as _cfg
        from llm import llm_client
        if not _cfg.as_bool("llm", "force_mock", False):
            prompt = (
                "你是本体建模专家。请从以下业务文本中提炼知识图谱本体草案（实体类型/关系类型/属性），"
                "仅输出 JSON 数组：\n"
                '[{"type_kind":"entity|relation|attribute","name":"类型名",'
                '"parent_name":"父类型(无则空)","properties":{},"relation_src":"关系域(relation时)",'
                '"relation_tgt":"关系范围(relation时)"}]\n'
                "约束：name 简洁（≤24 字）；entity 可给 parent_name；relation 必须给 relation_src/relation_tgt；"
                "无把握的类型不要输出。\n"
                f"文本：\n{text[:3000]}"
            )
            resp = llm_client.chat([
                {"role": "system", "content": "你是本体建模专家，输出严格 JSON。"},
                {"role": "user", "content": prompt},
            ])
            content = resp["choices"][0]["message"]["content"] or ""
            m = re.search(r"\[[\s\S]*\]", content)
            if m:
                candidates = json.loads(m.group(0))
    except Exception:
        candidates = None
    if candidates is None:
        candidates = _mock_draft(text)
    batch = f"ontd-{uuid.uuid4().hex[:8]}"
    drafts = []
    for c in (candidates or [])[:20]:
        name = str(c.get("name") or "").strip()[:24]
        if not name:
            continue
        type_kind = c.get("type_kind") or "entity"
        if type_kind not in ("entity", "relation", "attribute"):
            type_kind = "entity"
        props = c.get("properties") or {}
        if not isinstance(props, dict):
            props = {"raw": str(props)[:200]}
        ex = conn.execute(
            "SELECT id FROM ontology_drafts WHERE batch_id=? AND name=? AND type_kind=?",
            (batch, name, type_kind)).fetchone()
        if ex:
            continue
        cur = conn.execute(
            "INSERT INTO ontology_drafts (batch_id, name, type_kind, parent_name, properties,"
            " relation_src, relation_tgt, evidence, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (batch, name, type_kind, str(c.get("parent_name") or "")[:24],
             json.dumps(props, ensure_ascii=False), str(c.get("relation_src") or "")[:24],
             str(c.get("relation_tgt") or "")[:24], str(c.get("evidence") or "")[:300], operator))
        drafts.append({"id": cur.lastrowid, "name": name, "type_kind": type_kind,
                       "parent_name": c.get("parent_name") or "",
                       "relation_src": c.get("relation_src") or "",
                       "relation_tgt": c.get("relation_tgt") or "",
                       "evidence": c.get("evidence") or ""})
    conn.commit()
    return {"batch_id": batch, "drafts": drafts, "error": ""}


def draft_from_profile(conn, content: str, fmt: str = "1x",
                       operator: str = "知识工程师") -> dict:
    """P2-1 整合：SysML Profile（1.x XMI / 2.x KerML）→ 本体草案（ontology_drafts pending）。

    复用 sysml_profile.parse_profile（结构化解析）+ profile_to_candidates（候选+冲突标记），
    转写 ontology_drafts（batch 前缀 ontp-），保留 profile_source/profile_ref 溯源
    （apply 时透传写 ontology_types，维持「导出 Profile 供建模工具加载」双向互操作）。
    返回 {batch_id, drafts: [{id,name,type_kind,parent_name,relation_src,relation_tgt,evidence}], error}
    """
    from sysml_profile import parse_profile, profile_to_candidates
    content = (content or "").strip()
    if not content:
        return {"batch_id": "", "drafts": [], "error": "Profile 内容为空"}
    pm = parse_profile(content, fmt or "1x")
    if pm.get("error"):
        return {"batch_id": "", "drafts": [], "error": pm["error"]}
    pc = profile_to_candidates(conn, pm)
    profile_name = pm.get("name") or "UntitledProfile"
    batch = f"ontp-{uuid.uuid4().hex[:8]}"
    drafts = []
    for c in (pc.get("candidates") or [])[:40]:
        name = str(c.get("name") or "").strip()[:24]
        if not name:
            continue
        type_kind = c.get("type_kind") or "entity"
        parent = c.get("parent") or ""
        # relation：domain/range 来自 constraints.allowed_values（src/tgt 首项）
        src = tgt = ""
        av = (c.get("constraints") or {}).get("allowed_values") or {}
        if type_kind == "relation":
            src = (av.get("src") or [""])[0]
            tgt = (av.get("tgt") or [""])[0]
        evidence = "；".join(c.get("conflicts") or []) or "SysML Profile 结构化解析"
        cons = c.get("constraints") or {}
        if not isinstance(cons, dict):
            cons = {}
        cur = conn.execute(
            "INSERT INTO ontology_drafts (batch_id, name, type_kind, parent_name, properties,"
            " relation_src, relation_tgt, evidence, constraints, profile_source, profile_ref, created_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (batch, name, type_kind, parent, json.dumps(c.get("properties") or {}, ensure_ascii=False),
             src, tgt, evidence[:300], json.dumps(cons, ensure_ascii=False),
             profile_name, name, operator))
        drafts.append({"id": cur.lastrowid, "name": name, "type_kind": type_kind,
                       "parent_name": parent, "relation_src": src, "relation_tgt": tgt,
                       "evidence": evidence, "profile_source": profile_name})
    conn.commit()
    return {"batch_id": batch, "drafts": drafts, "error": ""}


def list_drafts(conn, batch_id: str = "", status: str = "pending", limit: int = 100) -> list:
    """本体草案列表。"""
    q = "SELECT * FROM ontology_drafts"
    params = []
    conds = []
    if batch_id:
        conds.append("batch_id=?")
        params.append(batch_id)
    if status:
        conds.append("status=?")
        params.append(status)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in conn.execute(q, params).fetchall():
        item = dict(r)
        try:
            item["properties"] = json.loads(item.get("properties") or "{}")
        except Exception:
            item["properties"] = {}
        out.append(item)
    return out


def apply_drafts(conn, ids: list, operator: str = "知识工程师") -> dict:
    """人工确认应用：校验后写 ontology_types，标记 applied。

    校验（每个草案独立，失败不阻断其余）：
    - 同名类型已存在 → error「类型已存在」
    - entity 且 parent_name 非空但父类型不存在 → error「父类型不存在」
    返回 {applied: [{name, type_kind}], errors: [{name, error}]}
    """
    applied, errors = [], []
    if not ids:
        return {"applied": applied, "errors": errors}
    for did in ids:
        row = conn.execute(
            "SELECT * FROM ontology_drafts WHERE id=? AND status='pending'", (did,)).fetchone()
        if not row:
            errors.append({"id": did, "error": "草案不存在或已处理"})
            continue
        name, type_kind = row["name"], row["type_kind"]
        exists = conn.execute(
            "SELECT id FROM ontology_types WHERE name=?", (name,)).fetchone()
        if exists:
            errors.append({"id": did, "name": name, "error": "类型已存在"})
            continue
        parent_id = None
        if type_kind == "entity" and row["parent_name"]:
            p = conn.execute(
                "SELECT id FROM ontology_types WHERE name=?", (row["parent_name"],)).fetchone()
            if not p:
                errors.append({"id": did, "name": name,
                               "error": f"父类型「{row['parent_name']}」不存在"})
                continue
            parent_id = p["id"]
        props = row["properties"]
        if isinstance(props, str):
            try:
                props = json.loads(props or "{}")
            except Exception:
                props = {}
        if not isinstance(props, dict):
            props = {"raw": str(props)[:200]}
        # 约束：优先草案 constraints（Profile 结构化 required/allowed_values/enum 等）；
        # relation 无 constraints 时按 relation_src/tgt 兜底生成 domain/range
        constraints = {}
        if isinstance(row["constraints"], str):
            try:
                constraints = json.loads(row["constraints"] or "{}")
            except Exception:
                constraints = {}
        elif isinstance(row["constraints"], dict):
            constraints = row["constraints"]
        if not isinstance(constraints, dict):
            constraints = {}
        if type_kind == "relation" and not constraints.get("allowed_values") \
                and (row["relation_src"] or row["relation_tgt"]):
            constraints["allowed_values"] = {
                "src": [row["relation_src"]] if row["relation_src"] else [],
                "tgt": [row["relation_tgt"]] if row["relation_tgt"] else [],
            }
        # P2-1 整合：profile 草案透传 profile_source/profile_ref（导出 Profile 双向互操作）
        profile_source = row["profile_source"] or ""
        profile_ref = row["profile_ref"] or ""
        desc = f"本体蓝图导入（{operator}）" if not profile_source \
            else f"imported from Profile: {profile_source}"
        conn.execute(
            "INSERT INTO ontology_types (name, type_kind, parent_id, properties, constraints,"
            " description, profile_source, profile_ref) VALUES (?,?,?,?,?,?,?,?)",
            (name, type_kind, parent_id, json.dumps(props, ensure_ascii=False),
             json.dumps(constraints, ensure_ascii=False), desc, profile_source, profile_ref))
        conn.execute(
            "UPDATE ontology_drafts SET status='applied' WHERE id=?", (did,))
        applied.append({"id": did, "name": name, "type_kind": type_kind,
                        "profile_source": profile_source})
    conn.commit()
    return {"applied": applied, "errors": errors}
