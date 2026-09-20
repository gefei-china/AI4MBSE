"""P2-①/②（2026-09-11）SPARQL 1.1 Endpoint：Fuseki 客户端 + 同步脚本。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_FUSEKI_URL = os.environ.get("FUSEKI_URL", "http://localhost:3030")
DEFAULT_DATASET = os.environ.get("FUSEKI_DATASET", "mbse")


class FusekiUnavailable(Exception):
    pass


class FusekiQueryError(Exception):
    pass


class FusekiClient:
    def __init__(self, base_url: str = DEFAULT_FUSEKI_URL, dataset: str = DEFAULT_DATASET):
        self.base_url = base_url.rstrip("/")
        self.dataset = dataset

    def ping(self, timeout: float = 3.0) -> bool:
        try:
            req = urllib.request.Request(f"{self.base_url}/$/ping", method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def dataset_exists(self, timeout: float = 3.0) -> bool:
        try:
            req = urllib.request.Request(
                f"{self.base_url}/$/datasets/{self.dataset}", method="GET"
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    def create_dataset(self, timeout: float = 10.0) -> bool:
        try:
            url = f"{self.base_url}/$/datasets/{urllib.parse.quote(self.dataset)}"
            data = ("dbName=" + urllib.parse.quote(self.dataset) + "&dbType=tdb2").encode("ascii")
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status in (200, 201)
        except Exception as e:
            logger.warning("create_dataset failed: %s", e)
            return False

    def query(self, sparql: str, timeout: float = 30.0) -> dict:
        url = f"{self.base_url}/{self.dataset}/query"
        try:
            req = urllib.request.Request(
                url + "?" + urllib.parse.urlencode({"query": sparql}),
                method="GET",
                headers={"Accept": "application/sparql-results+json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (404, 502, 503):
                raise FusekiUnavailable("Fuseki 不可达 (HTTP " + str(e.code) + "): " + body[:200])
            raise FusekiQueryError("查询失败 (HTTP " + str(e.code) + "): " + body[:500])
        except urllib.error.URLError as e:
            raise FusekiUnavailable("Fuseki 不可达: " + str(e.reason))
        except Exception as e:
            raise FusekiQueryError("查询异常: " + str(e))

    def update(self, sparql_update: str, timeout: float = 30.0) -> int:
        url = f"{self.base_url}/{self.dataset}/update"
        try:
            req = urllib.request.Request(
                url, data=sparql_update.encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/sparql-update"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            if e.code in (404, 502, 503):
                raise FusekiUnavailable("Fuseki 不可达 (HTTP " + str(e.code) + "): " + body[:200])
            raise FusekiQueryError("更新失败 (HTTP " + str(e.code) + "): " + body[:500])
        except urllib.error.URLError as e:
            raise FusekiUnavailable("Fuseki 不可达: " + str(e.reason))



def _ntriples_strip_prefixes(ttl: str) -> str:
    """INSERT DATA {} 用 N-Triples 语法，剥离所有 @prefix 指令行。"""
    return "\n".join(
        ln for ln in ttl.splitlines() if not ln.strip().startswith("@prefix")
    )



# INSERT DATA {} 是 N-Triples 语法：不接受 Turtle @prefix，但数据里仍有前缀名(owl:Class等)。
# 用 SPARQL PREFIX 子句前置声明，使前缀名可解析。
SPARQL_PREFIXES = " ".join([
    "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>",
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>",
    "PREFIX owl: <http://www.w3.org/2002/07/owl#>",
    "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>",
    "PREFIX mbse: <http://mbse/ontology/>",
    "PREFIX time: <http://www.w3.org/2006/time#>",
])


def _strip_turtle_prefixes(ttl: str) -> str:
    """剥离 Turtle @prefix 指令行（INSERT DATA 不接受）。"""
    return "\n".join(
        ln for ln in ttl.splitlines() if not ln.strip().startswith("@prefix")
    )


def _build_insert(chunk: str) -> str:
    return SPARQL_PREFIXES + " INSERT DATA { " + chunk + " }"



RDF_TYPE      = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
RDFS_LABEL    = "<http://www.w3.org/2000/01/rdf-schema#label>"
RDFS_SUBCLASS = "<http://www.w3.org/2000/01/rdf-schema#subClassOf>"
RDFS_COMMENT  = "<http://www.w3.org/2000/01/rdf-schema#comment>"
OWL_CLASS     = "<http://www.w3.org/2002/07/owl#Class>"
OWL_OBJECT_PROPERTY = "<http://www.w3.org/2002/07/owl#ObjectProperty>"
OWL_DATATYPE_PROPERTY = "<http://www.w3.org/2002/07/owl#DatatypeProperty>"
XSD_DATETIME  = "<http://www.w3.org/2001/XMLSchema#dateTime>"



def _chunk_lines(nt: str, max_size: int = 50000) -> list:
    """按完整行分块（N-Triples 每行以 . 结尾；绝不在行中间截断）。"""
    chunks, cur = [], ""
    for line in nt.splitlines():
        if not line.strip():
            continue
        if cur and (len(cur) + len(line) + 1 > max_size):
            chunks.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    return chunks or [""]


ONTOLOGY_IRI_BASE = "http://mbse/ontology/"


def _ttl_escape(s) -> str:
    if s is None:
        return '""'
    s = str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return '"' + s + '"'


def _ttl_iri(s: str) -> str:
    from core import ns as _ns
    if not s:
        return "<" + ONTOLOGY_IRI_BASE + ">"
    return "<" + _ns.class_iri(s, base=ONTOLOGY_IRI_BASE) + ">"


def _ttl_uri(s: str) -> str:
    from core import ns as _ns
    return "<" + _ns.class_iri(s, base=ONTOLOGY_IRI_BASE) + ">"


def _ttl_date(s: str) -> str:
    if not s:
        return ""
    s2 = s.replace(" ", "T")
    return '"' + s2 + '"^^<http://www.w3.org/2001/XMLSchema#dateTime>'


def _exec_dict(conn, sql, params=()):
    """execute + fetchall → [{col: val}]，列名来自 cursor.description。"""
    cur = conn.execute(sql, params)
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


def build_ontology_ttl(conn) -> str:
    """ontology_types → 严格 N-Triples（每行完整三元组 + 全 IRI）。"""
    lines = []
    for d in _exec_dict(conn, "SELECT * FROM ontology_types"):
        iri = _ttl_iri(d.get("name", ""))
        lines.append(f"{iri} {RDF_TYPE} {OWL_CLASS} .")
        lines.append(f"{iri} {RDFS_LABEL} {_ttl_escape(d.get('name', ''))} .")
        pid = d.get("parent_id")
        if pid:
            try:
                parents = _exec_dict(conn, "SELECT name FROM ontology_types WHERE id=?", (pid,))
                if parents:
                    lines.append(f"{iri} {RDFS_SUBCLASS} {_ttl_iri(parents[0]['name'])} .")
            except Exception:
                pass
        try:
            props = json.loads(d.get("properties") or "{}")
        except Exception:
            props = {}
        if props:
            lines.append(f"{iri} {RDFS_COMMENT} {_ttl_escape(json.dumps(props, ensure_ascii=False))} .")
    return "\n".join(lines)


def build_entities_ttl(conn, project_id=None) -> str:
    """entities + relations → 严格 N-Triples。"""
    lines = []
    MBSE_VF = "<" + ONTOLOGY_IRI_BASE + "validFrom>"
    MBSE_VT = "<" + ONTOLOGY_IRI_BASE + "validTo>"
    q = (
        "SELECT id, name, entity_type, properties, branch, "
        "valid_from, valid_to, is_current FROM entities "
        "WHERE status<>'deprecated' AND is_current=1"
    )
    params = []
    if project_id:
        q += " AND project_id=?"
        params.append(project_id)
    for d in _exec_dict(conn, q, params):
        iri = _ttl_uri(d.get("id", ""))
        type_iri = _ttl_iri(d["entity_type"]) if d.get("entity_type") else OWL_CLASS
        lines.append(f"{iri} {RDF_TYPE} {type_iri} .")
        lines.append(f"{iri} {RDFS_LABEL} {_ttl_escape(d.get('name', ''))} .")
        if d.get("valid_from"):
            lines.append(f"{iri} {MBSE_VF} {_ttl_date(d['valid_from'])} .")
        if d.get("valid_to"):
            lines.append(f"{iri} {MBSE_VT} {_ttl_date(d['valid_to'])} .")
        try:
            props = json.loads(d.get("properties") or "{}")
        except Exception:
            props = {}
        for k, v in props.items():
            prop_iri = _ttl_iri(k)
            if isinstance(v, (int, float)):
                lines.append(f"{iri} {prop_iri} {v} .")
            else:
                lines.append(f"{iri} {prop_iri} {_ttl_escape(v)} .")
    for d in _exec_dict(conn,
        "SELECT source_id, target_id, relation_type, properties FROM relations "
        "WHERE status<>'deprecated'"
    ):
        lines.append(f"{_ttl_uri(d.get('source_id',''))} {_ttl_iri(d.get('relation_type',''))} {_ttl_uri(d.get('target_id',''))} .")
    return "\n".join(lines)


def sync_to_fuseki(
    conn: sqlite3.Connection,
    base_url: str = DEFAULT_FUSEKI_URL,
    dataset: str = DEFAULT_DATASET,
    project_id=None,
    timeout: float = 30.0,
) -> dict:
    started = time.time()
    cli = FusekiClient(base_url=base_url, dataset=dataset)

    if not cli.ping(timeout=5.0):
        return {
            "ok": False,
            "error": "Fuseki 不可达: " + base_url,
            "duration_ms": int((time.time() - started) * 1000),
            "fuseki_url": base_url,
        }

    if not cli.dataset_exists():
        if not cli.create_dataset(timeout=timeout):
            return {
                "ok": False,
                "error": "无法创建 dataset: " + dataset,
                "duration_ms": int((time.time() - started) * 1000),
                "fuseki_url": base_url,
            }

    ttl = build_ontology_ttl(conn) + "\n" + build_entities_ttl(conn, project_id)
    # INSERT DATA {} 是 N-Triples 语法，不接受 @prefix 指令 → 剥离之（IRI 已全量展开）
    ntriples = _strip_turtle_prefixes(ttl)
    try:
        cli.update("CLEAR DEFAULT", timeout=timeout)
        for chunk in _chunk_lines(ntriples):
            if chunk.strip():
                cli.update(_build_insert(chunk), timeout=timeout)
    except FusekiUnavailable as e:
        return {
            "ok": False,
            "error": "Fuseki 写入失败: " + str(e),
            "duration_ms": int((time.time() - started) * 1000),
            "fuseki_url": base_url,
        }

    return {
        "ok": True,
        "triples": ttl.count("\n") // 5,
        "duration_ms": int((time.time() - started) * 1000),
        "fuseki_url": base_url,
        "dataset": dataset,
    }


def _tcp_probe(host: str, port: int, timeout: float = 0.6) -> bool:
    """快速 TCP 探测（避免 urllib 在连接丢包环境下挂起）。

    某些环境对不可达端口是静默丢包而非立即拒绝，urllib 会占用完整 timeout。
    用原生 socket connect 快速失败。
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def dependency_status() -> dict:
    # 先做快速 TCP 探测，Fail-fast；仅当端口可达再跑 HTTP ping
    from urllib.parse import urlparse
    pu = urlparse(DEFAULT_FUSEKI_URL)
    host = pu.hostname or "localhost"
    port = pu.port or 3030
    reachable = _tcp_probe(host, port)
    cli = FusekiClient()
    return {
        "fuseki_url": cli.base_url,
        "dataset": cli.dataset,
        "ping": cli.ping(timeout=1.0) if reachable else False,
        "dataset_exists": cli.dataset_exists(timeout=1.0) if (reachable and cli.ping(timeout=1.0)) else False,
    }
