# -*- coding: utf-8 -*-
"""数据源注册表 Repo（P0-4 2026-09-20）。

只做 SQL 访问，业务编排（连通性测试/预览/抽取）在 services.datasource_service。
"""
import json

from repositories.base import BaseRepo


class DataSourceRepo(BaseRepo):
    def list(self) -> list:
        """数据源列表（config 解析为 dict，前端直接消费）。"""
        rows = self.rows("SELECT * FROM data_sources ORDER BY id DESC")
        for r in rows:
            try:
                r["config"] = json.loads(r.get("config") or "{}")
            except (ValueError, TypeError):
                r["config"] = {}
        return rows

    def get(self, dsid: int):
        row = self.one("SELECT * FROM data_sources WHERE id=?", (dsid,))
        if row:
            try:
                row["config"] = json.loads(row.get("config") or "{}")
            except (ValueError, TypeError):
                row["config"] = {}
        return row

    def _config_json(self, config) -> str:
        if isinstance(config, str):
            try:
                config = json.loads(config or "{}")
            except (ValueError, TypeError):
                config = {}
        return json.dumps(config or {}, ensure_ascii=False)

    def insert(self, name: str, dtype: str, config, enabled: int, created_by: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO data_sources (name, type, config, enabled, created_by) VALUES (?,?,?,?,?)",
            (name, dtype, self._config_json(config), enabled, created_by))
        return cur.lastrowid

    def update(self, dsid: int, name: str, dtype: str, config, enabled: int) -> bool:
        cur = self.conn.execute(
            "UPDATE data_sources SET name=?, type=?, config=?, enabled=? WHERE id=?",
            (name, dtype, self._config_json(config), enabled, dsid))
        return cur.rowcount > 0

    def delete(self, dsid: int) -> bool:
        cur = self.conn.execute("DELETE FROM data_sources WHERE id=?", (dsid,))
        return cur.rowcount > 0

    def set_status(self, dsid: int, status: str):
        self.conn.execute(
            "UPDATE data_sources SET last_status=?, last_test_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, dsid))
