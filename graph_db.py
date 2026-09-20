# -*- coding: utf-8 -*-
"""图数据库 Writer/Reader 抽象（星网 ArcR-5 落地 · 2026-09-01）。

职责：把「设计知识库（图数据）」从 SQLite 表模拟升级为 Jena 兼容图库查询镜像。

架构原则（与《图数据库落地与双库路由_细化方案》一致）：
- SQLite（entities/relations/triples）仍是**治理与审核权威源**；
  本模块的图库是**查询镜像**——数据由物化消费链 / 存量迁移写入，只读查询走 SPARQL。
- Writer 抽象对齐澄清文档「图数据库 Writer」接口，P2 可无缝切换 Jena Fuseki / Neo4j 适配器。

IRI 约定（P0-1 / P0-2，2026-09-06 起：统一 HTTP 命名空间，且只依赖稳定 id）：
- 基命名空间 : http://www.xingwang.mbse/     （与 ontology_owl.BASE 同源）
- 本体类     : {BASE}ontology#{类局部名}      （TBox，与 OWL 导出完全一致）
- 实体实例   : {BASE}ent/{enc(id)}            （ABox，**只依赖 id**，改名不换 IRI）
- 类型谓词   : rdf:type（标准），客体为**类 IRI**（原为自造谓词 + 字面量）
- 名称载体   : rdfs:label                     （IRI 不含名称后，按名检索走这里）
- 关系谓词   : {BASE}rel/{enc(predicate)}
- 属性谓词   : {BASE}prop/{enc(predicate)}
- 命名图     : {BASE}graph/{enc(branch)}
- 元数据属性 : {BASE}meta/{key}（source_doc / triple_id / created_by / ...）

兼容：旧 `urn:mbse:*` 前缀仍可解析（parse_ent_uri / rel_name / graph_name），但不再写入。

降级：pyoxigraph 不可用时走 MockWriter（内存 dict），延续 embed 降级风格。
"""
# ── 统一命名空间（P0-1，2026-09-06）────────────────────────────
# 规则收敛到 core.ns（单一事实来源）：图库（ABox）与本体（TBox）同源，
# 否则 OWL 公理（domain/range/特性公理）对图库数据完全无效。
# 这里只做别名再导出，保证 `from graph_db import NS_ENT` 等既有写法继续可用。
import logging
import os
import re
import time
from urllib.parse import quote as _quote, unquote as _unquote

from core import ns as _ns

BASE = _ns.BASE
NS_ONTOLOGY = _ns.NS_ONTOLOGY
NS_ENT = _ns.NS_ENT
NS_PRED_REL = _ns.NS_PRED_REL
NS_PRED_PROP = _ns.NS_PRED_PROP
NS_META = _ns.NS_META
NS_GRAPH = _ns.NS_GRAPH
RDF_NS = _ns.RDF_NS
RDFS_NS = _ns.RDFS_NS
RDF_TYPE = _ns.RDF_TYPE
RDFS_LABEL = _ns.RDFS_LABEL
NS_PRED_TYPE = RDF_TYPE  # 兼容别名：类型谓词已改为标准 rdf:type

LEGACY_NS_ENT = _ns.LEGACY_NS_ENT
LEGACY_NS_PRED_TYPE = _ns.LEGACY_NS_PRED_TYPE
LEGACY_NS_PRED_REL = _ns.LEGACY_NS_PRED_REL
LEGACY_NS_PRED_PROP = _ns.LEGACY_NS_PRED_PROP
LEGACY_NS_META = _ns.LEGACY_NS_META
LEGACY_NS_GRAPH = _ns.LEGACY_NS_GRAPH

_IRI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

logger = logging.getLogger(__name__)


# 以下 IRI 构造/解析函数统一委托 core.ns（单一事实来源），此处仅保留兼容签名。
def loc_key(name) -> str:
    """名称 → 稳定局部标识（规则见 core.ns.loc_key）。"""
    return _ns.loc_key(name)


def _local(s: str) -> str:
    return _ns.local(s)


def _unlocal(s: str) -> str:
    return _ns.unlocal(s)


def ent_uri(name, eid="") -> str:
    """实体 IRI（P0-2）：**只依赖稳定 id**，改名不换 IRI，且幂等。规则见 core.ns.ent_uri。"""
    return _ns.ent_uri(name, eid)


def class_uri(name) -> str:
    """本体类 IRI（TBox）：与 ontology_owl 导出的 owl:Class 同源。规则见 core.ns.class_uri。"""
    return _ns.class_uri(name)


def pred_uri(predicate: str, object_type: str = "literal") -> str:
    """谓词 IRI：rdf:type / rdfs:label / rel:{p} / prop:{p}。规则见 core.ns.pred_uri。"""
    return _ns.pred_uri(predicate, object_type)


def graph_uri(branch: str) -> str:
    return _ns.graph_uri(branch)


def _pyoxigraph():
    """懒加载 pyoxigraph；不可用返回 None（调用方降级 Mock）。"""
    try:
        import pyoxigraph as pg
        return pg
    except Exception:
        return None


