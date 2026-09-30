"""验证 agent_tools.params 废列移除（2026-09-30，用户四轮反馈问题 1：彻底移除）。

## 验证对象
1. 迁移 `_migrate_drop_agent_tools_params` 真的把 params 列删掉；
2. **绑定关系一行不丢**（只删列不删行 —— 这是本改动唯一的安全红线）；
3. 幂等（init_db 每次启动都跑，第二次必须无事发生）；
4. SQLite < 3.35 的降级路径不抛异常、不删列；
5. 新库（表不存在）路径不抛异常；
6. 代码侧清理干净：DDL / add_tool / AgentToolIn / 路由调用点全无 params；
7. 真实 `AgentRepo.add_tool` 在新 schema（无 params 列）下仍能插入。

## 纪律（踩过坑才写下的）
- **夹具自证**：迁移前必须先断言「params 列确实存在」，否则"删掉了"是空转断言；
- **变异自证**：把迁移换成 no-op 再跑同一套判据，必须 FAIL —— 证明判据不是恒真的；
- **不动真库**：全部在同一个 tmp fixture 库里跑，真库由服务重启时的 init_db 自然迁移。
"""
import os
import re
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database.migrations.misc import _migrate_drop_agent_tools_params as MIG  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NEW_DDL = os.path.join(REPO, "database", "schema.py")

PASS, FAIL = [], []
CUR = FAIL          # 当前收集桶：变异跑期间切到独立列表，避免把"预期 FAIL"算成真失败
MF = []             # mutation failures（预期）


def chk(name, cond, detail=""):
    (PASS if cond else CUR).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail and not cond else ""))


# ── 旧结构（迁移前真库形态，取自真库 PRAGMA 实况）──
OLD_DDL = """
CREATE TABLE agent_tools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id INTEGER NOT NULL,
    tool_type TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    params TEXT DEFAULT '{}',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(agent_id, tool_type, tool_name)
)"""

FIXTURE_ROWS = [
    (1, 64, "mcp", "MCP-文档解析"),
    (2, 65, "skill", "MBSE_Requirements_Analysis_Workflow"),
    (3, 68, "tool", "conflict_check"),
    (4, 71, "tool", "graph_retrieve"),
    (5, 72, "tool", "impact_analyze"),
]


