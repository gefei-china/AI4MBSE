"""AI 建模 V2 产物的「归一确认 → 应用」实现（2026-09-08）。

职责（对应前端原型规范 v1.2 的闸口①/闸口②）：
1. apply_normalization：按人工决策把归一结果**应用回 V2 代码与视图**
   - 只写新版本（sysml_versions 新行，parent_id 指向源版本），**不覆盖**旧版本
   - 同步更新 messages.content 的 SysML 代码块 + card_data.sysml_views
   - 可选把「原名 → 规范名」写入概念层词库（默认不写，需显式勾选）
2. push_version_to_zhiyuan：闸口② —— 版本写入智源建模软件
   - 先 sysmlv2_check（只读语法检测），不通过则返回问题清单且**不写入**
   - 通过后 sysmlv2_import（覆盖导入，外系统写操作，只能由用户点击触发）
   - 结果回写到 sysml_versions.element_summary.push，供前端状态标识读取

设计约束：
- 幂等：同一 (会话, 消息) 可多次应用，每次产生新版本，旧版本置 superseded
- 零猜测：拒绝决策（reject）不改 V2 文本，只标记不入图库
- 不改关系文本：关系行只支持 accept/reject（改名无意义），避免破坏 V2 语法
"""
import json
import re
import uuid

from repositories.project_repo import conversation_project_id as _conv_project_id

# 标识符边界：改名时避免误替换子串（中英文/数字/下划线均视为词内字符）
_WORD = r"A-Za-z0-9_\u4e00-\u9fa5"


# ────────────────────────────── 源数据加载 ──────────────────────────────
def _extract_code_from_text(text: str) -> str:
    m = re.search(
        r"```(?:sysml|kerml|SysML|SysMLv2|sysmlv2|kerml2|sysml_v2|sysmlv1)?\s*\n"
        r"([\s\S]*?)```", text or "", re.IGNORECASE)
    return (m.group(1).strip() if m else "")


def _load_source(conn, conv_id: int, msg_id: int) -> dict:
    """取归一应用所需的源数据：views / code_text / 版本 id / 消息行。

    优先 sysml_versions（含纯净 views + V2 源码），回退 messages.card_data。
    """
    out = {"version_id": 0, "views": {}, "code_text": "", "msg": None,
           "sysml_views": {}, "from": ""}
    ver = conn.execute(
        "SELECT id, content, code_text FROM sysml_versions "
        "WHERE conversation_id=? AND message_id=? ORDER BY id DESC LIMIT 1",
        (conv_id, msg_id)).fetchone()
    msg = conn.execute(
        "SELECT id, content, card_data FROM messages WHERE id=? AND conversation_id=? LIMIT 1",
        (msg_id, conv_id)).fetchone()
    out["msg"] = msg
    if ver:
        try:
            content = json.loads(ver["content"] or "{}")
        except Exception:
            content = {}
        out["views"] = (content.get("views") or {}) if isinstance(content, dict) else {}
        out["code_text"] = ver["code_text"] or ""
        out["version_id"] = ver["id"]
        out["from"] = "sysml_versions"
    if msg:
        try:
            cd = json.loads(msg["card_data"] or "{}")
        except Exception:
            cd = {}
        out["sysml_views"] = cd.get("sysml_views") or {}
        if not out["views"]:
            out["views"] = out["sysml_views"].get("views") or {}
            out["from"] = "card_data"
        if not out["code_text"]:
            out["code_text"] = _extract_code_from_text(msg["content"] or "")
    return out


# ────────────────────────────── 决策 → 改名映射 ──────────────────────────────
def _build_rename_map(rows: list, decisions: dict) -> tuple:
    """人工决策 → {原名: 目标名} + 变更统计。

    仅实体行（kind='entity'）参与改名；关系行只接受 accept/reject，不改文本。
    """
    rename, changes = {}, {"renamed": 0, "merged": 0, "accepted": 0,
                           "rejected": 0, "attrs": 0}
    for k, d in (decisions or {}).items():
        try:
            idx = int(k)
        except Exception:
            continue
        if idx < 0 or idx >= len(rows):
            continue
        row = rows[idx]
        action = (d or {}).get("action") or "accept"
        name = row.get("name") or ""
        if action == "reject":
            changes["rejected"] += 1
            continue
        if row.get("kind") == "attr":
            changes["attrs"] += 1
            continue
        if row.get("kind") == "relation":
            changes["accepted"] += 1 if action == "accept" else 0
            continue
        target = ""
        if action == "rename":
            target = (d or {}).get("new_name") or row.get("normalized_name") or ""
            if target and target != name:
                changes["renamed"] += 1
        elif action == "merge":
            target = (d or {}).get("merge_to") or ""
            if target and target != name:
                changes["merged"] += 1
        else:  # accept
            changes["accepted"] += 1
            target = row.get("matched_entity_name") or row.get("normalized_name") or ""
        if target and target != name:
            rename[name] = target
    return rename, changes


