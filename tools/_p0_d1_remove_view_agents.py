# -*- coding: utf-8 -*-
"""P0 / D1 —— 永久移除 8 个「视图生成 Agent」（2026-11-15）。

## 背景与用户口径
视图生成能力**已由 `sysml_view_generation_*` Skill 承载**（取证见
`tools/calib/retire_view_agents.py`，2026-10-14 已把这 8 个 Agent 置 `status='disabled'`）。
本批按用户拍板把「停用」推进到「移除」：**不再归档为 Skill，直接删**。

## 为什么不能只删库行（本批的核心发现）
`data/bootstrap/pipeline_assets.json` 是随代码走的配置快照，`database/bootstrap_assets.py::
_bootstrap_tables()` 在 `init_db()` 末尾按 name UPSERT。快照的 agents 记录**刻意不含 `status` 列**
（SPEC exclude，运行期开关不覆盖）⇒ 库里删掉行后，下次启动走的是 **INSERT 分支**，
`status` 落 DDL 默认 `'active'` ⇒ **8 个视图 Agent 会以 active 身份复活**，比停用前更糟。
⇒ 必须库与快照**同步删**，并用常驻门禁真跑一次 `init_db()` 证明不复活。

## 精确范围（实测，勿凭印象扩大）
删：agents id 165~172（8 个视图生成 Agent）；`agent_tools` 30 行；`agent_team_members` 1 行；
    快照 `tables.agents` 8 条 / `relations.agent_tools` 30 条 / `relations.agent_team_members` 1 条。
留：agents id 174 `system_mgmt`（builtin=1 / domain=system）、id 362 `测试agent`（本机联调残留）。
    二者都不是视图生成 Agent；`测试agent` 本来就被 `_keep_row` 排除在快照之外。

## 快照为什么用「结构块行级删除」而不是 json.dumps
文件 782166 B / 6129 行 / 纯 CRLF / 无 BOM；实测 `json.dumps(indent=1/2/4/None)` 均**不能**
无损 round-trip（最短 509990 B，最长 605084 B，与原文都不等）。整体重排会制造 17 万字节的
假 diff，把「删 8 条」淹没掉。故按 `      {` … `      },` 结构块逐块删，其余字节**逐行不动**。

## 用法
    python -X utf8 tools/_p0_d1_remove_view_agents.py              # dry-run（默认，零写入）
    python -X utf8 tools/_p0_d1_remove_view_agents.py --apply       # 写库 + 写快照 + 留痕
    python -X utf8 tools/_p0_d1_remove_view_agents.py --verify      # 只验收当前最终态
    python -X utf8 tools/_p0_d1_remove_view_agents.py --rollback    # 精确回滚（见文末 caveat）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "mbse.db")
SNAP = os.path.join(ROOT, "data", "bootstrap", "pipeline_assets.json")
BACKUP_DIR = os.path.join(ROOT, "backups")
TRACE = os.path.join(ROOT, "docs", "评测", "p0-d1-view-agents-removal-20261115.json")

VIEW_NAMES = (
    "需求视图生成", "结构视图生成", "交互视图（IBD）生成", "用例视图生成",
    "活动图生成", "状态机视图生成", "参数视图生成", "顺序视图（时序图）生成",
)
VIEW_IDS = tuple(range(165, 173))
KEEP_NAMES = ("system_mgmt", "测试agent")
SKILL_PREFIX = "sysml_view_generation_"

# 段起止锚点（4 空格缩进的 `"<name>": [`）
SEG_KEYS = ("agents", "tools", "skills", "agent_tools", "agent_team_members")
TARGET_SEGS = ("agents", "agent_tools", "agent_team_members")

PASS, FAIL = [], []


def rec(code, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    print("[%s] %-4s %s%s" % (tag, code, detail, "" if ok else "  <<<"))
    (PASS if ok else FAIL).append(code)
    return ok


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _now() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


# ── 快照：结构块解析 ────────────────────────────────────────────────────

def parse_snapshot(raw: bytes):
    """返回 (lines, segs, blocks)；全部是字节级，不重排任何内容。

    lines = raw.split(b'\\r\\n')（末项 b''，因为文件以 CRLF 结尾）
    segs  = {段名: (起始行idx, 结束行idx)}   结束行是 `    ],` / `    ]`
    blocks= {段名: [(块起始idx, 块结束idx, 块字节)]}
    """
    lines = raw.split(b"\r\n")
    segs, blocks = {}, {}
    starts = []
    for i, b in enumerate(lines):
        if b.startswith(b"    ") and b.endswith(b": ["):
            key = b.strip().split(b'"')[1].decode("utf-8")
            starts.append((key, i))
    # 每个段的结束 = 该段起始之后第一个 `    ],` 或 `    ]`
    for key, sidx in starts:
        eidx = None
        for j in range(sidx + 1, len(lines)):
            if lines[j] in (b"    ],", b"    ]"):
                eidx = j
                break
        segs[key] = (sidx, eidx)
    for key, (sidx, eidx) in segs.items():
        if eidx is None:
            continue
        cur, blks = None, []
        for j in range(sidx + 1, eidx):
            if lines[j] == b"      {" and cur is None:
                cur = j
            elif cur is not None and lines[j] in (b"      },", b"      }"):
                blks.append((cur, j, b"\r\n".join(lines[cur:j + 1])))
                cur = None
        blocks[key] = blks
    return lines, segs, blocks


def _block_target(block: bytes) -> str | None:
    """块里若出现目标 name（作为 name/agent/target 字段值），返回该 name。"""
    s = block.decode("utf-8")
    for n in VIEW_NAMES:
        for field in ("name", "agent", "target"):
            if '"%s": "%s"' % (field, n) in s:
                return n
    return None


def build_new_snapshot(raw: bytes):
    """删掉目标块，返回 (new_raw, report)。不落盘。"""
    lines, segs, blocks = parse_snapshot(raw)
    old = json.loads(raw.decode("utf-8"))
    report = {"blocks_before": {}, "blocks_deleted": {}, "deleted_block_sha256": []}

    drop_ranges = []
    for key in TARGET_SEGS:
        blks = blocks.get(key) or []
        report["blocks_before"][key] = len(blks)
        hits = [(a, b, blk, _block_target(blk)) for a, b, blk in blks
                if _block_target(blk) is not None]
        report["blocks_deleted"][key] = len(hits)
        for a, b, blk, nm in hits:
            report["deleted_block_sha256"].append(
                {"seg": key, "lines": [a + 1, b + 1], "name": nm, "sha256": _sha(blk)})
            drop_ranges.append((a, b))

    # 从后往前删，索引不漂移
    for a, b in sorted(drop_ranges, reverse=True):
        del lines[a:b + 1]

    # 修正「最后一块带逗号」：删除后每段最后一个块的结束行必须是 `      }`
    _, segs2, blocks2 = parse_snapshot(b"\r\n".join(lines))
    comma_fixed = 0
    for key in TARGET_SEGS:
        blks = blocks2.get(key) or []
        if not blks:
            continue
        _, end, _ = blks[-1]
        if lines[end] == b"      },":
            lines[end] = b"      }"
            comma_fixed += 1
    report["comma_fixed"] = comma_fixed
    report["lines_before"] = len(raw.split(b"\r\n"))
    report["lines_after"] = len(lines)
    new_raw = b"\r\n".join(lines)
    return new_raw, old, report


def check_snapshot(raw: bytes, new_raw: bytes, old: dict, report: dict, apply_mode: bool):
    """落盘前的硬自证：格式 + 语法 + 语义逐项对拍 + 未改动行逐字节一致。"""
    tag = "S"
    # S0 必须先过：结构块切分若漏/多切，后面的语义对拍都不可信
    rec(tag + "0",
        report["blocks_before"]["agents"] == len(old["tables"]["agents"])
        and report["blocks_before"]["agent_tools"] == len(old["relations"]["agent_tools"])
        and report["blocks_before"]["agent_team_members"] == len(old["relations"]["agent_team_members"]),
        "结构块切分与 JSON list 长度逐项相等（agents %d/%d, at %d/%d, atm %d/%d）"
        % (report["blocks_before"]["agents"], len(old["tables"]["agents"]),
           report["blocks_before"]["agent_tools"], len(old["relations"]["agent_tools"]),
           report["blocks_before"]["agent_team_members"],
           len(old["relations"]["agent_team_members"])))
    rec(tag + "1", raw.count(b"\r\n") == raw.count(b"\n") and not raw.startswith(b"\xef\xbb\xbf"),
        "原快照纯 CRLF / 无 BOM（CRLF=%d / LF=%d）" % (raw.count(b"\r\n"), raw.count(b"\n")))
    try:
        new = json.loads(new_raw.decode("utf-8"))
        ok = True
    except Exception as e:
        new, ok = None, False
        rec(tag + "2", False, "新快照 json.loads 失败：%s" % e)
    if ok:
        rec(tag + "2", True, "新快照 JSON 语法合法")
        exp_agents = [r for r in old["tables"]["agents"] if r.get("name") not in VIEW_NAMES]
        exp_at = [r for r in old["relations"]["agent_tools"]
                  if r.get("agent") not in VIEW_NAMES and r.get("target") not in VIEW_NAMES]
        exp_atm = [r for r in old["relations"]["agent_team_members"]
                   if r.get("agent") not in VIEW_NAMES and r.get("target") not in VIEW_NAMES]
        rec(tag + "3", new["tables"]["agents"] == exp_agents,
            "tables.agents 逐项相等（%d → %d，删 %d）" % (
                len(old["tables"]["agents"]), len(new["tables"]["agents"]),
                report["blocks_deleted"]["agents"]))
        rec(tag + "4", new["relations"]["agent_tools"] == exp_at,
            "relations.agent_tools 逐项相等（%d → %d，删 %d）" % (
                len(old["relations"]["agent_tools"]), len(new["relations"]["agent_tools"]),
                report["blocks_deleted"]["agent_tools"]))
        rec(tag + "5", new["relations"]["agent_team_members"] == exp_atm,
            "relations.agent_team_members 逐项相等（%d → %d，删 %d）" % (
                len(old["relations"]["agent_team_members"]),
                len(new["relations"]["agent_team_members"]),
                report["blocks_deleted"]["agent_team_members"]))
        rec(tag + "6", report["blocks_deleted"]["agents"] == 8
            and report["blocks_deleted"]["agent_tools"] == 30
            and report["blocks_deleted"]["agent_team_members"] == 1,
            "删除块数 = 8 / 30 / 1（实测 %s）" % report["blocks_deleted"])
        # 其它结构完全不变
        old2 = dict(old); old2["tables"] = dict(old["tables"]); old2["relations"] = dict(old["relations"])
        old2["tables"]["agents"] = exp_agents
        old2["relations"]["agent_tools"] = exp_at
        old2["relations"]["agent_team_members"] = exp_atm
        rec(tag + "7", new == old2, "除三个目标 list 外，快照其它内容逐字节语义不变")
        # 保留项仍在 / 目标项全无
        names = [r.get("name") for r in new["tables"]["agents"]]
        rec(tag + "8", "system_mgmt" in names and "测试agent" not in names,
            "保留项：system_mgmt 仍在快照；测试agent 本就不在（_keep_row 过滤）")
        rec(tag + "9", not any(n in names for n in VIEW_NAMES),
            "8 个视图 Agent 在快照 tables.agents 中 0 命中")
        allrel = json.dumps(new["relations"], ensure_ascii=False)
        rec(tag + "10", not any('"%s"' % n in allrel for n in VIEW_NAMES),
            "8 个名字在快照 relations 中 0 命中")
    rec(tag + "11", new_raw.count(b"\r\n") == new_raw.count(b"\n")
        and not new_raw.startswith(b"\xef\xbb\xbf"),
        "新快照仍为纯 CRLF / 无 BOM（CRLF=%d）" % new_raw.count(b"\r\n"))
    return ok


# ── 数据库 ──────────────────────────────────────────────────────────────

def _conn(readonly=True):
    if readonly:
        c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    else:
        c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def db_snapshot_rows(c):
    ids = ",".join("?" * len(VIEW_IDS))
    agents = [dict(r) for r in c.execute(
        "SELECT * FROM agents WHERE id IN (%s) ORDER BY id" % ids, VIEW_IDS)]
    at = [dict(r) for r in c.execute(
        "SELECT * FROM agent_tools WHERE agent_id IN (%s) ORDER BY id" % ids, VIEW_IDS)]
    atm = [dict(r) for r in c.execute(
        "SELECT * FROM agent_team_members WHERE sub_agent_id IN (%s) ORDER BY id" % ids,
        VIEW_IDS)]
    return agents, at, atm


def check_db(c, expect_present: bool):
    """expect_present=True 校验「删除前」；False 校验「删除后」最终态。"""
    tag = "D"
    ids = ",".join("?" * len(VIEW_IDS))
    agents = [dict(r) for r in c.execute(
        "SELECT * FROM agents WHERE id IN (%s) OR name IN (%s)" % (ids, ids),
        VIEW_IDS + VIEW_NAMES)]
    if expect_present:
        rec(tag + "1", len(agents) == 8 and all(a["status"] == "disabled" for a in agents),
            "删除前：8 个视图 Agent 各 1 行且均为 disabled（实得 %d 行）" % len(agents))
        rec(tag + "2", sorted(a["name"] for a in agents) == sorted(VIEW_NAMES),
            "删除前：8 个名字与预期逐字一致")
    else:
        rec(tag + "1", len(agents) == 0,
            "删除后：8 个 id / 8 个名字在 agents 表 0 命中（实得 %d 行）" % len(agents))
    # 保留项无论前后都必须存在且 disabled
    keep = [dict(r) for r in c.execute(
        "SELECT id,name,status,builtin FROM agents WHERE name IN (?,?) ORDER BY id",
        KEEP_NAMES)]
    rec(tag + "3", len(keep) == 2 and all(k["status"] == "disabled" for k in keep),
        "保留项未被误删：%s" % [(k["id"], k["name"], k["status"]) for k in keep])
    # 能力承接方
    skills = [r[0] for r in c.execute(
        "SELECT name FROM skills WHERE name LIKE ? AND status='published' ORDER BY name",
        (SKILL_PREFIX + "%",))]
    rec(tag + "4", len(skills) == 9,
        "承接方 9 个 sysml_view_generation_* 技能仍 published（实得 %d）" % len(skills))
    # 关联行
    n_at = c.execute("SELECT COUNT(*) FROM agent_tools WHERE agent_id IN (%s)" % ids,
                     VIEW_IDS).fetchone()[0]
    n_atm = c.execute("SELECT COUNT(*) FROM agent_team_members WHERE sub_agent_id IN (%s)" % ids,
                      VIEW_IDS).fetchone()[0]
    n_atm2 = c.execute("SELECT COUNT(*) FROM agent_team_members WHERE main_agent_id IN (%s)" % ids,
                       VIEW_IDS).fetchone()[0]
    if expect_present:
        rec(tag + "5", (n_at, n_atm, n_atm2) == (30, 1, 0),
            "删除前关联行 agent_tools=30 / atm.sub=1 / atm.main=0（实得 %s）"
            % ((n_at, n_atm, n_atm2),))
    else:
        rec(tag + "5", (n_at, n_atm, n_atm2) == (0, 0, 0),
            "删除后关联行全 0（实得 %s）" % ((n_at, n_atm, n_atm2),))
    # 全库扫描：任何其它表还有引用这 8 个 id 的列 ⇒ 中止（防止漏删孤儿）
    extra = []
    for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if t.startswith("sqlite_"):
            continue
        cols = [d[1] for d in c.execute('PRAGMA table_info("%s")' % t).fetchall()]
        for col in cols:
            if col == "agent_id" or col.endswith("_agent_id") or col == "sub_agent_id":
                if t in ("agents", "agent_tools", "agent_team_members"):
                    continue
                try:
                    n = c.execute('SELECT COUNT(*) FROM "%s" WHERE "%s" IN (%s)' % (t, col, ids),
                                  VIEW_IDS).fetchone()[0]
                    if n:
                        extra.append("%s.%s=%d" % (t, col, n))
                except Exception:
                    pass
    rec(tag + "6", not extra, "其它表无残留 agent id 引用%s" % (
        "（%s）" % extra if extra else ""))
    return {"agents": len(agents), "agent_tools": n_at, "atm_sub": n_atm,
            "skills": len(skills), "keep": keep}


def key_table_counts(c):
    out = {}
    for t in ("agents", "skills", "tools", "agent_tools", "agent_team_members",
              "conversations", "messages", "agent_tasks", "eval_reports", "eval_metrics",
              "config_history", "roles", "settings", "online_eval_queue"):
        try:
            out[t] = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        except Exception:
            out[t] = None
    return out


def backup_db(dst: str):
    src = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    out.close(); src.close()
    # 判等看 sqlite_master + 关键表行数，不看字节数
    a = _conn(True); b = sqlite3.connect("file:%s?mode=ro" % dst.replace("\\", "/"), uri=True)
    b.row_factory = sqlite3.Row
    n1 = a.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view')").fetchone()[0]
    n2 = b.execute("SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view')").fetchone()[0]
    same = (n1 == n2) and (key_table_counts(a) == key_table_counts(b))
    a.close(); b.close()
    return same, n1, n2


# ── 主流程 ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    a = ap.parse_args()

    if a.rollback:
        return do_rollback()
    if a.verify:
        c = _conn(True)
        print("=" * 78); print("VERIFY 最终态（只读）"); print("=" * 78)
        check_db(c, expect_present=False)
        raw = open(SNAP, "rb").read()
        j = json.loads(raw.decode("utf-8"))
        # ★ 判据射程必须精确到「配置层」：tables.agents 的 name + relations 的字段值。
        #   技能正文里「XX视图生成指南」是与旧 Agent 同名的**领域短语**（说明文本），
        #   扫快照全文会把它们误判成 Agent 残留 —— 与 §6.9「射程精确到被测单元」同源。
        #   本口径与常驻门禁 verify_d1_view_agents_removed.py::snapshot_name_hits() 一致。
        _names = [r.get("name") for r in (j.get("tables") or {}).get("agents") or []]
        _rel = json.dumps(j.get("relations") or {}, ensure_ascii=False)
        _hits = [n for n in VIEW_NAMES if n in _names or ('"%s"' % n) in _rel]
        rec("V1", not _hits,
            "快照配置层 0 命中 8 个视图 Agent 名字%s" % ("（残留 %s）" % _hits if _hits else ""))
        _sk_txt = json.dumps((j.get("tables") or {}).get("skills") or [], ensure_ascii=False)
        _phrase = [n for n in VIEW_NAMES if n in _sk_txt]
        print("  [INFO] 技能正文里 %d 个同名领域短语（如「%s指南」）属说明文本，"
              "按上面口径不计 Agent 残留" % (len(_phrase), _phrase[0] if _phrase else "-"))
        c.close()
        print("=" * 78)
        print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
        return 1 if FAIL else 0

    print("=" * 78)
    print("P0 / D1 移除视图 Agent  %s" % ("APPLY" if a.apply else "DRY-RUN"))
    print("=" * 78)

    c = _conn(readonly=not a.apply)
    raw = open(SNAP, "rb").read()
    new_raw, old, rep = build_new_snapshot(raw)
    snap_ok = check_snapshot(raw, new_raw, old, rep, a.apply)
    print("-" * 78)
    before_db = check_db(c, expect_present=True)
    print("-" * 78)
    # 库 ↔ 快照一致性（删之前）：快照块数必须等于库行数
    rec("X1", rep["blocks_deleted"]["agents"] == 8
        and rep["blocks_deleted"]["agent_tools"] == 30
        and rep["blocks_deleted"]["agent_team_members"] == 1,
        "库与快照删除面一致（8 / 30 / 1）")
    rec("X2", len(PASS) > 0 and not FAIL, "静态自证全绿（%d PASS / %d FAIL）" % (len(PASS), len(FAIL)))

    if FAIL or not snap_ok:
        print("\n静态自证未通过 ⇒ 不写任何文件。")
        c.close()
        return 1

    agents_rows, at_rows, atm_rows = db_snapshot_rows(c)
    rec("X3", (len(agents_rows), len(at_rows), len(atm_rows)) == (8, 30, 1),
        "回滚素材已捕获（agents=%d / agent_tools=%d / atm=%d）"
        % (len(agents_rows), len(at_rows), len(atm_rows)))
    before_counts = key_table_counts(c)

    if not a.apply:
        print("\nDRY-RUN 结束：%d PASS / %d FAIL，零写入。" % (len(PASS), len(FAIL)))
        print("  预计删除：库 agents 8 / agent_tools 30 / atm 1 行；快照 39 块 / %d 行"
              % (rep["lines_before"] - rep["lines_after"]))
        c.close()
        return 0 if not FAIL else 1

    # ── APPLY：先备份，再写库，再写快照 ──
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = _now()
    db_bak = os.path.join(BACKUP_DIR, "mbse.db.bak-before-p0-d1-%s.db" % ts)
    snap_bak = os.path.join(BACKUP_DIR, "pipeline_assets.json.bak-before-p0-d1-%s" % ts)
    ok, n1, n2 = backup_db(db_bak)
    rec("W1", ok, "全库备份完成 %s（sqlite_master %d/%d）" % (os.path.basename(db_bak), n1, n2))
    if not ok:
        print("备份判等失败 ⇒ 中止，不写库。"); c.close(); return 1
    open(snap_bak, "wb").write(raw)
    rec("W2", _sha(open(snap_bak, "rb").read()) == _sha(raw),
        "快照备份完成 %s（sha256=%s）" % (os.path.basename(snap_bak), _sha(raw)[:16]))

    ids = ",".join("?" * len(VIEW_IDS))
    try:
        c.execute("BEGIN")
        n1 = c.execute("DELETE FROM agent_team_members WHERE sub_agent_id IN (%s)" % ids,
                       VIEW_IDS).rowcount
        n2 = c.execute("DELETE FROM agent_tools WHERE agent_id IN (%s)" % ids,
                       VIEW_IDS).rowcount
        n3 = c.execute("DELETE FROM agents WHERE id IN (%s)" % ids, VIEW_IDS).rowcount
        rec("W3", (n1, n2, n3) == (1, 30, 8),
            "库删除 rowcount atm/at/agents = %s" % ((n1, n2, n3),))
        if (n1, n2, n3) != (1, 30, 8):
            raise RuntimeError("rowcount 不符，回滚事务")
        c.commit()
    except Exception as e:
        c.rollback()
        rec("W3", False, "写库失败已回滚事务：%s" % e)
        c.close(); return 1

    # 写快照（字节级，保格式）
    open(SNAP, "wb").write(new_raw)
    rec("W4", _sha(open(SNAP, "rb").read()) == _sha(new_raw),
        "快照已按结构块落盘（%d → %d 行，%d → %d B）"
        % (rep["lines_before"], rep["lines_after"], len(raw), len(new_raw)))

    # 落盘后复跑：库最终态 + 快照语法
    c2 = _conn(True)
    print("-" * 78)
    after_db = check_db(c2, expect_present=False)
    after_counts = key_table_counts(c2)
    unchanged = {k: (before_counts[k], after_counts[k]) for k in before_counts
                 if k not in ("agents", "agent_tools", "agent_team_members")}
    rec("W5", all(x == y for x, y in unchanged.values()),
        "其它关键表行数不变（%d 张表逐项对拍）" % len(unchanged))
    rec("W6", after_counts["agents"] == before_counts["agents"] - 8
        and after_counts["agent_tools"] == before_counts["agent_tools"] - 30
        and after_counts["agent_team_members"] == before_counts["agent_team_members"] - 1,
        "三张目标表行数按预期下降（agents %d→%d / at %d→%d / atm %d→%d）"
        % (before_counts["agents"], after_counts["agents"],
           before_counts["agent_tools"], after_counts["agent_tools"],
           before_counts["agent_team_members"], after_counts["agent_team_members"]))
    c2.close()

    # 留痕
    trace = {
        "schema": "p0/d1-view-agents-removal@1",
        "applied_at_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "decision": "D1 用户纠偏：视图生成已由 Skill 实现，视图 Agent 直接移除，不再归档为 Skill",
        "method": "库行删除 + 快照结构块行级删除（同步，防 init_db 自举复活）",
        "why_not_only_db": (
            "pipeline_assets.json 的 agents 记录不含 status 列（SPEC exclude）；"
            "库行删除后自举走 INSERT 分支，status 落 DDL 默认 'active' ⇒ 会以 active 复活"),
        "removed": {
            "agents": [{"id": r["id"], "name": r["name"], "status": r["status"]}
                       for r in agents_rows],
            "keep": [{"id": k["id"], "name": k["name"], "status": k["status"]}
                     for k in before_db["keep"]],
            "rowcounts": {"agent_team_members": n1, "agent_tools": n2, "agents": n3},
        },
        "snapshot": {
            "path": "data/bootstrap/pipeline_assets.json",
            "blocks_deleted": rep["blocks_deleted"],
            "lines_before": rep["lines_before"], "lines_after": rep["lines_after"],
            "bytes_before": len(raw), "bytes_after": len(new_raw),
            "before_sha256": _sha(raw), "after_sha256": _sha(new_raw),
            "comma_fixed": rep["comma_fixed"],
            "deleted_block_sha256": rep["deleted_block_sha256"],
            "note": "保格式补丁：json.dumps(indent=1/2/4/None) 均无法无损 round-trip，故逐块删、其余字节不动",
        },
        "backups": {"db": os.path.relpath(db_bak, ROOT).replace("\\", "/"),
                    "snapshot": os.path.relpath(snap_bak, ROOT).replace("\\", "/")},
        "rollback": {
            "command": "python -X utf8 tools/_p0_d1_remove_view_agents.py --rollback",
            "rows_file": os.path.relpath(TRACE, ROOT).replace("\\", "/") + "  (见 rollback_rows)",
            "caveat": (
                "--rollback 精确重插 8+30+1 行（保留原 id），并用快照备份恢复快照；"
                "恢复快照前会断言当前快照 sha256 == after_sha256，若被后续改动则拒绝并提示人工 diff，"
                "绝不覆盖他人改动。全库备份仅供极端情况参考，不用于常规回滚（会抹掉 D1 之后的其它写入）。"),
        },
        "rollback_rows": {"agents": agents_rows, "agent_tools": at_rows,
                          "agent_team_members": atm_rows},
        "caveats": [
            "历史一次性脚本（tools/migrate_v*.py / tools/seed_team_definitions.py / "
            "tools/repair_main_agent_roster.py / tools/calib/retire_view_agents.py）仍含这 8 个名字；"
            "它们不在 init_db() 调用链里，按 AGENTS.md 坑 16「已执行完毕、不可重跑」。"
            "重跑会重建视图 Agent —— 常驻门禁 verify_d1_view_agents_removed.py 真跑 init_db 可挡住复活。",
            "docs/ 下历史方案文档与若干门禁脚本里的名字是历史陈述/静态词表，不回改（时点审计，见 §4）。",
            "verify_capability_clusters.py 的 B4 判据同步由『8 个已停用』升级为『8 个彻底不存在』。",
        ],
        "verification": {"pass": len(PASS), "fail": len(FAIL),
                         "before_counts": before_counts, "after_counts": after_counts},
    }
    os.makedirs(os.path.dirname(TRACE), exist_ok=True)
    with open(TRACE, "w", encoding="utf-8") as f:
        json.dump(trace, f, ensure_ascii=False, indent=2)
    rec("W7", os.path.getsize(TRACE) > 1000,
        "留痕落盘 %s（%d B）" % (os.path.relpath(TRACE, ROOT).replace("\\", "/"),
                               os.path.getsize(TRACE)))

    c.close()
    print("=" * 78)
    print("APPLY 结束：PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAIL:", ", ".join(FAIL))
    return 1 if FAIL else 0


def do_rollback() -> int:
    print("=" * 78); print("ROLLBACK D1"); print("=" * 78)
    if not os.path.isfile(TRACE):
        print("留痕不存在，无法回滚"); return 1
    t = json.load(open(TRACE, encoding="utf-8"))
    cur = open(SNAP, "rb").read()
    if _sha(cur) != t["snapshot"]["after_sha256"]:
        print("拒绝回滚：快照已被后续改动（当前 sha256=%s，期望=%s）"
              % (_sha(cur)[:16], t["snapshot"]["after_sha256"][:16]))
        print("请人工 diff 后再决定；不覆盖他人改动。")
        return 1
    rec("R0", True, "快照未被后续改动，可安全恢复")
    c = _conn(False)
    rr = t["rollback_rows"]
    try:
        c.execute("BEGIN")
        for tbl, rows in (("agents", rr["agents"]), ("agent_tools", rr["agent_tools"]),
                          ("agent_team_members", rr["agent_team_members"])):
            for r in rows:
                cols = list(r.keys())
                c.execute("INSERT INTO %s (%s) VALUES (%s)" % (
                    tbl, ",".join('"%s"' % x for x in cols), ",".join("?" * len(cols))),
                    tuple(r[x] for x in cols))
        c.commit()
        rec("R1", True, "重插 %d/%d/%d 行（保留原 id）"
            % (len(rr["agents"]), len(rr["agent_tools"]), len(rr["agent_team_members"])))
    except Exception as e:
        c.rollback(); rec("R1", False, "重插失败已回滚：%s" % e); c.close(); return 1
    c.close()
    bak = os.path.join(ROOT, t["backups"]["snapshot"])
    open(SNAP, "wb").write(open(bak, "rb").read())
    rec("R2", _sha(open(SNAP, "rb").read()) == t["snapshot"]["before_sha256"],
        "快照从备份恢复，sha256 回到删除前")
    c2 = _conn(True)
    check_db(c2, expect_present=True)
    c2.close()
    print("=" * 78); print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
