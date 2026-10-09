"""methodology_profile_load — 建模方法论规约卡加载工具（M0 方法论解析节点）。

回答「这个工程按什么方法论建模，有哪些硬约束」，产出**结构化规约卡**，
供 N1~N5 分阶段注入。刻意**不做**的事：
  · 不把整段 RAG 检索文本当规约卡（那是资料，不是规约）
  · 不推断「必填/可选属性」（静态解析不可靠）
  · 不替客户决定 enforce 分级（必须/建议机器判断不了，需业务方定）

三层架构（现状：A有 / B半残 / C空白 —— 本工具如实反映并声明）：
  A 文档型：document_chunks.content（"应该怎么画"，不可机器判定）
  B 本体型：ontology_profiles（"能用什么"，当前 0 行）
  C 规则型：methodology_rules（"必须怎样"，可机器判定，当前表不存在）
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(ROOT, "mbse.db")

# 六阶段各自需要的规约切片（分阶段注入，避免开头塞满 token）
STAGE_SLICES = {
    "M0": ["syntax_rules", "ontology_constraints", "naming_conventions",
           "mandatory_properties", "view_requirements"],
    "N1": ["naming_conventions.requirement", "mandatory_properties.Requirement"],
    "N2": ["naming_conventions.part_def", "ontology_constraints",
           "view_requirements.required_views", "mandatory_properties.Part"],
    "N3": ["syntax_rules", "mandatory_properties"],
    "N4": ["syntax_rules", "forbidden_rules"],
    "N5": ["view_requirements"],
}

ENFORCE_ZH = {"lint": "提示", "check": "阻断", "forbid": "禁止"}

DEFAULT_PROFILE = {
    "profile_id": "omg_sysml_v2_default",
    "methodology": "omg_sysml_v2",
    "display_name": "OMG SysML v2 默认规约",
    "fallback": True,
    "syntax_rules": [
        {"id": "NM-001", "scope": "package",
         "rule": "顶层 package 内的 import 须带可见性前缀（private import X::*;）",
         "enforce": "check", "message": "import 缺少可见性前缀",
         "source_doc_id": None, "source": "工程实测（autofix R01）"},
        {"id": "NM-002", "scope": "requirement",
         "rule": "satisfy 的被满足方须为 requirement usage，不能直接引用 requirement def",
         "enforce": "check", "message": "satisfy 引用了 requirement def，应改为 usage",
         "source_doc_id": None, "source": "知识文档 [U02] Must reference a requirement"},
        {"id": "NM-003", "scope": "requirement",
         "rule": "requirement 的 subject 与 satisfy 二选一，不同时出现",
         "enforce": "forbid", "message": "subject 与 satisfy 同时存在，语义冲突",
         "source_doc_id": None, "source": "知识文档 [S01]"},
        {"id": "NM-004", "scope": "connection",
         "rule": "连接用 connect A to B；无 connector ... from ... to 记法",
         "enforce": "check", "message": "connector 旧记法，v2 应用 connect",
         "source_doc_id": None, "source": "工程实测（autofix R02）"},
        {"id": "NM-005", "scope": "behavior",
         "rule": "分支须写判定条件；无 first/if-then-else 关键字",
         "enforce": "check", "message": "分支缺判定条件或使用 v1 关键字",
         "source_doc_id": None, "source": "知识文档（W 系列）"},
    ],
    "ontology_constraints": [
        {"id": "OT-001", "metaclass": "PartUsage",
         "required": [], "forbidden": ["extend"],
         "note": "v2 无 extend 关键字，用 include use case 或 :> "},
    ],
    "naming_conventions": {
        "part_def": r"^[A-Z][A-Za-z0-9_]*$",
        "requirement": r"^[A-Z][A-Za-z0-9_]*$",
        "port": r"^[a-z][A-Za-z0-9_]*$",
        "note": "类型用大驼峰、属性/端口用小驼峰；中文名须用单引号包裹",
    },
    "mandatory_properties": {
        "Part": ["name"], "Requirement": ["name"],
        "note": "静态解析不可靠，此处只列工程已实测确认的最小集；完整必填项以校验器为准",
    },
    "view_requirements": {
        "required_views": ["requirement", "structure"],
        "optional_views": ["usecase", "activity", "ibd", "part"],
        "note": "必需要求需与客户共同确认，当前取 OMG 最小可交付集",
    },
    "source_doc_ids": [],
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn, name) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _load_rule_layer(conn, profile_id):
    """C 规则型：从 methodology_rules 读。未建表则如实报告。"""
    if not _table_exists(conn, "methodology_rules"):
        return None, "表methodology_rules 不存在（规则型载体尚未落地）"
    rows = conn.execute(
        "SELECT rule_id, scope, rule_type, expression, message, enforce, source_doc_id "
        "FROM methodology_rules WHERE profile_id=? ORDER BY rule_id", (profile_id,)).fetchall()
    if not rows:
        return [], "规则表存在但该 profile 无规则行"
    return [{
        "id": r["rule_id"], "scope": r["scope"], "rule_type": r["rule_type"],
        "expression": r["expression"], "message": r["message"],
        "enforce": r["enforce"], "source_doc_id": r["source_doc_id"],
    } for r in rows], None


def _load_ontology_layer(conn, profile_name):
    """B 本体型：ontology_profiles 读取。0 行则如实报告。"""
    if not _table_exists(conn, "ontology_profiles"):
        return None, "表 ontology_profiles 不存在"
    row = conn.execute(
        "SELECT name, profile_type, entities, relations, description, status "
        "FROM ontology_profiles WHERE name=? AND status='active'", (profile_name,)).fetchone()
    if not row:
        n = conn.execute("SELECT COUNT(*) FROM ontology_profiles").fetchone()[0]
        return {"profile": profile_name, "entity_count": 0, "relation_count": 0}, \
               f"未找到 profile={profile_name} 的 active 行（表内共 {n} 行，方法论本体层尚为空）"
    def _cnt(v):
        try:
            x = json.loads(v or "{}")
            return len(x) if isinstance(x, (list, dict)) else 0
        except Exception:                # noqa: BLE001
            return 0
    return {"profile": row["name"], "profile_type": row["profile_type"],
            "description": row["description"],
            "entity_count": _cnt(row["entities"]), "relation_count": _cnt(row["relations"])}, None


def _load_doc_layer(conn, doc_ids, max_chars=2400):
    """A 文档型：从 document_chunks 取原文片段（只作参考，不作规约）。"""
    if not doc_ids:
        return []
    ids = [int(x) for x in doc_ids if str(x).isdigit()]
    if not ids:
        return []
    qs = ",".join("?" * len(ids))
    # ⚠️ lifecycle_status 全库实测枚举值是 'stored'（2026-10-08 实测 7269 条全部为 stored），
    #不是 'active'。用不存在的枚举值过滤 ⇒ 永远查不到数据，且不报错。
    rows = conn.execute(
        f"SELECT document_id, chunk_index, section, content FROM document_chunks "
        f"WHERE document_id IN ({qs}) "
        f"ORDER BY document_id, chunk_index LIMIT 12", ids).fetchall()
    out, used = [], 0
    for r in rows:
        t = (r["content"] or "").strip()
        if not t:
            continue
        take = t[:400]
        if used + len(take) > max_chars:
            break
        used += len(take)
        out.append({"doc_id": r["document_id"], "chunk": r["chunk_index"],
                    "section": r["section"], "excerpt": take})
    return out


def _render(card, stage, layers):
    L = ["【建模规约卡】"]
    L.append(f"profile：{card['display_name']}（{card['methodology']}）")
    if card.get("fallback"):
        L.append("⚠️ **fallback 规约**：未找到项目绑定的方法论，返回的是 OMG 通用默认卡。"
                 "若客户有自定义方法论，必须先补充规则后再建模。")
    L.append("")

    keys = STAGE_SLICES.get(stage or "M0", STAGE_SLICES["M0"])
    if stage and stage != "M0":
        L.append(f"（已按阶段 {stage} 裁剪，只给该阶段需要的规则子集）")
        L.append("")

    if "syntax_rules" in keys and card["syntax_rules"]:
        L.append("── ① 语法强制规则（enforce 分级决定能否自动执行）──")
        for r in card["syntax_rules"]:
            lvl = r.get("enforce", "lint")
            src = f" [来源 doc{r['source_doc_id']}]" if r.get("source_doc_id") else \
                  f" [来源:{r.get('source', '内置')}]"
            L.append(f"  [{ENFORCE_ZH.get(lvl, lvl)}] {r['id']} ({r['scope']})")
            L.append(f"      {r['rule']}{src}")
        L.append("")

    if "forbidden_rules" in keys:
        forb = [c for c in card.get("ontology_constraints", []) if c.get("forbidden")]
        if forb:
            L.append("── ② 禁止使用 ──")
            for c in forb:
                L.append(f"  {c['metaclass']}：禁止 {'、'.join(c['forbidden'])}")
            L.append("")

    if "ontology_constraints" in keys and card.get("ontology_constraints"):
        L.append("── ③ 本体约束（能用什么）──")
        for c in card["ontology_constraints"]:
            bits = []
            if c.get("required"):
                bits.append(f"必含 {'、'.join(c['required'])}")
            if c.get("forbidden"):
                bits.append(f"禁止 {'、'.join(c['forbidden'])}")
            if bits:
                L.append(f"  {c['id']} {c['metaclass']}：{'；'.join(bits)}")
        L.append("")

    if keys and "naming_conventions" in " ".join(keys):
        L.append("── ④ 命名规范（可直接喂给正则检查）──")
        for k, v in card["naming_conventions"].items():
            if k == "note" or not isinstance(v, str):
                continue
            L.append(f"  {k}: {v}")
        L.append("")

    if "mandatory_properties" in " ".join(keys):
        L.append("── ⑤ 最小必填属性 ──")
        for k, v in card["mandatory_properties"].items():
            if k == "note":
                continue
            L.append(f"  {k}: {'、'.join(v)}")
        L.append(f"  注：{card['mandatory_properties'].get('note','')}")
        L.append("")

    if "view_requirements" in keys:
        vr = card["view_requirements"]
        L.append("── ⑥ 视图齐备要求 ──")
        L.append(f"  必需视图：{'、'.join(vr['required_views'])}")
        L.append(f"  可选视图：{'、'.join(vr['optional_views'])}")
        L.append("")

    L.append("── ⑦ 载体层现状（决定哪些约束能被真正执行）──")
    for key, (data, warn) in layers.items():
        if isinstance(data, list):
            mark = "有数据" if data else "空"
        elif isinstance(data, dict):
            # 只认真实计数，profile 名字非空**不算**有数据（否则 0 实体也报有数据）
            has = any(int(data.get(k) or 0) > 0 for k in ("entity_count", "relation_count"))
            has = has or any(isinstance(v, list) and v for v in data.values())
            mark = "有数据" if has else "空"
        else:
            mark = "空"
        L.append(f"  [{mark}] {key}" + (f" —— {warn}" if warn else ""))
    L.append("")
    L.append("⚠️ 使用约束：文档型规范只是参考，不能机器判定；"
             "只有带 enforce 分级的规则型才能自动阻断。"
             "任何与客户方法论冲突之处，以客户为准并反馈修订本卡。")
    return "\n".join(L)


def _load(args: dict) -> dict:
    stage = (args.get("stage") or "M0").strip()
    methodology = (args.get("methodology") or "").strip()
    profile_id = (args.get("profile_id") or "").strip()
    doc_ids = args.get("source_doc_ids") or []

    try:
        conn = _db()
    except Exception as exc:                # noqa: BLE001
        return {"ok": False,
                "result": f"数据库不可用：{type(exc).__name__}: {exc}。"
                          f"⚠️ 无法加载规约卡 —— 不要凭记忆假设方法论要求。"}

    try:
        card = dict(DEFAULT_PROFILE)

        # 项目绑定优先
        if _table_exists(conn, "project_methodology"):
            row = conn.execute(
                "SELECT pm.profile_id, op.name, op.description "
                "FROM project_methodology pm JOIN ontology_profiles op "
                "ON op.name = pm.profile_id WHERE pm.project_id=? AND pm.is_active=1",
                (args.get("project_id") or "",)).fetchone()
            if row:
                card["profile_id"] = row["name"]
                card["display_name"] = f"{row['name']}（项目绑定）"
                card["methodology"] = methodology or "custom"
                card["fallback"] = False

        if profile_id:
            card["profile_id"] = profile_id
            card["display_name"] = profile_id
            card["fallback"] = False
        if methodology:
            card["methodology"] = methodology

        rule_rows, rule_warn = _load_rule_layer(conn, card["profile_id"])
        onto, onto_warn = _load_ontology_layer(conn, card["profile_id"])
        doc_refs = _load_doc_layer(conn, doc_ids)

        if rule_rows:
            card["syntax_rules"] = rule_rows

        layers = {
            "A 文档型（应该怎么画·不可机器判定）":
                (doc_refs, None if doc_refs else "未指定 source_doc_ids"),
            f"B 本体型（能用什么）: {onto.get('profile') if onto else '—'}":
                (onto, onto_warn),
            "C 规则型（必须怎样·可机器判定）":
                (rule_rows, rule_warn),
        }
        return {
            "ok": True,
            "result": _render(card, stage, layers),
            "profile_id": card["profile_id"],
            "methodology": card["methodology"],
            "is_fallback": bool(card.get("fallback")),
            "stage": stage,
            "rule_count": len(card["syntax_rules"]),
            "rule_rows_present": bool(rule_rows),
            "ontology_present": bool(onto and onto.get("entity_count")),
            "doc_refs_present": bool(doc_refs),
            "card": card,
        }
    finally:
        conn.close()


def exec_tool(name: str, arguments: dict | None = None) -> dict:
    args = arguments or {}
    if name == "methodology_profile_load":
        return _load(args)
    return {"ok": False, "result": f"未知工具: {name}"}


def _selftest():
    import json as _json
    print("=" * 72)
    print("methodology_profile_load 自测")
    print("=" * 72)

    print("\n① 默认（fallback 规约，必须显式声明）:")
    r = _load({})
    print(f"  ok={r.get('ok')} profile={r.get('profile_id')} "
          f"is_fallback={r.get('is_fallback')} rules={r.get('rule_count')}")
    assert r.get("is_fallback") is True, "未绑定项目时必须标 fallback"
    assert "fallback" in r.get("result", ""), "输出文本必须含 fallback 警示"

    print("\n② 分阶段裁剪（N1 只给需求相关）:")
    r2 = _load({"stage": "N1"})
    print(f"  stage={r2.get('stage')} 输出含命名规范={'命名规范' in r2['result']}")
    assert "裁剪" in r2["result"], "非 M0 阶段必须声明已裁剪"

    print("\n③ 三层载体现状必须如实反映（不粉饰）:")
    for line in r2["result"].splitlines():
        if "载体层现状" in line or line.strip().startswith("["):
            print("  " + line[:76])

    print("\n④ 指定文档来源（广汽方法论 doc812）:")
    r4 = _load({"source_doc_ids": [812]})
    print(f"  doc_refs_present={r4.get('doc_refs_present')}")
    if r4.get("doc_refs_present"):
        print("  样例section=" + str(r4["result"].split("载体层现状")[0][:0]) )
    print("\n⑤ 显式指定 profile（消除 fallback）:")
    r5 = _load({"profile_id": "xingwang_profile", "methodology": "custom"})
    print(f"  is_fallback={r5.get('is_fallback')} profile={r5.get('profile_id')}")
    assert r5.get("is_fallback") is False

    print("\n" + "=" * 72)
    print("  自测通过：fallback 声明 / 阶段裁剪 / 载体现状如实 / profile 可覆盖")
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())