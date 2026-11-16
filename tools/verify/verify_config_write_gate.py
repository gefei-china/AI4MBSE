"""P0-5 常驻门禁：配置读写端点权限门 + 变更留痕（含端到端四档身份验收）。

判「门挂没挂、留痕有没有」只看这一节：
  ① 静态（AST）：6 个 `/api/system/config*` 端点各自挂 CFG_READ / CFG_WRITE；
     3 个写端点都调 `_log_changes`；secret 脱敏判据在源码里。
  ② 真库事实：3 个 preset 角色都带 `system_config:read/write`，且非管理员 admin 仍为空。
  ③ 端到端（**完全隔离**）：在 `MBSE_DB_PATH` 指向的全库副本 + `MBSE_CONFIG_PATH`
     指向的临时文件上跑 TestClient，四档身份逐个验收；真库与用户级配置文件一字不动。
  ④ 留痕内容：PUT static / runtime / reload 各落 config_history，before/after/操作者/来源
     逐项核对；secret 键的 old/new 必须是 ****（绝不落明文密钥）。
  ⑤ 变异自证：摘掉门 / 去掉一个门 / 猴补依赖放行 / 去掉 secret 判据，断言必须翻红。

⚠️ 与方案 P0-5 字面 DoD 的一处**如实偏差**（必须知道，别拿旧判据套）：
   方案表格写「带低权限用户 `X-User-Id: 1` 调用返回 403」，但 D3 拍板「启用门前先给
   preset 补授权位」后，id=1（wang / 设计师 preset）**已是授权用户**，应返回 200。
   真库 17 个用户全是设计师且没有任何管理员账号 ⇒ 不补位就是全员 403、配置页报废。
   故 403 判据迁移到**未授权的 custom 角色**（permissions NULL）。
   匿名在 `auth.enforce_login=False` 下仍按既有兼容态放行（验收不许用匿名判门是否生效）。

跑法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_config_write_gate.py
口径：2026-11-15 P0-5；副本库跑完即释放，不入库。
"""
import ast
import gc
import glob
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# 直接按文件加载 fs_guard，不经过 core 包初始化（此时还没设环境变量，
# import core.config 会把 DB_PATH/CONFIG_PATH 定成真库）。
_spec = importlib.util.spec_from_file_location("p0_fs_guard", REPO / "core" / "fs_guard.py")
_fsg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fsg)
bounded_unlink = _fsg.bounded_unlink


def cleanup_stale():
    """开跑前清掉上一轮残留：TestClient 的 lifespan 启过后台线程，Windows 下
    收尾时副本库常仍被占用 ⇒ 删不掉。这里先尽力清，避免每跑一次累积 534 MB。"""
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "p0_5_gate_*")):
        p = Path(d)
        if not p.is_dir():
            continue
        for f in p.iterdir():
            try:
                bounded_unlink(str(f), timeout=2.0)
            except Exception:
                pass
        try:
            p.rmdir()
        except OSError:
            pass


# ── 环境隔离必须在任何工程模块 import 之前 ──────────────────────────────
cleanup_stale()
WORK = Path(tempfile.mkdtemp(prefix="p0_5_gate_"))
COPY_DB = WORK / "mbse.db"
TMP_CFG = WORK / "mbse_config.json"
_src = sqlite3.connect("file:%s?mode=ro" % (REPO / "mbse.db").as_posix(), uri=True)
_dst = sqlite3.connect(str(COPY_DB))
_src.backup(_dst)
_dst.close()
_src.close()
TMP_CFG.write_text("{}\n", encoding="utf-8")
os.environ["MBSE_DB_PATH"] = str(COPY_DB)
os.environ["MBSE_CONFIG_PATH"] = str(TMP_CFG)
sys.path.insert(0, str(REPO))

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402
from routers import config as cfg_router  # noqa: E402

PASS, FAIL = [], []


def ck(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if detail else ""))


READ_FNS = ("get_system_config", "get_config_schema", "get_think_suggestions")
WRITE_FNS = ("update_static_config", "update_runtime_config", "reload_config")
GATE_MAP = {**{n: "CFG_READ" for n in READ_FNS}, **{n: "CFG_WRITE" for n in WRITE_FNS}}