def new_fixture(path):
    """建最小夹具库：旧结构 agent_tools + 5 行绑定（params 全 '{}'，与真库一致）。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(OLD_DDL)
    for _id, aid, tt, tn in FIXTURE_ROWS:
        conn.execute(
            "INSERT INTO agent_tools (id, agent_id, tool_type, tool_name, params) VALUES (?,?,?,?,'{}')",
            (_id, aid, tt, tn))
    conn.commit()
    return conn


def fingerprint(conn):
    """绑定关系指纹：id/agent_id/tool_type/tool_name/enabled 全量。"""
    rows = conn.execute(
        "SELECT id, agent_id, tool_type, tool_name, enabled FROM agent_tools ORDER BY id").fetchall()
    return [(r["id"], r["agent_id"], r["tool_type"], r["tool_name"], r["enabled"]) for r in rows]


def cols_of(conn):
    return [r["name"] for r in conn.execute("PRAGMA table_info(agent_tools)").fetchall()]


class _LowVersionConn:
    """代理连接：只对 SELECT sqlite_version() 撒谎，用于验证 <3.35 降级分支。"""

    def __init__(self, real, version="3.34.0"):
        self._r, self._v = real, version

    def cursor(self):
        return self._r.cursor()

    def execute(self, sql, *a):
        if "sqlite_version" in sql:
            return _FakeCur([(self._v,)])
        return self._r.execute(sql, *a)

    def commit(self):
        self._r.commit()

    def rollback(self):
        self._r.rollback()


class _FakeCur:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row[0]


def battery(mig) -> bool:
    """一整套判据，mig 可替换为 no-op 做变异自证。返回通过数量。"""
    ok = 0

    # ── A. 主路径：删列 + 保行 ──
    tmp = os.path.join(tempfile.mkdtemp(), "t.db")
    conn = new_fixture(tmp)
    before = fingerprint(conn)
    before_cols = cols_of(conn)
    # 夹具自证：迁移前 params 列确实在（否则后面的"删掉了"是空转断言）
    chk("夹具自证：迁移前 agent_tools 含 params 列", "params" in before_cols, before_cols)
    chk("夹具自证：迁移前有 5 条绑定行", len(before) == 5, len(before))

    mig(conn)
    after_cols = cols_of(conn)
    after = fingerprint(conn)
    ok += 1 if "params" not in after_cols else 0
    chk("A1 迁移后 params 列已消失", "params" not in after_cols, after_cols)
    chk("A2 其余列保持完整", after_cols == ["id", "agent_id", "tool_type", "tool_name", "enabled", "created_at"], after_cols)
    ok += 1 if after == before else 0
    chk("A3 绑定关系一行不丢（指纹全等）", after == before, f"{before} -> {after}")
    chk("A4 行数仍为 5", conn.execute("SELECT COUNT(*) FROM agent_tools").fetchone()[0] == 5)

    # ── B. 幂等：再跑一次（init_db 每启动必跑）──
    try:
        mig(conn)
        idem = True
    except Exception as e:  # noqa: BLE001
        idem = False
        chk("B1 二次调用不抛异常", False, str(e))
    if idem:
        chk("B1 二次调用不抛异常", True)
        chk("B2 二次调用后结构不变", cols_of(conn) == after_cols)
        chk("B3 二次调用后数据不变", fingerprint(conn) == before)
        ok += 1 if cols_of(conn) == after_cols and fingerprint(conn) == before else 0

    # ── C. 迁移后 add_tool 仍能写（真 repo，非手写 SQL）──
    try:
        from repositories.agent_repo import AgentRepo
        repo = AgentRepo(conn)
        repo.add_tool(999, "skill", "some_skill")
        repo.add_tool(999, "skill", "some_skill")   # 重复 → INSERT OR IGNORE
        n = conn.execute("SELECT COUNT(*) FROM agent_tools WHERE agent_id=999").fetchone()[0]
        ok += 1 if n == 1 else 0
        chk("C1 新 schema 下 AgentRepo.add_tool 可插入", n == 1, n)
        chk("C2 重复绑定幂等（仍只有 1 行）", n == 1, n)
    except Exception as e:  # noqa: BLE001
        chk("C1 新 schema 下 AgentRepo.add_tool 可插入", False, str(e))
        chk("C2 重复绑定幂等（仍只有 1 行）", False, str(e))
    conn.close()
    shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)

    # ── D. 降级：SQLite < 3.35 不删列、不抛异常 ──
    tmp2 = os.path.join(tempfile.mkdtemp(), "t2.db")
    c2 = new_fixture(tmp2)
    try:
        MIG(_LowVersionConn(c2, "3.34.0"))
        chk("D1 旧 SQLite 不抛异常", True)
    except Exception as e:  # noqa: BLE001
        chk("D1 旧 SQLite 不抛异常", False, str(e))
    chk("D2 旧 SQLite 保留 params 列（降级不毁结构）", "params" in cols_of(c2), cols_of(c2))
    chk("D3 旧 SQLite 绑定行仍全在", fingerprint(c2) == before)
    ok += 1 if "params" in cols_of(c2) and fingerprint(c2) == before else 0
    c2.close()
    shutil.rmtree(os.path.dirname(tmp2), ignore_errors=True)

    # ── E. 新库路径：agent_tools 表尚不存在 ──
    tmp3 = os.path.join(tempfile.mkdtemp(), "t3.db")
    c3 = sqlite3.connect(tmp3)
    c3.row_factory = sqlite3.Row
    try:
        mig(c3)
        chk("E1 表不存在时不抛异常", True)
        ok += 1
    except Exception as e:  # noqa: BLE001
        chk("E1 表不存在时不抛异常", False, str(e))
    c3.close()
    shutil.rmtree(os.path.dirname(tmp3), ignore_errors=True)
    return ok


def static_checks():
    """代码侧清理：DDL / add_tool / AgentToolIn / 调用点。"""
    print("\n── 静态检查（全仓无残留引用）──")
    src_root = REPO
    hits = []
    for root, dirs, files in os.walk(src_root):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv", "tmp"}]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(root, fn)
            try:
                txt = open(p, encoding="utf-8", errors="ignore").read()
            except Exception:  # noqa: BLE001
                continue
            if os.path.abspath(p) == os.path.abspath(__file__):
                continue
            if fn == "misc.py" and "_migrate_drop_agent_tools_params" in txt:
                continue     # 迁移本身允许出现 params 字样
            for ln in txt.splitlines():
                s = ln.strip()
                # 忽略注释行、迁移注册行（它们含"移除 agent_tools.params"字样但非 SQL）
                if s.startswith("#") or "_migrate_drop_agent_tools_params" in s:
                    continue
                if "agent_tools" not in s or "params" not in s:
                    continue
                # 只认真正的 SQL 语句：必须含 SQL 动词
                if not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|ALTER|CREATE|PRAGMA)\b", s, re.I):
                    continue
                hits.append(f"{os.path.relpath(p, REPO)}: {s[:90]}")
    chk("S1 全仓 .py 无 agent_tools↔params 的 SQL 引用", not hits, "; ".join(hits[:3]))

    ddl = open(NEW_DDL, encoding="utf-8").read()
    seg = re.search(r"CREATE TABLE IF NOT EXISTS agent_tools \((.*?)\)", ddl, re.S)
    seg_txt = seg.group(1) if seg else ""
    chk("S2 建表 DDL 已无 params 列", "params" not in seg_txt, seg_txt.strip()[:80])
    chk("S3 schema.py 已注册迁移调用", "_migrate_drop_agent_tools_params(conn)" in ddl)

    init_py = os.path.join(REPO, "database", "migrations", "__init__.py")
    itxt = open(init_py, encoding="utf-8").read()
    chk("S4 migrations/__init__ 导入+导出均已注册",
        "_migrate_drop_agent_tools_params," in itxt and '"_migrate_drop_agent_tools_params"' in itxt)

    repo_py = os.path.join(REPO, "repositories", "agent_repo.py")
    rtxt = open(repo_py, encoding="utf-8").read()
    sig = re.search(r"def add_tool\(([^)]*)\)", rtxt)
    chk("S5 AgentRepo.add_tool 签名已无 params", bool(sig) and "params" not in sig.group(1), sig.group(1) if sig else "NOT FOUND")
    chk("S6 add_tool INSERT 已无 params 列", "agent_tools (agent_id, tool_type, tool_name)" in rtxt)
    chk("S7 绑定读取输出已无 params 字段", 'json.loads(b.get("params")' not in rtxt)

    sch = os.path.join(REPO, "models", "studio.py")
    stxt = open(sch, encoding="utf-8").read()
    m = re.search(r"class AgentToolIn\(BaseModel\):(.*?)\n\n", stxt, re.S)
    chk("S8 AgentToolIn 已无 params 字段", bool(m) and "params:" not in m.group(1), (m.group(1) if m else "")[:80])

    callers = []
    for p in [os.path.join(REPO, "routers", "studio_parts", "agents.py"),
              os.path.join(REPO, "routers", "studio_parts", "hooks_copy.py")]:
        t = open(p, encoding="utf-8").read()
        for mm in re.finditer(r"repo\.add_tool\([^\n]*", t):
            callers.append((os.path.basename(p), mm.group(0).strip()))
    chk("S9 所有 add_tool 调用点只剩 3 参",
        all(c.count(",") == 2 for _, c in callers) and len(callers) == 2, str(callers))


def main():
    print("=" * 72)
    print("verify_agent_tools_no_params —— agent_tools.params 废列移除")
    print("=" * 72)

    print("\n── 主判据（真实迁移）──")
    battery(MIG)

    print("\n── 变异自证：把迁移换成 no-op，同一套判据必须 FAIL ──")
    global CUR
    CUR = MF
    battery(lambda conn: None)
    CUR = FAIL
    # 判据设计：no-op 变异**应当**翻转的是「结构类」判据（A1/A2）；
    # A3/A4/C1 是"不变量"（无论迁移跑没跑都该为绿），不作为判别力证据，故阈值=2。
    chk("M1 no-op 变异后结构类判据确实翻转 FAIL（证明 A1/A2 非空转）", len(MF) >= 2, f"翻转 {len(MF)} 条：{MF}")
    # 变异只是临时替换函数入参，未落盘，无需还原；此处显式说明
    print("     （变异仅替换本次调用的可调用对象，未改动任何磁盘文件）")

    static_checks()

    print("\n" + "=" * 72)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - " + f)
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
