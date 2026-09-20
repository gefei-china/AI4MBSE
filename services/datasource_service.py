# -*- coding: utf-8 -*-
"""数据源接入服务（P0-4 2026-09-20）。

SRS 对拍：KG-FQ 数据导入（数据库/接口两类多源接入）+ 数据入图处理。

设计要点：
- 三类源：file（本地文件）/ db（关系库只读连接）/ api（REST 拉取）
- 凭据不落库：config 中只存 env 变量名（token_env / password_env），运行时解析
- 抽取两条腿（缺一不可，只进向量不进图会丢「数据入图处理」）：
  ① 记录标准化为「记录型文档」→ ingest_document 向量管线（可检索）
  ② 同文档 chunks → v2g_extract → 候选实体/关系（进未评审候选区，人工确认后入图谱）
- 预览确认再提交：preview() 给样例数据，前端确认后再 ingest（SRS 用例「检查预览结果」）
- db 连接强制只读（sqlite 用 URI mode=ro）；postgres/mysql 驱动缺失时给出明确指引
"""
import json
import os
import sqlite3
import urllib.request

from services.base import BaseService
from repositories.datasource_repo import DataSourceRepo

_DS_TYPES = ("file", "db", "api")
_DEFAULT_LIMIT = 200


class DataSourceService(BaseService):
    def _repo(self) -> DataSourceRepo:
        return self.repo(DataSourceRepo)

    # ── 配置解析 ──
    def _cfg(self, ds: dict) -> dict:
        c = ds.get("config") or {}
        return c if isinstance(c, dict) else {}

    def _env(self, cfg: dict, key: str) -> str:
        """凭据解析：config 只存 env 变量名，运行时取值。"""
        name = cfg.get(key) or ""
        return os.environ.get(name, "") if name else ""

    def _limit(self, cfg: dict) -> int:
        try:
            return max(1, min(int(cfg.get("limit") or _DEFAULT_LIMIT), 2000))
        except (ValueError, TypeError):
            return _DEFAULT_LIMIT

    # ── 连通性测试 ──
    def test(self, ds: dict) -> tuple:
        """→ (ok: bool, message: str)。更新 last_status 留痕。"""
        cfg = self._cfg(ds)
        ok, msg = False, ""
        try:
            if ds["type"] == "file":
                path = cfg.get("path") or ""
                if not path:
                    msg = "file 类型需在配置中指定 path"
                elif not os.path.exists(path):
                    msg = f"文件不存在: {path}"
                else:
                    ok, msg = True, f"文件可达（{os.path.getsize(path)} 字节）"
            elif ds["type"] == "db":
                conn = self._db_connect(cfg)
                try:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
                    ok, msg = True, f"连接成功（{n} 张表）"
                finally:
                    conn.close()
            elif ds["type"] == "api":
                url = cfg.get("url") or ""
                if not url.startswith(("http://", "https://")):
                    msg = f"url 必须是 http(s):// 开头: {url}"
                else:
                    raw = self._http_get(url, cfg)
                    ok, msg = True, f"接口可达（响应 {len(raw)} 字节）"
            else:
                msg = f"未知类型: {ds['type']}"
        except Exception as e:  # 连通性测试失败不抛——结果写 last_status 留痕
            msg = f"{type(e).__name__}: {e}"
        self._repo().set_status(ds["id"], ("通过" if ok else f"失败: {msg}")[:200])
        self.conn.commit()
        return ok, msg

    # ── db 连接器（强制只读） ──
    def _db_connect(self, cfg: dict) -> sqlite3.Connection:
        cs = cfg.get("connection") or ""
        if cs.startswith("sqlite:///"):
            path = cs[len("sqlite:///"):]
            return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        if cs.startswith(("postgres://", "postgresql://", "mysql://")):
            try:
                if cs.startswith("mysql://"):
                    import pymysql  # noqa: F401 后端可选依赖（pip install pymysql）
                else:
                    import psycopg2  # noqa: F401 后端可选依赖（pip install psycopg2-binary）
                raise NotImplementedError(
                    "pg/mysql 连接器需驱动适配层（P1 批次），当前请用 sqlite 源或 file/api 源")
            except ImportError:
                raise NotImplementedError(
                    "pg/mysql 驱动未安装（后端可选依赖），当前请用 sqlite 源或 file/api 源")
        raise ValueError(f"不支持的 connection 串（当前支持 sqlite:///）: {cs[:60]}")

    # ── api 连接器（stdlib，零新依赖） ──
    def _http_get(self, url: str, cfg: dict, timeout: int = 20) -> bytes:
        headers = dict(cfg.get("headers") or {})
        token = self._env(cfg, "token_env")
        if token and not any(k.lower() == "authorization" for k in headers):
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers, method=cfg.get("method") or "GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    # ── 记录拉取（db/api → 行列表；file → None 走原文） ──
    def _fetch_records(self, cfg: dict, dtype: str) -> list:
        limit = self._limit(cfg)
        if dtype == "db":
            conn = self._db_connect(cfg)
            try:
                sql = cfg.get("sql") or (f"SELECT * FROM {cfg['table']} LIMIT {limit}"
                                         if cfg.get("table") else None)
                if not sql:
                    raise ValueError("db 源需在配置中提供 sql 或 table")
                cur = conn.execute(sql)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchmany(limit)]
            finally:
                conn.close()
        if dtype == "api":
            raw = self._http_get(cfg.get("url") or "", cfg)
            data = json.loads(raw.decode("utf-8", "ignore"))
            key = cfg.get("data_key")
            if key and isinstance(data, dict):
                data = data.get(key, [])
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                raise ValueError("api 响应不是对象数组（可配置 data_key 指定数组字段）")
            return data[:limit]
        return []

    @staticmethod
    def _records_to_text(ds_name: str, records: list) -> str:
        """记录 → 记录型文档文本（Markdown 风格，进向量管线可切片可检索）。

        8 条记录合一个小节：单记录小节 < 50 字符会被分块器 min_chunk 过滤
        （曾实测 3 记录全滤空 → 「文本为空」），合并后每节数百字符安全通过。
        """
        lines = [f"# 数据源抽取：{ds_name}", ""]
        for i, rec in enumerate(records, 1):
            if i % 8 == 1:
                lines.append(f"## 记录批次 {(i - 1) // 8 + 1}")
                lines.append("")
            fields = "；".join(f"{k}={v}" for k, v in (rec or {}).items())
            lines.append(f"记录{i}：{fields}")
        return "\n".join(lines)

    # ── 预览（确认再提交，SRS 导入用例场景 3） ──
    def preview(self, ds: dict, limit: int = 20) -> dict:
        cfg = self._cfg(ds)
        if ds["type"] == "file":
            path = cfg.get("path") or ""
            if not os.path.exists(path):
                return {"ok": False, "error": f"文件不存在: {path}"}
            with open(path, "rb") as f:
                head = f.read(4000).decode("utf-8", "ignore")
            return {"ok": True, "kind": "text", "sample": head,
                    "note": f"文件源整篇作为文档入库（{path}）"}
        try:
            records = self._fetch_records({**cfg, "limit": limit}, ds["type"])
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        cols = sorted({k for r in records for k in (r or {})}) if records else []
        return {"ok": True, "kind": "records", "columns": cols,
                "rows": records[:limit], "count": len(records),
                "note": f"将抽取前 {self._limit(cfg)} 条记录拼接为记录型文档入库"}

    # ── 抽取（两条腿） ──
    def ingest(self, ds: dict, actor: str, doc_title: str = "") -> dict:
        """拉取源内容 → 向量管线（腿①）→ v2g 候选（腿②，进未评审候选区）。"""
        if not ds.get("enabled"):
            raise ValueError("数据源已停用，不可执行抽取（请先启用）")
        cfg = self._cfg(ds)
        # 腿①：内容 → 记录型文档 → 向量管线
        if ds["type"] == "file":
            path = cfg.get("path") or ""
            if not os.path.exists(path):
                raise ValueError(f"源文件不存在: {path}")
            with open(path, "rb") as f:
                content = f.read()
            filename = os.path.basename(path)
        else:
            records = self._fetch_records(cfg, ds["type"])
            if not records:
                raise ValueError("源记录为空，未执行抽取")
            filename = f"{ds['name']}.md"
            content = self._records_to_text(doc_title or ds["name"], records).encode("utf-8")
        from knowledge_pipeline.ingest import ingest_document
        ir = ingest_document(self.conn, filename, "md", content,
                             uploaded_by=actor, branch="global")
        if ir.get("error") or ir.get("parse_status") != "completed":
            raise ValueError(f"向量管线失败: {ir.get('error') or ir.get('parse_status')}")
        doc_id = ir["doc_id"]
        # 溯源：documents.source_id 关联回数据源（R1=B 保留列恢复语义）
        self.conn.execute("UPDATE documents SET source_id=? WHERE id=?", (ds["id"], doc_id))
        # 腿②：同文档 chunks → v2g 候选（LLM 受本体 schema 约束，进未评审候选区）
        from services.knowledge_service import KnowledgeService
        vr = KnowledgeService(self.conn).v2g_extract(
            query=doc_title or ds["name"], doc_id=doc_id, actor=actor)
        self._repo().set_status(
            ds["id"], f"抽取完成: 文档 {doc_id} 块 {ir.get('chunk_count')} 候选 "
                      f"{vr.get('node_count', 0) + vr.get('edge_count', 0)}")
        self.conn.commit()
        return {"doc_id": doc_id, "chunk_count": ir.get("chunk_count"),
                "node_count": vr.get("node_count", 0), "edge_count": vr.get("edge_count", 0),
                "batch_id": vr.get("batch_id")}
