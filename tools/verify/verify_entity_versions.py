# -*- coding: utf-8 -*-
"""P0-1 时态主键解耦（影子历史表 entity_versions）：自检脚本。

判据分层
  [0] 静态一致性（建表/索引灌数在迁移里、写读两侧接线、主表语义未被改动）
  [1] 副本迁移（S1/S2）：建表 + 初始版本灌数 == 主表行数 + 幂等复跑
  [2] 一次真实改属性（S3）：主表行数不变 + 该实体版本行 = 2 + 半开区间闭合
  [3] 读取侧（S4）：/history 与 /at 的绝对量判据（含当前时刻与历史时刻）
  [4] 回退与结构约束：无版本行回退主表；UNIQUE(id,branch,valid_from) 真的在拦
  [5] 变异自证（M1 写入退回仅主表 / M2 不灌初始版本 / M3 读取忽略版本表）

⚠️ 全部写操作只在 SQLite backup API 保真快照的**库副本**上，真库零写入
   （⚠️ 本库 WAL 未 checkpoint，shutil.copy2 会丢未落主文件的改动，必须用 backup API）。
"""
import os
import sqlite3
import sys

ROOT = r"C:\Users\gefei\WorkBuddy\2026-08-04-19-05-52\mbse_system"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from database.migrations import _migrate_entity_temporal     # noqa: E402
from repositories.knowledge_repo import KnowledgeRepo        # noqa: E402

REAL = os.path.join(ROOT, "mbse.db")
PAST = "2026-09-15 00:00:00"      # 早于本次更新、晚于数据创建（2026-09-10）
FUTURE = "2099-01-01 00:00:00"

_ok, _fail = [], []


def chk(name, cond, extra=""):
    (_ok if cond else _fail).append(name)
    print("  [%s] %s%s" % ("OK" if cond else "FAIL", name, ("  → " + str(extra)) if extra else ""))


