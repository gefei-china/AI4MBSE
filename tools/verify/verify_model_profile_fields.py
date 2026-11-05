"""验证「模型描述 / 能力标签 / 上下文档位 / 思考强度」在配置接口层的闭环（2026-10-31）。

覆盖三件最容易"看着有其实不通"的事：
  ① `description` 能否穿过 ProviderIn → LlmRepo → SQLite → list_providers 回来（新增 + 更新两条路径）。
  ② 上下文档位 **1M = 1,048,576** 是否被 `_check_context_window` 接受
     —— 旧上限恰好是 1,000,000，1M 档会被 400 拦掉（"选了 1M 保存报错"）。
  ③ `model_params` 里 `thinking_level` 与 `thinking_mode` 并存时是否都被持久化
     （前端两者都写：老链路读 mode、新链路读 level）。

用法：cd 仓库 && ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_model_profile_fields.py
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database.migrations.columns import _migrate_columns  # noqa: E402
from repositories.llm_repo import LlmRepo  # noqa: E402
from routers.llm import _check_context_window, list_providers  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  [ok] " if cond else "  [FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def main():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # 真实表结构子集（够本用例即可；列名与 mbse.db 一致）
    conn.execute("""
        CREATE TABLE llm_providers (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, provider_type TEXT DEFAULT 'openai',
          base_url TEXT, api_key TEXT DEFAULT '', model_name TEXT, max_tokens INTEGER DEFAULT 8192,
          temperature REAL DEFAULT 0.3, is_default INTEGER DEFAULT 0, status TEXT DEFAULT 'active',
          created_at TEXT DEFAULT CURRENT_TIMESTAMP, model_params TEXT DEFAULT '{}',
          model_type TEXT DEFAULT 'chat', context_window INTEGER DEFAULT 8192,
          tags TEXT DEFAULT '[]', priority INTEGER DEFAULT 0, budget_tokens INTEGER DEFAULT 0
        )""")
    _migrate_columns(conn)          # 补 description 列（与生产同一条迁移路径）

    repo = LlmRepo(conn)

    print("① description 新增 → 读取")
    pid = repo.create_provider("测试-1M", "openai", "https://api.deepseek.com/v1", "sk-x",
                               "deepseek-chat", 8192, 0.3, 1, "chat", 1048576,
                               json.dumps(["long-ctx", "code"], ensure_ascii=False), 5, 0,
                               "长上下文对话模型，适合整份技术要求的通读与比对")
    row = conn.execute("SELECT description, context_window, tags FROM llm_providers WHERE id=?", (pid,)).fetchone()
    check(row["description"].startswith("长上下文"), "新增时 description 落库: %r" % row["description"])
    check(row["context_window"] == 1048576, "1M 档位如数落库: %s" % row["context_window"])

    items = list_providers(conn=conn)
    p = next(x for x in items if x["id"] == pid)
    check(p.get("description", "").startswith("长上下文"), "list_providers 回传 description")
    check(p.get("tags") == ["long-ctx", "code"], "list_providers 把 tags 解析成数组: %s" % p.get("tags"))

    print("② description 更新路径")
    repo.update_provider(pid, "测试-1M", "openai", "https://api.deepseek.com/v1", "deepseek-chat",
                         8192, 0.3, 1, "chat", 614400, json.dumps(["long-ctx"], ensure_ascii=False),
                         5, 0, "改后的描述：600K 档")
    row = conn.execute("SELECT description, context_window FROM llm_providers WHERE id=?", (pid,)).fetchone()
    check(row["description"] == "改后的描述：600K 档", "更新时 description 覆盖生效")
    check(row["context_window"] == 614400, "600K 档位如数落库: %s" % row["context_window"])

    print("③ 上下文档位边界校验")
    for v in (1048576, 614400, 307200, 2000000):
        try:
            _check_context_window(v)
            check(True, "接受 %s" % v)
        except ValueError as e:
            check(False, "拒绝 %s（不应）: %s" % (v, e))
    for v in (100, 2000001):
        try:
            _check_context_window(v)
            check(False, "接受 %s（不应）" % v)
        except ValueError:
            check(True, "拒绝越界值 %s" % v)

    print("④ model_params 同时持久化 thinking_level / thinking_mode")
    repo.update_provider_params(pid, json.dumps(
        {"top_p": 0.9, "top_k": None, "thinking_mode": "开启", "thinking_level": "高"},
        ensure_ascii=False))
    mp = json.loads(conn.execute("SELECT model_params FROM llm_providers WHERE id=?", (pid,)).fetchone()[0])
    check(mp.get("thinking_level") == "高", "thinking_level 落库: %s" % mp.get("thinking_level"))
    check(mp.get("thinking_mode") == "开启", "thinking_mode 兼容字段仍在: %s" % mp.get("thinking_mode"))

    print("\n%s（%d 项失败）" % ("全部通过" if not FAILS else "存在失败", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