class GraphDBWriter:
    """图数据库 Writer 抽象接口（对齐澄清文档「图数据库 Writer」）。

    write_triples 输入：list[dict]，每条：
        {s, s_id, p, o, o_id, o_value, o_type("entity"|"literal"), graph, meta:{...}}
    """

    def write_triples(self, triples: list, graph: str = "dev") -> dict:
        raise NotImplementedError

    def delete_subjects(self, subjects: list, graph: str = "dev") -> dict:
        raise NotImplementedError

    def query_sparql(self, query: str, limit: int = 50) -> list:
        raise NotImplementedError

    def query_bind(self, query: str, binds: dict, limit: int = 50) -> list:
        """带变量绑定的 SPARQL 查询（中文 IRI 安全）。

        binds: {var_name: NamedNode/Literal}——实体 URI 含中文时不能写在查询文本 <...> 中
        （SPARQL 语法非法），必须参数绑定；被替换变量须出现在 SELECT 投影（pyoxigraph 约束）。
        """
        raise NotImplementedError

    def export_nq(self, path: str) -> int:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class PyoxigraphWriter(GraphDBWriter):
    """pyoxigraph 后端（Rust 原生，SPARQL 1.1 标准，零外部服务零 JVM）。"""

    def __init__(self, store_path: str = "", use_memory: bool = False):
        pg = _pyoxigraph()
        if pg is None:
            raise RuntimeError("pyoxigraph 不可用，请先安装（pip install pyoxigraph==0.5.10）")
        self._pg = pg
        if use_memory or not store_path:
            self._store = pg.Store()  # path=None → 内存
            self._path = ""
        else:
            os.makedirs(store_path, exist_ok=True)
            self._store = pg.Store(path=store_path)
            self._path = store_path

    # ── 内部转换 ──────────────────────────────────────────────
    def _to_term(self, t) -> str:
        """SPARQL 结果项 → 可读字符串。"""
        if t is None:
            return ""
        try:
            v = getattr(t, "value", None)
            if v is None and hasattr(t, "triple"):  # RDF-star / blank
                v = str(t)
            return str(v)
        except Exception:
            return str(t)

    @staticmethod
    def _select_vars(query: str) -> list:
        """从 SPARQL SELECT 子句提取变量名（按出现顺序；SELECT * 时按顺序取 ?s ?p ?o ?g）。"""
        m = re.search(r"SELECT\s+(DISTINCT\s+)?(.*?)\s+WHERE", query, re.S | re.I)
        if m:
            return re.findall(r"\?([A-Za-z_][A-Za-z0-9_]*)", m.group(2))
        if re.search(r"SELECT\s+\*", query, re.I):
            return ["s", "p", "o", "g"]
        return []

    def _quad(self, d: dict, graph: str):
        """triple dict → pyoxigraph Quad（subject, predicate, object, graph_name）。

        o_type 语义：
        - entity → 对象属性，客体是实体 IRI
        - class   → rdf:type，客体是**本体类 IRI**（P0-1：类型必须是类 IRI，不能是字面量，
                    否则 OWL 公理与推理机完全看不见类型）
        - 其它    → 数据属性，客体是字面量
        """
        pg = self._pg
        o_type = str(d.get("o_type") or "literal")
        subj = pg.NamedNode(ent_uri(d.get("s"), d.get("s_id")))
        pred = pg.NamedNode(pred_uri(d.get("p"), o_type))
        if o_type == "entity":
            obj = pg.NamedNode(ent_uri(d.get("o") or d.get("o_value"), d.get("o_id")))
        elif o_type == "class":
            obj = pg.NamedNode(class_uri(d.get("o_value") or d.get("o")))
        else:
            obj = pg.Literal(str(d.get("o_value") if d.get("o_value") is not None else ""))
        return pg.Quad(subj, pred, obj, pg.NamedNode(graph_uri(d.get("graph") or graph)))

    # ── 接口实现 ──────────────────────────────────────────────
    def write_triples(self, triples: list, graph: str = "dev") -> dict:
        if not triples:
            return {"written": 0, "skipped": 0}
        pg = self._pg
        n = 0
        n_labels = 0
        meta_triples = []
        for d in triples:
            try:
                q = self._quad(d, graph)
                self._store.add(q)
                n += 1
                # 名称载体（P0-2）：IRI 改为只依赖 id 后，按名称检索必须走 rdfs:label
                s_label = d.get("s_label")
                if s_label:
                    self._store.add(pg.Quad(q.subject, pg.NamedNode(RDFS_LABEL),
                                            pg.Literal(str(s_label)), q.graph_name))
                    n_labels += 1
                # 元数据随行写入（FR-KG-11 来源追溯）
                meta = d.get("meta") or {}
                for k, v in meta.items():
                    if v is None or v == "":
                        continue
                    mt = pg.Quad(q.subject, pg.NamedNode(NS_META + str(k)),
                                 pg.Literal(str(v)), q.graph_name)
                    self._store.add(mt)
                    meta_triples.append((k, str(v)))
            except Exception as e:
                logger.warning("graph_db.write_triples 单条失败: %s", e)
        return {"written": n, "meta": len(meta_triples), "labels": n_labels}

    def delete_subjects(self, subjects: list, graph: str = "dev") -> dict:
        """删除指定实体（含其全部谓词）。软删除同步用。

        支持传入名称或完整 URI：名称按 loc_key 前缀模糊匹配（覆盖带 id 后缀的实体 URI），
        完整 URI 精确匹配。跨命名图删除（分支无关）。
        """
        pg = self._pg
        if not subjects:
            return {"deleted": 0}
        deleted = 0
        for s in subjects:
            # 定位候选 URI：完整 URI 直接删；名称按 loc_key 前缀匹配
            if str(s).startswith(NS_ENT):
                uris = [str(s)]
            else:
                prefix = NS_ENT + loc_key(s)
                rows = self.query_sparql(
                    'SELECT DISTINCT ?s WHERE { GRAPH ?g { ?s ?p ?o . '
                    'FILTER(strstarts(str(?s), "' + prefix + '")) } }', limit=200)
                uris = [r["s"] for r in rows if r.get("s")]
            for uri in uris:
                try:
                    subj = pg.NamedNode(uri)
                except Exception:
                    continue
                # 中文 IRI 不能出现在 SPARQL 文本 <...> 中（语法非法），用 substitutions 参数绑定；
                # 注意：被替换变量 ?s 必须出现在 SELECT 投影中（pyoxigraph 约束）
                q = "SELECT ?s ?g ?p ?o WHERE { GRAPH ?g { ?s ?p ?o } }"
                try:
                    for row in self._store.query(
                            q, substitutions={pg.Variable("s"): subj}):
                        g = row["g"]
                        if g is None:
                            continue
                        self._store.remove(pg.Quad(subj, row["p"], row["o"], g))
                        deleted += 1
                except Exception as e:
                    logger.warning("graph_db.delete_subjects 失败 %s: %s", uri, e)
        return {"deleted": deleted}

    def query_sparql(self, query: str, limit: int = 50) -> list:
        """执行 SPARQL 查询，返回 [{var: value_str}, ...]（最多 limit 行）。"""
        vars_ = self._select_vars(query)
        out = []
        try:
            for row in self._store.query(query):
                if len(out) >= limit:
                    break
                rec = {}
                for v in vars_:
                    try:
                        rec[v] = self._to_term(row[v])
                    except Exception:
                        rec[v] = ""
                out.append(rec)
        except Exception as e:
            logger.warning("graph_db.query_sparql 失败: %s", e)
        return out

    def query_bind(self, query: str, binds: dict, limit: int = 50) -> list:
        """带变量绑定的 SPARQL 查询（中文 IRI 安全，P1 图遍历/实体定位用）。"""
        pg = self._pg
        vars_ = self._select_vars(query)
        subs = {}
        for k, v in (binds or {}).items():
            subs[pg.Variable(str(k))] = v
        out = []
        try:
            for row in self._store.query(query, substitutions=subs):
                if len(out) >= limit:
                    break
                rec = {}
                for v in vars_:
                    try:
                        rec[v] = self._to_term(row[v])
                    except Exception:
                        rec[v] = ""
                out.append(rec)
        except Exception as e:
            logger.warning("graph_db.query_bind 失败: %s", e)
        return out

    def export_nq(self, path: str) -> int:
        """导出 N-Quads（含命名图，Jena/Fuseki 可直接加载）。"""
        pg = self._pg
        n = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        def _fmt(t) -> str:
            if isinstance(t, pg.Literal):
                return f'"{t.value}"'
            if isinstance(t, pg.BlankNode):
                return f"_:{t.value}"
            return f"<{t.value}>"  # NamedNode / RDF-star triple

        with open(path, "w", encoding="utf-8") as f:
            try:
                for row in self._store.query(
                        "SELECT ?g ?s ?p ?o WHERE { GRAPH ?g { ?s ?p ?o } }"):
                    g = row["g"]
                    if g is None:
                        continue
                    f.write(f"{_fmt(row['s'])} {_fmt(row['p'])} {_fmt(row['o'])} "
                            f"<{g.value}> .\n")
                    n += 1
            except Exception as e:
                logger.warning("graph_db.export_nq 失败: %s", e)
        return n

    def stats(self) -> dict:
        size = 0
        try:
            rows = self.query_sparql(
                "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }", limit=1)
            if rows and rows[0].get("n"):
                size = int(rows[0]["n"])
        except Exception:
            size = 0
        graphs = []
        try:
            for r in self.query_sparql(
                    "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }", limit=100):
                graphs.append(r.get("g", ""))
        except Exception:
            pass
        return {
            "backend": "pyoxigraph",
            "path": self._path or "(memory)",
            "triple_count": size,
            "graphs": graphs,
        }

    def close(self) -> None:
        try:
            self._store.close()
        except Exception:
            pass