def _apply_rename_views(views: dict, rename: dict) -> tuple:
    """把改名应用到视图：节点名 + 边端点；合并后同名节点去重、自环边删除。"""
    import copy
    new_views = copy.deepcopy(views or {})
    removed_nodes, removed_edges = [], []
    for _vname, vd in new_views.items():
        if not isinstance(vd, dict):
            continue
        # 1) 节点改名
        for n in (vd.get("nodes") or []):
            if not isinstance(n, dict):
                continue
            for key in ("name", "id", "label"):
                v = n.get(key)
                if isinstance(v, str) and v in rename:
                    n[key] = rename[v]
        # 2) 边端点改名
        for e in (vd.get("edges") or []):
            if not isinstance(e, dict):
                continue
            for key in ("source", "target", "source_name", "target_name"):
                v = e.get(key)
                if isinstance(v, str) and v in rename:
                    e[key] = rename[v]
        # 3) 改名后同名节点去重（合并语义）
        seen, dedup_nodes = set(), []
        for n in (vd.get("nodes") or []):
            nm = n.get("name") or n.get("id") or ""
            if nm and nm in seen:
                removed_nodes.append(nm)
                continue
            if nm:
                seen.add(nm)
            dedup_nodes.append(n)
        vd["nodes"] = dedup_nodes
        # 4) 边去重 + 自环删除
        seen_e, dedup_edges = set(), []
        for e in (vd.get("edges") or []):
            s = e.get("source_name") or e.get("source") or ""
            t = e.get("target_name") or e.get("target") or ""
            rt = e.get("relation_type") or e.get("type") or ""
            if not s or not t or s == t:
                removed_edges.append(f"{s}--{rt}-->{t}")
                continue
            key = (s, rt, t)
            if key in seen_e:
                removed_edges.append(f"{s}--{rt}-->{t}")
                continue
            seen_e.add(key)
            dedup_edges.append(e)
        vd["edges"] = dedup_edges
    return new_views, removed_nodes, removed_edges


def _apply_rename_code(code: str, rename: dict) -> str:
    """V2 源码改名：按词边界替换，长名优先（避免子串误伤）。"""
    if not code or not rename:
        return code or ""
    out = code
    for old in sorted(rename.keys(), key=len, reverse=True):
        new = rename[old]
        try:
            out = re.sub(r"(?<![" + _WORD + r"])" + re.escape(old) + r"(?![" + _WORD + r"])",
                         new.replace("\\", "\\\\"), out)
        except Exception:
            out = out.replace(old, new)
    return out


# ────────────────────────────── 词库同步（概念层） ──────────────────────────────
def _write_glossary(conn, pairs: list, domain: str = "") -> dict:
    """把「原名 → 规范名」写入概念层词库（幂等：术语已存在则跳过）。

    旧表 glossary 已于 2026-09-07 废弃，统一写 glossary_concepts + glossary_terms。
    """
    res = {"written": 0, "concepts_created": 0, "skipped": 0, "items": []}
    try:
        from migrate_p1_semantic import _loc_key
    except Exception:
        _loc_key = None
    for orig, canon in pairs:
        if not orig or not canon or orig == canon:
            continue
        try:
            ex = conn.execute(
                "SELECT concept_id FROM glossary_terms WHERE term=? LIMIT 1",
                (orig,)).fetchone()
            if ex:
                res["skipped"] += 1
                continue
            cid = ("C-" + _loc_key(canon)) if _loc_key else ("C-" + uuid.uuid4().hex[:8])
            has = conn.execute(
                "SELECT 1 FROM glossary_concepts WHERE concept_id=?", (cid,)).fetchone()
            if not has:
                conn.execute(
                    "INSERT INTO glossary_concepts (concept_id, pref_label, definition, domain, "
                    "concept_status, maps_to_class, maps_to_prop, maps_to_inst, created_by, "
                    "intent, boost, context, note, source, broader, related) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, canon, "", domain or "unknown", "candidate", "", "", "",
                     "AI归一", "", 1.0, "", "AI 建模归一确认同步", "norm_apply", "", ""))
                res["concepts_created"] += 1
            conn.execute(
                "INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, term_kind, "
                "term_status, source) VALUES (?,?,?,?,?,?)",
                (cid, canon, "zh", "preferred", "preferred", "norm_apply"))
            conn.execute(
                "INSERT OR IGNORE INTO glossary_terms (concept_id, term, lang, term_kind, "
                "term_status, source) VALUES (?,?,?,?,?,?)",
                (cid, orig, "zh", "alias", "admitted", "norm_apply"))
            res["written"] += 1
            res["items"].append({"from": orig, "to": canon, "concept_id": cid})
        except Exception:
            res["skipped"] += 1
    return res


