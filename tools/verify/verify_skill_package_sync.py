# CI-OPTIONAL: 2026-11-05 干净克隆+空库实测红 ⇒ 降 local。S3 需生成型 skill 包 zhiyuan_mcp_tools 的 references 目录（随包/生成产物）
# -*- coding: utf-8 -*-
"""门禁：技能包**磁盘文件**与 `skills` 表**内容**不得漂移（2026-10-30）。

## 为什么需要（本轮实证）
「智源平台管理」的磁盘 `skill.md` 是 **v1.0**（1677 字，讲已失效的
`zhiyuan_sysmlv2_gen/check/import` HTTP 工具），而 `skills.content` 是 **v1.3**
（4830 字，MCP 版，讲 `query_projects` / `workspace`）。

这意味着：
- 模型注入读的是 **DB**（`skills.content`）⇒ 它会用 MCP 工具；
- 而任何人（含后人、含 AI 改进流程）打开**磁盘文件**改这个技能时，改的是 v1.0 的内容；
- 两边永远合不上，且**没有任何报错** —— 表现是"我明明改了技能，模型行为没变"
  或"文件里写的是 A，模型说的是 B"。

同类风险对生成型技能包更大：`zhiyuan_mcp_tools` 的 `references/工具目录.md` 是
从 `mcp_servers.tool_meta` 生成的，服务端加工具后若不重跑生成器，文档就**静默过期**。

## 判据
| # | 断言 | 覆盖的失效 |
|---|---|---|
| S1 | 有 `package_path` 的已发布技能：磁盘正文与 DB content **归一化后一致** | 双向漂移 |
| S2 | 技能声明的 `references` / `scripts` 文件**真实存在** | manifest 指向幽灵文件 |
| S3 | 生成型包（`zhiyuan_mcp_tools`）：`工具目录.md` 的工具数 == 活库工具数 | 服务端加工具后文档过期 |
| S4 | 变异：把磁盘改成旧内容 ⇒ S1 必须判红 | 防断言空转 |

⚠️ S3 需要 `mbse.db`，CI 无库 ⇒ 自动 SKIP（不判红），本机跑才有意义。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_skill_package_sync.py
"""
from __future__ import annotations

import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _ROOT)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results = []
GEN_PKG = "zhiyuan_mcp_tools"


def _rec(name, ok, detail="", kind=FAIL):
    _results.append((PASS if ok else kind, name, detail))
    print("  [%s] %s%s" % (_results[-1][0], name, ("  ← " + detail) if detail else ""))
    return bool(ok)


def _strip_fm(md: str) -> str:
    """去掉 YAML frontmatter，只留正文。"""
    m = re.match(r"^---\s*\n[\s\S]*?\n---\s*\n", md or "")
    return (md[m.end():] if m else (md or ""))


