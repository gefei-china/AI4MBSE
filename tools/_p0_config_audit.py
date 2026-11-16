"""P0-4：静态配置审计 —— 生成「SCHEMA 有 DEFAULT 无」与「零消费点」两类清单。

⚠️ 口径（引用任何数字都要带这一句）：
  · 集合差：CONFIG_SCHEMA（含 _EXTRA_SCHEMA 合并后） - DEFAULT_CONFIG 的 (section,key)
  · **业务层**扫描：业务 .py，排除 .git/.venv/__pycache__/node_modules/tests/tools/docs/
    backups/tmp/outputs/data/logs/static/skill_packages/archive/.workbuddy，
    并排除 core/config.py（定义源）与 core/config_audit.py（清单独本）。
  · **全仓层**扫描：.py/.js/.html，只排除运行期生成物，保留 tools/eval、static/js、tests。
    为什么必须有这一层（2026-11-15 实测）：`eval_gates.*` / `trace2dataset.*` 的消费点在
    `tools/eval/`，`context.budget_*` 的消费点在 `static/js/mods/35-ctxconfig.js`。
    只扫业务 .py 会把它们误判成「死配置」——这是**口径失真**，不是产品缺陷。
  · 判据（业务层，保守，宁可漏报不多报）：
      ① 组合读取：`get("sec","key")` / `CONFIG.get("sec","key")`
      ② 点路径字面量：`"sec.key"`（save_override / --cfg / shadow 风格）
      ③ 任意 `"key"` 字面量（可能动态消费）
    ①②③ 全 0 ⇒ `no_business_consumer`（业务侧零消费点，**打标但不删键**）；
    仅 ③ 命中 ⇒ `suspect`（只列清单不标记）。
  · `no_business_consumer` 里再挑出**全仓层也 0 命中**的 ⇒ `dead_config_suspect`
    （真正可疑的死配置，仍只标记，删键属产品决策）。
  · 时点：2026-11-15；清单会随代码漂移 ⇒ `tools/verify/verify_p0_config_audit.py` 重扫对拍。

默认 dry-run；--apply 才生成 core/config_audit.py 与 docs/评测/config_audit_p0-20261115.json。
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from core import config as C  # noqa: E402

GEN_DIRS = {".git", ".venv", "__pycache__", "node_modules", "backups", "tmp",
            "outputs", "data", "logs", "skill_packages", "archive", ".workbuddy",
            "screenshots", "perf-report"}
BIZ_EXTRA = {"tests", "tools", "docs", "static"}
# 审计工具自身也必须排除：它们含清单常量名与配置键片段，若计入「全仓消费点」，
# 会把自己的字符串当成活的读取点 ⇒ 清单随工具增删而漂移（2026-11-15 P0-4 预防）。
SKIP_FILES = {"core/config.py", "core/config_audit.py",
              "tools/_p0_config_audit.py", "tools/verify/verify_p0_config_audit.py"}
SCAN_EXTS = (".py", ".js", ".html")
OUT_JSON = REPO / "docs" / "评测" / "config_audit_p0-20261115.json"
OUT_PY = REPO / "core" / "config_audit.py"


def scan(extra_exclude):
    ex = GEN_DIRS | extra_exclude
    out = []
    for dp, dns, fns in os.walk(REPO):
        dns[:] = [d for d in dns if d not in ex]
        for fn in fns:
            if not fn.endswith(SCAN_EXTS):
                continue
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, REPO).replace("\\", "/")
            if rel in SKIP_FILES:
                continue
            try:
                out.append((rel, open(full, encoding="utf-8", errors="ignore").read()))
            except OSError:
                continue
    return out


def has_any_ref(sec, key, joined):
    return bool(re.search(r"['\"]%s\.%s['\"]" % (re.escape(sec), re.escape(key)), joined)
                or re.search(r"['\"]%s['\"]" % re.escape(key), joined))


def compute():
    """重扫并计算四组清单 —— 生成器和常驻门禁**共用这一处**，防止两份实现漂移。

    返回的是「当前磁盘真实状态」；门禁拿它与已落盘的 core/config_audit.py 对拍。
    """
    biz = scan(BIZ_EXTRA)
    whole = scan(set())
    biz_joined = "\n".join(t for _, t in biz)
    whole_joined = "\n".join(t for _, t in whole)
    schema_keys = {(s, k) for s, items in C.CONFIG_SCHEMA.items() for k in items}
    default_keys = {(s, k) for s, items in C.DEFAULT_CONFIG.items()
                    if isinstance(items, dict) for k in items}
    phantom = sorted("%s.%s" % (s, k) for s, k in (schema_keys - default_keys))

    no_business, suspect, dead = [], [], []
    for s, k in sorted(schema_keys):
        grouped = bool(
            re.search(r"get\s*\(\s*['\"]%s['\"]\s*,\s*['\"]%s['\"]" %
                      (re.escape(s), re.escape(k)), biz_joined)
            or re.search(r"['\"]%s\.%s['\"]" % (re.escape(s), re.escape(k)), biz_joined))
        if grouped:
            continue
        dotted = "%s.%s" % (s, k)
        if has_any_ref(s, k, biz_joined):
            suspect.append(dotted)
        else:
            no_business.append(dotted)
            if not has_any_ref(s, k, whole_joined):
                dead.append(dotted)

    return {
        "scope": {"business_files": len(biz), "whole_repo_files": len(whole)},
        "schema_keys": len(schema_keys), "default_keys": len(default_keys),
        "counts": {"schema_only": len(phantom), "no_business_consumer": len(no_business),
                   "dead_config_suspect": len(dead), "suspect": len(suspect)},
        "schema_only": phantom, "no_business_consumer": no_business,
        "dead_config_suspect": dead, "suspect": suspect,
    }


def main():
    r = compute()
    print("口径：业务层 %d 文件 / 全仓层 %d 文件（均排除生成物 + 审计工具自身）"
          % (r["scope"]["business_files"], r["scope"]["whole_repo_files"]))
    print("CONFIG_SCHEMA=%d DEFAULT_CONFIG=%d SCHEMA_ONLY=%d NO_BUSINESS_CONSUMER=%d "
          "DEAD_SUSPECT=%d SUSPECT=%d"
          % (r["schema_keys"], r["default_keys"], r["counts"]["schema_only"],
             r["counts"]["no_business_consumer"], r["counts"]["dead_config_suspect"],
             r["counts"]["suspect"]))
    print("SCHEMA_ONLY:", r["schema_only"])
    print("NO_BUSINESS_CONSUMER:", r["no_business_consumer"])
    print("DEAD_SUSPECT(全仓也无引用):", r["dead_config_suspect"])
    print("SUSPECT(仅键名字面量命中，未标记): %d 项，见 JSON" % r["counts"]["suspect"])

    if "--apply" not in sys.argv:
        print("DRY-RUN（未写盘）；加 --apply 生成 core/config_audit.py 与清单 JSON")
        return 0

    payload = {
        "schema": "p0/config-audit@1",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "scope": {**r["scope"],
                  "gen_dirs_excluded": sorted(GEN_DIRS), "business_extra_excluded": sorted(BIZ_EXTRA),
                  "skip_files": sorted(SKIP_FILES), "scan_exts": list(SCAN_EXTS),
                  "schema_keys": r["schema_keys"], "default_keys": r["default_keys"]},
        "definition": {
            "schema_only": "CONFIG_SCHEMA 有、DEFAULT_CONFIG 无 ⇒ get() 返回 None；只能由 override 提供",
            "no_business_consumer": "业务 .py 三层判据都 0 命中；消费点可能在 tools/eval 或前端 JS",
            "dead_config_suspect": "业务层 + 全仓层都 0 命中 ⇒ 真正可疑的死配置",
            "suspect": "业务层只有键名字面量命中，可能动态消费；只列清单不打标",
        },
        "counts": r["counts"],
        "schema_only": r["schema_only"], "no_business_consumer": r["no_business_consumer"],
        "dead_config_suspect": r["dead_config_suspect"], "suspect": r["suspect"],
        "disposition": "本批只做显式标注（audit_flags），不改默认值、不删键、不改变 get() 行为",
        "caveat": "静态扫描结论；动态拼接的读取可能漏报；删键前必须全仓复扫并跑评测集",
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")

    def py_tuple(name, xs):
        return "%s = (\n" % name + "".join("    %r,\n" % x for x in xs) + ")\n"

    body = '''"""P0-4 配置审计标注的**单一真源**（由 tools/_p0_config_audit.py --apply 生成，勿手改）。

