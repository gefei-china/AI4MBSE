# -*- coding: utf-8 -*-
"""带自检的 mbse.db 热备。

为什么不用现成的 safe_init_db 备份路径
------------------------------------
`sqlite3.Connection.backup()` **只复制内容、不校验源库有效性** ——
只要文件能 copy 就报成功。本脚本因此在备份后做三项自检：
  ① 源库本身像不像真库（含核心业务表且行数合理）
  ② 备份表集合不少于源库
  ③ 关键表行数抽样一致
任一不过 => 删备份 + 抛错（不留下会炸的假备份）。

⚠️ 第一版只查 ②③ 会漏掉「空库备份照样通过」的情况（空库 1 张、备份也是这 1 张，
   集合一致）。故判据必须是「源库像不像真库」，与备份无关。

用法：
    ./.venv/Scripts/python.exe -X utf8 tools/backup_verify.py --dry-run   # 只体检，不备份
    ./.venv/Scripts/python.exe -X utf8 tools/backup_verify.py --tag xxx   # 体检+备份+复检
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

DB = "mbse.db"
# 核心业务表：备份必须覆盖这些（非空 + 行数量级合理）
CORE_TABLES = {
    "agents": 10,          # Agent 定义，v3 实施的关键表
    "tools": 20,           # 工具
    "skills": 10,          # 技能
    "agent_tools": 40,     # Agent-工具绑定
    "agent_team_members": 10,
    "documents": 10,       # 知识库文档
    "entities": 100,       # 图谱实体
    "relations": 150,      # 图谱关系
}


def _connect_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _counts(conn: sqlite3.Connection) -> dict:
    out = {}
    for (t,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        try:
            out[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except Exception:
            out[t] = -1
    return out


def audit_source(path: str) -> tuple[bool, str, dict]:
    """判据：源库像不像真库 —— 与备份无关。"""
    if not os.path.exists(path):
        return False, f"源库不存在：{path}", {}
    size = os.path.getsize(path)
    if size < 1_000_000:
        return False, f"源库过小（{size} B），不像真库", {}
    try:
        conn = _connect_ro(path)
        counts = _counts(conn)
        conn.close()
    except Exception as e:
        return False, f"源库不可读：{e}", {}
    missing, too_small = [], []
    for t, min_rows in CORE_TABLES.items():
        if t not in counts:
            missing.append(t)
        elif counts[t] < min_rows:
            too_small.append(f"{t}={counts[t]}(期望≥{min_rows})")
    if missing:
        return False, f"源库缺核心表：{missing}", counts
    if too_small:
        return False, f"源库核心表行数异常：{too_small}", counts
    return True, f"源库体检通过（{len(counts)} 张表，{size/1e6:.1f} MB）", counts


def verify_backup(src_counts: dict, bak: str) -> tuple[bool, str]:
    """备份有效性：表集合不少于源库 + 关键表行数一致。"""
    if not os.path.exists(bak):
        return False, "备份文件不存在"
    size = os.path.getsize(bak)
    if size < 1_000_000:
        return False, f"备份过小（{size} B）"
    try:
        conn = _connect_ro(bak)
        bak_counts = _counts(conn)
        conn.close()
    except Exception as e:
        return False, f"备份不可读：{e}"
    lost = set(src_counts) - set(bak_counts)
    if lost:
        return False, f"备份丢了 {len(lost)} 张表：{sorted(lost)[:5]}"
    drift = []
    for t in CORE_TABLES:
        if src_counts.get(t) != bak_counts.get(t):
            drift.append(f"{t}: 源={src_counts.get(t)} 备份={bak_counts.get(t)}")
    if drift:
        return False, f"关键表行数不一致：{drift}"
    return True, f"备份校验通过（{len(bak_counts)} 张表，{size/1e6:.1f} MB）"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="manual")
    ap.add_argument("--dry-run", action="store_true", help="只体检，不写备份")
    args = ap.parse_args()

    print("=" * 68)
    print("① 源库体检")
    ok, msg, src_counts = audit_source(DB)
    print(f"   {'PASS' if ok else 'FAIL'} · {msg}")
    if not ok:
        print("   ⇒ 源库本身就不合格，拒绝备份（否则会留下假备份）")
        return 2
    print(f"   核心表行数：" + ", ".join(
        f"{t}={src_counts[t]}" for t in CORE_TABLES))

    if args.dry_run:
        print("\n[dry-run] 未写备份")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = f"backups/pre-v3nodes-{args.tag}-{stamp}.db"
    os.makedirs("backups", exist_ok=True)
    print("\n② 备份")
    t0 = time.time()
    conn = sqlite3.connect(DB)
    dst = sqlite3.connect(bak)
    conn.backup(dst)
    dst.close()
    conn.close()
    print(f"   已写 {bak}（{os.path.getsize(bak)/1e6:.1f} MB，{time.time()-t0:.1f}s）")

    print("\n③ 备份校验")
    ok2, msg2 = verify_backup(src_counts, bak)
    print(f"   {'PASS' if ok2 else 'FAIL'} · {msg2}")
    if not ok2:
        os.remove(bak)
        print("   ⇒ 备份无效，已删除，不留会炸的假备份")
        return 3

    print("\n" + "=" * 68)
    print(f"✅ 备份可用：{bak}")
    print(f"   还原命令：sqlite3 {bak} \".restore {DB}\"  或 cp {bak} {DB}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
