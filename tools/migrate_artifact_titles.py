# -*- coding: utf-8 -*-
"""历史产物改名回填：把「代码块 1（sysml）」「代码文件 v0.1（SysML）」换成有语义的名字。

（米爸 2026-11-09）「产物清单中代码文件名称都是代码块，都不知道具体这个代码是什么」

安全边界（很重要，先读）：
  · **只用客观标识取名**（package 名 → 首个类型定义名 → 首个实例名）。
    不拿会话诉求硬凑 —— 历史产物与当期诉求未必对应（实测 conv 325 的
    preview_content 是 ASCII 框图，不是 SysML 代码，按诉求取名会变成一篇乱码）。
    取不到客观标识的，**保留原名**，宁可不改。
  · 只处理 `is_placeholder_title()` 判为占位名的行；用户/模型自取的名字一律不动。
  · 同一会话内**同一主体**共享名字前缀（多版本一致性），版本号取 meta.version_label，
    退而从旧标题 `v0.x` 解析；同名冲突时补 `（n）`，不伪造版本。
  · 幂等：重复执行不会二次改名（改完就不再是占位名）。

用法：
    python tools/migrate_artifact_titles.py            # dry-run，只打印
    python tools/migrate_artifact_titles.py --apply    # 真写库（会先自动备份）
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.config as cfg  # noqa: E402
from core.artifact_naming import (  # noqa: E402
    extract_instance_name, extract_package_name, extract_primary_def,
    filename_for, is_placeholder_title,
)

_RE_VER = re.compile(r"v(\d+\.\d+)")


def _subject_of(code: str, old_title: str) -> str:
    """客观标识优先级：package > 类型定义 > 实例声明。取不到返回 ""（= 不改）。"""
    return (extract_package_name(code) or extract_primary_def(code)
            or extract_instance_name(code) or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写库（否则 dry-run）")
    args = ap.parse_args()

    db = cfg.DB_PATH
    if args.apply:
        bak = os.path.join(
            os.path.dirname(db),
            f"backups/mbse.db.bak-artifact-titles-{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        shutil.copy2(db, bak)
        print(f"[备份] {bak}")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT id, conversation_id, message_id, title, filename, preview_content, meta "
        "FROM artifacts WHERE kind='code' ORDER BY conversation_id, id"))

    # 会话内按主体名规划标题（先规划、后写，避免边改边查导致幂等判定漂移）
    plan = []                      # (id, old_title, new_title, new_filename)
    taken = {}                     # (conv, new_title) -> 已用次数
    taken_fn = {}                  # (conv, filename) -> 已用次数
    for r in rows:
        old = (r["title"] or "").strip()
        if not is_placeholder_title(old):
            continue
        code = r["preview_content"] or ""
        subj = _subject_of(code, old)
        if not subj:
            continue
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:
            meta = {}
        ver = str(meta.get("version_label") or "") if isinstance(meta, dict) else ""
        if not ver:
            m = _RE_VER.search(old)
            ver = f"v{m.group(1)}" if m else ""
        base = subj
        core = f"{base} · {ver}" if ver else base
        key = (r["conversation_id"], core)
        # ⚠️ 首版这里写错过：冲突后把计数重置成 1，导致第 2、3 条都叫「X（2）」。
        #    正确做法是"先自增、再按自增后的值决定后缀"。
        used = taken.get(key, 0) + 1
        taken[key] = used
        new = core if used == 1 else f"{core}（{used}）"
        # 文件名同样按会话去重；冲突时把序号带进文件名，避免下载互相覆盖
        lang = str(meta.get("lang") or "sysml") if isinstance(meta, dict) else "sysml"
        fn = filename_for(subj if used == 1 else f"{subj}（{used}）", ver, lang)
        fk = (r["conversation_id"], fn)
        if taken_fn.get(fk):          # 极端情况：同名同 lang 仍撞（理论上不会）
            taken_fn[fk] += 1
            fn = filename_for(f"{subj}（{used}-{taken_fn[fk]}）", ver, lang)
        taken_fn[fk] = 1
        plan.append((r["id"], old, new, fn))

    print(f"\n[计划] 待改名 {len(plan)} / 全部 code 产物 {len(rows)}；"
          f"其余 {len(rows) - len(plan)} 条无可提取的客观标识或名字已有语义，保留原样\n")
    for pid, old, new, fn in plan[:30]:
        print(f"  #{pid:<5} {old}  →  {new}   [{fn}]")
    if len(plan) > 30:
        print(f"  … 其余 {len(plan) - 30} 条略")

    if not args.apply:
        print("\n[dry-run] 未写库。确认无误后加 --apply。")
        conn.close()
        return 0

    n = 0
    try:
        conn.execute("BEGIN")
        for pid, old, new, fn in plan:
            # 保留可追溯性：旧名进 meta，便于回溯"这条以前叫什么"
            row = conn.execute("SELECT meta FROM artifacts WHERE id=?", (pid,)).fetchone()
            try:
                m = json.loads(row["meta"] or "{}")
            except Exception:
                m = {}
            m["title_before_migration"] = old
            m["name_key"] = new.split(" · ")[0]
            conn.execute("UPDATE artifacts SET title=?, filename=?, meta=? WHERE id=?",
                         (new, fn, json.dumps(m, ensure_ascii=False), pid))
            n += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"\n[失败] 已回滚：{e}")
        conn.close()
        return 1

    left = conn.execute(
        "SELECT COUNT(*) FROM artifacts WHERE kind='code' AND (title LIKE '代码块%' "
        "OR title LIKE '代码文件 v%')").fetchone()[0]
    conn.close()
    print(f"\n[完成] 改名 {n} 条；剩余占位名 {left} 条（无客观标识，按设计保留）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
