"""verify_intent_cache_key.py —— 意图缓存 key 口径（P0-3）常驻断言。

背景（2026-09-30 实测）：`detect()` 用 `text.lower()` 查缓存，而 `_done()` 把**原文**交给
`_cache_set` → 写 key = md5(原文) / 读 key = md5(lower) ⇒ **含任意 ASCII 大写的输入永不命中**。
真库 `intent_cache` 实测 6 行写入 / `hit_count` 总和 **0**，其中 2 行（`SysML v2模型代码`、`BDD 视图`）
是**结构性死行**（其原文码 ≠ lower 码）。

判据全部是**行为级**（"写后能不能读到"）+ **落库口径级**（"存进去的 hash 到底是哪个"），
不是"源码里有没有 .lower()" —— 后者属于空转断言（本仓 §6.2 已证伪过同类）。
每条断言配一条**变异自证**：用 `exec` 变异孪生体把旧写法注回（磁盘文件一字不动），
断言必须**新增 FAIL**（§6.7a：只认"新增失败"，基线自身失败不算数）。

跑法：<repo>\\.venv\\Scripts\\python.exe -X utf8 tools\\verify\\verify_intent_cache_key.py
"""
import hashlib
import inspect
import os
import sqlite3
import sys
import tempfile
import textwrap
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.intent import IntentRouter  # noqa: E402

PASS, FAIL = [], []

# case_id → 人类可读描述（顺序即断言输出顺序）
CASES = {
    "C1": "含 ASCII 大写：写原文、按 lower 读，能读到",
    "C2": "全小写：能读到（对照；旧写法在这条上也过，不构成修复证明）",
    "C3": "混合大小写+数字：能读到",
    "C4": "query 列存的是**原文**（可观测性不被 lower 破坏）",
    "C5": "confidence<0.7 不写缓存（既有守卫未被改动误删）",
    "C6": "空 fp：不可读且不写库",
    "C7": "大小写不同但 lower 相同的两条 → 命中同一行（不是两行）",
    "C8": "**落库 hash 就是 lower 口径**（且不等于原文口径）",
}
# 变异 → 强制必须被抓住的 case（核心集；其余作观察，不写死以免脆弱）
MUTATIONS = [
    ("M1 注回旧写法：_cache_set 去 lower（=写原文）", "text.encode", {"C1", "C3", "C8"}),
    ("M2 _cache_set 改用 upper", "text.upper().encode", {"C1", "C3", "C8"}),
]

