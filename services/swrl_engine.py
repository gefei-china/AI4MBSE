"""P1-①（2026-09-11）SWRL 规则引擎：owlready2 + Pellet 推理。"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

logger = logging.getLogger(__name__)

try:
    import owlready2  # noqa: F401
    _HAS_OWLREADY2 = True
except ImportError:
    _HAS_OWLREADY2 = False
    owlready2 = None  # type: ignore[assignment]

try:
    import jpype  # noqa: F401
    _HAS_JPYPE = True
except ImportError:
    _HAS_JPYPE = False
    jpype = None  # type: ignore[assignment]

# P1-①（2026-09-11）便携 JRE 自动发现：Pellet 是 Java 推理器，需 JVM。
# 若机器未装系统 Java，可用项目内自带的便携 JRE（java-runtime/）。
# sync_reasoner_pellet 先 startJVM，这里确保其能找到捆绑的 java。
import os as _os

_BUNDLED_JRE_DIR = _os.path.join(_os.path.dirname(__file__), "..", "java-runtime")


def ensure_jvm_home() -> str | None:
    """确保 Pellet 推理能找到 java（sync_reasoner_pellet 内部用 subprocess 调 java，走 PATH）。

    优先用捆绑便携 JRE（java-runtime/），其次 JAVA_HOME，最后系统 PATH。
    返回 JVM 主目录（其下应有 bin/java.exe）或 None。
    """
    jh = None
    # 1) 捆绑便携 JRE 优先
    if _os.path.isdir(_os.path.join(_BUNDLED_JRE_DIR, "bin")) and _os.path.exists(
        _os.path.join(_BUNDLED_JRE_DIR, "bin", "java.exe")
    ):
        jh = _os.path.abspath(_BUNDLED_JRE_DIR)
    # 2) 已有 JAVA_HOME
    elif _os.environ.get("JAVA_HOME") and _os.path.exists(
        _os.path.join(_os.environ["JAVA_HOME"], "bin", "java.exe")
    ):
        jh = _os.path.abspath(_os.environ["JAVA_HOME"])
    # 3) 系统 PATH
    elif shutil.which("java"):
        return None

    if jh:
        _os.environ["JAVA_HOME"] = jh
        # 关键：sync_reasoner_pellet 用 subprocess 调 `java`，必须把其 bin 加进 PATH
        _jre_bin = _os.path.join(jh, "bin")
        cur_path = _os.environ.get("PATH", "")
        if _jre_bin not in cur_path:
            _os.environ["PATH"] = _jre_bin + _os.pathsep + cur_path
    return jh



def dependency_status() -> dict:
    return {
        "owlready2": _HAS_OWLREADY2,
        "jpype": _HAS_JPYPE,
        "pellet_ready": _HAS_OWLREADY2 and _HAS_JPYPE,
        "install_hint": (
            "pip install owlready2 jpype1"
            if not (_HAS_OWLREADY2 and _HAS_JPYPE)
            else None
        ),
    }


_SIMPLE_PATTERN = re.compile(
    r"^(?P<body>.+?)\s*(?:->|→|=>)\s*(?P<head>.+)$",
    re.DOTALL,
)


def parse_rule_text(text: str) -> tuple[str, str] | None:
    if not text or ("->" not in text and "→" not in text and "=>" not in text):
        return None
    m = _SIMPLE_PATTERN.match(text.strip())
    if not m:
        return None
    return m.group("body").strip(), m.group("head").strip()


@dataclass
class RunSummary:
    rules_run: int = 0
    rules_skipped: int = 0
    facts_inferred: int = 0
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    mode: str = "warn"

    def to_dict(self) -> dict:
        return {
            "rules_run": self.rules_run,
            "rules_skipped": self.rules_skipped,
            "facts_inferred": self.facts_inferred,
            "errors": self.errors,
            "warnings": self.warnings,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "mode": self.mode,
            "ok": not self.errors,
        }


class SWRLEngine:
    """SWRL 规则引擎（Pellet via owlready2/JPype）。"""

    def __init__(self, conn: sqlite3.Connection, ontology_iri: str = "http://mbse/onto"):
        self.conn = conn
        self.ontology_iri = ontology_iri
        self._onto = None
        self._rules_cache: list = []
        self._class_index: dict = {}
        self._prop_index: dict = {}
        self._individual_index: dict = {}
        self._loaded = False

    def load(self) -> dict:
        if not _HAS_OWLREADY2:
            raise RuntimeError(
                "owlready2 未安装。推理需要：" + str(dependency_status()["install_hint"])
            )

        from owlready2 import get_ontology, Thing, ObjectProperty, DataProperty, FunctionalProperty

        # 关键：用默认 world（get_ontology），保证 Imp / 类 / sync_reasoner_pellet
        # 处于同一世界。此前用自定义 World() 导致 SWRL 规则注册到别的 world、推理不触发。
        onto = get_ontology(f"{self.ontology_iri}#")
        self._onto = onto
        self._class_index = {}
        self._prop_index = {}
        self._individual_index = {}

        with onto:
            entity_types = self._rows(
                "SELECT id, name, parent_id, properties FROM ontology_types "
                "WHERE type_kind='entity' ORDER BY id"
            )
            id2name = {}
            for r in entity_types:
                id2name[r["id"]] = r["name"]

            for r in entity_types:
                parent = None
                if r["parent_id"] and r["parent_id"] in id2name:
                    parent_name = id2name[r["parent_id"]]
                    parent = self._class_index.get(parent_name) or Thing
                cls = type(r["name"], (parent or Thing,), {})
                self._class_index[r["name"]] = cls

            prop_types = self._rows(
                "SELECT name, type_kind, properties FROM ontology_types "
                "WHERE type_kind IN ('relation','attribute')"
            )
            for r in prop_types:
                name = r["name"]
                try:
                    if r["type_kind"] == "relation":
                        prop_cls = type(name, (ObjectProperty,), {})
                        self._prop_index[name] = prop_cls
                    else:
                        # attribute = functional 数据属性，方可用标量赋值（ind.attr = v）
                        prop_cls = type(name, (DataProperty, FunctionalProperty), {})
                        self._prop_index[name] = prop_cls
                except Exception:
                    pass

            ents = self._rows(
                "SELECT id, name, entity_type, properties, branch "
                "FROM entities WHERE is_current=1 AND status<>'deprecated'"
            )
            for e in ents:
                cls = self._class_index.get(e["entity_type"])
                if not cls:
                    continue
                ind = cls(e["id"])
                try:
                    props = json.loads(e.get("properties") or "{}")
                except Exception:
                    props = {}
                for k, v in props.items():
                    if k in self._prop_index:  # attribute 数据属性
                        try:
                            setattr(ind, k, v)
                        except Exception:
                            pass
                self._individual_index[e["id"]] = ind

            rels = self._rows(
                "SELECT source_id, target_id, relation_type, properties "
                "FROM relations WHERE status<>'deprecated'"
            )
            for rel in rels:
                p = self._prop_index.get(rel["relation_type"])
                src = self._individual_index.get(rel["source_id"])
                tgt = self._individual_index.get(rel["target_id"])
                if p and src and tgt and isinstance(p, ObjectProperty):
                    try:
                        p[src].append(tgt)
                    except Exception:
                        try:
                            setattr(src, rel["relation_type"], tgt)
                        except Exception:
                            pass

        self._rules_cache = self._rows(
            "SELECT id, name, comment, body, head, priority, is_active "
            "FROM swrl_rules ORDER BY priority DESC, id"
        )

        self._loaded = True
        return {
            "types": len(self._class_index),
            "props": len(self._prop_index),
            "individuals": len(self._individual_index),
            "rules": len(self._rules_cache),
        }

    def register_rule(
        self,
        name: str,
        body: str,
        head: str,
        comment: str = "",
        priority: int = 0,
        is_active: bool = True,
    ) -> int:
        cur = self.conn.execute(
            "INSERT OR REPLACE INTO swrl_rules "
            "(name, comment, body, head, priority, is_active) "
            "VALUES (?,?,?,?,?,?)",
            (name, comment, body, head, priority, 1 if is_active else 0),
        )
        self.conn.commit()
        return cur.lastrowid or 0

    def set_rule_active(self, rule_id: int, is_active: bool) -> int:
        cur = self.conn.execute(
            "UPDATE swrl_rules SET is_active=? WHERE id=?",
            (1 if is_active else 0, rule_id),
        )
        self.conn.commit()
        return cur.rowcount

    def delete_rule(self, rule_id: int) -> int:
        cur = self.conn.execute("DELETE FROM swrl_rules WHERE id=?", (rule_id,))
        self.conn.commit()
        return cur.rowcount

    def run(
        self,
        mode: str = "warn",
        rule_ids=None,
        clear_accepted: bool = False,
    ) -> RunSummary:
        summary = RunSummary(mode=mode)
        summary.started_at = datetime.utcnow().isoformat() + "Z"

        if not self._loaded:
            try:
                self.load()
            except Exception as e:
                summary.errors.append("load failed: " + str(e))
                summary.finished_at = datetime.utcnow().isoformat() + "Z"
                return summary

        if mode == "off":
            summary.warnings.append("mode=off, 跳过推理")
            summary.finished_at = datetime.utcnow().isoformat() + "Z"
            return summary

        if not _HAS_JPYPE:
            msg = ("JPype 未安装，Pellet 推理不可用。"
                   "修复：" + str(dependency_status()["install_hint"]))
            if mode == "enforce":
                summary.errors.append(msg)
            else:
                summary.warnings.append(msg)
            summary.finished_at = datetime.utcnow().isoformat() + "Z"
            return summary

        # P1-①：推理前确保能定位 JVM（捆绑便携 JRE / JAVA_HOME / PATH）
        ensure_jvm_home()

        if clear_accepted:
            self.conn.execute("DELETE FROM inferred_facts")
        else:
            self.conn.execute("DELETE FROM inferred_facts WHERE is_accepted=0")
        self.conn.commit()

        target_ids = set(rule_ids) if rule_ids else None
        rules = [
            r for r in self._rules_cache
            if r["is_active"] and (target_ids is None or r["id"] in target_ids)
        ]

        from owlready2 import Imp

        with self._onto:
            for r in rules:
                try:
                    body_str, head_str = self._split_body_head(r)
                    self._register_imp(r, body_str, head_str)
                    summary.rules_run += 1
                except Exception as e:
                    msg = "规则 " + str(r["name"]) + "(id=" + str(r["id"]) + ") 注册失败: " + str(e)
                    if mode == "enforce":
                        summary.errors.append(msg)
                    else:
                        summary.warnings.append(msg)
                    summary.rules_skipped += 1

        try:
            import owlready2 as _owl
            with self._onto:
                _owl.sync_reasoner_pellet(
                    infer_property_values=True,
                    infer_data_property_values=True,
                    debug=False,
                )
        except Exception as e:
            summary.errors.append("Pellet 推理失败: " + str(e))
            summary.finished_at = datetime.utcnow().isoformat() + "Z"
            if mode == "enforce":
                return summary

        # P1-①/②：确定性物化器（Python/SQL 评估 SWRL 规则）——本环境 bundled Pellet
        # 无法把 SWRL 头 materialize 回实体，用它保障"规则可执行"可落地可验证。
        # Pellet 仍作为 OWL DL 推理器尽力运行（一致性/等价/内置断言语义）。
        materialized = []
        try:
            from services.swrl_materializer import execute_rules_sql
            materialized = execute_rules_sql(self.conn, rules)
        except Exception as _me:
            summary.warnings.append("SQL 物化器异常（已跳过）: " + str(_me))

        # 去重（按 subject|predicate|object）
        seen = set()
        new_facts = []
        for f in materialized or []:
            key = (f["subject"], f["predicate"], f["object"])
            if key not in seen:
                seen.add(key)
                new_facts.append(f)

        for fact in new_facts:
            self.conn.execute(
                "INSERT INTO inferred_facts "
                "(rule_id, fact_type, subject, predicate, object, confidence) "
                "VALUES (?,?,?,?,?,?)",
                (fact["rule_id"], fact["fact_type"],
                 fact["subject"], fact["predicate"], fact["object"],
                 fact.get("confidence", 1.0)),
            )
        self.conn.commit()
        summary.facts_inferred = len(new_facts)

        finished = summary.finished_at or (datetime.utcnow().isoformat() + "Z")
        for r in rules:
            try:
                self.conn.execute(
                    "UPDATE swrl_rules SET last_executed_at=?, last_inferred_count=? WHERE id=?",
                    (finished,
                     sum(1 for f in new_facts if f["rule_id"] == r["id"]),
                     r["id"]),
                )
            except Exception:
                pass
        self.conn.commit()

        summary.finished_at = datetime.utcnow().isoformat() + "Z"
        if mode == "warn" and not summary.facts_inferred and summary.rules_run:
            summary.warnings.append(
                "推理完成但无新事实：" + str(summary.rules_run) + " 条规则可能过严或本体缺数据"
            )
        return summary

    def accept_fact(self, fact_id: int) -> int:
        cur = self.conn.execute(
            "UPDATE inferred_facts SET is_accepted=1 WHERE id=?", (fact_id,)
        )
        self.conn.commit()
        return cur.rowcount

    def reject_fact(self, fact_id: int) -> int:
        cur = self.conn.execute(
            "DELETE FROM inferred_facts WHERE id=?", (fact_id,)
        )
        self.conn.commit()
        return cur.rowcount

    def list_facts(
        self,
        rule_id=None,
        accepted=None,
        limit: int = 200,
    ) -> list:
        q = ("SELECT f.*, r.name AS rule_name FROM inferred_facts f "
             "LEFT JOIN swrl_rules r ON r.id=f.rule_id WHERE 1=1")
        params = []
        if rule_id is not None:
            q += " AND f.rule_id=?"
            params.append(rule_id)
        if accepted is not None:
            q += " AND f.is_accepted=?"
            params.append(accepted)
        q += " ORDER BY f.inferred_at DESC LIMIT ?"
        params.append(limit)
        return self._rows(q, params)

    def _split_body_head(self, rule: dict) -> tuple:
        body = rule.get("body") or ""
        head = rule.get("head") or ""
        if not head:
            parsed = parse_rule_text(body)
            if parsed:
                body, head = parsed
        return body, head

    def _register_imp(self, rule: dict, body: str, head: str) -> None:
        from owlready2 import Imp
        try:
            imp = Imp(rule["name"])
            setattr(imp, "antecedent", body)
            setattr(imp, "consequent", head)
        except Exception:
            try:
                Imp(rule["name"])
            except Exception:
                pass

    def _extract_inferred_facts(self, rules: list) -> list:
        facts: list = []
        for ind in self._individual_index.values():
            for prop_name in list(self._prop_index.keys()):
                try:
                    values = list(getattr(ind, prop_name, []) or [])
                except Exception:
                    continue
                for v in values:
                    if not hasattr(v, "name"):
                        continue
                    facts.append({
                        "rule_id": 0,
                        "fact_type": "ObjectPropertyAssertion",
                        "subject": ind.name,
                        "predicate": prop_name,
                        "object": v.name,
                        "confidence": 1.0,
                    })
            for prop_name in list(self._prop_index.keys()):
                try:
                    val = getattr(ind, prop_name, None)
                except Exception:
                    continue
                if val is None:
                    continue
                if isinstance(val, (str, int, float, bool)):
                    facts.append({
                        "rule_id": 0,
                        "fact_type": "DataPropertyAssertion",
                        "subject": ind.name,
                        "predicate": prop_name,
                        "object": str(val),
                        "confidence": 1.0,
                    })
        return facts

    def _rows(self, sql: str, params: Iterable = ()) -> list:
        cur = self.conn.execute(sql, tuple(params))
        cols = [d[0] for d in cur.description] if cur.description else []
        out = []
        for r in cur.fetchall():
            if isinstance(r, dict):
                out.append(r)
                continue
            if hasattr(r, "keys"):
                try:
                    out.append(dict(r))
                    continue
                except Exception:
                    pass
            if cols and len(cols) == len(r):
                out.append(dict(zip(cols, r)))
            else:
                out.append({"_" + str(i): v for i, v in enumerate(r)})
        return out


BUILTIN_RULE_TEMPLATES = [
    {
        "name": "AdultRequiresNoGuardian",
        "comment": "年龄 ≥ 18 的人不需要监护人（示例规则，工程师可改写）",
        "body": "Person(?p), hasAge(?p, ?age), greaterThanOrEqual(?age, 18)",
        "head": "requiresGuardian(?p, false)",
    },
    {
        "name": "MinorRequiresGuardian",
        "comment": "年龄 < 18 的人需要监护人",
        "body": "Person(?p), hasAge(?p, ?age), lessThan(?age, 18)",
        "head": "requiresGuardian(?p, true)",
    },
    {
        "name": "TransitivelyRelated",
        "comment": "通过 partOf 关系，所有部件都属于整个系统（传递闭包）",
        "body": "Component(?c), partOf(?c, ?p), System(?p)",
        "head": "belongsTo(?c, ?p)",
    },
]


def seed_builtin_templates(conn) -> int:
    added = 0
    for tpl in BUILTIN_RULE_TEMPLATES:
        cur = conn.execute("SELECT 1 FROM swrl_rules WHERE name=?", (tpl["name"],))
        if cur.fetchone():
            continue
        conn.execute(
            "INSERT INTO swrl_rules (name, comment, body, head, priority, is_active) "
            "VALUES (?,?,?,?,?,1)",
            (tpl["name"], tpl["comment"], tpl["body"], tpl["head"], 0),
        )
        added += 1
    conn.commit()
    return added
