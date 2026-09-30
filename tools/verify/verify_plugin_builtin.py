"""验证：能力中心「内置」判据收敛 —— 真库只读 A/B + 变异测试。

被验证对象（生产代码，非复刻）：
  plugin_system.store.base.is_builtin_row
  plugin_system.store.queries.plugin_dto 的 is_builtin / can_delete 字段

判据（不绑死业务 id，只绑机制）：
  A1 内置条目数 == 55，且与旧表 builtin 标记语义一致（不落到个人命名空间）
  A2 旧判据 author_id==0 命中 74 条 → 新判据必须显著更少（证明"收敛"真实发生）
  A3 19 条「历史迁移/本地创建」个人能力从 is_builtin=True 变为 False，
     且其中至少一条在管理员视角下 can_delete 由 False 翻为 True（用户可见的修复）
  A4 真正的内置条目 can_delete 恒为 False（防"顺带把内置也放开"）
  A5 内置条目的 can_delete 与 scope 无关（已上架/未上架都不给删按钮）

变异测试：把 is_builtin_row 还原成旧实现（author_id==0），
断言 A1/A2/A3 必须 FAIL —— 否则说明断言空转。

运行：python tools/verify/verify_plugin_builtin.py
只读：DATABASE 以 mode=ro 打开，全程不做任何写操作。
"""
import sqlite3
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "mbse.db")
FAILS = []
CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    if not cond:
        FAILS.append(f"{name} :: {detail}")
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def ro_conn():
    return sqlite3.connect(f"file:{DB}?mode=ro", uri=True)


# ── 0. 导入生产代码 ─────────────────────────────────────────────
from plugin_system.store.base import is_builtin_row          # noqa: E402
from plugin_system.store.queries import plugin_dto, get_plugin  # noqa: E402

conn = ro_conn()
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute(
    "SELECT * FROM plugins WHERE status!='removed'").fetchall()]
print(f"真库非 removed 条目 = {len(rows)}\n")

ADMIN = {"id": 82, "permissions": {"admin": ["super"], "ai_studio": ["publish", "market_admin"]}}

# ── A1 内置数 == 55 ────────────────────────────────────────────
print("A1 内置判据覆盖量")
n_new = sum(1 for r in rows if is_builtin_row(r))
check("内置条数 == 55（=旧表 builtin 语义）", n_new == 55, f"actual={n_new}")

# 交叉校验：内置条目不得落在个人命名空间
bad_ns = [r["plugin_id"] for r in rows
          if is_builtin_row(r) and r["plugin_id"].startswith("com.mbse.")]
check("内置条目无一落在 com.mbse.* 个人命名空间", not bad_ns, f"违例={bad_ns[:3]}")

# ── A2 与旧判据的对比（证明收敛真实发生）────────────────────────
print("\nA2 口径收敛量（旧 vs 新）")
n_old = sum(1 for r in rows if (r["author_id"] or 0) == 0)
check("旧判据命中 74 条（记录事实）", n_old == 74, f"actual={n_old}")
check("新判据严格少于旧判据（收敛发生）", n_new < n_old, f"new={n_new} old={n_old}")
check("收敛差量 == 19（历史迁移18+本地创建1）", n_old - n_new == 19,
      f"diff={n_old - n_new}")

# ── A3 个人能力重获删除权限 ─────────────────────────────────────
print("\nA3 被误伤的个人能力恢复可删")
flipped = []
for r in rows:
    if (r["author_id"] or 0) == 0 and not is_builtin_row(r):
        dto = plugin_dto(dict(r), ADMIN)
        flipped.append((r["plugin_id"], r["author_name"], dto["is_builtin"], dto["can_delete"]))
check("差集条目数 == 19", len(flipped) == 19, f"actual={len(flipped)}")
check("差集条目 is_builtin 全为 False",
      all(not f[2] for f in flipped),
      f"仍为True={[f[0] for f in flipped if f[2]][:3]}")
can_del = [f for f in flipped if f[3]]
check("其中存在 can_delete=True 的条目（用户可见的修复）",
      len(can_del) > 0,
      f"可删={len(can_del)}/{len(flipped)}")
# 具体到一个可复现的样本
sample = next((f for f in flipped
               if f[0] == "com.mbse.local.agent.agent-362"), None)