BUILTIN_DDL = """
CREATE TABLE intent_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    query_hash TEXT NOT NULL UNIQUE,
    intent TEXT DEFAULT '',
    route TEXT DEFAULT '',
    confidence REAL DEFAULT 0,
    index_fp TEXT DEFAULT '',
    hit_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""
REQUIRED_COLS = {"query", "query_hash", "intent", "route", "confidence", "index_fp", "hit_count"}

UPPER = "帮我生成这个系统的SysML v2模型代码"
LOWER_ONLY = "画一张参数图"
MIXED = "输出一份 BDD 视图"
FP = "fp-fixture-0001"


def check(tag, cond, detail=""):
    (PASS if cond else FAIL).append(tag)
    print(("  [OK] " if cond else "  [!!] ") + tag + ("" if cond else "  → " + str(detail)[:220]))


# ─────────────────────────── 夹具 ───────────────────────────
def ddl_from_real_db():
    """优先用**真库**的 DDL，避免手抄漂移（本仓已被此坑咬过两次）。"""
    try:
        c = sqlite3.connect("file:mbse.db?mode=ro", uri=True)
        try:
            row = c.execute("SELECT sql FROM sqlite_master WHERE name='intent_cache'").fetchone()
            return row[0] if row else None
        finally:
            c.close()
    except Exception:
        return None


def make_fixture():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="verify_intent_cache_")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ddl = ddl_from_real_db()
    src = "真库 DDL" if ddl else "内置兜底 DDL"
    conn.execute(ddl or BUILTIN_DDL)          # ⚠️ 只建 intent_cache，全程不碰真库
    cols = {r[1] for r in conn.execute("PRAGMA table_info(intent_cache)")}
    conn.commit()
    conn.close()
    assert not (REQUIRED_COLS - cols), f"夹具缺列 {REQUIRED_COLS - cols}（DDL 来源：{src}）"
    return path, src


def q1(path, sql, args=()):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql, args).fetchone()
    finally:
        c.close()


class PatchedDB:
    """把 `database.get_db` 指向夹具；每次返回**新连接**（被测代码 finally 会 close 它）。"""

    def __init__(self, path):
        self.path = path
        import database
        self.mod = database
        self.orig = database.get_db

    def __enter__(self):
        def _get():
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            return c
        self.mod.get_db = _get
        return self

    def __exit__(self, *a):
        self.mod.get_db = self.orig
        return False


def md5(s):
    return hashlib.md5(s.encode("utf-8", "ignore")).hexdigest()


# ─────────────────────────── 用例 ───────────────────────────
def run_cases(router, path):
    """真实调用形态：`_cache_set(原文)` / `_cache_get(原文.lower())` —— 与 detect/_done 一致。"""
    res = {}

    router._cache_set(UPPER, FP, "design", "rule", 0.95)
    got = router._cache_get(UPPER.lower(), FP)
    res["C1"] = bool(got and got[0] == "design")

    router._cache_set(LOWER_ONLY, FP, "design", "rule", 0.95)
    got = router._cache_get(LOWER_ONLY.lower(), FP)
    res["C2"] = bool(got and got[0] == "design")

    router._cache_set(MIXED, FP, "design", "rule", 0.95)
    got = router._cache_get(MIXED.lower(), FP)
    res["C3"] = bool(got and got[0] == "design")

    row = q1(path, "SELECT query FROM intent_cache WHERE query_hash=?", (md5(UPPER.lower()),))
    res["C4"] = bool(row and row[0] == UPPER)

    n0 = q1(path, "SELECT COUNT(*) FROM intent_cache")[0]
    router._cache_set("低置信句子XYZ", FP, "chat", "llm_weak", 0.5)
    n1 = q1(path, "SELECT COUNT(*) FROM intent_cache")[0]
    res["C5"] = (n0 == n1)

    router._cache_set("无指纹句子ABC", "", "design", "rule", 0.95)
    n2 = q1(path, "SELECT COUNT(*) FROM intent_cache")[0]
    res["C6"] = (router._cache_get("无指纹句子ABC", "") is None) and (n2 == n1)

    router._cache_set("SysML代码生成", FP, "design", "rule", 0.90)
    router._cache_set("sysml代码生成", FP, "design", "rule", 0.91)
    rows = q1(path, "SELECT COUNT(*) FROM intent_cache WHERE query_hash=?", (md5("sysml代码生成"),))[0]
    res["C7"] = (rows == 1)

    hrow = q1(path, "SELECT query_hash FROM intent_cache WHERE query=?", (UPPER,))
    res["C8"] = bool(hrow and hrow[0] == md5(UPPER.lower()) and hrow[0] != md5(UPPER))
    return res


# ─────────────────────────── 变异 ───────────────────────────
def mutated_fn(name, repl):
    """§6.2 安全姿势：取函数源码 → 文本外科手术 → exec 成孪生体（磁盘文件一字不动）。"""
    src = textwrap.dedent(inspect.getsource(getattr(IntentRouter, name)))
    anchor = "text.lower().encode"
    assert anchor in src, f"变异锚点未命中：{anchor!r} in {name}"
    mutant = src.replace(anchor, repl)
    assert mutant != src, "变异未产生差异"
    ns = {"__name__": "mutant"}
    exec(compile(mutant, "<mutant>", "exec"), ns)
    return ns[name]


def main():
    print("=" * 78)
    print("P0-3 意图缓存 key 口径 —— 常驻断言 + 变异自证")
    print("=" * 78)

    path, src = make_fixture()
    print(f"夹具库：{path}\nDDL 来源：{src}\n")

    h_before = hashlib.sha256(open("agent/intent.py", "rb").read()).hexdigest()

    print("[1] 真实实现（应全绿）")
    r = IntentRouter.__new__(IntentRouter)      # 裸对象：两方法不使用实例属性，无副作用
    with PatchedDB(path):
        base = run_cases(r, path)
    for cid, desc in CASES.items():
        check(f"{cid} {desc}", base.get(cid, False), f"res={base.get(cid)}")
    base_fail = {k for k, v in base.items() if not v}

    print("\n[2] 变异自证（必须**新增 FAIL**；§6.7a：只认新增失败）")
    for tag, repl, must in MUTATIONS:
        r2 = IntentRouter.__new__(IntentRouter)
        r2._cache_set = types.MethodType(mutated_fn("_cache_set", repl), r2)
        # 每次用干净夹具，避免上一轮残留影响
        p2, _ = make_fixture()
        with PatchedDB(p2):
            got = run_cases(r2, p2)
        new_fail = {k for k, v in got.items() if not v} - base_fail
        check(f"{tag} → 新增失败 {sorted(new_fail)}", bool(new_fail & must),
              f"期望触及 {sorted(must)}，实际新增 {sorted(new_fail)}")
        # 作用面限定：变异不该把全小写对照 C2 也打挂（否则说明断言集无分辨力）
        check(f"{tag}：对照项 C2 仍 PASS（作用面限定在含大写路径）", got.get("C2") is True,
              f"C2={got.get('C2')}")
        try:
            os.unlink(p2)
        except Exception:
            pass

    print("\n[3] 文件卫生")
    h_after = hashlib.sha256(open("agent/intent.py", "rb").read()).hexdigest()
    check("真实文件未被变异污染（sha256 前后一致）", h_before == h_after,
          f"{h_before[:12]} vs {h_after[:12]}")

    print("\n[4] 引用可解析性（§6.8：防改名后引用静默失效）")
    known = set(CASES)
    for tag, _repl, must in MUTATIONS:
        bad = must - known
        check(f"{tag.split(' ')[0]} 引用的断言 id 全部存在", not bad, f"不存在的 id：{sorted(bad)}")

    try:
        os.unlink(path)
    except Exception:
        pass

    print("\n" + "=" * 78)
    print(f"结果：{len(PASS)} pass / {len(FAIL)} fail")
    for f in FAIL:
        print("  - " + f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