# ────────────────────── AI 续写 V2 代码（提交并更新后） ──────────────────────
_AI_SYS = (
    "你是 SysML v2 / KerML 建模专家。下面是一段已生成的 SysML v2 代码，以及人工审核确认的"
    "术语归一决策。请严格按决策改写代码中的元素名称，除此之外不得改动任何内容。\n"
    "硬性约束：\n"
    "1) 只替换决策表中出现的名称，不得新增、删除或合并元素；\n"
    "2) 保持原有语法结构、缩进、属性、关系与注释不变；\n"
    "3) 名称替换需完整一致（所有出现位置都替换）；\n"
    "4) 输出且仅输出改写后的完整代码，包裹在 ```sysml 代码块中，不要任何解释文字。"
)


def _extract_sysml_block(text: str) -> str:
    """从 LLM 输出里抽出 sysml/kerml 代码块（无代码块时返回去围栏后的正文）。"""
    if not text:
        return ""
    m = re.search(r"```(?:sysml|kerml|SysML|SysMLv2|sysmlv2|kerml2)?\s*\n([\s\S]*?)```",
                  text, re.IGNORECASE)
    if m:
        return (m.group(1) or "").strip()
    return re.sub(r"^\s*```[a-zA-Z0-9]*\s*|\s*```\s*$", "", text.strip()).strip()


