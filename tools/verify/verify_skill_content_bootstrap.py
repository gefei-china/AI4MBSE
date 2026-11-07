# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：需真实服务 / 真 LLM key / 生产真库金标或随包运行时资产，CI 无服务无 secret ⇒ 跑不了；本机已实测通过，改法须同时改清单 tier 并接线，不允许只删这行
# -*- coding: utf-8 -*-
"""门禁：资产自举**不得**再用快照覆盖人工编辑的技能正文（2026-11-06）。

## 事故（实测，非假设）

界面把「智源平台管理」正文合并成 **5744 字（v1.4，含「★ 输出铁律」）**，
重启服务后变回 **4958 字（v1.3，无铁律）**，且 `updated_at` 不变（看起来"没人改过"）。

- 真凶：`database/bootstrap_assets.py` 的 UPSERT，`skills` 表以 name 为键、
  `content` 在"声明性列"里 ⇒ 每次 `init_db()` 按快照覆盖。
- **触发条件是被上一处修复创造的**：该函数原有"同名多行一律跳过"的保护，
  此前 3 行同名 ⇒ 被偶然挡住；清理重复草稿只剩 1 行后 ⇒ 保护失效、正文被覆盖。
- 证据：SQLite 触发器 `_skill_probe` 记到 `old_n=5744 / new_n=4958`，时间戳
  与服务启动时刻（`/api/system/boot` 的 `started_at`）**同一秒**。

## 判据（三层，缺一层就会假绿）

| # | 断言 | 防的失效 |
|---|---|---|
| B1 | `SPEC['skills']` 声明了 `update_exclude` 且含 content/frontmatter/version | 有人删掉开关 |
| B2 | 行为：临时库里把正文改成标记串 → 跑 `bootstrap_assets` → 标记串仍在 | 开关声明了但没用上 |
| B3 | 行为：**空库**（无该技能行）跑 `bootstrap_assets` → 行被创建且**带正文** | 修过火，把"空库重建"也砍掉 |
| B4 | 变异：把 `update_exclude` 去掉再跑 B2 的同一场景 → **必须被覆盖** | 断言空转（场景本身不触发） |
| B5 | 其它配置类列仍按快照更新（description/triggers） | 一刀切冻结，导致配置升级失效 |

B4 是关键：只有"同一段落、唯一变量是开关"时，B2 的绿才有意义。

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_skill_content_bootstrap.py
"""
import json
import os
import shutil
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)

PASS, FAIL = "PASS", "FAIL"
_res = []
NAME = "智源平台管理"
MARK = "\n<!-- GATE-MARK：人工编辑内容，自举不得覆盖 -->"


def _rec(name, ok, detail=""):
    _res.append(ok)
    print("  [%s] %s%s" % (PASS if ok else FAIL, name, ("  | " + detail) if detail else ""))
    return ok


def _make_db(tmp_path: str, with_row: bool) -> str:
    """造一个最小 skills 库：有行 / 无行 两种。"""
    src = os.path.join(_ROOT, "mbse.db")
    dst = os.path.join(tmp_path, "gate_skill.db")
    shutil.copyfile(src, dst)
    c = sqlite3.connect(dst)
    if with_row:
        c.execute("UPDATE skills SET content=content||?, version='v9.9-gate' WHERE name=?",
                  (MARK, NAME))
    else:
        c.execute("DELETE FROM skills WHERE name=?", (NAME,))
    c.commit()
    c.close()
    return dst


def _run_bootstrap(db_path: str, drop_exclude: bool = False) -> dict:
    import importlib

    import database.bootstrap_assets as ba
    importlib.reload(ba)
    if drop_exclude:
        ba.SPEC["skills"].pop("update_exclude", None)
    conn = sqlite3.connect(db_path)
    try:
        return ba.bootstrap_assets(conn)
    finally:
        conn.close()


def _row(db_path):
    c = sqlite3.connect("file:%s?mode=ro" % db_path.replace("\\", "/"), uri=True)
    r = c.execute("SELECT length(content), version, description, triggers "
                  "FROM skills WHERE name=?", (NAME,)).fetchone()
    c.close()
    return r


def main() -> int:
    import tempfile

    tmp = tempfile.mkdtemp(prefix="gate_skill_boot_")
    try:
        # ── B1 声明 ──
        import database.bootstrap_assets as ba
        ex = set((ba.SPEC.get("skills") or {}).get("update_exclude") or ())
        _rec("B1 SPEC[skills].update_exclude 含 content/frontmatter/version",
             {"content", "frontmatter", "version"} <= ex, str(sorted(ex)))

        # ── B2 行为：有行 + 标记 → 不被覆盖 ──
        db = _make_db(tmp, with_row=True)
        before = _row(db)
        out = _run_bootstrap(db)
        after = _row(db)
        _rec("B2 已存在行：正文与版本不被快照覆盖",
             after[0] == before[0] and after[1] == "v9.9-gate",
             "before_len=%s after_len=%s ver=%s" % (before[0], after[0], after[1]))
        _rec("B2b 统计里有 content_kept（可观测，不静默）", (out.get("content_kept") or 0) >= 1,
             "content_kept=%s updated=%s" % (out.get("content_kept"), out.get("updated")))

        # ── B5 配置类列仍更新（不是一刀切冻结）──
        #   快照里的 description 与库内不同才可判；这里用"库内被改成哨兵值"来构造差异。
        c = sqlite3.connect(db)
        c.execute("UPDATE skills SET description='__SENTINEL__' WHERE name=?", (NAME,))
        c.commit()
        c.close()
        _run_bootstrap(db)
        d = _row(db)[2]
        _rec("B5 配置类列（description）仍按快照更新", d != "__SENTINEL__", (d or "")[:40])

        # ── B3 空库：建行时**必须带正文**（重建能力不能被砍）──
        db2 = _make_db(tmp, with_row=False)
        out2 = _run_bootstrap(db2)
        r2 = _row(db2)
        _rec("B3 空库：创建行且带正文（重建能力保留）",
             bool(r2) and (r2[0] or 0) > 1000 and (out2.get("inserted") or 0) >= 1,
             "len=%s inserted=%s" % (r2[0] if r2 else None, out2.get("inserted")))

        # ── B4 变异：去掉开关 ⇒ 同一场景必须被覆盖（证明 B2 不是空转）──
        db3 = _make_db(tmp, with_row=True)
        before3 = _row(db3)
        _run_bootstrap(db3, drop_exclude=True)
        after3 = _row(db3)
        _rec("B4 变异测试：去掉开关后正文被覆盖（B2 非空转）",
             after3[0] != before3[0],
             "before=%s after=%s" % (before3[0], after3[0]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = all(_res)
    print("\n" + "=" * 60 + "  ALL_OK=%s  (%d passed / %d failed)"
          % (ok, sum(1 for x in _res if x), sum(1 for x in _res if not x)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