两类「问题键」被显式标注，而不是悄悄补默认值或删键：
  · SCHEMA_ONLY_KEYS：CONFIG_SCHEMA 有、DEFAULT_CONFIG 无。`config.get()` 对它们返回 None，
    只能由用户 override 或调用方传入的 default 提供 —— 这是**设计**，不是漏写；
    但前端若显示「默认 —」会误导，故打标让 UI / 审计可识别。
  · NO_BUSINESS_CONSUMER_KEYS：业务 .py 中三层判据都 0 命中。
    ⚠️ **不等于死配置**：实测 `eval_gates.*`/`trace2dataset.*` 由 tools/eval 消费，
    `context.budget_*` 由 static/js/mods/35-ctxconfig.js 消费。标记只表示
    「业务侧没有读取点」，供 UI 提示与后续清理决策，**本批不删任何键**。
  · DEAD_CONFIG_SUSPECT_KEYS：业务层 + 全仓层都 0 命中，才是真可疑；同样只标记。

⚠️ 清单会随代码漂移：`tools/verify/verify_p0_config_audit.py` 重扫并对拍，
   不一致即判红。任何数字引用都要带「口径 + 时点」
   （见 docs/评测/config_audit_p0-20261115.json）。

生成命令：./.venv/Scripts/python.exe -X utf8 tools/_p0_config_audit.py --apply
"""
GENERATED_AT = %r

''' % payload["generated_at_utc"]
    body += py_tuple("SCHEMA_ONLY_KEYS", r["schema_only"]) + "\n"
    body += py_tuple("NO_BUSINESS_CONSUMER_KEYS", r["no_business_consumer"]) + "\n"
    body += py_tuple("DEAD_CONFIG_SUSPECT_KEYS", r["dead_config_suspect"]) + "\n"
    body += py_tuple("SUSPECT_KEYS", r["suspect"]) + "\n"
    body += '''

def flags(section: str, key: str) -> list:
    """返回该键的审计标记（无标记返回空列表，不制造噪音字段）。"""
    dotted = "%s.%s" % (section, key)
    out = []
    if dotted in SCHEMA_ONLY_KEYS:
        out.append("schema_only_no_default")
    if dotted in NO_BUSINESS_CONSUMER_KEYS:
        out.append("no_business_consumer")
    if dotted in DEAD_CONFIG_SUSPECT_KEYS:
        out.append("dead_config_suspect")
    return out
'''
    OUT_PY.write_text(body, encoding="utf-8", newline="\n")
    print("WROTE", OUT_JSON, OUT_PY)
    return 0


if __name__ == "__main__":
    sys.exit(main())