def regenerate_v2_with_llm(conn, code_text: str, rename: dict, intent: str = "") -> dict:
    """人工确认提交后，由 AI 基于决策续写（重写）V2 代码。

    失败 / 未配置 LLM / 走了 Mock → 返回 ok=False，调用方回退到确定性字符串替换，
    保证任何环境下都能产出可用的 V2 代码（不因 LLM 不可用而中断流程）。
    """
    out = {"ok": False, "used_llm": False, "code": "", "note": "", "model": ""}
    if not code_text or not rename:
        out["note"] = "无 V2 源码或无变更决策，跳过 AI 续写"
        return out
    try:
        from llm import llm_client
    except Exception as e:
        out["note"] = f"LLM 模块不可用：{e}"
        return out
    pairs = "\n".join(f"- 「{k}」→「{v}」" for k, v in sorted(rename.items(), key=lambda x: x[0]))
    prompt = (
        f"归一决策（人工已确认，原名 → 目标名），共 {len(rename)} 条：\n{pairs}\n\n"
        f"原始 SysML v2 代码：\n```sysml\n{code_text}\n```\n\n"
        "请输出改写后的完整代码。"
    )
    try:
        resp = llm_client.chat(
            [{"role": "system", "content": _AI_SYS},
             {"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=8192, _intent="norm_apply_v2")
        meta = resp.get("_meta") or {}
        content = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
        if meta.get("used_mock"):
            out["note"] = "LLM 未配置或调用失败（Mock 兜底），已回退确定性替换"
            return out
        code = _extract_sysml_block(content)
        if not code or len(code) < max(20, int(len(code_text) * 0.5)):
            out["note"] = "AI 返回代码为空或长度异常，已回退确定性替换"
            return out
        # 校验：目标名必须出现在新代码里（至少一半命中），否则视为改写失败
        hit = sum(1 for v in rename.values() if v and v in code)
        if hit < max(1, len(rename) // 2):
            out["note"] = f"AI 改写结果未包含目标名（{hit}/{len(rename)}），已回退确定性替换"
            return out
        out.update({"ok": True, "used_llm": True, "code": code,
                    "model": str(meta.get("model") or ""), "note": "AI 已按确认结果续写 V2 代码"})
        return out
    except Exception as e:
        out["note"] = f"AI 续写异常：{e}"
        return out


# ────────────────────────────── 主流程：应用归一 ──────────────────────────────
def apply_normalization(conn, conv_id: int, msg_id: int, decisions: dict,
                        write_glossary: bool = False, actor: str = "王工",
                        use_ai: bool = True, inplace: bool = True) -> dict:
    """应用人工归一决策：改写 V2 代码与视图 → 写回 → 可选写词库。

    inplace=True（默认，2026-09-08 用户要求）：**就地更新原来的代码文件与视图** ——
      直接 UPDATE 源版本记录（content / code_text / diff / element_summary），
      不新建版本行、不把旧版置 superseded；同时回写 messages 的代码块与视图卡片。
    inplace=False：走「新版本」模式（旧版置 superseded，保留快照）。

    返回 {ok, new_version_id, version_label, inplace, changes, glossary, diff}。
    """
    from sysml_importer import build_normalize_report

    src = _load_source(conn, conv_id, msg_id)
    if not src["views"]:
        return {"error": "未找到该消息的 SysML 视图数据（sysml_versions / card_data 均为空）"}
    # 1) 重建报告行（decisions 以 row idx 为键）
    report = build_normalize_report(
        conn, {"views": src["views"]}, code_text=src["code_text"],
        version_id=src["version_id"], message_id=msg_id)
    rows = report.get("rows") or []
    if not rows:
        return {"error": "归一报告无数据行，无法应用决策"}

    # 2) 决策 → 改名映射
    rename, changes = _build_rename_map(rows, decisions)
    if not rename:
        # 不是失败：建议类型若为「新增 / 忽略 / 已对齐」，本就无需改写任何名称
        return {"ok": False, "no_change": True, "changes": changes,
                "message": "本次确认未产生任何名称改写（建议类型为新增 / 忽略 / 已对齐），"
                           "V2 代码无需更新，未创建新版本"}

    # 3) 应用到视图 / 源码
    #    V2 源码优先由 AI 按确认结果续写（人在回路的第二步）；AI 不可用 → 确定性替换兜底
    new_views, removed_nodes, removed_edges = _apply_rename_views(src["views"], rename)
    regen = {"ok": False, "used_llm": False, "code": "", "note": "未启用 AI 续写"}
    if use_ai:
        regen = regenerate_v2_with_llm(conn, src["code_text"], rename,
                                       intent=(src["sysml_views"] or {}).get("intent", ""))
    new_code = regen["code"] if (regen.get("ok") and regen.get("code")) \
        else _apply_rename_code(src["code_text"], rename)

    # 4) 写版本：默认就地更新原代码文件与视图（不新建版本、不覆盖历史文件以外的内容）
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM sysml_versions WHERE conversation_id=?", (conv_id,)).fetchone()
    n = (row["n"] if row else 0) or 0
    inplace_done = False
    if not (inplace and src["version_id"]):
        label = f"v0.{n + 1}"
        # 旧 current/draft → superseded（含源版本）
        conn.execute(
            "UPDATE sysml_versions SET status='superseded' "
            "WHERE conversation_id=? AND status IN ('draft','current')", (conv_id,))
        if src["version_id"]:
            conn.execute("UPDATE sysml_versions SET status='superseded' WHERE id=?",
                         (src["version_id"],))
    nodes, edges = [], []
    for _vn, vd in (new_views or {}).items():
        for nd in (vd.get("nodes") or []):
            nm = nd.get("name") or nd.get("id") or ""
            if nm:
                nodes.append(nm)
        for ed in (vd.get("edges") or []):
            edges.append(f"{ed.get('source_name') or ed.get('source') or ''}"
                         f"--{ed.get('relation_type') or ed.get('type') or ''}-->"
                         f"{ed.get('target_name') or ed.get('target') or ''}")
    summary = {
        "entities": len(set(nodes)), "relations": len(edges),
        "nodes": sorted(set(nodes))[:200], "edges": edges[:200],
        "intent": (src["sysml_views"] or {}).get("intent", ""),
        "views": len(new_views or {}),
        "applied_from": "normalize_apply",
    }
    diff = {
        "renamed": [{"from": k, "to": v} for k, v in rename.items()],
        "removed_nodes": removed_nodes[:100],
        "removed_edges": removed_edges[:100],
        "note": f"归一确认应用（{actor}）：改名 {changes['renamed']} · 合并 {changes['merged']}",
    }
    _content_json = json.dumps({"views": new_views, "intent": summary["intent"]},
                               ensure_ascii=False)
    if inplace and src["version_id"]:
        # 就地更新：改写原代码文件（code_text）与视图（content），版本号不变
        _old = conn.execute(
            "SELECT version_label, diff FROM sysml_versions WHERE id=?",
            (src["version_id"],)).fetchone()
        label = (_old["version_label"] if _old and _old["version_label"] else "") or f"v0.{n}"
        # 合并上一轮改名记录，便于回溯（去重，限长）
        try:
            _prev = json.loads((_old["diff"] if _old else "") or "{}") or {}
        except Exception:
            _prev = {}
        if isinstance(_prev, dict) and _prev.get("renamed"):
            _seen_r = {d.get("from") for d in diff["renamed"]}
            diff["renamed"] = diff["renamed"] + [
                d for d in _prev["renamed"] if d.get("from") not in _seen_r][:50]
        # 就地更新不改 status（原版本若已是 current 则保持；不动同会话其它消息的版本）
        conn.execute(
            "UPDATE sysml_versions SET content=?, diff=?, element_summary=?, code_text=? "
            "WHERE id=?",
            (_content_json, json.dumps(diff, ensure_ascii=False),
             json.dumps(summary, ensure_ascii=False), new_code or "", src["version_id"]))
        new_version_id = src["version_id"]
        inplace_done = True
    else:
        # P1-1（2026-09-28）：project_id **写入时定格**会话所属工程（空=无工程会话，合法）。
        # 定格后会话改归属不会让历史版本漂移（写回智源按版本产生时的工程判定）。
        cur = conn.execute(
            "INSERT INTO sysml_versions (artifact_id, conversation_id, message_id, project_id, "
            "version_label, content, diff, element_summary, parent_id, status, created_by, code_text) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (0, conv_id, msg_id, _conv_project_id(conn, conv_id), label, _content_json,
             json.dumps(diff, ensure_ascii=False),
             json.dumps(summary, ensure_ascii=False),
             src["version_id"] or 0, "current", actor, new_code or ""))
        new_version_id = cur.lastrowid

    # 5) 回写消息：card_data.sysml_views.views + content 代码块
    msg = src["msg"]
    if msg is not None:
        try:
            cd = json.loads(msg["card_data"] or "{}")
        except Exception:
            cd = {}
        sv = cd.get("sysml_views") or {}
        sv["views"] = new_views
        sv["applied_version_id"] = new_version_id
        cd["sysml_views"] = sv
        conn.execute("UPDATE messages SET card_data=? WHERE id=?",
                     (json.dumps(cd, ensure_ascii=False), msg_id))
        old_content = msg["content"] or ""
        if new_code and "```" in old_content:
            try:
                new_content = re.sub(
                    r"```(sysml|kerml|SysML|SysMLv2|sysmlv2|kerml2|sysml_v2|sysmlv1)?\s*\n"
                    r"[\s\S]*?```",
                    lambda m: "```" + (m.group(1) or "sysml") + "\n" + new_code + "\n```",
                    old_content, count=1, flags=re.IGNORECASE)
                conn.execute("UPDATE messages SET content=? WHERE id=?", (new_content, msg_id))
            except Exception:
                pass

    # 6) 可选：同步词库（概念层，幂等）
    gl = {"written": 0}
    if write_glossary:
        gl = _write_glossary(conn, sorted(rename.items(), key=lambda x: x[0]))
    conn.commit()
    return {
        "ok": True,
        "new_version_id": new_version_id,
        "version_label": label,
        "inplace": inplace_done,
        "parent_version_id": src["version_id"] or 0,
        "source": src["from"],
        "changes": changes,
        "renamed_count": len(rename),
        "removed_nodes": len(removed_nodes),
        "removed_edges": len(removed_edges),
        "glossary": gl,
        "diff": diff,
        "code_text": new_code or "",   # 更新后的 V2 代码（前端预览区直接展示）
        "v2_regen": {"used_llm": bool(regen.get("used_llm")), "note": regen.get("note") or "",
                     "model": regen.get("model") or ""},
    }


# ────────────────────────────── 闸口②：写入智源建模软件 ──────────────────────────────
def push_version_to_zhiyuan(conn, version_id: int, vc: str = "",
                            target_package_data_id: int | None = None,
                            actor: str = "王工") -> dict:
    """把某版本的 V2 源码写入智源：先 check（只读）后 import（写）。

    check 不通过 → 返回问题清单且**不执行写入**。
    """
    from zhiyuan_client import ZhiyuanClient, _load_config

    ver = conn.execute(
        "SELECT id, code_text, content, element_summary FROM sysml_versions WHERE id=?",
        (version_id,)).fetchone()
    if not ver:
        return {"error": f"版本不存在: {version_id}"}
    code = (ver["code_text"] or "").strip()
    if not code:
        return {"error": "该版本无 V2 源码（code_text 为空），无法写入建模软件"}
    cfg = _load_config()
    vc = (vc or cfg.get("default_vc") or "").strip()
    if not vc:
        return {"error": "缺少版本上下文 vc（格式 branchId,queryType），无法写入建模软件",
                "code": "VC_REQUIRED"}
    if not cfg.get("base_url"):
        return {"error": "智源平台未配置（ZHIYUAN_BASE_URL 为空），无法写入建模软件",
                "code": "CONFIG_MISSING"}
    cli = ZhiyuanClient(cfg["base_url"], cfg.get("token") or "",
                        headers=cfg.get("headers") or {}, timeout=cfg.get("timeout") or 15)
    # ① 语法检测（只读）
    try:
        chk = cli.sysmlv2_check(vc, int(target_package_data_id or 0), code)
    except Exception as e:
        return {"error": f"智源语法检测调用失败: {e}", "stage": "check", "code": "CALL_FAILED"}
    if not chk.get("ok"):
        detail = chk.get("result") or ""
        # 区分「调用失败（鉴权/网络/配置）」与「语法检测确实不通过」—— 文案不可误导
        if any(k in detail for k in ("调用失败", "401", "403", "未授权", "超时", "Timeout")):
            return {"ok": False, "stage": "check", "code": "CALL_FAILED",
                    "error": "智源语法检测调用失败，未执行写入（请检查 base_url / token 配置）",
                    "detail": detail, "version_id": version_id}
        return {"ok": False, "stage": "check", "code": "CHECK_FAILED",
                "error": "语法检测未通过，已阻止写入建模软件",
                "detail": detail, "version_id": version_id}
    # ② 覆盖导入（外系统写操作）
    try:
        imp = cli.sysmlv2_import(vc, code, target_package_data_id)
    except Exception as e:
        return {"error": f"智源写入调用失败: {e}", "stage": "import", "code": "CALL_FAILED"}
    ok = bool(imp.get("ok"))
    result = {
        "ok": ok, "stage": "import", "version_id": version_id, "vc": vc,
        "target_package_data_id": target_package_data_id or 0,
        "check": "语法检测通过", "detail": imp.get("result") or "",
        "pushed_by": actor,
    }
    # ③ 结果回写 element_summary.push（供前端状态标识）
    try:
        es = json.loads(ver["element_summary"] or "{}")
    except Exception:
        es = {}
    es["push"] = {"ok": ok, "vc": vc, "version_id": version_id,
                  "package_id": target_package_data_id or 0,
                  "detail": (imp.get("result") or "")[:500], "by": actor}
    # 2026-09-24 方案A：写回成功落 zhiyuan_imported_id（vc#package 标识）——
    # 入口条状态展示 + 防重复写回闸（routers/sysml_versions.py push_zhiyuan 前置检查）；
    # 智源 import 不返回独立模型 id，用「vc#包id」定位写入位置（同 vc 覆盖导入）。
    if ok:
        conn.execute("UPDATE sysml_versions SET element_summary=?, zhiyuan_imported_id=? WHERE id=?",
                     (json.dumps(es, ensure_ascii=False), f"{vc}#{target_package_data_id or 0}", version_id))
    else:
        conn.execute("UPDATE sysml_versions SET element_summary=? WHERE id=?",
                     (json.dumps(es, ensure_ascii=False), version_id))
    conn.commit()
    if not ok:
        result["error"] = "智源写入失败（详见 detail）"
    return result