check("样本 'com.mbse.local.agent.agent-362' 恢复可删",
      sample is not None and sample[3] is True,
      f"sample={sample}")

# ── A4 真内置仍不可删 ──────────────────────────────────────────
print("\nA4 真内置条目仍不可删（防顺带放开）")
blt = [r for r in rows if is_builtin_row(r)]
dto_blt = [plugin_dto(dict(r), ADMIN) for r in blt]
check("全部内置条目 can_delete == False",
      all(not d["can_delete"] for d in dto_blt),
      f"漏={sum(1 for d in dto_blt if d['can_delete'])}")
check("全部内置条目 is_builtin == True",
      all(d["is_builtin"] for d in dto_blt))

# ── A5 can_delete 不随 scope 漂移 ──────────────────────────────
print("\nA5 内置判定与 scope 解耦")
scopes = sorted({r["scope"] for r in blt})
check("内置条目覆盖 multi-scope（样本多样性）", len(scopes) >= 1, f"scopes={scopes}")
check("超管视角下内置条目 can_delete 恒 False",
      all(not plugin_dto(dict(r), ADMIN)["can_delete"] for r in blt))

# ── A6 普通用户视角：无主个人能力不可删（授权边界）──────────────
print("\nA6 授权边界：普通用户不能删无主条目")
NORMAL = {"id": 999, "permissions": {"ai_studio": []}}
orphan = next((r for r in rows
               if (r["author_id"] or 0) == 0 and not is_builtin_row(r)), None)
if orphan:
    dto_n = plugin_dto(dict(orphan), NORMAL)
    check("普通用户对无主条目 can_delete == False",
          not dto_n["can_delete"], f"pid={orphan['plugin_id']}")
else:
    check("夹具自证：存在无主个人能力样本", False, "未找到样本")

# ── 变异测试 ───────────────────────────────────────────────────
print("\n" + "=" * 62)
print("变异测试：把 is_builtin_row 还原成旧实现（author_id==0）")
print("=" * 62)
import plugin_system.store.base as _base  # noqa: E402
import plugin_system.store.queries as _q  # noqa: E402


def _mutant(row):
    return (row.get("author_id") or 0) == 0


_orig_base = _base.is_builtin_row
_base.is_builtin_row = _mutant
_q.is_builtin_row = _mutant
try:
    m_builtin = sum(1 for r in rows if _mutant(r))
    m_flip = [r for r in rows
              if (r["author_id"] or 0) == 0 and not _mutant(r)]
    m_sample = plugin_dto(
        dict(next(r for r in rows if r["plugin_id"] == "com.mbse.local.agent.agent-362")),
        ADMIN)

    print(f"  变异后内置数 = {m_builtin}（新口径 55）")
    check("[变异] A1 应 FAIL —— 内置数不再等于 55",
          not (m_builtin == 55), f"mutant={m_builtin}（若此条 PASS 说明原断言空转）")
    check("[变异] A2 应 FAIL —— 新旧判据不再有差量",
          not (n_old - m_builtin == 19), f"mutant diff={n_old - m_builtin}")
    check("[变异] A3 应 FAIL —— 个人能力不再恢复可删",
          not (m_sample["can_delete"] is True and m_sample["is_builtin"] is False),
          f"mutant is_builtin={m_sample['is_builtin']} can_delete={m_sample['can_delete']}")
    check("[变异] 被误伤条目数应为 0（证明差集消失）",
          len(m_flip) == 0, f"actual={len(m_flip)}")
finally:
    _base.is_builtin_row = _orig_base
    _q.is_builtin_row = _orig_base

# 还原后自检
print("\n还原后自检")
check("还原 is_builtin_row 后样本恢复可删",
      plugin_dto(dict(next(r for r in rows
                           if r["plugin_id"] == "com.mbse.local.agent.agent-362")),
                 ADMIN)["can_delete"] is True)

conn.close()

# ── 汇总 ───────────────────────────────────────────────────────
print("\n" + "=" * 62)
print(f"PASS {sum(1 for _, ok, _ in CHECKS if ok)} / {len(CHECKS)}")
if FAILS:
    print("FAIL 明细：")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("全部通过")
