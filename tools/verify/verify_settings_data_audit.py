# -*- coding: utf-8 -*-
"""设置模块数据审计：settings 表每个键的「有效性 + 合理性」可复现验证。

用法：
    python tools/verify/verify_settings_data_audit.py            # 审计报告（人读）
    python tools/verify/verify_settings_data_audit.py --json     # 机器可读结果
    python tools/verify/verify_settings_data_audit.py --strict   # 发现无效键时 exit 1（CI 门禁用）

判据（与「错位类 bug」教训同源：同一语义的量多处各写一份必然漂移）：
  ① 每个 settings 键必须有**真实消费点**（代码里读它并影响行为），
     只出现在 seeds/migrations/注释/docstring 的 = 无效键（孤儿数据）；
  ② 指针类键（default_project_id / default_branch）必须指向**真实存在的行**；
  ③ 布尔/枚举类键的取值必须在消费点认可的集合内。

完整性守卫（自证扫描器没坏）：
  · 阳性对照 triple_only_writer 必须检出 >=1 个消费点，否则 exit 2；
  · 对照失败说明扫描范围/模式失效，此时一切"无效"结论都不可信。
"""
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB = os.path.join(ROOT, "mbse.db")
SELF = os.path.abspath(__file__)

# 扫描范围：只扫会真实执行的代码/前端；排除 文档/测试/缓存/验证脚本自身
SCAN_EXTS = (".py", ".js", ".html")
EXCLUDE_DIRS = {".git", ".venv", "__pycache__", "node_modules", "tests",
                "tools", "docs", ".pytest_cache", ".workbuddy", "评审报告",
                "outputs", "tmp", "run", "screenshots", "perf-report"}

# 只出现在这些文件里的引用不算消费点（种子/迁移定义 = 数据来源，不是消费者）
SEED_FILES = {"database/seeds.py", "database/migrations/plugins.py"}

# ── 消费点判据（2026-10-23 修正）：裸字面量匹配会把 docstring/注释里的名词
#    误判成消费点（query_route 仅出现在 core/config.py 的模块 docstring 里，
#    曾被误判 VALID）。⇒ 只认「读取/写入表达式」上下文：
READ_PATTERNS = [
    r"FROM\s+settings[^\n;]*?['\"]{k}['\"]",      # SELECT ... FROM settings ... 'key'
    r"INTO\s+settings[^\n;]*?['\"]{k}['\"]",      # INSERT INTO settings ... 'key'
    r"UPDATE\s+settings[^\n;]*?['\"]{k}['\"]",    # UPDATE settings ... 'key'
    r"\.get\(\s*['\"]{k}['\"]",                   # d.get('key') / _sw.get("key")
    r"\[\s*['\"]{k}['\"]\s*\]",                   # d['key']
    r"WHERE\s+key\s*=\s*['\"]{k}['\"]",           # WHERE key='key'
    r"key\s+IN\s*\([^)\n]*['\"]{k}['\"]",         # key IN ('a','key')
    r"LIKE\s+['\"]{k}",                           # LIKE 'prefix.%'
    r"=\s*['\"]{k}['\"]",                         # GATE_MODE_KEY = "key"
]
# 种子定义形态（用于区分"数据来源"与"消费者"，不算消费点）
SEED_PATTERNS = [
    r"\(\s*['\"]{k}['\"]\s*,",                    # ("key", "value", "desc"),
    r"VALUES\s*\(\s*['\"]{k}['\"]",               # VALUES ('key', ...)
]

# 已知局限：staging_fuse.* 用 `LIKE ?` + 变量 _PREFIX 拼接读取，
#   上述静态模式扫不到变量拼接 —— 该批键当前 DB 无行（全走代码默认值），不影响本审计。

# 阳性对照：已知被 vector2graph 消费的键
POSITIVE_CONTROL = "triple_only_writer"

# 指针类键 → (目标表, 目标列)
POINTER_KEYS = {
    "default_project_id": ("projects", "id"),
    "default_branch": ("branches", "name"),
}

# 枚举类键 → 合法取值集合
ENUM_KEYS = {
    "entity_candidate_source": {"sysml", "llm"},
    "query_route": {"vector_first", "graph_first"},
    "shacl_gate_mode": {"warn", "off", "error"},
    "plugin_review_policy": {"forced", "auto"},
}


def iter_source_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if not fn.endswith(SCAN_EXTS):
                continue
            p = os.path.join(dirpath, fn)
            if os.path.abspath(p) == SELF:
                continue
            yield p


def find_refs(key: str):
    """返回 [(relpath, lineno, line)]：源码中所有**读取/写入该键的表达式**位置。

    注释行（# // * /* 开头）与 docstring 内的裸词不算 —— 只有进入
    READ_PATTERNS 的才算消费点（修复：docstring 名词曾被误判为消费点）。
    """
    pats = [re.compile(p.format(k=re.escape(key))) for p in READ_PATTERNS]
    seed_pats = [re.compile(p.format(k=re.escape(key))) for p in SEED_PATTERNS]
    hits, seed_hits = [], []
    for p in iter_source_files():
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, 1):
                    stripped = line.strip()
                    if stripped.startswith(("#", "//", "*", "/*")):
                        continue  # 注释行不算消费点
                    if any(pt.search(line) for pt in pats):
                        rel = os.path.relpath(p, ROOT).replace("\\", "/")
                        hits.append((rel, i, stripped[:110]))
                    elif any(pt.search(line) for pt in seed_pats):
                        rel = os.path.relpath(p, ROOT).replace("\\", "/")
                        seed_hits.append((rel, i, stripped[:110]))
        except OSError:
            continue
    return hits, seed_hits


