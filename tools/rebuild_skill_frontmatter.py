# -*- coding: utf-8 -*-
"""`skills.frontmatter` 重建工具 —— **默认 dry-run，只打印差异，不写库**。

## 问题回顾（实测，生产库快照）

| 项 | 实测 |
|---|---|
| 发布技能 | 24 个 |
| `frontmatter` 存了**整份正文** | **21 个** |
| `[:400]` 截断点**落在句中** | **21 个（全部）** |
| 含「轮次/上限」与运行时真值**冲突** | **10 个** |
| 污染段注入合计 | ≈5,594 tok |

真实成因：`frontmatter` 被写成「旧版 YAML 头 + 旧版全文」，
而 `content` 已更新为「新版 YAML 头 + 新版全文」⇒ 两者不同步。
注入侧只取 `frontmatter[:400]`（`skills.py:179`）⇒
模型看到的是**旧版正文的开头 400 字符**，其中包含与运行时注入相矛盾的话术。

## 重建规则（**不可简单截 400**）

从 `content` 开头提取 `---\\n...\\n---` 这个 **YAML 头**，作为新的 `frontmatter`：

```
content = "---\nname: X\ndescription: ...\nauthor: ...\n---\n## 正文..."
                    ↓
新 frontmatter = "---\nname: X\ndescription: ...\nauthor: ...\n---"
```

这样：
  · 长度降到 **112~284 字符**（全部在 400 以内）⇒ **不再被截断**，句子完整；
  · 与 `content` **同源同步** ⇒ 不再有"旧约束"冲突；
  · `name/description/triggers/category/author/version` 键**全部保留且更全**
    （实测 content 侧 22 个有 YAML，现 frontmatter 只有 21 个）；
  · `core/capability_registry.frontmatter_author()` 用正则 `^author:` 提取，
    重建后仍可取到（且多 1 个可取的）。

## 消费方核查（改之前必须确认，已做）

| 消费方 | 用法 | 重建后影响 |
|---|---|---|
| `agent/pipeline_parts/skills.py:179` | `frontmatter[:400]` 注入 | **改善**（不再截断） |
| `core/capability_registry.py:116` | 正则取 `author:` | **不变/更全** |
| `skill_body_on_demand.strip_frontmatter` | 剥 content 的 YAML 头（**不读该列**） | **无影响** |
| `models/studio.py:22` | 默认值 `"{}"` | 无影响 |

## 用法

    # 1) 预览差异（默认，只读）
    ./.venv/Scripts/python.exe -X utf8 tools/rebuild_skill_frontmatter.py

    # 2) 指定技能细看（打印现状注入文本 vs 建议值）
    ./.venv/Scripts/python.exe -X utf8 tools/rebuild_skill_frontmatter.py \\
        --skill sysml_view_generation_activity --detail

    # 3) 真正落库（**需显式确认**，先自动备份 DB）
    ./.venv/Scripts/python.exe -X utf8 tools/rebuild_skill_frontmatter.py --apply

    # 4) 落库后回滚
    ./.venv/Scripts/python.exe -X utf8 tools/rebuild_skill_frontmatter.py \\
        --rollback backups/skills_fm_YYYYmmdd_HHMMSS.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.token_counter import count_tokens        # noqa: E402
from database import get_db                        # noqa: E402

#: 注入侧的上限（与 `skills.py:179` 的 `[:400]` 保持一致 —— 单一真源）
INJECT_CAP = 400

#: 会与"运行时注入真值"冲突的表述（重建后应归零）
_CONFLICT_PAT = [
    r"轮次上限为\s*\d+", r"轮次上限\s*\d+", r"上限为\s*\d+\s*轮",
    r"共\s*\d+\s*轮", r"第\s*\d+\s*轮就", r"预算\s*\d+",
]

_YAML_RE = re.compile(r"^\s*---\r?\n([\s\S]*?)\r?\n---", re.MULTILINE)


def _extract_yaml(content: str) -> str:
    """从 content 开头提取 YAML 头，返回带 `---` 包裹的完整文本；无则空串。"""
    m = _YAML_RE.match(content or "")
    if not m:
        return ""
    inner = m.group(1)
    if not inner.strip():
        return ""
    return "---\n" + inner.strip() + "\n---"


def _conflicts(text: str) -> list:
    out = []
    for pat in _CONFLICT_PAT:
        for m in re.finditer(pat, text or ""):
            out.append(m.group(0))
    return sorted(set(out))


def _author_of(text: str) -> str:
    """复刻 `core/capability_registry.frontmatter_author` 的口径（同一条正则）。"""
    m = re.search(r"(?m)^author:\s*(.+?)\s*$", str(text or ""))
    return m.group(1).strip().strip('"').strip("'") if m else ""


def plan(con, only: str = "") -> list:
    """生成重建计划（**只读**）。返回每行一个 dict。"""
    sql = ("SELECT id, name, frontmatter, content FROM skills "
           "WHERE enabled=1 AND status='published'")
    args = ()
    if only:
        sql += " AND name=?"
        args = (only,)
    sql += " ORDER BY LENGTH(COALESCE(frontmatter,'')) DESC"

    out = []
    for r in con.execute(sql, args):
        fm = r["frontmatter"] or ""
        ct = r["content"] or ""
        new_fm = _extract_yaml(ct)
        cur_inj = fm.strip()[:INJECT_CAP]
        new_inj = new_fm.strip()[:INJECT_CAP] if new_fm else cur_inj

        if not new_fm:
            action = "skip_no_yaml"
            reason = "content 无 YAML 头 ⇒ 保持原值（需人工）"
        elif not fm.strip() or fm.strip() in ("{}",):
            action = "fill_empty"
            reason = f"frontmatter 为空/{{}} ⇒ 用 content 的 YAML 头填充（{len(new_fm)} 字符）"
        elif fm.strip() == new_fm.strip():
            action = "noop"
            reason = "已与 content 的 YAML 头一致"
        elif len(fm) > len(new_fm) + 40:
            action = "rebuild"
            reason = (f"存了整份正文（{len(fm)} 字符）⇒ 换成 YAML 头（{len(new_fm)} 字符）")
        else:
            action = "skip_similar"
            reason = "长度接近 YAML 头，无需改"

        out.append({
            "id": r["id"], "name": r["name"], "action": action, "reason": reason,
            "fm_chars_before": len(fm), "fm_chars_after": len(new_fm) or len(fm),
            "inject_chars_before": len(cur_inj), "inject_chars_after": len(new_inj),
            "inject_tokens_before": count_tokens(cur_inj),
            "inject_tokens_after": count_tokens(new_inj),
            "truncated_before": len(fm.strip()) > INJECT_CAP,
            "truncated_after": bool(new_fm) and len(new_fm.strip()) > INJECT_CAP,
            "conflicts_before": _conflicts(cur_inj),
            "conflicts_after": _conflicts(new_inj),
            "author_before": _author_of(fm),
            "author_after": _author_of(new_fm) if new_fm else _author_of(fm),
            "cur_injected": cur_inj,
            "new_injected": new_inj,
            "new_frontmatter": new_fm,
        })
    return out


def summarize(rows: list) -> dict:
    act = {}
    for r in rows:
        act[r["action"]] = act.get(r["action"], 0) + 1
    chg = [r for r in rows if r["action"] in ("rebuild", "fill_empty")]
    return {
        "total": len(rows),
        "by_action": act,
        "changed": len(chg),
        "tokens_before": sum(r["inject_tokens_before"] for r in chg),
        "tokens_after": sum(r["inject_tokens_after"] for r in chg),
        "truncated_before": sum(1 for r in rows if r["truncated_before"]),
        "truncated_after": sum(1 for r in chg if r["truncated_after"]),
        "conflicts_before": sum(len(r["conflicts_before"]) for r in chg),
        "conflicts_after": sum(len(r["conflicts_after"]) for r in chg),
        "author_lost": [r["name"] for r in chg
                        if r["author_before"] and not r["author_after"]],
    }


def print_preview(rows: list) -> None:
    s = summarize(rows)
    print("=" * 100)
    print("skills.frontmatter 重建 —— 差异预览（DRY-RUN，未写库）")
    print("=" * 100)
    print(f"  发布技能 {s['total']} 个；将变更 **{s['changed']}** 个")
    print(f"  动作分布：{json.dumps(s['by_action'], ensure_ascii=False)}")
    print()
    print(f"  {'skill':<40}{'fm现':>7}{'fm新':>6}{'注入tok':>10}{'句中截断':>10}{'冲突词':>8}{'author':>8}")
    print("  " + "-" * 96)
    for r in rows:
        if r["action"] not in ("rebuild", "fill_empty"):
            continue
        print(f"  {r['name'][:38]:<40}{r['fm_chars_before']:>7}{r['fm_chars_after']:>6}"
              f"{str(r['inject_tokens_before']) + '→' + str(r['inject_tokens_after']):>10}"
              f"{('是→否' if r['truncated_before'] and not r['truncated_after'] else ('是' if r['truncated_before'] else '否')):>10}"
              f"{str(len(r['conflicts_before'])) + '→' + str(len(r['conflicts_after'])):>8}"
              f"{('保留' if r['author_after'] == r['author_before'] else '变化'):>8}")
    print("  " + "-" * 96)
    print(f"  注入 token 合计：{s['tokens_before']} → {s['tokens_after']}"
          f"（{(s['tokens_after'] - s['tokens_before']):+d}）")
    print(f"  句中截断：{s['truncated_before']} → **{s['truncated_after']}**")
    print(f"  冲突表述：{s['conflicts_before']} 处 → **{s['conflicts_after']} 处**")
    if s["author_lost"]:
        print(f"  ⚠️ author 会丢失：{s['author_lost']}  ← **禁止执行**，需先修")
    else:
        print("  author：全部保留 ✅")

    skipped = [r for r in rows if r["action"] not in ("rebuild", "fill_empty")]
    if skipped:
        print()
        print("  跳过项：")
        for r in skipped:
            print(f"    · {r['name'][:44]:<46} [{r['action']}] {r['reason']}")


def print_detail(rows: list) -> None:
    for r in rows:
        if r["action"] not in ("rebuild", "fill_empty"):
            continue
        print("\n" + "=" * 100)
        print(f"{r['name']}　（{r['fm_chars_before']} → {r['fm_chars_after']} 字符）")
        print("=" * 100)
        print(f"\n【现状】注入的就是这 {len(r['cur_injected'])} 字符（frontmatter[:400]）：")
        print("-" * 100)
        print(r["cur_injected"])
        print("-" * 100)
        print(f"\n【建议】注入将变成这 {len(r['new_injected'])} 字符：")
        print("-" * 100)
        print(r["new_injected"])
        print("-" * 100)
        if r["conflicts_before"]:
            print(f"\n  现状含冲突表述：{r['conflicts_before']}（重建后消失）")
        print(f"  author：{r['author_before']!r} → {r['author_after']!r}")


def apply(rows: list) -> int:
    """真正落库。先备份为 JSON，再逐条 UPDATE（单事务）。"""
    chg = [r for r in rows if r["action"] in ("rebuild", "fill_empty")]
    if not chg:
        print("没有需要变更的技能。")
        return 0

    s = summarize(rows)
    if s["author_lost"]:
        print(f"❌ 拒绝执行：这些技能的 author 会丢失 {s['author_lost']}")
        return 1
    if s["truncated_after"]:
        n = [r["name"] for r in chg if r["truncated_after"]]
        print(f"❌ 拒绝执行：重建后仍超 {INJECT_CAP} 字符会被截断 {n}")
        return 1

    os.makedirs(os.path.join(_ROOT, "backups"), exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(_ROOT, "backups", f"skills_fm_{stamp}.json")
    payload = {"created_at": stamp,
               "rows": [{"id": r["id"], "name": r["name"],
                         "frontmatter_old": r["cur_injected"] if False else None,
                         "frontmatter_new": r["new_frontmatter"]} for r in chg]}
    # 备份需要**原始完整值**（不是截断后的注入值）——重新读一次
    con = get_db()
    try:
        for it in payload["rows"]:
            got = con.execute("SELECT frontmatter FROM skills WHERE id=?",
                              (it["id"],)).fetchone()
            it["frontmatter_old"] = (got["frontmatter"] if got else "") or ""
        with io.open(bak, "w", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"已备份 {len(payload['rows'])} 条原值 → {bak}")

        n = 0
        for it in payload["rows"]:
            con.execute("UPDATE skills SET frontmatter=? WHERE id=?",
                        (it["frontmatter_new"], it["id"]))
            n += 1
        con.commit()
        print(f"✅ 已更新 {n} 个技能的 frontmatter")
        print(f"   回滚：--rollback {os.path.relpath(bak, _ROOT)}")
        return 0
    finally:
        con.close()


def rollback(path: str) -> int:
    full = path if os.path.isabs(path) else os.path.join(_ROOT, path)
    if not os.path.exists(full):
        print(f"❌ 备份文件不存在：{full}")
        return 1
    with io.open(full, encoding="utf-8") as f:
        payload = json.load(f)
    con = get_db()
    try:
        n = 0
        for it in payload.get("rows") or []:
            con.execute("UPDATE skills SET frontmatter=? WHERE id=?",
                        (it["frontmatter_old"], it["id"]))
            n += 1
        con.commit()
        print(f"✅ 已回滚 {n} 个技能")
        return 0
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="skills.frontmatter 重建（默认 dry-run）")
    ap.add_argument("--skill", default="", help="只处理指定技能名")
    ap.add_argument("--detail", action="store_true", help="打印逐条注入文本对比")
    ap.add_argument("--json", action="store_true", help="输出计划 JSON")
    ap.add_argument("--apply", action="store_true", help="真正落库（会先备份）")
    ap.add_argument("--rollback", default="", help="从备份 JSON 回滚")
    args = ap.parse_args()

    if args.rollback:
        return rollback(args.rollback)

    con = get_db()
    try:
        rows = plan(con, only=args.skill)
    finally:
        con.close()

    if args.json:
        print(json.dumps({"summary": summarize(rows), "rows": [
            {k: v for k, v in r.items() if not k.endswith("_frontmatter")
             and k not in ("cur_injected", "new_injected")} for r in rows]},
            ensure_ascii=False, indent=2))
        return 0

    print_preview(rows)
    if args.detail:
        print_detail(rows)
    if args.apply:
        print()
        return apply(rows)
    print()
    print("（以上为 dry-run，未写库。确认无误后加 --apply 执行。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