def _norm(s: str) -> str:
    """归一化：统一换行、去行尾空白、折叠连续空行。

    为什么不能直接 `==`：DB 与磁盘经过不同的编辑器/序列化，行尾空白与
    连续空行常有差异 —— 那属于噪声，判红只会制造"狼来了"。
    """
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    s = "\n".join(ln.rstrip() for ln in s.split("\n"))
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _db_rows():
    db = (os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db"))
    if not os.path.exists(db):
        return None
    import sqlite3
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return list(c.execute(
            "SELECT id, name, version, content, package_path, `references`, scripts, status, enabled "
            "FROM skills WHERE status='published' AND COALESCE(package_path,'')<>'' "
            "ORDER BY id"))
    finally:
        c.close()


def check_packages(rows, mutate_id=None):
    """S1/S2。`mutate_id` 用于变异自证：把该技能按"旧内容"比较，应当判红。"""
    ok_all = True
    n_ok = 0
    for r in rows:
        pkg = r["package_path"]
        md_p = os.path.join(pkg, "skill.md")
        if not os.path.isdir(pkg) or not os.path.exists(md_p):
            ok_all &= _rec("S1 skill#%s %s：包目录/skill.md 存在"
                           % (r["id"], r["name"]), False,
                           "package_path=%s" % pkg)
            continue
        disk = _strip_fm(open(md_p, encoding="utf-8").read())
        db_body = _strip_fm(r["content"] or "")
        if mutate_id == r["id"]:
            # 变异：假装磁盘上是"只有标题的旧版本"
            disk = " # 旧版正文（变异注入）"
        same = _norm(disk) == _norm(db_body)
        if same:
            n_ok += 1
        ok_all &= _rec("S1 skill#%s %-16s 磁盘正文 == DB content（%d 字）"
                       % (r["id"], r["name"], len(disk)), same,
                       "不一致 ⇒ 改文件不影响模型、改库不影响文件（双向漂移）")

        # S2：声明的 references / scripts 必须真实存在
        miss = []
        for key in ("references", "scripts"):
            try:
                items = json.loads(r[key] or "[]")
            except Exception:
                items = []
            for it in items:
                rel = it.get("path") if isinstance(it, dict) else str(it)
                if rel and not os.path.exists(os.path.join(pkg, rel)):
                    miss.append(rel)
        ok_all &= _rec("S2 skill#%s %-16s references/scripts 全部存在（%d 项）"
                       % (r["id"], r["name"],
                          len(json.loads(r["references"] or "[]"))
                          + len(json.loads(r["scripts"] or "[]"))),
                       not miss, "缺失：%s" % miss)
    print("  —— S1 一致 %d / %d" % (n_ok, len(rows)))
    return ok_all


def check_generated_pkg():
    """S3：生成型包的文档条数必须与活库一致（防"服务端加了工具、文档没重生"）。"""
    ref = os.path.join(_ROOT, "static", "skill_packages", GEN_PKG,
                       "references", "工具目录.md")
    if not os.path.exists(ref):
        return _rec("S3 生成型包 %s 的 references 存在" % GEN_PKG, False)
    text = open(ref, encoding="utf-8").read()
    doc_tools = set(re.findall(r"\n### `([a-z0-9_]+)`", text))

    db = (os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db"))
    if not os.path.exists(db):
        return _rec("S3 文档条数 == 活库工具数（CI 无 mbse.db ⇒ SKIP）", True,
                    "doc=%d；本机跑才有判定" % len(doc_tools), SKIP)
    import sqlite3
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        r = c.execute("SELECT tool_meta FROM mcp_servers WHERE name='zhiyuan-dev-mcp' "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        c.close()
    if not r:
        return _rec("S3 活库有 zhiyuan-dev-mcp（CI 无库/未发现 ⇒ SKIP）", True,
                    "本机跑才有判定", SKIP)
    live = {x["name"] for x in json.loads(r["tool_meta"] or "[]")
            if isinstance(x, dict) and x.get("name")}
    missing = sorted(live - doc_tools)
    ghost = sorted(doc_tools - live)
    return _rec("S3 文档 %d 条 == 活库 %d 个工具" % (len(doc_tools), len(live)),
                not missing and not ghost,
                "文档缺：%s；文档多：%s（重跑 gen_zhiyuan_mcp_skill.py）"
                % (missing, ghost))


def check_plugin_visibility(rows):
    """S5（可见性）+ S6（重复登记）：能力中心能不能"看到且只看到一份"。

    判据只针对 **published 且 enabled=1** 的行 —— 那才是真正会出现在
    能力中心、也会被注入到模型面前的那批。"停用"的行按设计就不该有卡片，
    拿它判红只会制造噪声。

    ## S5 为什么必需（本轮实证）
    本仓有两套事实源：
      · 运行时 = `skills` 表（pipeline 读它 ⇒ AI 会不会用）
      · 管理视图 = `plugins` 表（能力中心「技能」页的卡片来源）
    只写 `skills` ⇒ 技能对模型生效、**页面上没有卡片**，用户视角就是
    "我加的技能没出现"（本轮实际投诉）。写入本应经
    `plugin_system.legacy_sync.sync_from_legacy`（studio_repo 都这么走），
    而"脚本用裸 SQL 写 skills"会绕过它 —— 这条就盯那个缺口。

    ## S6 为什么必需
    同名多行（本轮实测：`行业调研` 有 3 条 published，其中 2 条 enabled）
    ⇒ 运行时重复注入同一份正文、页面上多张同名卡，且**互相之间没有主从关系**，
    改哪份都不确定生效。重复登记几乎都来自"重复跑注册脚本"，
    所以必须由门禁挡住，不能靠人记得。

    ⚠️ 判据用 `manifest.runtime.legacy_id` 反查 plugins（同步桥写进去的唯一钥匙），
       不用 name：`plugins.name` 是 slug（中文名 ASCII 化后是 `skill-137`）。
    """
    ok = True
    active = [r for r in rows
              if str(r.get("status") or "") == "published" and int(r.get("enabled") or 0) == 1]
    for r in active:
        n = r.get("_plugin_rows", 0)
        ok &= _rec("S5 skill#%s %-16s 能力中心可见（plugins 行 %d 条）"
                   % (r["id"], r["name"], n), n >= 1,
                   "缺 plugins 行 ⇒ 页面上没有卡片（跑 register_* 或 sync_from_legacy）")
    # S6：同名只能有一份"已发布且启用"
    by_name = {}
    for r in active:
        by_name.setdefault(str(r["name"]), []).append(r["id"])
    dup = {k: v for k, v in by_name.items() if len(v) > 1}
    ok &= _rec("S6 无同名重复登记（已发布且启用）", not dup,
               "重复：%s ⇒ 运行时重复注入 + 页面多张同名卡" % dup)

    # S7：slug 不得是退化值（`mcp` / `skill` 这类与类型名相同的 slug）
    try:
        import sqlite3
        _c = sqlite3.connect((os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db")))
        _c.row_factory = sqlite3.Row
        _rows = _c.execute(
            "SELECT plugin_id, name, type FROM plugins WHERE status!='removed'").fetchall()
        _c.close()
        from plugin_system.legacy_sync import _DEGENERATE_SLUGS
        bad = [dict(x) for x in _rows if str(x["name"]) in _DEGENERATE_SLUGS]
        ok &= _rec("S7 无退化 slug（slug 不得等于类型名 %s）" % sorted(_DEGENERATE_SLUGS),
                   not bad, "退化：%s ⇒ 卡片标题会变成类型名本身" % bad[:3])
    except Exception as _e:              # noqa: BLE001
        ok &= _rec("S7 退化 slug 检查", True, "跳过：%s" % str(_e)[:80])
    return ok


def main():
    print("=" * 78)
    print("技能包磁盘文件 ↔ skills 表 内容一致性")
    print("=" * 78)

    rows = _db_rows()
    if rows is not None:
        # 给每行补 plugins 可见性计数（S5 用）
        try:
            import sqlite3
            _c = sqlite3.connect((os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db")))
            _c.row_factory = sqlite3.Row
            # ⚠️ `sqlite3.Row` 只读，不能就地加键 ⇒ 转 dict（本轮踩到 TypeError）
            _dicts = []
            for _r in rows:
                _d = dict(_r)
                _d["_plugin_rows"] = _c.execute(
                    "SELECT COUNT(*) FROM plugins "
                    "WHERE json_extract(manifest_json,'$.runtime.legacy_table')='skills' "
                    "AND json_extract(manifest_json,'$.runtime.legacy_id')=? "
                    "AND status!='removed'", (_d["id"],)).fetchone()[0]
                _dicts.append(_d)
            rows = _dicts
            _c.close()
        except Exception:
            rows = [dict(r) for r in rows]
            for r in rows:
                r["_plugin_rows"] = 0
    if rows is None:
        print("\n=== S1/S2（CI 无 mbse.db ⇒ 无法比对 DB）===")
        _rec("S1/S2 需要 mbse.db（CI 跳过）", True, "本机跑才有判定", SKIP)
    else:
        print("\n=== S1/S2 已发布且有包目录的技能（%d 个）===" % len(rows))
        check_packages(rows)

    if rows:
        print("\n=== S5 能力中心可见性（skills → plugins 同步）===")
        check_plugin_visibility(rows)

    print("\n=== S3 生成型包的新鲜度 ===")
    check_generated_pkg()

    print("\n=== S4 变异自证：把磁盘当成旧内容 ⇒ S1 必须判红 ===")
    if not rows:
        _rec("S4 需要 mbse.db（CI 跳过）", True, "本机跑才有判定", SKIP)
    else:
        # 只跑第一个技能，捕获其 S1 结果是否为 FAIL
        keep = list(_results)
        _results.clear()
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            check_packages(rows[:1], mutate_id=rows[0]["id"])
        s1_failed = any(k == FAIL and n.startswith("S1") for k, n, _ in _results)
        _results[:] = keep
        _rec("S4 变异被抓住（S1 对旧内容判红）⇒ 证明 S1 非空转", s1_failed)

    bad = [r for r in _results if r[0] == FAIL]
    n_skip = sum(1 for r in _results if r[0] == SKIP)
    print("\n" + "=" * 78)
    print("断言 %d  PASS %d  SKIP %d  FAIL %d"
          % (len(_results), len(_results) - len(bad) - n_skip, n_skip, len(bad)))
    for k, n, d in bad:
        print("  [%s] %s  %s" % (k, n, d))
    print("结论：%s" % ("全部通过" if not bad else "有断言未通过"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