# ── P2：适配器切换验证（Jena Fuseki / Neo4j）────────────────────
def _iri_encode(text: str) -> str:
    """对 IRI 中的非 ASCII 字符做 percent-encode（跨引擎兼容）。

    **幂等**：URI 保留字符与已有 `%XX` 序列原样保留，已编码的 IRI 再次调用不会被二次编码
    （ent_uri / pred_uri 输出的局部名已是 ASCII，这里只兜底处理遗留的中文路径）。
    """
    from urllib.parse import quote
    return quote(str(text or ""), safe=":/?#[]@!$&'()*+,;=-._~%")


def _nt_escape(text: str) -> str:
    """N-Triples 字面量转义（引号/反斜杠/换行）。"""
    return (str(text or "")
            .replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


class FusekiWriter(GraphDBWriter):
    """Jena Fuseki HTTP SPARQL 1.1 Protocol 适配器（P2 适配器切换验证）。

    - query:  POST {endpoint}/query  (Content-Type: application/sparql-query)
    - update: POST {endpoint}/update (Content-Type: application/sparql-update)
    - 中文 IRI 统一 percent-encode（pyoxigraph 拒绝非 ASCII IRIREF，Fuseki/ARQ 虽允许但跨引擎统一）
    - 结果解析 application/sparql-results+json；uri 值读取时 unquote 还原为裸中文（与 pyoxigraph 后端一致）
    - 零新增依赖（标准库 urllib），符合私有化/信创约束
    """

    def __init__(self, endpoint: str = "", update_endpoint: str = "",
                 timeout: int = 15, username: str = "", password: str = ""):
        self.endpoint = (endpoint or "").rstrip("/")
        self.update_endpoint = (update_endpoint or self.endpoint + "/update").rstrip("/")
        self.timeout = timeout
        self._auth = None
        if username:
            import base64
            self._auth = "Basic " + base64.b64encode(
                f"{username}:{password}".encode("utf-8")).decode("ascii")
        if not self.endpoint:
            raise RuntimeError("FusekiWriter 需要 endpoint 配置（graph_db.endpoint）")

    # ── HTTP 传输 ──────────────────────────────────────────────
    def _post(self, url: str, body: str, ctype: str) -> str:
        import urllib.request
        import urllib.error
        headers = {"Content-Type": ctype,
                   "Accept": "application/sparql-results+json, application/n-quads, */*"}
        if self._auth:
            headers["Authorization"] = self._auth
        req = urllib.request.Request(url, data=body.encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")[:300]
            raise RuntimeError(f"Fuseki HTTP {e.code}: {msg}")

    @staticmethod
    def _parse_json_results(text: str) -> list:
        import json
        from urllib.parse import unquote
        data = json.loads(text or "{}")
        vars_ = (data.get("head") or {}).get("vars", [])
        out = []
        for b in ((data.get("results") or {}).get("bindings") or []):
            rec = {}
            for v in vars_:
                cell = b.get(v) or {}
                val = cell.get("value", "")
                rec[v] = unquote(val) if cell.get("type") == "uri" else val
            out.append(rec)
        return out

    # ── Term 构造 ──────────────────────────────────────────────
    def _subj(self, d: dict) -> str:
        return f"<{_iri_encode(ent_uri(d.get('s'), d.get('s_id')))}>"

    def _pred(self, d: dict) -> str:
        return f"<{_iri_encode(pred_uri(d.get('p'), str(d.get('o_type') or 'literal')))}>"

    def _obj(self, d: dict) -> str:
        o_type = str(d.get("o_type") or "literal")
        if o_type == "entity":
            return f"<{_iri_encode(ent_uri(d.get('o') or d.get('o_value'), d.get('o_id')))}>"
        if o_type == "class":
            # P0-1：类型客体是本体类 IRI，不是字面量
            return f"<{_iri_encode(class_uri(d.get('o_value') or d.get('o')))}>"
        return f'"{_nt_escape(d.get("o_value") if d.get("o_value") is not None else "")}"'

    # ── 接口实现 ──────────────────────────────────────────────
    def write_triples(self, triples: list, graph: str = "dev") -> dict:
        if not triples:
            return {"written": 0, "skipped": 0}
        parts, n_meta = [], 0
        for d in triples:
            g = _iri_encode(graph_uri(d.get("graph") or graph))
            s = self._subj(d)
            parts.append(f"GRAPH <{g}> {{ {s} {self._pred(d)} {self._obj(d)} . }}")
            # 名称载体（P0-2）：按名检索走 rdfs:label
            if d.get("s_label"):
                parts.append(f'GRAPH <{g}> {{ {s} <{RDFS_LABEL}> '
                             f'"{_nt_escape(str(d.get("s_label")))}" . }}')
            for k, v in (d.get("meta") or {}).items():
                if v is None or v == "":
                    continue
                parts.append(f"GRAPH <{g}> {{ {s} <{NS_META}{k}> "
                             f'"{_nt_escape(str(v))}" . }}')
                n_meta += 1
        # update_endpoint 已含 /update（endpoint + "/update"）
        self._post(self.update_endpoint,
                   "INSERT DATA { " + " ".join(parts) + " }",
                   "application/sparql-update")
        return {"written": len(triples), "meta": n_meta}
    def delete_subjects(self, subjects: list, graph: str = "dev") -> dict:
        deleted = 0
        for s in subjects:
            if str(s).startswith(NS_ENT):
                uris = [str(s)]
            else:
                prefix = NS_ENT + str(s)
                rows = self.query_sparql(
                    'SELECT DISTINCT ?s WHERE { GRAPH ?g { ?s ?p ?o . '
                    'FILTER(strstarts(str(?s), "' + _iri_encode(prefix) + '")) } }')
                uris = [r["s"] for r in rows if r.get("s")]
            for uri in uris:
                enc = _iri_encode(uri)
                # DELETE 模板（?g/?p/?o 遍历删除，一条语句删全部）；update_endpoint 已含 /update
                self._post(self.update_endpoint,
                           f"DELETE {{ GRAPH ?g {{ <{enc}> ?p ?o }} }} "
                           f"WHERE {{ GRAPH ?g {{ <{enc}> ?p ?o }} }}",
                           "application/sparql-update")
                deleted += 1
        return {"deleted": deleted}

    def _query(self, sparql: str, limit: int = 50) -> list:
        q = sparql
        if "LIMIT" not in q.upper() and limit < 10 ** 9:
            q = q.rstrip().rstrip(";") + f" LIMIT {int(limit)}"
        text = self._post(self.endpoint + "/query", q, "application/sparql-query")
        return self._parse_json_results(text)

    def query_sparql(self, query: str, limit: int = 50) -> list:
        try:
            return self._query(query, limit)
        except Exception as e:
            logger.warning("graph_db.fuseki.query_sparql 失败: %s", e)
            return []

    def query_bind(self, query: str, binds: dict, limit: int = 50) -> list:
        """Fuseki HTTP 无 substitutions——把绑定项注入查询文本（IRI 编码）。"""
        q = query
        for k, v in (binds or {}).items():
            cls = type(v).__name__.lower()
            if "namednode" in cls:
                q = q.replace(f"?{k}", f"<{_iri_encode(v.value)}>")
            else:
                q = q.replace(f"?{k}", f'"{_nt_escape(getattr(v, "value", ""))}"')
        return self.query_sparql(q, limit)

    def export_nq(self, path: str) -> int:
        import os
        n = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        rows = self._query("SELECT ?g ?s ?p ?o WHERE { GRAPH ?g { ?s ?p ?o } }",
                           limit=10 ** 9)
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(f"<{_iri_encode(r.get('s',''))}> <{_iri_encode(r.get('p',''))}> "
                        f"<{_iri_encode(r.get('o',''))}> <{_iri_encode(r.get('g',''))}> .\n")
                n += 1
        return n

    def stats(self) -> dict:
        size = 0
        rows = self.query_sparql(
            "SELECT (COUNT(*) AS ?n) WHERE { GRAPH ?g { ?s ?p ?o } }", limit=1)
        if rows and rows[0].get("n"):
            try:
                size = int(rows[0]["n"])
            except (TypeError, ValueError):
                size = 0
        graphs = [r.get("g", "") for r in
                  self.query_sparql(
                      "SELECT DISTINCT ?g WHERE { GRAPH ?g { ?s ?p ?o } }", limit=100)]
        return {"backend": "fuseki", "path": self.endpoint,
                "triple_count": size, "graphs": graphs}

    def close(self) -> None:
        pass


class Neo4jWriter(GraphDBWriter):
    """Neo4j 属性图适配器（P2 骨架）：懒加载 neo4j driver，Cypher MERGE 写入。

    属性图映射（简化）：实体 → (:Entity {uri, name}) + (:Type {name}) 类型标注；
    关系 → (:Entity)-[:REL {type}]->(:Entity)。完整 RDF 语义映射建议走
    N-Quads + neosemantics(n10s) 导入（export_nq 产出可直接喂 n10s）。

    依赖：pip install neo4j（未安装时明确报错，不静默降级——骨架定位）。
    """

    def __init__(self, uri: str = "bolt://localhost:7687", user: str = "neo4j",
                 password: str = "", database: str = "neo4j", timeout: int = 15):
        self.uri, self.user, self.database = uri, user, database
        try:
            from neo4j import GraphDatabase
        except ImportError:
            raise RuntimeError(
                "Neo4jWriter 需要 neo4j driver（pip install neo4j）；"
                "或使用 N-Quads + neosemantics(n10s) 导入（export_nq 产出）")
        self._driver = GraphDatabase.driver(uri, auth=(user, password),
                                            connection_timeout=timeout)

    def _cypher(self, cypher: str, params: dict | None = None) -> list:
        with self._driver.session(database=self.database) as s:
            return list(s.run(cypher, params or {}))

    def write_triples(self, triples: list, graph: str = "dev") -> dict:
        n = 0
        for d in triples:
            s_uri = ent_uri(d.get("s"), d.get("s_id"))
            o_type = str(d.get("o_type") or "literal")
            if o_type == "entity":
                o_uri = ent_uri(d.get("o") or d.get("o_value"), d.get("o_id"))
                self._cypher(
                    "MERGE (s:Entity {uri:$s}) MERGE (o:Entity {uri:$o}) "
                    "MERGE (s)-[r:REL {type:$p}]->(o) "
                    "SET s.name=$sn, o.name=$on",
                    {"s": s_uri, "o": o_uri, "p": str(d.get("p")),
                     "sn": str(d.get("s")), "on": str(d.get("o") or d.get("o_value"))})
            else:
                self._cypher(
                    "MERGE (s:Entity {uri:$s}) SET s.$p = $v",
                    {"s": s_uri, "p": str(d.get("p")),
                     "v": str(d.get("o_value") if d.get("o_value") is not None else "")})
            n += 1
        return {"written": n, "meta": 0}

    def delete_subjects(self, subjects: list, graph: str = "dev") -> dict:
        n = 0
        for s in subjects:
            res = self._cypher("MATCH (e:Entity {uri:$u}) DETACH DELETE e RETURN count(e)",
                               {"u": ent_uri(s, "")})
            n += res[0][0] if res else 0
        return {"deleted": n}

    def query_sparql(self, query: str, limit: int = 50) -> list:
        # 骨架：SPARQL 需经 n10s.translate 或 Cypher 转换，直接查询返回空并提示
        logger.warning("Neo4jWriter 不支持原生 SPARQL——请用 Cypher 或 n10s.translate")
        return []

    def query_bind(self, query: str, binds: dict, limit: int = 50) -> list:
        return []

    def export_nq(self, path: str) -> int:
        import os
        n = 0
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        rows = self._cypher(
            "MATCH (s)-[r]->(o) "
            "RETURN s.uri AS s, type(r) AS p, o.uri AS o")
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(f"<{r['s']}> <{NS_PRED_REL}{_iri_encode(r['p'])}> <{r['o']}> "
                        f"<{graph_uri('dev')}> .\n")
                n += 1
        return n

    def stats(self) -> dict:
        nodes = self._cypher("MATCH (e:Entity) RETURN count(e) AS n")[0]["n"]
        rels = self._cypher("MATCH ()-[r:REL]->() RETURN count(r) AS n")[0]["n"]
        return {"backend": "neo4j", "path": self.uri,
                "triple_count": int(nodes) + int(rels), "graphs": ["neo4j"]}

    def close(self) -> None:
        try:
            self._driver.close()
        except Exception:
            pass


class MockWriter(GraphDBWriter):
    """无依赖降级：内存 dict 存储，query_sparql 返回空（保证主流程不中断）。"""

    def __init__(self):
        self._data = {}  # {(graph, s, p, o): 1}

    def write_triples(self, triples: list, graph: str = "dev") -> dict:
        """与 PyoxigraphWriter 保持同一 IRI 语义（便于用 Mock 做无依赖单测）。"""
        n = 0
        for d in triples:
            o_type = str(d.get("o_type") or "literal")
            g = graph_uri(d.get("graph") or graph)
            s = ent_uri(d.get("s"), d.get("s_id"))
            p = pred_uri(d.get("p"), o_type)
            if o_type == "entity":
                o = ent_uri(d.get("o") or d.get("o_value"), d.get("o_id"))
            elif o_type == "class":
                o = class_uri(d.get("o_value") or d.get("o"))
            else:
                o = str(d.get("o_value") if d.get("o_value") is not None else "")
            key = (g, s, p, o)
            if key not in self._data:
                self._data[key] = 1
                n += 1
            if d.get("s_label"):
                lk = (g, s, RDFS_LABEL, str(d.get("s_label")))
                if lk not in self._data:
                    self._data[lk] = 1
        return {"written": n, "meta": 0}

    def delete_subjects(self, subjects: list, graph: str = "dev") -> dict:
        before = len(self._data)
        self._data = {k: v for k, v in self._data.items()
                      if str(k[1]) not in {str(s) for s in subjects}}
        return {"deleted": before - len(self._data)}

    def query_sparql(self, query: str, limit: int = 50) -> list:
        return []

    def query_bind(self, query: str, binds: dict, limit: int = 50) -> list:
        return []

    def export_nq(self, path: str) -> int:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for (g, s, p, o) in self._data:
                f.write(f"{s} {p} \"{o}\" <{g}> .\n")
        return len(self._data)

    def stats(self) -> dict:
        return {"backend": "mock", "path": "(memory)", "triple_count": len(self._data),
                "graphs": sorted({k[0] for k in self._data})}

    def close(self) -> None:
        pass


# ── 工厂 ─────────────────────────────────────────────────────
def get_writer(enabled: bool = True, backend: str = "pyoxigraph",
               store_path: str = "", use_memory: bool = False,
               endpoint: str = "", username: str = "", password: str = "",
               neo4j_uri: str = "", neo4j_user: str = "", neo4j_password: str = "") -> GraphDBWriter:
    """按配置创建 Writer；不可用/未启用时降级 Mock（保持接口一致，调用方无需分支）。

    backend: pyoxigraph（默认，嵌入式 SPARQL）| fuseki（HTTP SPARQL 协议）| neo4j（Cypher 骨架）。
    """
    if not enabled:
        return MockWriter()
    if backend == "fuseki":
        try:
            return FusekiWriter(endpoint=endpoint or "http://localhost:3030/ds",
                                username=username, password=password)
        except Exception as e:
            logger.warning("graph_db.get_writer 降级 Mock（fuseki）: %s", e)
    elif backend == "neo4j":
        try:
            return Neo4jWriter(uri=neo4j_uri or "bolt://localhost:7687",
                               user=neo4j_user or "neo4j", password=neo4j_password)
        except Exception as e:
            logger.warning("graph_db.get_writer 降级 Mock（neo4j）: %s", e)
    else:
        try:
            return PyoxigraphWriter(store_path=store_path, use_memory=use_memory)
        except Exception as e:
            logger.warning("graph_db.get_writer 降级 Mock: %s", e)
    return MockWriter()


# ── 物化消费链：triples(graph_stored=0, approved) → TDB ─────────
def sync_pending_triples(conn, writer: GraphDBWriter | None = None,
                         batch: int | None = None) -> dict:
    """消费 triples 表已审核未入图的三元组，增量写入图数据库并置 graph_stored=1。

    - 分支解析：优先取 subject 实体在 entities 表的分支（命名图=branch），无则 dev。
    - 幂等：triple_id 在 SQLite 已 UNIQUE；TDB 侧重复 add 覆盖不产生脏数据。
    - 返回 {"synced", "pending_left", "errors"}。
    """
    from core import config
    if writer is None:
        writer = get_writer(config.as_bool("graph_db", "enabled"),
                            config.get("graph_db", "backend"),
                            config.get("graph_db", "path"),
                            config.get("graph_db", "use_memory"))
    batch = batch or int(config.get("graph_db", "sync_batch", 500) or 500)
    synced, errors = 0, 0
    while True:
        rows = conn.execute(
            "SELECT t.*, (SELECT e.branch FROM entities e WHERE e.id=t.subject_id LIMIT 1) AS br "
            "FROM triples t WHERE t.graph_stored=0 AND t.status='approved' "
            "ORDER BY t.id LIMIT ?", (batch,)).fetchall()
        if not rows:
            break
        rdf_batch = []
        ids = []
        for r in rows:
            o_type = str(r["object_type"] or "literal")
            pred = str(r["predicate"] or "")
            meta = {
                "source_doc": r["source_doc"] or "",
                "source_type": r["source_type"] or "",
                "sysml_version_id": r["sysml_version_id"] or 0,
                "created_by": r["created_by"] or "",
                "triple_id": r["triple_id"] or "",
            }
            rdf_batch.append({
                "s": r["subject_name"], "s_id": r["subject_id"],
                # P0-2：IRI 只依赖 id，名称以 rdfs:label 承载（按名检索走 label）
                "s_label": r["subject_name"],
                "p": pred,
                "o": r["object_id"], "o_value": r["object_value"],
                # P0-1：类型三元组的客体必须是本体类 IRI（原来写成字面量，推理机看不见）
                "o_type": "class" if pred in ("type", "a", "rdf:type") else o_type,
                "graph": r["br"] or "dev",
                "meta": meta,
            })
            ids.append(r["triple_id"])
        try:
            res = writer.write_triples(rdf_batch)
            synced += res["written"]
            # 置位（仅成功写入的批次）
            ph = ",".join("?" * len(ids))
            conn.execute(f"UPDATE triples SET graph_stored=1 WHERE triple_id IN ({ph})", ids)
            conn.commit()
        except Exception as e:
            logger.warning("graph_db.sync_pending_triples 批次失败: %s", e)
            errors += len(ids)
            break
    return {"synced": synced, "pending_left": _pending_count(conn), "errors": errors}


def _pending_count(conn) -> int:
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM triples WHERE graph_stored=0 AND status='approved'"
        ).fetchone()[0]
    except Exception:
        return 0


# ── P1：SPARQL 实体链接 + 影响网络分层 BFS（真图遍历）──────────────
def parse_ent_uri(uri: str) -> tuple:
    """实体 IRI → (name, id)。规则见 core.ns.parse_ent_uri（兼容 legacy 前缀）。"""
    return _ns.parse_ent_uri(uri)


def rel_name(pred_uri: str) -> str:
    """谓词 IRI → 关系/属性名（rel:{t} → {t}；rdf:type → type）。规则见 core.ns.rel_name。"""
    return _ns.rel_name(pred_uri)


def graph_name(graph_iri: str) -> str:
    """命名图 IRI → 分支名（兼容新旧前缀）。"""
    return _ns.graph_name(graph_iri)


def _tokens(text: str) -> list:
    """中文 2-4 字滑窗 + 英文词（与 GraphRAG._tokens 同思路，图库侧独立实现避免循环依赖）。

    排序：3 字优先（实体名常见长度，如「转发器/变频器」），同组内长词优先——
    避免 4 字噪声词（如「有什么依」）抢占前 N 导致真实实体名 token 排后被截断。
    """
    toks = set()
    for w in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{1,}", (text or "").lower()):
        toks.add(w)
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", text or "")
    n = len(cjk)
    for i in range(n):
        for L in (2, 3, 4):
            w = cjk[i:i + L]
            if len(w) == L:
                toks.add(w)
    return sorted(toks, key=lambda t: (0 if len(t) == 3 else 1, -len(t)))


def _branch_vals(branches: list) -> str:
    bs = [b for b in (branches or []) if b] or ["release"]
    return " ".join(f"<{graph_uri(b)}>" for b in bs)


def query_entities_by_name(writer: GraphDBWriter, query: str,
                           branches: list | None = None, limit: int = 10) -> list:
    """SPARQL 实体链接（P1）：按名称匹配实体，限定分支命名图，返回实体类型。

    两级匹配：整句 loc_key 包含 → 无命中时用 2-4 字 token OR 补充（长句 query 友好）。
    返回 [{name, id, uri, branch, entity_type}]；query 空/writer 为 Mock 返回 []。
    """
    q = str(query or "").strip()
    if not q:
        return []
    lk = loc_key(q)
    vals = _branch_vals(branches)

    def _run_by_label(term: str):
        """新路径（P0-2）：按 rdfs:label 匹配 + rdf:type 取类 IRI。"""
        return writer.query_sparql(
            f"SELECT DISTINCT ?s ?t ?l ?g WHERE {{ VALUES ?g {{ {vals} }} "
            f"GRAPH ?g {{ ?s <{RDF_TYPE}> ?t . ?s <{RDFS_LABEL}> ?l . "
            f'FILTER(contains(str(?l), "{term}")) }} }} LIMIT {int(limit)}', limit=limit)

    def _run_legacy(term: str):
        """旧路径兜底：存量镜像仍是 urn:mbse 前缀 + 类型字面量时不会查空。"""
        return writer.query_sparql(
            f"SELECT DISTINCT ?s ?t ?g WHERE {{ VALUES ?g {{ {vals} }} "
            f"GRAPH ?g {{ ?s <{LEGACY_NS_PRED_TYPE}> ?t }} "
            f'FILTER(contains(str(?s), "{term}")) }} LIMIT {int(limit)}', limit=limit)

    def _pick(term: str):
        rows = _run_by_label(term)
        if not rows:
            rows = _run_legacy(term)
        return rows

    rows = _pick(lk)
    if not rows:
        for t in _tokens(q)[:8]:
            if len(t) >= 3 and t != lk:
                rows = _pick(t)
                if rows:
                    break
    out, seen = [], set()
    for r in rows:
        uri = r.get("s", "")
        _n, eid = parse_ent_uri(uri)
        name = r.get("l") or _n
        if (not name and not eid) or uri in seen:
            continue
        seen.add(uri)
        out.append({
            "name": name, "id": eid or name, "uri": uri,
            "branch": graph_name(r.get("g", "")),
            "entity_type": r.get("t", ""),
        })
        if len(out) >= limit:
            break
    return out


def _out_edges(writer: GraphDBWriter, uri: str, branches: list | None = None) -> list:
    """出边 (object_uri, pred_uri)。substitutions 绑定 subject（中文 IRI 安全）。

    注意：被替换变量 ?s 必须出现在 SELECT 投影（pyoxigraph 约束）；
    branches 非空时用 VALUES ?g 限定命名图（FR-KG-14 分支隔离）。
    """
    pg = _pyoxigraph()
    if pg is None or not hasattr(writer, "query_bind"):
        return []
    g_vals = f"VALUES ?g {{ {_branch_vals(branches)} }} " if branches else ""
    rows = writer.query_bind(
        f"SELECT ?s ?o ?p WHERE {{ {g_vals}GRAPH ?g {{ ?s ?p ?o . "
        f'FILTER(strstarts(str(?p), "{NS_PRED_REL}") || '
        f'strstarts(str(?p), "{LEGACY_NS_PRED_REL}")) }} }}',
        {"s": pg.NamedNode(uri)}, limit=500)
    return [(r["o"], r["p"]) for r in rows if r.get("o")]


def _in_edges(writer: GraphDBWriter, uri: str, branches: list | None = None) -> list:
    """入边 (subject_uri, pred_uri)。substitutions 绑定 object。"""
    pg = _pyoxigraph()
    if pg is None or not hasattr(writer, "query_bind"):
        return []
    g_vals = f"VALUES ?g {{ {_branch_vals(branches)} }} " if branches else ""
    rows = writer.query_bind(
        f"SELECT ?s ?o ?p WHERE {{ {g_vals}GRAPH ?g {{ ?s ?p ?o . "
        f'FILTER(strstarts(str(?p), "{NS_PRED_REL}") || '
        f'strstarts(str(?p), "{LEGACY_NS_PRED_REL}")) }} }}',
        {"o": pg.NamedNode(uri)}, limit=500)
    return [(r["s"], r["p"]) for r in rows if r.get("s")]


def fetch_impact_network(writer: GraphDBWriter, conn, source: dict,
                         depth: int = 3, direction: str = "both",
                         branches: list | None = None) -> dict:
    """SPARQL 分层 BFS 影响网络发现（FR-CIA-1 N 层传播，真图遍历）。

    source: {id, name}（已匹配变更源）→ 定位 URI → 逐层扩展出/入边（≤max_depth_guard 层防爆）。
    返回 {nodes, edges}，属性从 SQLite 权威源回填（nodes: {id,name,entity_type,status}；
    edges: {source_id,target_id,relation_type}），与 impact_engine.analyze_graph 输入兼容。
    未启用/无命中/失败返回空网络（调用方回退 SQLite 全量）。
    """
    if writer is None or not hasattr(writer, "query_bind"):
        return {"nodes": [], "edges": []}
    sid = str((source or {}).get("id") or "")
    sname = str((source or {}).get("name") or "")
    hits = query_entities_by_name(writer, sname or sid, branches=branches, limit=5)
    src_uri = ""
    for h in hits:
        if sid and h["id"] == sid:
            src_uri = h["uri"]
            break
        if not sid and h["name"] == sname:
            src_uri = h["uri"]
            break
    if not src_uri and hits:
        src_uri = hits[0]["uri"]
    if not src_uri:
        return {"nodes": [], "edges": []}

    dmax = int(depth or 3)
    if dmax <= 0 or dmax > 10:
        dmax = 10  # 深度保护（0=不限 → 上限 10 层）
    frontier, seen = {src_uri}, {src_uri}
    uri_edges = []  # (from_uri, to_uri, rel_type)
    for d in range(dmax + 1):
        nxt = set()
        for u in frontier:
            if direction in ("both", "down"):
                for o_uri, pred in _out_edges(writer, u, branches=branches):
                    uri_edges.append((u, o_uri, rel_name(pred)))
                    if o_uri not in seen:
                        seen.add(o_uri)
                        nxt.add(o_uri)
            if direction in ("both", "up"):
                for s_uri, pred in _in_edges(writer, u, branches=branches):
                    uri_edges.append((s_uri, u, rel_name(pred)))
                    if s_uri not in seen:
                        seen.add(s_uri)
                        nxt.add(s_uri)
        frontier = nxt
        if not frontier:
            break

    # 属性回填（SQLite 权威源）
    nodes, id_seen = [], set()
    for u in seen:
        _, eid = parse_ent_uri(u)
        key = eid or parse_ent_uri(u)[0]
        row = None
        if conn is not None and eid:
            try:
                row = conn.execute(
                    "SELECT id, name, entity_type, status FROM entities WHERE id=? LIMIT 1",
                    (eid,)).fetchone()
            except Exception:
                row = None
        if row is not None:
            nodes.append({"id": row["id"], "name": row["name"],
                          "entity_type": row["entity_type"] or "", "status": row["status"]})
        else:
            nm, _ = parse_ent_uri(u)
            nodes.append({"id": key, "name": nm, "entity_type": "", "status": "reviewed"})
        id_seen.add(nodes[-1]["id"])
    edges = []
    for fu, tu, rtype in uri_edges:
        fname, feid = parse_ent_uri(fu)
        tname, teid = parse_ent_uri(tu)
        edges.append({"source_id": feid or fname, "target_id": teid or tname,
                      "relation_type": rtype})
    node_ids = {n["id"] for n in nodes}
    edges = [e for e in edges if e["source_id"] in node_ids and e["target_id"] in node_ids]
    return {"nodes": nodes, "edges": edges}