def main() -> int:
    if not os.path.exists(DB):
        print("FATAL: mbse.db not found at", DB)
        return 2
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT key, value, description, updated_at FROM settings ORDER BY key").fetchall()

    report = []
    invalid = []
    for r in rows:
        key = r["key"]
        hits, seed_hits = find_refs(key)
        real = [h for h in hits if h[0] not in SEED_FILES]
        seed_only = [h for h in seed_hits if h[0] in SEED_FILES]

        entry = {"key": key, "value": r["value"], "desc": r["description"],
                 "updated_at": r["updated_at"],
                 "consumers": [{"file": h[0], "line": h[1], "code": h[2]} for h in real],
                 "seed_refs": [h[0] for h in seed_only]}

        # ① 消费点判据
        if not real:
            entry["verdict"] = "INVALID_NO_CONSUMER"
            invalid.append(key)
        else:
            entry["verdict"] = "VALID"

        # ② 指针有效性
        if key in POINTER_KEYS:
            tbl, col = POINTER_KEYS[key]
            try:
                hit = con.execute(
                    "SELECT 1 FROM %s WHERE %s=?" % (tbl, col), (r["value"],)).fetchone()
                entry["pointer_ok"] = bool(hit)
                if not hit:
                    entry["verdict"] = "INVALID_DANGLING_POINTER"
                    if key not in invalid:
                        invalid.append(key)
            except sqlite3.Error as e:
                entry["pointer_ok"] = None
                entry["pointer_err"] = str(e)

        # ③ 枚举合法性
        if key in ENUM_KEYS and r["value"] not in ENUM_KEYS[key]:
            entry["enum_ok"] = False
            entry["verdict"] = "INVALID_ENUM_VALUE"
            if key not in invalid:
                invalid.append(key)
        elif key in ENUM_KEYS:
            entry["enum_ok"] = True

        report.append(entry)
    con.close()

    # ── 完整性守卫：阳性对照必须命中，否则扫描器失效，一切结论不可信 ──
    ctrl, _ctrl_seed = find_refs(POSITIVE_CONTROL)
    ctrl_real = [h for h in ctrl if h[0] not in SEED_FILES]
    if not ctrl_real:
        print("GUARD FAILED: 阳性对照 %s 未检出消费点 —— 扫描范围/模式失效，"
              "本次审计结论不可信，禁止据此删数据" % POSITIVE_CONTROL)
        return 2

    # ── 白名单双向校验（2026-10-23 收口）：DB 有键 ↔ core/setting_keys.py ──
    # ① 白名单腐烂：白名单精确键必须仍有消费点（否则白名单在放行无效键）；
    # ② 绕过收口：DB 中的每个键必须被白名单放行（否则有键绕过了通用写入入口的准入）。
    sys.path.insert(0, ROOT)
    whitelist_issues = []
    try:
        from core.setting_keys import EXACT_KEYS, is_valid_setting_key
        for k in sorted(EXACT_KEYS):
            hits, _ = find_refs(k)
            real = [h for h in hits if h[0] not in SEED_FILES]
            if not real:
                whitelist_issues.append("白名单键 %s 已无消费点（白名单腐烂，应除名）" % k)
        for e in report:
            if not is_valid_setting_key(e["key"]):
                whitelist_issues.append("DB 键 %s 不在白名单（绕过了通用入口收口）" % e["key"])
        wl_note = "白名单 %d 精确键 + 前缀放行；双向校验 %s" % (
            len(EXACT_KEYS), "通过" if not whitelist_issues else "失败")
    except Exception as _e:  # noqa: BLE001
        whitelist_issues.append("无法导入 core.setting_keys：%s" % _e)
        wl_note = "白名单模块导入失败"

    if "--json" in sys.argv:
        print(json.dumps({"total": len(report), "invalid": invalid,
                          "whitelist_issues": whitelist_issues,
                          "guard": "positive-control OK (%d hits)" % len(ctrl_real),
                          "items": report}, ensure_ascii=False, indent=1))
    else:
        print("=" * 100)
        print("设置模块数据审计（settings 表 %d 键）  阳性对照 %s：%d 个消费点（守卫通过）"
              % (len(report), POSITIVE_CONTROL, len(ctrl_real)))
        print("=" * 100)
        for e in report:
            mark = "✅" if e["verdict"] == "VALID" else "❌"
            print("%s %-28s = %-28s [%s]" % (mark, e["key"], e["value"][:28], e["verdict"]))
            for c in e["consumers"]:
                print("      └─ 消费: %s:%s  %s" % (c["file"], c["line"], c["code"][:80]))
            if e.get("seed_refs"):
                print("      └─ 种子定义: %s" % ", ".join(sorted(set(e["seed_refs"]))))
            if e.get("pointer_ok") is False:
                print("      └─ ⚠️ 指针悬空：%s=%r 在 %s 不存在" % (e["key"], e["value"], POINTER_KEYS[e["key"]][0]))
        print("-" * 100)
        print("无效键 %d 个：%s" % (len(invalid), ", ".join(invalid) or "（无）"))
        print("白名单校验：%s" % wl_note)
        for w in whitelist_issues:
            print("  ⚠️ %s" % w)

    if "--strict" in sys.argv and (invalid or whitelist_issues):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
