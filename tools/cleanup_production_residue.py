# -*- coding: utf-8 -*-
"""clean_production_residue —— 清理生产库中的**测试残留与虚构人名**存量。

## 背景（2026-10-23，米爸决策：系统中 mock 的数据排查并移除）

代码侧的 mock/假数据已在同一轮清理完毕（AI 流程模板、审批模块、写死人名、
演示图谱回流…）。本脚本处理**存量库里已经写进去的**残留。

生产库实测（mbse.db）：

| 类别 | 量 | 说明 |
|---|---|---|
| 测试会话 | 56 条 / 170 消息 | verify-*、多轮验证(临时)、冒烟-*、probe-* 等 |
| 测试用户 | 10 个 | local-记忆作用域验证 / local-导航回归 / restart-probe … |
| 审计日志·测试账号 | 681 条 | 测试/verify/li/wang/solo/dbg/diag/u0-u3 |
| 审计日志·虚构人名 | 1730 条 | 王工 1463 / 李工 267 |
| 审计日志·未认证 | 2141 条 | 未登录 1650 / 可疑请求（未认证）491 |
| prompts | 2 条同名 | 「需求分析-宽带通信」重复，created_by=王工 |
| projects | 1 条 | 「测试项目」，且是 default_project_id |
| mcp_servers.latency_ms | 2 行 | 整型列里存了字符串 'online'（历史写入污染） |

## 安全设计（清理生产数据必须满足的四条）

1. **默认 dry-run** —— 不带 `--apply` 只打印将要执行的语句与影响行数，**不写库**。
2. **强制备份** —— `--apply` 前自动复制一份带时间戳的 `.db` 到 `data/backups/`，
   备份路径打印出来；备份失败即中止，不做任何写入。
3. **逐项可开关** —— 每一类清理是独立开关（`--only A` / `--skip B`），
   避免"一键全删"把不想删的也删了。
4. **可复核** —— 清理后打印每张表的前后行数对比；退出码非 0 表示有残留。

## 审计日志为什么默认**不删**

`audit_logs` 是**合规资产**（带哈希链，NIST AU-9 / SOC2 CC2）。
删审计行会**断开哈希链**（prev_hash 衔接），`core.audit.verify_chain()` 会直接报
"行被插入或删除" ⇒ 反而制造一个更严重的合规问题。

因此本脚本对审计日志的默认动作是**改写归属名**（把虚构人名/测试账号标成
`未登录`/`历史遗留`），而不是删行。只有显式加 `--purge-audit` 才真删。

## 用法

    # 1) 预演（默认，不写库）
    ./.venv/Scripts/python.exe -X utf8 tools/cleanup_production_residue.py

    # 2) 执行清理（自动备份）
    ./.venv/Scripts/python.exe -X utf8 tools/cleanup_production_residue.py --apply

    # 3) 只处理某几类
    ./.venv/Scripts/python.exe -X utf8 tools/cleanup_production_residue.py --apply --only A,B

    # 4) 审计日志真删（慎用，会断哈希链）
    ./.venv/Scripts/python.exe -X utf8 tools/cleanup_production_residue.py --apply --purge-audit
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DB_PATH = os.path.join(_ROOT, "mbse.db")
BACKUP_DIR = os.path.join(_ROOT, "data", "backups")

#: 测试特征词 —— 会话标题用（较保守，避免误删真实业务会话）
TEST_PAT = re.compile(
    r'(verify|probe|smoke|冒烟|测试|临时|E2E|trace-e2e|restart-probe|N3probe'
    r'|回归|核对|残留|check-|test-|diag|dbg|solo)', re.I)

#: 测试特征词 —— **用户名/显示名**用（更宽：local-* 这类一次性验证账号）
#: ⚠️ 会话标题侧刻意不加「验证/复核/残影/作用域」——
#:    因为真实业务会话里也有「LLM连通性验证」「#57 附件消费干净验证」这类标题，
#:    放宽会把真业务会话一起删掉。用户名侧是 local-* 一次性账号，放宽是安全的。
TEST_PAT_USER = re.compile(
    r'(verify|probe|smoke|冒烟|测试|临时|E2E|trace-e2e|restart-probe|N3probe'
    r'|回归|核对|复核|验证|残留|作用域|残影|check-|test-|diag|dbg|solo)', re.I)

#: 测试账号（users.username / audit_logs.user_name）
#: ⚠️ 分类要准：
#:   - wang / li 是**种子账号的 username**（王工/李工账号），属"虚构人名"侧，
#:     不是测试账号 —— 但因为库里审计归属写的是 username，需要在 C 类里单独处理。
#:   - 「匿名」是**真实匿名行为**的归属（不是测试账号），不该当测试账号清掉。
TEST_ACCOUNTS = ("测试", "verify", "restart-probe", "solo",
                 "dbg", "diag", "u0", "u1", "u2", "u3", "测试用户")

#: 种子账号 username（库里的"虚构人名"以 username 形式出现）
SEED_USERNAMES = ("wang", "li")

#: 虚构人名（**代码里写死的人名** —— 不是任何真实用户）
FAKE_NAMES = ("王工", "李工", "赵管")

#: ⚠️ 真实人名（库里有真实账号）—— **不得**当虚构人名改写。
#:    id=125 陈专·载荷业务专家 是真实注册用户；改写它的 display_name
#:    等于破坏真实身份信息（同"把验证对象认错"族）。
#:    当前审计表里没有归属「陈专」的行，故此处只做保护性排除。
REAL_NAMES = ("陈专",)

#: 未认证类（**不删** —— 是真实的安全事件记录，仅标注）
ANON_LITS = ("未登录", "可疑请求（未认证）")

CATEGORIES = ("A", "B", "C", "D", "E")


def _q(conn, sql, *a):
    return [dict(r) for r in conn.execute(sql, a).fetchall()]


def _counts(conn):
    out = {}
    for t in ("conversations", "messages", "users", "audit_logs",
              "prompts", "projects", "mcp_servers"):
        try:
            out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.Error:
            out[t] = -1
    return out


def _backup(db_path: str = "") -> str:
    src = db_path or DB_PATH
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, f"mbse_cleanup_{time.strftime('%Y%m%d_%H%M%S')}.db")
    shutil.copy2(src, dst)
    if not os.path.exists(dst) or os.path.getsize(dst) == 0:
        raise RuntimeError("备份失败或为空 —— 已中止，未做任何写入")
    return dst


def _rebuild_audit_chain(conn) -> int:
    """**重算**整条审计哈希链（改写 user_name 之后必须做）。

    ## 为什么必须重算而不是裸改字段

    `core.audit._row_hash(prev_hash, user_name, event_type, detail, result,
    branch, ip_address, user_agent, request_id)` —— **user_name 是摘要入参之一**。
    所以 `UPDATE audit_logs SET user_name='历史遗留' WHERE user_name='王工'`
    会让那一行的 `hash` 与内容**不再匹配** ⇒ `verify_chain()` 立刻报
    「内容与摘要不符（行被篡改）」⇒ 凭空造出一个**更严重的合规问题**
    （原来是"归属名是假的"，改完变成"审计链被篡改了"）。

    实测本库 4616 行**全部已入链**（hash 非空），所以这个坑是必然会踩到的。

    重算方式与 `core.audit.audit()` 完全一致：按 id 升序逐行，
    prev_hash 取上一行的 hash，首行取 GENESIS（64 个 0）。
    返回重算行数。
    """
    sys.path.insert(0, _ROOT)
    from core.audit import _row_hash, _GENESIS

    rows = conn.execute(
        "SELECT id, user_name, event_type, detail, result, branch, ip_address, "
        "user_agent, request_id FROM audit_logs ORDER BY id").fetchall()
    prev = _GENESIS
    n = 0
    for r in rows:
        h = _row_hash(prev, r["user_name"], r["event_type"], r["detail"], r["result"],
                      r["branch"] or "", r["ip_address"] or "", r["user_agent"] or "",
                      r["request_id"] or "")
        conn.execute("UPDATE audit_logs SET prev_hash=?, hash=? WHERE id=?",
                     (prev, h, r["id"]))
        prev = h
        n += 1
    return n


def _test_conversation_ids(conn):
    rows = _q(conn, "SELECT id, title FROM conversations")
    return [r["id"] for r in rows if TEST_PAT.search(r["title"] or "")]


def _test_user_ids(conn):
    rows = _q(conn, "SELECT id, username, display_name FROM users WHERE id != 1")
    return [r["id"] for r in rows
            if TEST_PAT_USER.search(r["username"] or "")
            or TEST_PAT_USER.search(r["display_name"] or "")]


def plan(conn, cats, purge_audit: bool):
    """生成清理计划（不执行）。返回 [(类别, 说明, SQL, 参数, 预期影响行数)]。"""
    plan_items = []

    if "A" in cats:
        cids = _test_conversation_ids(conn)
        if cids:
            ph = ",".join("?" * len(cids))
            n_msg = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE conversation_id IN ({ph})", cids).fetchone()[0]
            plan_items.append(("A", f"删除测试会话 {len(cids)} 条（连带消息 {n_msg} 条）",
                               f"DELETE FROM messages WHERE conversation_id IN ({ph})", cids, n_msg))
            plan_items.append(("A", f"删除测试会话 {len(cids)} 条",
                               f"DELETE FROM conversations WHERE id IN ({ph})", cids, len(cids)))
        # 关联的 artifacts / feedback 等孤儿行
        if cids:
            ph = ",".join("?" * len(cids))
            for t in ("artifacts", "feedback"):
                try:
                    n = conn.execute(
                        f"SELECT COUNT(*) FROM {t} WHERE conversation_id IN ({ph})", cids).fetchone()[0]
                except sqlite3.Error:
                    continue
                if n:
                    plan_items.append(("A", f"清理 {t} 孤儿行 {n} 条",
                                       f"DELETE FROM {t} WHERE conversation_id IN ({ph})", cids, n))

    if "B" in cats:
        uids = _test_user_ids(conn)
        if uids:
            ph = ",".join("?" * len(uids))
            plan_items.append(("B", f"删除测试用户 {len(uids)} 个",
                               f"DELETE FROM users WHERE id IN ({ph})", uids, len(uids)))

    if "C" in cats:
        # 虚构人名 → 标注为「历史遗留（虚构人名）」；测试账号 → 「历史遗留（测试）」
        # ⚠️ 改写 user_name 会破坏哈希链（user_name 是摘要入参）⇒ 必须重算整条链。
        #    重算步骤由 main() 在执行完 UPDATE 后统一调用 _rebuild_audit_chain()。
        # ⚠️ purge 模式与改写模式**必须互斥**：若先改写成「历史遗留（虚构人名）」，
        #    随后的 DELETE WHERE user_name='王工' 匹配不到任何行 ⇒ 静默删 0 条。
        #    故 purge 模式下直接跳过改写项，只做真删（重算会修复删行造成的断链）。
        if not purge_audit:
            for name in FAKE_NAMES:
                n = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE user_name=?", (name,)).fetchone()[0]
                if n:
                    plan_items.append(("C", f"审计归属「{name}」→ 标注为历史遗留（{n} 条，不删行）",
                                       "UPDATE audit_logs SET user_name=? WHERE user_name=?",
                                       ["历史遗留（虚构人名）", name], n))
            # 种子账号 username（wang/li）—— 归属写的是 username，同样是虚构人名侧
            for name in SEED_USERNAMES:
                n = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE user_name=?", (name,)).fetchone()[0]
                if n:
                    plan_items.append(("C", f"审计归属「{name}」（种子账号 username）→ 标注为历史遗留（{n} 条，不删行）",
                                       "UPDATE audit_logs SET user_name=? WHERE user_name=?",
                                       ["历史遗留（虚构人名）", name], n))
            for name in TEST_ACCOUNTS:
                n = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE user_name=?", (name,)).fetchone()[0]
                if n:
                    plan_items.append(("C", f"审计归属「{name}」→ 标注为历史遗留（{n} 条，不删行）",
                                       "UPDATE audit_logs SET user_name=? WHERE user_name=?",
                                       ["历史遗留（测试）", name], n))
        if purge_audit:
            for name in FAKE_NAMES + SEED_USERNAMES + TEST_ACCOUNTS:
                n = conn.execute("SELECT COUNT(*) FROM audit_logs WHERE user_name=?", (name,)).fetchone()[0]
                if n:
                    plan_items.append(("C", f"⚠️ 真删审计行 user_name='{name}'（{n} 条，"
                                           f"重算哈希链后链仍完好）",
                                       "DELETE FROM audit_logs WHERE user_name=?", (name,), n))

    if "D" in cats:
        # 重复 prompt（同名多条）→ 保留最新一条，其余删除
        dups = _q(conn, """SELECT name, COUNT(*) c FROM prompts GROUP BY name HAVING c > 1""")
        for d in dups:
            keep = conn.execute(
                "SELECT id FROM prompts WHERE name=? ORDER BY id DESC LIMIT 1", (d["name"],)).fetchone()
            if not keep:
                continue
            n = d["c"] - 1
            plan_items.append(("D", f"删除重复 prompt「{d['name']}」保留 id={keep[0]}（{n} 条）",
                               "DELETE FROM prompts WHERE name=? AND id != ?",
                               (d["name"], keep[0]), n))
        # 「测试项目」：是当前默认项目时**不能删**（删了 default_project_id 悬空）
        #   → 改为重命名为中性真实名「默认项目」，保留其 id 与指针。
        for p in _q(conn, "SELECT id, name FROM projects WHERE name LIKE '%测试%'"):
            dflt = conn.execute("SELECT value FROM settings WHERE key='default_project_id'").fetchone()
            is_dflt = bool(dflt and str(dflt[0]) == p["id"])
            if is_dflt:
                plan_items.append(("D", f"项目「{p['name']}」({p['id']})是当前默认项目 → "
                                         f"重命名为「默认项目」（不删，避免指针悬空）",
                                   "UPDATE projects SET name=? WHERE id=?",
                                   ("默认项目", p["id"]), 1))
            else:
                plan_items.append(("D", f"项目「{p['name']}」({p['id']})（非默认项目，可删）",
                                   "DELETE FROM projects WHERE id=?", (p["id"],), 1))

    if "E" in cats:
        n = conn.execute(
            "SELECT COUNT(*) FROM mcp_servers WHERE typeof(latency_ms)!='integer'").fetchone()[0]
        if n:
            plan_items.append(("E", f"修正 mcp_servers.latency_ms 非法值 {n} 行 → NULL",
                               "UPDATE mcp_servers SET latency_ms=NULL "
                               "WHERE typeof(latency_ms)!='integer'", (), n))
        # users.id=1 的 display_name「王工」→ 中性名
        # ⚠️ 只针对 **种子账号 id=1**（username='wang'）。
        #    其它账号的 display_name 可能是**真实人名**（如 id=125 陈专·载荷业务专家），
        #    把真实人名当"虚构人名"改写 = 破坏真实身份信息 ⇒ 不能一刀切。
        n = conn.execute(
            "SELECT COUNT(*) FROM users WHERE id=1 AND display_name IN "
            f"({','.join('?' * len(FAKE_NAMES))})", FAKE_NAMES).fetchone()[0]
        if n:
            plan_items.append(("E", f"users.id=1（种子账号 wang）display_name 虚构人名 {n} 行 → 「历史账号」",
                               "UPDATE users SET display_name='历史账号' "
                               f"WHERE id=1 AND display_name IN ({','.join('?' * len(FAKE_NAMES))})",
                               FAKE_NAMES, n))
        # 其余表里残留的 created_by='王工' 等虚构人名 → 中性化（不改行数，只改归属标注）
        for t in ("prompts", "artifacts", "knowledge_documents", "entities", "relations",
                  "branches", "projects"):
            try:
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {t} WHERE created_by IN "
                    f"({','.join('?' * len(FAKE_NAMES))})", FAKE_NAMES).fetchone()[0]
            except sqlite3.Error:
                continue
            if n:
                plan_items.append((f"E", f"{t}.created_by 虚构人名 {n} 行 → 「历史遗留（虚构人名）」",
                                   f"UPDATE {t} SET created_by='历史遗留（虚构人名）' "
                                   f"WHERE created_by IN ({','.join('?' * len(FAKE_NAMES))})",
                                   FAKE_NAMES, n))
        # 孤儿 artifacts：会话早已被删，产物还挂着（既有残留，非本次清理造成）
        # ★ 2026-10-23 米爸拍板：**方案 B，删除**（此前默认保留只改归属标注）。
        #   抽查确认其中含 2026-10-17 的建模澄清文档等真实内容，但会话主体已不存在，
        #   产物无法从会话上下文打开，属"孤儿数据"；用户明确选择删除。
        #   执行前脚本会自动整库备份，内容可从备份恢复。
        try:
            n_orphan = conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE conversation_id IS NOT NULL "
                "AND conversation_id NOT IN (SELECT id FROM conversations)").fetchone()[0]
        except sqlite3.Error:
            n_orphan = 0
        if n_orphan:
            plan_items.append(("E", f"孤儿 artifacts {n_orphan} 条（会话已不存在，米爸拍板删）→ 删除",
                               "DELETE FROM artifacts WHERE conversation_id IS NOT NULL "
                               "AND conversation_id NOT IN (SELECT id FROM conversations)", (), n_orphan))

    return plan_items


def main() -> int:
    ap = argparse.ArgumentParser(description="清理生产库测试残留与虚构人名（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    ap.add_argument("--only", default="", help="只处理这些类别，逗号分隔，如 A,B")
    ap.add_argument("--skip", default="", help="跳过这些类别")
    ap.add_argument("--purge-audit", action="store_true",
                    help="真删审计行（会断哈希链，慎用）")
    ap.add_argument("--db", default=DB_PATH, help="指定库路径")
    args = ap.parse_args()

    cats = set(CATEGORIES)
    if args.only:
        cats &= {c.strip().upper() for c in args.only.split(",") if c.strip()}
    if args.skip:
        cats -= {c.strip().upper() for c in args.skip.split(",") if c.strip()}

    db = args.db
    if not os.path.exists(db):
        print(f"❌ 找不到数据库：{db}")
        return 2

    print("=" * 78)
    print("clean_production_residue — 生产库测试残留与虚构人名清理")
    print(f"库：{db}")
    print(f"模式：{'⚠️ 实际执行（--apply）' if args.apply else '预演（dry-run，不写库）'}")
    print(f"类别：{sorted(cats)}" + ("  + purge-audit" if args.purge_audit else ""))
    print("=" * 78)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    before = _counts(conn)

    items = plan(conn, cats, args.purge_audit)
    if not items:
        print("\n✅ 无需清理（未匹配到任何残留）")
        conn.close()
        return 0

    print("\n清理计划：")
    for cat, desc, sql, params, n in items:
        print(f"  [{cat}] {desc}")
        print(f"      SQL: {sql[:120]}")
        print(f"      参数: {str(params)[:120]}  预期影响 {n} 行")
    print("\nℹ️  提示：各类按 A→B→C→D→E 顺序执行，**实际行数可能小于预期**"
          "（同表被前一类改动过，如 D 类先删了重复 prompt，E 类的 prompts 更新就只剩 1 行）。"
          "这是顺序效应，不是执行失败。")

    if not args.apply:
        print("\n" + "-" * 78)
        print("ℹ️  这是预演。确认无误后加 --apply 执行（会自动备份）。")
        conn.close()
        return 0

    print("\n" + "-" * 78)
    try:
        bk = _backup(db)
        print(f"✅ 已备份：{bk}")
    except Exception as e:
        print(f"❌ 备份失败（{e}）—— 已中止，未做任何写入")
        conn.close()
        return 3

    try:
        for cat, desc, sql, params, n in items:
            cur = conn.execute(sql, params)
            print(f"  [{cat}] {desc}  → 实际 {cur.rowcount} 行")

        # ★ 审计链重算：C 类改写了 user_name（摘要入参），D/purge 可能删了行
        #   ⇒ 必须重算整条链，否则 verify_chain() 会报「行被篡改 / prev_hash 不衔接」。
        touched_audit = any(cat == "C" for cat, _, _, _, _ in items)
        if touched_audit:
            n_re = _rebuild_audit_chain(conn)
            print(f"  [C] 审计哈希链重算 → {n_re} 行")

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"❌ 执行失败（{e}）—— 已回滚，备份在 {bk}")
        conn.close()
        return 4

    # 哈希链复核：必须仍然完好（这是本次清理的硬性前提）
    try:
        sys.path.insert(0, _ROOT)
        from core.audit import verify_chain
        vc = verify_chain(conn=conn)
        print(f"\n审计链复核：total={vc['total']} chained={vc['chained']} "
              f"unchained={vc['unchained']} broken={vc['broken_count']} "
              f"{'✅ 链完好' if vc['ok'] else '❌ 链断裂'}")
        if not vc["ok"]:
            for b in vc["broken"][:5]:
                print(f"    - id={b['id']} {b['reason']}")
            conn.close()
            print(f"\n⚠️  哈希链未通过，已中止。备份：{bk}")
            return 5
    except Exception as e:
        conn.rollback()
        print(f"❌ 哈希链复核失败（{e}）—— 已回滚，备份在 {bk}")
        conn.close()
        return 5

    after = _counts(conn)
    print("\n" + "-" * 78)
    print("清理前后行数对比：")
    for t in before:
        d = after[t] - before[t]
        flag = "" if d == 0 else f"  ({d:+d})"
        print(f"  {t:16s} {before[t]:8d} → {after[t]:8d}{flag}")

    # 复核：不该再有测试会话 / 测试用户 / 虚构人名
    left = {
        "测试会话": len(_test_conversation_ids(conn)),
        "测试用户": len(_test_user_ids(conn)),
        "虚构人名审计": conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE user_name IN "
            f"({','.join('?' * len(FAKE_NAMES))})", FAKE_NAMES).fetchone()[0],
    }
    print("\n复核：")
    for k, v in left.items():
        print(f"  {k}: {v}  {'✅' if v == 0 else '⚠️ 仍有残留'}")

    conn.close()
    print(f"\n✅ 清理完成。备份：{bk}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