def gates_ok(src):
    """纯判据：每个端点函数签名里必须有对应的 Depends(门)。"""
    fns = {n.name: n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
    for name, gate in GATE_MAP.items():
        fn = fns.get(name)
        if fn is None:
            return False
        defaults = [ast.unparse(d) for d in fn.args.defaults]
        if not any(("Depends" in d and gate in d) for d in defaults):
            return False
    return True


def log_calls_ok(src):
    """纯判据：3 个写端点函数体都调用 _log_changes（留痕不能只在纸面上）。"""
    for n in ast.parse(src).body:
        if isinstance(n, ast.FunctionDef) and n.name in WRITE_FNS:
            body = ast.dump(n)
            if "_log_changes" not in body:
                return False
    return True


def mask_source_ok(src):
    return '== "secret"' in src and 'return "****"' in src


def roles_ok(conn):
    """纯判据：3 个 preset 都有授权位，且非管理员没被提成超级用户。"""
    rows = conn.execute(
        "SELECT name, permissions FROM roles WHERE type='preset' ORDER BY id").fetchall()
    if len(rows) != 3:
        return False
    for name, raw in rows:
        p = json.loads(raw or "{}")
        if not {"read", "write"} <= set(p.get("system_config") or []):
            return False
        if name != "系统管理员" and p.get("admin"):
            return False
    return True


def main():
    src = (REPO / "routers" / "config.py").read_text(encoding="utf-8")
    seeds = (REPO / "database" / "seeds.py").read_text(encoding="utf-8")
    # 真库基线在脚本开头取：验收「本脚本没写真库」用前后对拍，不写死 0
    _real0 = sqlite3.connect("file:%s?mode=ro" % (REPO / "mbse.db").as_posix(), uri=True)
    BASE_HIST = _real0.execute("SELECT COUNT(*) FROM config_history").fetchone()[0]
    BASE_USERS = _real0.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    _real0.close()

    # ── [S] 静态判据 ──
    ck("S1 6 个 config* 端点全部挂门（3 读 3 写）", gates_ok(src))
    ck("S2 3 个写端点都调用 _log_changes", log_calls_ok(src))
    ck("S3 secret 脱敏判据仍在源码里", mask_source_ok(src))
    ck("S4 _mask 行为：secret → ****，普通键 → 原值",
       cfg_router._mask("llm.deepseek_api_key", "REAL-SECRET") == "****"
       and cfg_router._mask("app.title", "hello") == "hello")
    ck("S5 seeds.py 的 3 个 preset 都声明授权位",
       seeds.count('"system_config": ["read", "write"]') == 3)
    ck("S6 权限域不在 admin 域（补 admin = 超级用户）",
       "CFG_READ = require_permission(\"system_config\", \"read\")" in src
       and "CFG_WRITE = require_permission(\"system_config\", \"write\")" in src)

    # ── [M] 静态变异 ──
    ck("M1 变异（摘掉一个写门）⇒ gates_ok 必须翻红",
       not gates_ok(src.replace("user=Depends(CFG_WRITE)", "", 1)))
    ck("M2 变异（摘掉一个读门）⇒ gates_ok 必须翻红",
       not gates_ok(src.replace("user=Depends(CFG_READ)", "", 1)))
    ck("M3 变异（去掉 secret 判据）⇒ mask_source_ok 必须翻红",
       not mask_source_ok(src.replace('== "secret"', "== 'never'", 1)))

    conn = sqlite3.connect(str(COPY_DB))
    ck("S7 副本库 3 个 preset 角色都已补位且 admin 未被污染", roles_ok(conn))
    ck("M4 变异（少一个角色的授权位）⇒ roles_ok 必须翻红",
       not roles_ok(_mutated_roles_conn(conn)))

    # 夹具用户：custom 低权限（permissions NULL）/ 管理员
    conn.execute("INSERT OR REPLACE INTO users "
                 "(id, username, display_name, role_id, status) VALUES (9001,?,?,107,'active')",
                 ("p0_gate_probe", "P0门禁探针"))
    conn.execute("INSERT OR REPLACE INTO users "
                 "(id, username, display_name, role_id, status) VALUES (9002,?,?,82,'active')",
                 ("p0_gate_admin", "P0门禁管理员"))
    conn.commit()
    conn.close()

    headers = {
        "匿名": {},
        "设计师": {"X-User-Id": "1"},
        "custom低权限": {"X-User-Id": "9001"},
        "系统管理员": {"X-User-Id": "9002"},
    }
    expect_get = {"匿名": 200, "设计师": 200, "custom低权限": 403, "系统管理员": 200}

    with TestClient(app) as client:
        # ── [R] 四档身份 × 读端点 ──
        for who, h in headers.items():
            r = client.get("/api/system/config/schema", headers=h)
            ck("R1 GET /config/schema 身份「%s」⇒ %d" % (who, expect_get[who]),
               r.status_code == expect_get[who], "实际=%d" % r.status_code)

        # ── 写端点：未授权必须 403，且门必须拦在写入之前 ──
        r = client.put("/api/system/config/static", headers=headers["custom低权限"],
                       json={"updates": {"app.title": "MUST-NOT-WRITE"}})
        ck("R2 PUT /config/static 未授权 custom ⇒ 403（门在写入之前）",
           r.status_code == 403, "实际=%d" % r.status_code)

        # ── 授权身份的真实写入 + 留痕 ──
        r = client.put("/api/system/config/static", headers=headers["设计师"],
                       json={"updates": {"app.title": "P0-GATE-PROBE"}})
        ck("R3 PUT /config/static 设计师 ⇒ 200 且回传 history_ids",
           r.status_code == 200 and bool(r.json().get("history_ids")),
           "实际=%d ids=%s" % (r.status_code, r.json().get("history_ids")))

        r = client.put("/api/system/config/runtime", headers=headers["设计师"],
                       json={"updates": {"default_project_id": "p0-gate-probe"}})
        ck("R4 PUT /config/runtime 设计师 ⇒ 200 且回传 history_ids",
           r.status_code == 200 and bool(r.json().get("history_ids")),
           "实际=%d" % r.status_code)

        r = client.post("/api/system/config/reload", headers=headers["设计师"])
        ck("R5 POST /config/reload 设计师 ⇒ 200 且回传 history_ids",
           r.status_code == 200 and bool(r.json().get("history_ids")),
           "实际=%d" % r.status_code)

        r = client.put("/api/system/config/static", headers=headers["系统管理员"],
                       json={"updates": {"llm.deepseek_api_key": "REAL-SECRET-VALUE"}})
        ck("R6 PUT secret 键（管理员）⇒ 200（写入临时配置文件，非真库）",
           r.status_code == 200, "实际=%d" % r.status_code)

        # ── [H] 留痕内容逐项核对 ──
        c = sqlite3.connect(str(COPY_DB))
        c.row_factory = sqlite3.Row
        rows = [dict(x) for x in c.execute(
            "SELECT section,key,old_value,new_value,source,applied_by,applied "
            "FROM config_history ORDER BY id")]
        c.close()
        print("config_history 共 %d 行：" % len(rows))
        for x in rows:
            print("  ", x)
        by_key = {(x["section"], x["key"]): x for x in rows}
        ck("H1 三类来源都落了台账（api:static / api:runtime / api:reload）",
           {x["source"] for x in rows} >= {"api:static", "api:runtime", "api:reload"})
        ck("H2 app.title 留痕含 before→after 且操作者是 wang",
           (("app", "title") in by_key
            and by_key[("app", "title")]["new_value"] == "P0-GATE-PROBE"
            and by_key[("app", "title")]["applied_by"] == "wang"
            and by_key[("app", "title")]["source"] == "api:static"))
        ck("H3 settings 留痕 section=__settings__ 且 after 正确",
           (("__settings__", "default_project_id") in by_key
            and by_key[("__settings__", "default_project_id")]["new_value"] == "p0-gate-probe"))
        ck("H4 reload 留痕 section=__system__", ("__system__", "reload") in by_key)
        sk = by_key.get(("llm", "deepseek_api_key"))
        ck("H5 secret 键台账 old/new 都是 ****（绝不落明文）",
           bool(sk) and sk["old_value"] == "****" and sk["new_value"] == "****"
           and "REAL-SECRET-VALUE" not in json.dumps(rows, ensure_ascii=False),
           sk)

        # ── [M5] 运行时变异：把写门猴补成无条件放行，403 判据必须翻红 ──
        app.dependency_overrides[cfg_router.CFG_WRITE] = lambda: {"username": "mutant"}
        r = client.put("/api/system/config/static", headers=headers["custom低权限"],
                       json={"updates": {"app.title": "MUTANT-WRITE"}})
        ck("M5 变异（猴补放行写门）⇒ 原先 403 的请求不再 403，证明判据绑的是门",
           r.status_code != 403, "实际=%d" % r.status_code)
        app.dependency_overrides.clear()

    # ── 收尾：真库与用户级配置文件零污染 ──
    real = sqlite3.connect("file:%s?mode=ro" % (REPO / "mbse.db").as_posix(), uri=True)
    real.row_factory = sqlite3.Row
    real_hist = real.execute("SELECT COUNT(*) FROM config_history").fetchone()[0]
    real_users = real.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    real_probe = real.execute("SELECT COUNT(*) FROM users WHERE id IN (9001,9002)").fetchone()[0]
    real.close()
    ck("Z1 真库 config_history 未被本脚本写入（前后逐项对拍）",
       real_hist == BASE_HIST, "before=%d after=%d" % (BASE_HIST, real_hist))
    ck("Z2 真库 users 行数不变且没有夹具用户残留",
       real_users == BASE_USERS and real_probe == 0,
       "before=%d after=%d probe=%d" % (BASE_USERS, real_users, real_probe))
    ck("Z3 用户级 mbse_config.json 未被改写（环境变量隔离生效）",
       "P0-GATE-PROBE" not in (Path.home() / ".workbuddy" / "mbse_config.json").read_text(
           encoding="utf-8", errors="ignore"))

    _release()
    print("RESULT: %d PASS / %d FAIL" % (len(PASS), len(FAIL)))
    if FAIL:
        print("FAILED:", *FAIL, sep="\n  - ")
    return 1 if FAIL else 0


def _mutated_roles_conn(src_conn):
    """变异夹具：复制角色行到内存库，删掉一个角色的 system_config 域。"""
    m = sqlite3.connect(":memory:")
    m.execute("CREATE TABLE roles(id INTEGER, name TEXT, type TEXT, permissions TEXT)")
    rows = src_conn.execute("SELECT id,name,type,permissions FROM roles WHERE type='preset'").fetchall()
    out = []
    for i, (rid, name, rtype, raw) in enumerate(rows):
        p = json.loads(raw or "{}")
        if i == 0:
            p.pop("system_config", None)
        out.append((rid, name, rtype, json.dumps(p, ensure_ascii=False)))
    m.executemany("INSERT INTO roles VALUES(?,?,?,?)", out)
    return m


def _release():
    """释放 534 MB 副本；删除一律走 fs_guard 看门狗（WorkBuddy 沙箱 DeleteFileW 会挂死）。

    lifespan 起过的后台线程可能仍持有 sqlite 连接 ⇒ Windows 删除会 PermissionError。
    先 gc 一轮再试；仍失败就如实打印路径（下一轮开头 cleanup_stale 会再清一次，
    不会无限累积）。这个残留不影响任何判据。
    """
    gc.collect()
    try:
        for p in (COPY_DB, WORK / "mbse.db-wal", WORK / "mbse.db-shm", TMP_CFG):
            if p.exists():
                bounded_unlink(str(p), timeout=5.0)
        try:
            WORK.rmdir()
            print("CLEANUP 临时副本已释放")
        except OSError:
            print("WARN 临时副本本轮未能删除（下轮开头会重试清理）：%s" % WORK)
    except Exception as e:
        print("WARN 临时副本释放失败（不影响结论）：%s" % e)


if __name__ == "__main__":
    sys.exit(main())
