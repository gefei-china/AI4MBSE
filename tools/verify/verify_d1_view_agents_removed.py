# -*- coding: utf-8 -*-
"""常驻门禁：D1「8 个视图生成 Agent」已永久移除，且**不会被资产自举复活**（2026-11-15）。

## 为什么需要这条门禁
`pipeline_assets.json` 的 agents 记录**不含 `status` 列**（`SPEC["agents"]["exclude"]`）。
库行删除后，`_bootstrap_tables()` 走的是 **INSERT 分支**，`status` 落 DDL 默认 `'active'`
⇒ 只删库、不删快照，8 个视图 Agent 会以 **active** 身份复活（比停用前更糟）。
⇒ 静态的「库里 0 命中」不够，**必须真跑一次 `init_db()`** 证明空库重建后仍 0 命中。

## 判据分组
  R 真库最终态 / S 快照文本 / B 空库重建不复活（核心）/ M 变异自证 / D 判据升级留痕 / Z 零污染

## 变异自证（M 组，防「判据空转」）
把一条视图 Agent 记录塞回快照副本 → 用 `bootstrap_assets()` 灌进临时库 →
断言 ① 它**确实被重建且 `status='active'`**（证明风险真实），
     ② `check_removed()` **确实判红**（证明本门禁的判据有牙齿）。
用干净快照跑同样流程 → 判据必须判绿。两者互为对照。

用法：python -X utf8 tools/verify/verify_d1_view_agents_removed.py
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "mbse.db")
SNAP = os.path.join(ROOT, "data", "bootstrap", "pipeline_assets.json")
CLUSTER_GATE = os.path.join(ROOT, "tools", "verify", "verify_capability_clusters.py")
TRACE = os.path.join(ROOT, "docs", "评测", "p0-d1-view-agents-removal-20261115.json")

VIEW_NAMES = (
    "需求视图生成", "结构视图生成", "交互视图（IBD）生成", "用例视图生成",
    "活动图生成", "状态机视图生成", "参数视图生成", "顺序视图（时序图）生成",
)
VIEW_IDS = tuple(range(165, 173))
KEEP_NAMES = ("system_mgmt", "测试agent")
SKILL_PREFIX = "sysml_view_generation_"

PASS, FAIL = [], []


def rec(code, ok, detail=""):
    print("[%s] %-4s %s%s" % ("PASS" if ok else "FAIL", code, detail, "" if ok else "  <<<"))
    (PASS if ok else FAIL).append(code)
    return ok


def _bounded_unlink(path):
    """Windows / WorkBuddy 沙箱下裸 os.remove 可能被 tsbx.dll 挂钩挂死（§1r.2）。"""
    try:
        from core.fs_guard import bounded_unlink
        return bounded_unlink(path)
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass


def check_no_view_agents(conn):
    """★ 复活判据（单一真源）：只认「8 个视图 Agent 是否出现」。

    刻意**不掺入**保留项/技能等其它条件 —— 否则变异自证里「因保留项缺失而判红」
    会冒充「因视图 Agent 复活而判红」，是空转判据（§6.7）。
    """
    ph = ",".join("?" * len(VIEW_NAMES))
    iph = ",".join("?" * len(VIEW_IDS))
    rows = conn.execute(
        "SELECT id,name,status FROM agents WHERE name IN (%s) OR id IN (%s)" % (ph, iph),
        VIEW_NAMES + VIEW_IDS).fetchall()
    if rows:
        return ["agents 表残留 %d 行: %s" % (
            len(rows), [(r[0], r[1], r[2]) for r in rows][:4])]
    return []


def check_removed(conn):
    """真库完整判据 = 复活判据 + 本机保留项（仅用于真库，不用于空库重建）。"""
    bad = check_no_view_agents(conn)
    keep = conn.execute(
        "SELECT name,status FROM agents WHERE name IN (?,?)", KEEP_NAMES).fetchall()
    if len(keep) != 2:
        bad.append("保留项被误删：应有 2 个，实得 %d" % len(keep))
    return bad


def snapshot_name_hits(snap):
    """快照里 8 个名字的命中：tables.agents 的 name + relations 的任意字段值。"""
    hits = []
    names = [r.get("name") for r in (snap.get("tables") or {}).get("agents") or []]
    rel_txt = json.dumps(snap.get("relations") or {}, ensure_ascii=False)
    for n in VIEW_NAMES:
        if n in names or ('"%s"' % n) in rel_txt:
            hits.append(n)
    return hits


def main() -> int:
    print("=" * 78)
    print("D1 常驻门禁：视图 Agent 已移除 + 空库重建不复活")
    print("=" * 78)
    # CI 上真库（282 MB，不入库）可能不存在 ⇒ R 组降级但**高声标注**，
    # B 组（空库重建）与 M/S/D 组照跑；静默跳过等于假绿（§6.1 / §1n.2 纪律）。
    degraded = []
    _has_db = os.path.isfile(DB) and os.path.getsize(DB) > 0
    # 测试钩子：本机复现「CI 无真库」的降级路径，证明 B/S/M/D 组不依赖真库、降级不假绿。
    if os.environ.get("MBSE_D1_SIMULATE_NO_DB") == "1":
        _has_db = False
    _db_stat0 = ((os.path.getsize(DB), os.path.getmtime(DB))
                 if (_has_db and os.path.isfile(DB)) else None)

    # ── R 真库最终态 ──────────────────────────────────────────────────
    if _has_db:
        conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
        rec("R1", not check_removed(conn),
            "真库：%s" % (check_removed(conn) or "8 个 id/name 0 命中，2 个保留项仍在"))
        iph = ",".join("?" * len(VIEW_IDS))
        n_at = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE agent_id IN (%s)" % iph,
                            VIEW_IDS).fetchone()[0]
        n_atm = conn.execute("SELECT COUNT(*) FROM agent_team_members WHERE sub_agent_id IN (%s) "
                             "OR main_agent_id IN (%s)" % (iph, iph),
                             VIEW_IDS + VIEW_IDS).fetchone()[0]
        rec("R2", (n_at, n_atm) == (0, 0),
            "真库关联行 0 残留（agent_tools=%d / atm=%d）" % (n_at, n_atm))
        n_sk = conn.execute("SELECT COUNT(*) FROM skills WHERE name LIKE ? AND status='published'",
                            (SKILL_PREFIX + "%",)).fetchone()[0]
        rec("R3", n_sk == 9, "承接方 9 个 sysml_view_generation_* 技能仍 published（实得 %d）" % n_sk)
        # 全库扫描：任何表还有引用这 8 个 id 的列
        extra = []
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            if t.startswith("sqlite_"):
                continue
            cols = [d[1] for d in conn.execute('PRAGMA table_info("%s")' % t).fetchall()]
            for col in cols:
                if col == "agent_id" or col.endswith("_agent_id"):
                    if t == "agents":
                        continue
                    try:
                        n = conn.execute('SELECT COUNT(*) FROM "%s" WHERE "%s" IN (%s)' % (t, col, iph),
                                         VIEW_IDS).fetchone()[0]
                        if n:
                            extra.append("%s.%s=%d" % (t, col, n))
                    except Exception:
                        pass
        rec("R4", not extra, "全库无残留 agent id 引用%s" % ("（%s）" % extra if extra else ""))
        conn.close()
    else:
        degraded.append("R1~R4 真库最终态（mbse.db 不存在：CI 空库环境；由 B 组空库重建覆盖）")
        print("  [INFO] 真库 mbse.db 不存在 ⇒ 跳过 R1~R4，已计入 degraded（不静默）")

    # ── S 快照 ────────────────────────────────────────────────────────
    raw = open(SNAP, "rb").read()
    # ★ 2026-11-25 修（实测假红 / 假绿各一）：
    #   原来 S1 要求「纯 CRLF」，S5 数 `      {`（6 空格缩进）—— 这两个都是**旧导出格式**
    #   的字节特征。导出器早已改成 `indent=1 + 纯 LF`（见 bootstrap_assets.dump_snapshot_text），
    #   于是 S1/S5 恒红（CRLF=0、6 空格块=0），而它们想守的东西（快照没被手改/没鬼行）没变。
    #   正解：不再钉字节格式，改钉**规范导出**——用同一个序列化函数重算，
    #   与磁盘字节逐字相等即"文件就是导出器产物"。格式变了门禁跟着变，不会漂。
    from database.bootstrap_assets import dump_snapshot_text
    rec("S1", not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw,
        "快照无 BOM / 纯 LF 行尾（CR=%d）" % raw.count(b"\r"))
    try:
        snap = json.loads(raw.decode("utf-8"))
        ok_json = True
    except Exception as e:
        snap, ok_json = {}, False
        rec("S2", False, "快照 JSON 解析失败：%s" % e)
    if ok_json:
        rec("S2", True, "快照 JSON 语法合法")
        hits = snapshot_name_hits(snap)
        rec("S3", not hits, "快照 tables.agents / relations 中 8 个名字 0 命中%s"
            % ("（残留 %s）" % hits if hits else ""))
        names = [r.get("name") for r in (snap.get("tables") or {}).get("agents") or []]
        rec("S4", "system_mgmt" in names and "测试agent" not in names,
            "快照保留项：system_mgmt 在、测试agent 不在（_keep_row 过滤）")
        # S5：字节 == 规范导出（防手改 / 防格式被整体重排后藏鬼行）
        _canon = dump_snapshot_text(snap).encode("utf-8")
        rec("S5", raw == _canon,
            "快照与规范导出逐字节一致（磁盘 %d B / 规范 %d B）" % (len(raw), len(_canon)))
        n_snap_sk = len([r for r in (snap.get("tables") or {}).get("skills") or []
                         if str(r.get("name") or "").startswith(SKILL_PREFIX)])
        rec("S6", n_snap_sk == 9, "快照里 9 个视图技能未被误删（实得 %d）" % n_snap_sk)

    # ── B 空库重建（核心）────────────────────────────────────────────
    tmpdir = tempfile.mkdtemp(prefix="mbse_d1_")
    tmpdb = os.path.join(tmpdir, "rebuild.db")
    env = dict(os.environ)
    env["MBSE_DB_PATH"] = tmpdb
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    boot_ok, boot_tail = False, ""
    try:
        r = subprocess.run([sys.executable, "-X", "utf8", "-c",
                            "from database import init_db; init_db()"],
                           cwd=ROOT, capture_output=True, env=env, timeout=300)
        boot_ok = (r.returncode == 0)
        boot_tail = ((r.stdout or b"").decode("utf-8", "replace")[-300:]
                     + (r.stderr or b"").decode("utf-8", "replace")[-300:])
    except Exception as e:
        boot_tail = "%s: %s" % (type(e).__name__, str(e)[:200])
    rec("B1", boot_ok, "`init_db()` 在隔离空库跑通%s" % ("" if boot_ok else "；输出=%r" % boot_tail[-200:]))
    if boot_ok and os.path.isfile(tmpdb):
        c2 = sqlite3.connect("file:%s?mode=ro" % tmpdb.replace("\\", "/"), uri=True)
        # 只用「复活判据」：空库重建不要求本机残留的 测试agent 存在（它被 _keep_row 排除）
        bad = check_no_view_agents(c2)
        rec("B2", not bad,
            "★ 空库重建后 8 个视图 Agent 仍 0 命中（不复活）%s" % ("；违规=%s" % bad if bad else ""))
        # 关键：即便意外重建，也绝不能是 active（DDL 默认就是 active）
        ph = ",".join("?" * len(VIEW_NAMES))
        act = c2.execute("SELECT COUNT(*) FROM agents WHERE name IN (%s) AND status='active'" % ph,
                         VIEW_NAMES).fetchone()[0]
        rec("B3", act == 0, "重建库中不存在 active 状态的视图 Agent（实得 %d）" % act)
        keep_sys = c2.execute("SELECT COUNT(*) FROM agents WHERE name=?",
                              ("system_mgmt",)).fetchone()[0]
        rec("B4", keep_sys == 1,
            "重建库中 system_mgmt 仍在（测试agent 是本机残留、被 _keep_row 排除，不要求重建）")
        sk2 = c2.execute("SELECT COUNT(*) FROM skills WHERE name LIKE ?",
                         (SKILL_PREFIX + "%",)).fetchone()[0]
        rec("B5", sk2 >= 9, "重建库中 9 个视图技能仍在（实得 %d，能力承接方未丢）" % sk2)
        c2.close()

    # ── M 变异自证 ───────────────────────────────────────────────────
    # 构造「污染快照」：把一条视图 Agent 塞回 tables.agents（不含 status 列）。
    try:
        from database.bootstrap_assets import bootstrap_assets
        agents_rows = (snap.get("tables") or {}).get("agents") or []
        template = copy.deepcopy(agents_rows[0])
        poison = copy.deepcopy(template)
        poison["name"] = VIEW_NAMES[0]
        poison["display_name"] = VIEW_NAMES[0]
        poison.pop("status", None)
        bad_snap = copy.deepcopy(snap)
        bad_snap["tables"]["agents"] = agents_rows + [poison]
        bad_path = os.path.join(tmpdir, "poisoned_snapshot.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            json.dump(bad_snap, f, ensure_ascii=False)
        rec("M0", snapshot_name_hits(bad_snap) == [VIEW_NAMES[0]],
            "污染快照构造成功：命中 %s" % snapshot_name_hits(bad_snap))

        mut_db = os.path.join(tmpdir, "mutant.db")
        cm = sqlite3.connect(mut_db)
        cm.row_factory = sqlite3.Row
        cm.execute("CREATE TABLE agents (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
                   "display_name TEXT, description TEXT, system_prompt TEXT, "
                   "model_provider_id INTEGER, model_params TEXT, hil_level TEXT, "
                   "kb_required INTEGER, intent_keywords TEXT, icon TEXT, "
                   "status TEXT DEFAULT 'active', version TEXT, kb_scope TEXT, "
                   "capabilities TEXT, input_schema TEXT, output_schema TEXT, "
                   "max_concurrency INTEGER, protocol_range TEXT, builtin INTEGER DEFAULT 0, "
                   "agent_role TEXT, task_domain TEXT, intent_keywords_active INTEGER, "
                   "stage_order INTEGER, is_code_stage INTEGER, is_orch_default INTEGER, "
                   "orch_order INTEGER)")
        for t in ("tools", "skills"):
            cm.execute("CREATE TABLE %s (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
                       "status TEXT)" % t)
        cm.execute("CREATE TABLE agent_tools (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "agent_id INTEGER, tool_type TEXT, tool_name TEXT, enabled INTEGER DEFAULT 1)")
        cm.execute("CREATE TABLE agent_team_members (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                   "main_agent_id INTEGER, sub_agent_id INTEGER, enabled INTEGER DEFAULT 1)")
        cm.commit()
        res = bootstrap_assets(cm, path=bad_path)
        rebuilt = cm.execute("SELECT name,status FROM agents WHERE name=?",
                             (VIEW_NAMES[0],)).fetchall()
        # ① 风险真实：快照残留 ⇒ 被重建，且 status 落 DDL 默认 'active'
        rec("M1", len(rebuilt) == 1 and rebuilt[0]["status"] == "active",
            "变异① 快照残留确实让视图 Agent 复活为 active（实得 %s；bootstrap ok=%s）"
            % ([(r["name"], r["status"]) for r in rebuilt], res.get("ok")))
        # ② 判据有牙齿：只用「复活判据」，排除「保留项缺失」这一替代解释
        bad = check_no_view_agents(cm)
        rec("M2", bool(bad),
            "变异② check_no_view_agents() 对复活库判红（%s）" % (bad or "未判红 = 判据空转"))
        # ②b 反向对照：把该复活行删掉，同一判据必须立刻转绿（证明它认的确实是这一行）
        cm.execute("DELETE FROM agents WHERE name=?", (VIEW_NAMES[0],))
        cm.commit()
        rec("M2b", not check_no_view_agents(cm),
            "变异②b 删掉复活行后判据转绿（确认判据绑定的是视图 Agent 本身）")
        cm.close()

        # 对照：干净快照 ⇒ 判据判绿
        clean_db = os.path.join(tmpdir, "clean.db")
        cc = sqlite3.connect(clean_db)
        cc.row_factory = sqlite3.Row
        # 复用同一份最小 DDL
        for stmt in (
            "CREATE TABLE agents (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
            "display_name TEXT, description TEXT, system_prompt TEXT, "
            "model_provider_id INTEGER, model_params TEXT, hil_level TEXT, "
            "kb_required INTEGER, intent_keywords TEXT, icon TEXT, "
            "status TEXT DEFAULT 'active', version TEXT, kb_scope TEXT, "
            "capabilities TEXT, input_schema TEXT, output_schema TEXT, "
            "max_concurrency INTEGER, protocol_range TEXT, builtin INTEGER DEFAULT 0, "
            "agent_role TEXT, task_domain TEXT, intent_keywords_active INTEGER, "
            "stage_order INTEGER, is_code_stage INTEGER, is_orch_default INTEGER, "
            "orch_order INTEGER)",
            "CREATE TABLE tools (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, status TEXT)",
            "CREATE TABLE skills (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, status TEXT)",
            "CREATE TABLE agent_tools (id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id INTEGER, "
            "tool_type TEXT, tool_name TEXT, enabled INTEGER DEFAULT 1)",
            "CREATE TABLE agent_team_members (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "main_agent_id INTEGER, sub_agent_id INTEGER, enabled INTEGER DEFAULT 1)",
        ):
            cc.execute(stmt)
        cc.commit()
        clean_path = os.path.join(tmpdir, "clean_snapshot.json")
        with open(clean_path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        bootstrap_assets(cc, path=clean_path)
        # 干净快照里没有保留项 ⇒ check_removed 会因「保留项被误删」判红，
        # 故这里只断言「8 个视图 Agent 0 命中」这一半（判据的复活分支）
        ph = ",".join("?" * len(VIEW_NAMES))
        n_hit = cc.execute("SELECT COUNT(*) FROM agents WHERE name IN (%s)" % ph,
                           VIEW_NAMES).fetchone()[0]
        rec("M3", n_hit == 0, "对照：干净快照自举后视图 Agent 0 命中（实得 %d）" % n_hit)
        cc.close()
    except Exception as e:
        rec("M0", False, "变异自证异常：%s: %s" % (type(e).__name__, str(e)[:200]))

    # ── D 判据升级留痕 ───────────────────────────────────────────────
    gs = open(CLUSTER_GATE, encoding="utf-8").read()
    rec("D1", "BETWEEN 165 AND 172" not in gs and "load_snapshot as _load_snap" in gs,
        "verify_capability_clusters.py 的 B4 已升级为「已移除」（旧 disabled 判据已清除）")
    rec("D2", os.path.isfile(TRACE)
        and json.load(open(TRACE, encoding="utf-8")).get("schema") == "p0/d1-view-agents-removal@1",
        "D1 留痕文件存在且 schema 正确")

    # ── Z 零污染 ─────────────────────────────────────────────────────
    _db_stat1 = ((os.path.getsize(DB), os.path.getmtime(DB))
                 if (_has_db and os.path.isfile(DB)) else None)
    _z1_detail = ("真库 size/mtime 全程未变（%d B / %.0f）" % (_db_stat1[0], _db_stat1[1])
                  if _db_stat1 else "门禁全程未创建真库（CI 空库环境）")
    rec("Z1", _db_stat0 == _db_stat1, _z1_detail)
    _self = open(os.path.abspath(__file__), encoding="utf-8").read()
    rec("Z1b", 'MBSE_DB_PATH' in _self and 'mode=ro' in _self,
        "源码真有隔离与只读接线（init_db 走 MBSE_DB_PATH；真库连接 mode=ro）")
    for p in (tmpdb, tmpdb + "-wal", tmpdb + "-shm"):
        if os.path.isfile(p):
            _bounded_unlink(p)
    for n in ("poisoned_snapshot.json", "clean_snapshot.json", "mutant.db", "clean.db"):
        p = os.path.join(tmpdir, n)
        if os.path.isfile(p):
            _bounded_unlink(p)
    try:
        os.rmdir(tmpdir)
    except Exception:
        pass
    rec("Z2", True, "临时库/污染快照已尽力清理（bounded_unlink）")

    print("=" * 78)
    print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
    if degraded:
        print("DEGRADED（已标注、非静默跳过）:")
        for d in degraded:
            print("  -", d)
    if FAIL:
        print("FAIL:", ", ".join(FAIL))
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
