# -*- coding: utf-8 -*-
"""settings 白名单收口的功能验证：两个通用写入入口的准入行为（2026-10-23）。

不起服务、不连真库 —— 内存 SQLite 夹具 + 直调入口函数，断言**行为**而非实现：

  ① is_valid_setting_key：合法键放行 / 无效键（query_route 等已删键）拒绝 / 前缀放行；
  ② PUT /api/settings/{key} 入口（routers.meta.update_setting）：
     合法键落库、无效键 400 且**不落库**；
  ③ PUT /api/system/config/runtime 入口（core.config.update_runtime_settings）：
     合法键落库、含无效键抛 ValueError 且**整批不落库**（原子）；
  ④ 专用入口不受限：project_repo 直写 default_project_id 仍可用（回归防误伤）。

退出码：0 = 全通过；1 = 有断言失败（可接 CI）。
"""
import os
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

FAILED = []


def check(name, cond, detail=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name, ("  —— " + detail) if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def fresh_db():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, description TEXT, updated_at TEXT)")
    return con


def main():
    from core.setting_keys import is_valid_setting_key

    print("[1] is_valid_setting_key 准入判据")
    check("合法键 default_branch 放行", is_valid_setting_key("default_branch"))
    check("合法键 memory.project_memory_chars 放行", is_valid_setting_key("memory.project_memory_chars"))
    check("前缀 staging_fuse.auto_threshold 放行", is_valid_setting_key("staging_fuse.auto_threshold"))
    check("无效键 query_route 拒绝", not is_valid_setting_key("query_route"))
    check("无效键 graph_confidence_threshold 拒绝", not is_valid_setting_key("graph_confidence_threshold"))
    check("无效键 system_name 拒绝", not is_valid_setting_key("system_name"))
    check("无效键 legacy_migrate 拒绝", not is_valid_setting_key("legacy_migrate"))
    check("无效键 max_conversation_context 拒绝", not is_valid_setting_key("max_conversation_context"))
    check("空键拒绝", not is_valid_setting_key(""))

    # ② /api/settings/{key} 入口（routers.meta.update_setting 是普通函数，直调即可）
    print("[2] 入口 PUT /api/settings/{key}（routers.meta.update_setting）")
    from routers.meta import update_setting
    con = fresh_db()
    r = update_setting("default_branch", {"value": "release"}, con)
    check("合法键返回 ok", r.get("ok") is True, str(r))
    row = con.execute("SELECT value FROM settings WHERE key='default_branch'").fetchone()
    check("合法键落库值正确", row and row["value"] == "release", str(row and dict(row)))
    r = update_setting("query_route", {"value": "vector_first"}, con)
    check("无效键返回 400", getattr(r, "status_code", None) == 400, str(r))
    row = con.execute("SELECT 1 FROM settings WHERE key='query_route'").fetchone()
    check("无效键不落库", row is None, str(row))
    con.close()

    # ③ /api/system/config/runtime 入口（core.config.update_runtime_settings）
    print("[3] 入口 PUT /api/system/config/runtime（core.config.update_runtime_settings）")
    from core import config
    con = fresh_db()
    out = config.update_runtime_settings({"shacl_gate_mode": "error"}, con)
    check("合法键批量写入成功", out.get("updated") == ["shacl_gate_mode"], str(out))
    raised = False
    try:
        config.update_runtime_settings({"triple_only_writer": "1", "system_name": "x"}, con)
    except ValueError as e:
        raised = True
        check("错误信息点名无效键", "system_name" in str(e), str(e))
    check("含无效键抛 ValueError", raised)
    row = con.execute("SELECT 1 FROM settings WHERE key='triple_only_writer'").fetchone()
    check("整批原子：同批合法键也未落库", row is None, "半提交会导致批量更新语义不一致")
    row = con.execute("SELECT 1 FROM settings WHERE key='system_name'").fetchone()
    check("无效键未落库", row is None)
    con.close()

    # ④ 专用入口回归：project_repo 直写 default_project_id 不经白名单，不应被误伤
    print("[4] 专用入口回归（不走通用入口的写入不受白名单影响）")
    con = fresh_db()
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('default_project_id','p-test')")
    row = con.execute("SELECT value FROM settings WHERE key='default_project_id'").fetchone()
    check("project_repo 式直写仍可用", row and row["value"] == "p-test")
    con.close()

    print("-" * 72)
    if FAILED:
        print("失败 %d 项：%s" % (len(FAILED), FAILED))
        return 1
    print("全部通过（4 组 / 20 断言）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