def read(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def snapshot(src, dst):
    if os.path.exists(dst):
        os.remove(dst)
    s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
    d = sqlite3.connect(dst)
    with d:
        s.backup(d)
    d.close()
    s.close()
    return dst


def open_db(path):
    con = sqlite3.connect(path, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=8000")
    return con


def fresh_copy(tag):
    """保真副本 + 跑一次 _migrate_entity_temporal（等价 init_db 的该步）。"""
    con = open_db(snapshot(REAL, os.path.join(ROOT, "tmp", "p01_%s.db" % tag)))
    _migrate_entity_temporal(con)
    con.commit()
    return con


def pick_target(con):
    return dict(con.execute(
        "SELECT * FROM entities WHERE branch='dev' AND status='reviewed' "
        "ORDER BY id LIMIT 1").fetchone())


def scenario(con):
    """跑一次真实改属性，返回全部观测值（供基线与变异共用）。"""
    repo = KnowledgeRepo(con)
    tgt = pick_target(con)
    eid = tgt["id"]
    new_name = tgt["name"] + "·v2"
    main_before = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    vno = repo.update_with_history(
        eid, "dev",
        {"name": new_name, "entity_type": tgt["entity_type"],
         "properties": tgt["properties"],
         "knowledge_category": tgt["knowledge_category"] or ""},
        changed_by="verify-script", change_kind="update")
    con.commit()
    hist = repo.list_entity_history(eid, "dev")
    # as_of 取样点必须**相对于该实体的真实版本时刻**，不能硬编码日期：
    # 「旧版本生效那一刻」用退役行的 valid_from，正好测到区间下界的包含性；
    # 「新版本生效那一刻」用当前行的 valid_from，测半开区间上界（旧版本 valid_to 不含）。
    past_ts = hist[1]["valid_from"] if len(hist) > 1 else PAST
    now_ts = hist[0]["valid_from"] if hist else FUTURE
    return {
        "eid": eid, "old_name": tgt["name"], "new_name": new_name,
        "vno": vno,
        "main_before": main_before,
        "main_after": con.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
        "vcount": con.execute("SELECT COUNT(*) FROM entity_versions WHERE id=? AND branch=?",
                              (eid, "dev")).fetchone()[0],
        "cur_count": con.execute(
            "SELECT COUNT(*) FROM entity_versions WHERE id=? AND branch=? AND is_current=1",
            (eid, "dev")).fetchone()[0],
        "hist": hist,
        "past_ts": past_ts,
        "now_ts": now_ts,
        "as_of_past": repo.get_entity_as_of(eid, past_ts, "dev"),
        "as_of_boundary": repo.get_entity_as_of(eid, now_ts, "dev"),
        "as_of_now": repo.get_entity_as_of(eid, FUTURE, "dev"),
        "main_row_name": con.execute("SELECT name FROM entities WHERE id=? AND branch=?",
                                     (eid, "dev")).fetchone()[0],
    }


def judge(s):
    """把观测值转成一组布尔判据（基线与变异共用同一组判据）。"""
    h = s["hist"]
    return {
        "主表行数不变": s["main_after"] == s["main_before"],
        "版本表该实体 == 2 行": s["vcount"] == 2,
        "当前版本行唯一": s["cur_count"] == 1,
        "history 返回 2 条": len(h) == 2,
        "history[0] 为当前版本且是新值": bool(h) and h[0]["is_current"] == 1
                                         and h[0]["name"] == s["new_name"],
        "history[1] 为已退役旧值": len(h) > 1 and h[1]["is_current"] == 0
                                   and h[1]["name"] == s["old_name"] and bool(h[1]["valid_to"]),
        "半开区间闭合(旧.valid_to == 新.valid_from)": len(h) > 1
                                          and h[1]["valid_to"] == h[0]["valid_from"],
        "as_of 旧版本生效时刻 → 旧值": bool(s["as_of_past"]) and s["as_of_past"]["name"] == s["old_name"],
        "as_of 新版本生效时刻 → 新值（半开区间上界不含旧版本）": bool(s["as_of_boundary"])
                                          and s["as_of_boundary"]["name"] == s["new_name"],
        "as_of 当前时刻 → 新值": bool(s["as_of_now"]) and s["as_of_now"]["name"] == s["new_name"],
        "主表已是新值（语义不变=仅当前行）": s["main_row_name"] == s["new_name"],
    }


def main():
    print("=" * 78)
    print("P0-1 时态主键解耦 · 自检  |  影子历史表 entity_versions")
    print("=" * 78)

    # ══════════════ [0] 静态 ══════════════
    print("\n--- [0] 静态一致性 ---")
    src_mig = read(os.path.join(ROOT, "database", "migrations", "ontology.py"))
    src_repo = read(os.path.join(ROOT, "repositories", "knowledge_repo.py"))
    src_ent = read(os.path.join(ROOT, "routers", "knowledge_parts", "entities.py"))
    src_gph = read(os.path.join(ROOT, "routers", "knowledge_parts", "graph.py"))
    src_rebuild = read(os.path.join(ROOT, "database", "migrations", "rebuild.py"))
    chk("[0a] 迁移内建 entity_versions 表 + PK(id,branch,version_no) + UNIQUE(id,branch,valid_from)",
        "CREATE TABLE IF NOT EXISTS entity_versions" in src_mig
        and "PRIMARY KEY (id, branch, version_no)" in src_mig
        and "UNIQUE (id, branch, valid_from)" in src_mig)
    chk("[0b] 迁移内含初始版本灌数（NOT EXISTS 幂等）",
        "entity_versions 初始化灌入" in src_mig
        and "WHERE NOT EXISTS (" in src_mig and "FROM entity_versions v" in src_mig)
    chk("[0c] 读取侧改读版本表（history/as_of 各有回退）",
        "FROM entity_versions WHERE id=? AND branch=?" in src_repo
        and "FROM entity_versions WHERE id=?" in src_repo)
    chk("[0d] 写入侧：update_with_history 落版本 + 版本辅助函数齐备",
        "_append_version_snapshot" in src_repo and "_ensure_initial_version" in src_repo
        and "def _insert_version" in src_repo and "def _vnow" in src_repo)
    chk("[0e] 两处写路径合一（update_entity 委托 update_with_history）",
        "self.update_with_history(" in src_repo)
    chk("[0f] 决策 D1 = 方案 B：主表主键保持不变（未把版本维塞进 entities PK）",
        "PRIMARY KEY (id, branch)" in src_rebuild
        and "PRIMARY KEY (id, branch, valid_from)" not in src_rebuild
        and "PRIMARY KEY (id, branch, valid_from)" not in src_mig
        and "DROP TABLE entities" not in src_mig)
    chk("[0g] create/review 两侧均落版本，且端点透传 changed_by",
        "self._ensure_initial_version(entity_id, branch)" in src_repo
        and 'self._append_version_snapshot(entity_id, branch, "review", actor)' in src_repo
        and "changed_by=_actor(user)" in src_ent and "changed_by=_actor(user)" in src_gph)
    chk("[0h] 版本时间戳为微秒精度（防同秒 UNIQUE 冲突）",
        "timespec='microseconds'" in src_repo)

    # ══════════════ [1] 副本迁移 ══════════════
    print("\n--- [1] 副本迁移（S1/S2） ---")
    con = fresh_copy("base")
    main_rows = con.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    v_rows = con.execute("SELECT COUNT(*) FROM entity_versions").fetchone()[0]
    pairs_main = con.execute("SELECT COUNT(*) FROM (SELECT id,branch FROM entities GROUP BY 1,2)"
                             ).fetchone()[0]
    pairs_ver = con.execute("SELECT COUNT(*) FROM (SELECT id,branch FROM entity_versions GROUP BY 1,2)"
                            ).fetchone()[0]
    chk("[1a] 初始版本数 == 主表行数（绝对量）", v_rows == main_rows, "version=%s main=%s" % (v_rows, main_rows))
    chk("[1b] 每个 (id,branch) 都有版本行（集合等势）", pairs_ver == pairs_main,
        "ver=%s main=%s" % (pairs_ver, pairs_main))
    chk("[1c] 初始行均为 is_current=1 且 valid_to IS NULL",
        con.execute("SELECT COUNT(*) FROM entity_versions WHERE is_current!=1 OR valid_to IS NOT NULL"
                    ).fetchone()[0] == 0)
    chk("[1d] change_kind='init' 计数 == 主表行数",
        con.execute("SELECT COUNT(*) FROM entity_versions WHERE change_kind='init'"
                    ).fetchone()[0] == main_rows)
    tnames = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    inames = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")]
    chk("[1e] 表与两个索引均已创建",
        "entity_versions" in tnames and "idx_ev_current" in inames and "idx_ev_range" in inames)

    before = [tuple(r) for r in con.execute(
        "SELECT id,branch,version_no,valid_from,name FROM entity_versions ORDER BY id,branch,version_no")]
    _migrate_entity_temporal(con)
    con.commit()
    after = [tuple(r) for r in con.execute(
        "SELECT id,branch,version_no,valid_from,name FROM entity_versions ORDER BY id,branch,version_no")]
    chk("[1f] 迁移幂等复跑不改变版本表内容", before == after, "%d 行" % len(after))

    # ══════════════ [2][3] 真实改属性 + 读取侧 ══════════════
    print("\n--- [2] 一次真实改属性（S3） + [3] 读取侧（S4） ---")
    base = scenario(con)
    bj = judge(base)
    for k, v in bj.items():
        chk("[2/3] " + k, v)
    chk("[2] update_with_history 返回 version_no == 2", base["vno"] == 2, "实测 %s" % base["vno"])
    print("     观测：%s「%s」→「%s」；版本行 %d；history[0].valid_from=%s history[1].valid_to=%s"
          % (base["eid"], base["old_name"], base["new_name"], base["vcount"],
             base["hist"][0]["valid_from"] if base["hist"] else "-",
             base["hist"][1]["valid_to"] if len(base["hist"]) > 1 else "-"))

    # 连续两次变更（验证微秒精度下的 UNIQUE 不误撞 + 版本号递增）
    repo = KnowledgeRepo(con)
    repo.update_with_history(base["eid"], "dev",
                             {"name": base["new_name"] + "·v3"}, changed_by="verify", change_kind="update")
    con.commit()
    h3 = repo.list_entity_history(base["eid"], "dev")
    chk("[2] 连续两次变更 → 3 个版本且 version_no 连续",
        [r["version_no"] for r in h3] == [3, 2, 1], "实测 %s" % [r["version_no"] for r in h3])
    chk("[2] 连续变更后当前版本仍唯一",
        con.execute("SELECT COUNT(*) FROM entity_versions WHERE id=? AND branch=? AND is_current=1",
                    (base["eid"], "dev")).fetchone()[0] == 1)

    # ══════════════ [4] 回退与结构约束 ══════════════
    print("\n--- [4] 回退与结构约束 ---")
    con.execute("INSERT INTO entities (id, name, entity_type, properties, status, branch, project_id,"
                " valid_from, tx_from) VALUES (?,?,?,?,?,?,?,?,?)",
                ("V-verify-bypass", "旁路实体", "Component", "{}", "reviewed", "dev",
                 "project-satnet-broadband", "2026-09-01 00:00:00", "2026-09-01 00:00:00"))
    con.commit()
    repo = KnowledgeRepo(con)
    fb = repo.list_entity_history("V-verify-bypass", "dev")
    chk("[4a] 无版本行实体 → /history 回退主表（返回 1 条而非空）", len(fb) == 1, "实测 %d 条" % len(fb))
    chk("[4b] 回退行字段形状兼容（含 name/entity_type/branch）",
        fb and fb[0]["name"] == "旁路实体" and fb[0]["branch"] == "dev")
    fb2 = repo.get_entity_as_of("V-verify-bypass", PAST, "dev")
    chk("[4c] 无版本行实体 → /at 走主表回退并正确命中（旧逻辑不丢）",
        bool(fb2) and fb2["id"] == "V-verify-bypass" and fb2["name"] == "旁路实体",
        "→ %s" % (fb2["name"] if fb2 else None))

    row0 = con.execute("SELECT id,branch,version_no,valid_from FROM entity_versions LIMIT 1").fetchone()
    dup_err = None
    try:
        con.execute("INSERT INTO entity_versions (id,branch,version_no,valid_from,valid_to,is_current) "
                    "VALUES (?,?,?,?,?,?)",
                    (row0[0], row0[1], 999, row0[3], None, 0))
    except sqlite3.IntegrityError as e:
        dup_err = str(e)
    chk("[4d] UNIQUE(id,branch,valid_from) 真的在拦（重复生效时刻被拒）",
        dup_err is not None and "UNIQUE" in dup_err.upper(), dup_err or "(未报错!)")
    dup_err2 = None
    try:
        con.execute("INSERT INTO entity_versions (id,branch,version_no,valid_from,valid_to,is_current) "
                    "VALUES (?,?,?,?,?,?)", (row0[0], row0[1], row0[2], "2099-01-01 00:00:00", None, 0))
    except sqlite3.IntegrityError as e:
        dup_err2 = str(e)
    chk("[4e] PRIMARY KEY(id,branch,version_no) 真的在拦（重复版本号被拒）",
        dup_err2 is not None and "UNIQUE" in dup_err2.upper(), dup_err2 or "(未报错!)")
    chk("[4f] 无重复当前行（全局）",
        con.execute("SELECT COUNT(*) FROM (SELECT id,branch FROM entity_versions WHERE is_current=1 "
                    "GROUP BY 1,2 HAVING COUNT(*)>1)").fetchone()[0] == 0)
    con.close()

    # ══════════════ [5] 变异自证 ══════════════
    print("\n--- [5] 变异自证（断言本身必须能被击穿） ---")
    # M1：写入退回「仅 UPDATE 主表」（用旧的空转实现替换）
    def m1_impl(self, entity_id, branch, new_data, changed_by="", change_kind="update"):
        row = self.one("SELECT * FROM entities WHERE id=? AND branch=?", (entity_id, branch))
        if not row:
            return 0
        self.execute("UPDATE entities SET name=?, entity_type=?, properties=?, knowledge_category=? "
                     "WHERE id=? AND branch=?",
                     (new_data.get("name", row["name"]), new_data.get("entity_type", row["entity_type"]),
                      new_data.get("properties", row["properties"]),
                      new_data.get("knowledge_category", row["knowledge_category"] or ""),
                      entity_id, branch))
        return 0
    orig_uwh = KnowledgeRepo.update_with_history
    try:
        KnowledgeRepo.update_with_history = m1_impl
        c1 = fresh_copy("m1")
        s1 = scenario(c1)
        j1 = judge(s1)
        c1.close()
    finally:
        KnowledgeRepo.update_with_history = orig_uwh
    broke1 = [k for k, v in j1.items() if not v]
    chk("[M1] 写入退回仅主表 → 版本判据必须 FAIL（证明 [2] 真在读版本行）",
        ("版本表该实体 == 2 行" in broke1) and ("history 返回 2 条" in broke1), "被击穿: %s" % broke1)

    # M2：追加版本时**不退役旧版本**（漏掉 UPDATE ... SET valid_to, is_current=0 这一步）
    #     → 同一 (id,branch) 出现两条 is_current=1 → 版本区间重叠、当前版本不唯一
    def m2_impl(self, entity_id, branch, change_kind, changed_by=""):
        self._ensure_initial_version(entity_id, branch)
        now = self._vnow()
        vno = self.scalar("SELECT COALESCE(MAX(version_no),0)+1 FROM entity_versions "
                          "WHERE id=? AND branch=?", (entity_id, branch))
        self._insert_version(entity_id, branch, vno, now, None, 1, change_kind, changed_by)
        return vno
    orig_avs = KnowledgeRepo._append_version_snapshot
    try:
        KnowledgeRepo._append_version_snapshot = m2_impl
        c2 = fresh_copy("m2")
        s2 = scenario(c2)
        j2 = judge(s2)
        c2.close()
    finally:
        KnowledgeRepo._append_version_snapshot = orig_avs
    broke2 = [k for k, v in j2.items() if not v]
    chk("[M2] 不退役旧版本 → 「当前版本行唯一」与「history[1] 为已退役旧值」必须 FAIL"
        "（证明退役步骤与 [4f] 非空转）",
        "当前版本行唯一" in broke2 and "history[1] 为已退役旧值" in broke2, "被击穿: %s" % broke2)

    # M3：读取侧忽略版本表（强制走主表）
    def m3_impl(self, entity_id, branch="dev"):
        return self.rows("SELECT * FROM entities WHERE id=? AND branch=? ORDER BY valid_from DESC",
                         (entity_id, branch or "dev"))
    orig_leh = KnowledgeRepo.list_entity_history
    try:
        KnowledgeRepo.list_entity_history = m3_impl
        c3 = fresh_copy("m3")
        s3 = scenario(c3)
        j3 = judge(s3)
        c3.close()
    finally:
        KnowledgeRepo.list_entity_history = orig_leh
    broke3 = [k for k, v in j3.items() if not v]
    chk("[M3] 读取侧忽略版本表 → 「history 返回 2 条」必须 FAIL（证明 [3] 真在读版本表）",
        "history 返回 2 条" in broke3, "被击穿: %s" % broke3)

    print("\n" + "=" * 78)
    print("基线通过 %d / 失败 %d%s" % (len(_ok), len(_fail),
                                       ("  失败项: " + " | ".join(_fail)) if _fail else ""))
    print("=" * 78)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
