# -*- coding: utf-8 -*-
"""第三期「连得通」验证：图库开关 + 增量同步钩子 + 推断裁决过滤。

    .venv/Scripts/python.exe tools/verify/verify_ontology_graph_enabled.py

纪律：不写真库、不真的开启图库同步（避免改变生产部署状态）。
用「环境变量覆盖层 + 源码断言 + 真库只读」验证开关链。

判据：
  G1 默认 graph_db.enabled=False
  G2 MBSE_GRAPH_DB_ENABLED=1 能覆盖为 True（部署级）
  G3 该键在 _BOOL_KEYS（"1" 被解析为 bool 而非字符串）
  G4 main.py 启动钩子有条件判断（默认关时零副作用）
  G5 钩子返回的字段名与 sync_pending_triples 实际返回一致（防日志字段漂移）
  G6 materialize 默认 only_accepted=True，且过滤失败时拒绝而非放行
  G7 变异自证：ENV_MAP 去掉该键后 G2 不成立
"""
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else " FAIL ") + name + (f"  {detail}" if detail else ""))


def main():
    import importlib
    import core.config as cfg

    print("\n[1] 配置默认值与覆盖链")
    check("G1 默认图库关闭", cfg.get("graph_db", "enabled") is False,
          f"default={cfg.get('graph_db', 'enabled')}")
    check("G2 ENV 映射存在", cfg.ENV_MAP.get("MBSE_GRAPH_DB_ENABLED") == ("graph_db", "enabled"),
          str(cfg.ENV_MAP.get("MBSE_GRAPH_DB_ENABLED")))
    check("G3 布尔键登记", ("graph_db", "enabled") in cfg._BOOL_KEYS)

    # 实测环境变量覆盖：改 os.environ 后重新加载 _load_env（不动全局 _CONFIG）
    os.environ["MBSE_GRAPH_DB_ENABLED"] = "1"
    try:
        ov = cfg._load_env()
        val = ov.get("graph_db", {}).get("enabled")
        check("G2b ENV=1 解析为 True", val is True, f"value={val!r}")
    finally:
        os.environ.pop("MBSE_GRAPH_DB_ENABLED", None)

    print("\n[2] 启动钩子与返回字段")
    import inspect
    import main as app_main
    src = inspect.getsource(app_main.lifespan)
    check("G4 钩子有条件判断", 'as_bool("graph_db", "enabled")' in src
          and "图库总开关关闭" in src, "main.lifespan 源码")
    from graph_db import sync_pending_triples
    gs = inspect.getsource(sync_pending_triples)
    check("G5 返回字段一致", '"synced"' in gs and '"pending_left"' in gs and '"errors"' in gs,
          "graph_db.sync_pending_triples 源码")
    check("G5b 日志用真实字段", "pending_left" in src and "errors" in src,
          "main.lifespan 日志源码")

    print("\n[3] 推断裁决过滤")
    from routers.graph_workspace import reasoning_materialize
    ms = inspect.getsource(reasoning_materialize)
    check("G6a 默认 only_accepted=True", 'get("only_accepted", True)' in ms)
    check("G6b 过滤失败拒绝写入", "未写入任何推断" in ms and "HTTPException" in ms)
    check("G6c 回包带 filtered_out", '"filtered_out": filtered_out' in ms)

    print("\n[4] 变异自证：ENV_MAP 去键后覆盖失效")
    saved = cfg.ENV_MAP.get("MBSE_GRAPH_DB_ENABLED")
    cfg.ENV_MAP.pop("MBSE_GRAPH_DB_ENABLED", None)
    os.environ["MBSE_GRAPH_DB_ENABLED"] = "1"
    try:
        mutant = cfg._load_env().get("graph_db", {}).get("enabled")
        check("G7 变异击穿（去键后无覆盖）", mutant is None, f"mutant={mutant!r}")
    finally:
        cfg.ENV_MAP["MBSE_GRAPH_DB_ENABLED"] = saved
        os.environ.pop("MBSE_GRAPH_DB_ENABLED", None)

    print("\n[5] 真库只读：确认未因验证改变入图水位")
    db = ROOT / "mbse.db"
    if db.exists():
        c = sqlite3.connect(str(db))
        pending = c.execute("SELECT COUNT(*) FROM triples WHERE graph_stored=0 AND status='approved'").fetchone()[0]
        enabled_now = cfg.get("graph_db", "enabled")
        check("G8 验证未开启图库", enabled_now is False, f"enabled={enabled_now} pending={pending}")

    print(f"\n结果：{len(PASS)} pass / {len(FAIL)} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
