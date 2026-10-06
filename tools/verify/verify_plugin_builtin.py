"""验证：能力中心「内置」判据收敛 —— 真库只读 A/B + 变异测试。

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# ── 已接进 CI（2026-10-05 第二轮第 2 项收尾）──────────
# 修复要点：根因是**门禁口径**：2026-09-29「按用户指示整体停用内置标记」
#   （BUILTIN_MARKING_ENABLED=False，base.py:78-88 明写"恢复开关即可回到内置口径"），
#   而门禁写死了停用前的 55/74/19 三个历史数值 ⇒ 改为断言当前口径的机制不变式，
#   基准改用当前实得值 n_new。
#   ⚠️ 顺带修一个**假覆盖**：内置数为 0 时 `all(not can_delete)` 是空集恒真，看着绿
#   什么都没验 ⇒ 改 SKIP + 补「开关打开 ⇒ 判据面必须回来」的自证。
#   ⚠️ 再修一个隐蔽崩溃：还原自检段 `next(... == "<写死历史 id>")` 抛 StopIteration。
# 双环境实测：生产库真跑 + 全新干净库（无样本处干净 SKIP）均 rc=0。
# 注：本门禁原硬编码 `ROOT/"mbse.db"`（本项目第 7/8 处）⇒ 已改读 MBSE_DB_PATH，
#   否则 CI 干净库上根本没有该文件，会直接崩。


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

# ⚠️ 2026-10-05：原硬编码 `ROOT/"mbse.db"`（本项目第 8 处）⇒ 设 MBSE_DB_PATH 也无效，
#   CI 干净库上根本没有该文件 ⇒ 直接崩。改读配置（与 verify_skill_injection 同口径）。
DB = os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")
FAILS = []
CHECKS = []
SKIPPED = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    if not cond:
        FAILS.append(f"{name} :: {detail}")
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def skip(name, why=""):
    """判据面不存在（本库无样本）→ SKIP，不判 FAIL。

    ⚠️ 关键：**空集上的 `all(...)` 是恒真的假覆盖**（看着绿，什么都没验）。
    内置标记停用时 blt 为空，A4/A5 的 `all(...)` 会全绿——必须显式 SKIP，
    并另加「开关打开 ⇒ 判据面必须回来」的自证。
    """
    SKIPPED.append(name)
    print(f"  [SKIP] {name}" + (f"  <- {why}" if why else ""))


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

# ── A0/A1 内置标记口径（2026-09-29 起按用户指示**整体停用**）────────
# ⚠️ 2026-10-05 重新定性：原 A1/A2/A3 写死了 55 / 74 / 19 三个**停用前的历史数值**，
#   而产品已按用户指示把 `BUILTIN_MARKING_ENABLED` 置 False ⇒ `is_builtin_row` **恒 False**
#   ⇒ 内置数 0、差集 = 旧判据全量 ⇒ 三条必然红。**这不是产品退化，是门禁口径过期。**
#   证据：plugin_system/store/base.py:78-88 明写"用户明确要求目前的插件先不标记内置"
#   且"未来要恢复时把开关改回 True 即可（判据本体保留，勿删）"。
#   ⇒ 改为断言**当前口径下的机制不变式**，并把"开关打开时判据必须翻转"做成自证。
from plugin_system.store import base as _base                  # noqa: E402

MARK = bool(getattr(_base, "BUILTIN_MARKING_ENABLED", False))

print("A0/A1 内置标记口径（MARKING=%s）" % MARK)
n_new = sum(1 for r in rows if is_builtin_row(r))
check("A1 内置数与开关状态一致（关闭⇒0 条；打开⇒>0 条）",
      (n_new == 0) if not MARK else (n_new > 0), f"MARK={MARK} actual={n_new}")

# 交叉校验：内置条目不得落在个人命名空间（MARK 打开时才有内容可查）
bad_ns = [r["plugin_id"] for r in rows
          if is_builtin_row(r) and r["plugin_id"].startswith("com.mbse.")]
check("内置条目无一落在 com.mbse.* 个人命名空间", not bad_ns, f"违例={bad_ns[:3]}")

# ── A2 与旧判据的对比（证明两口径确实不同，且当前完全分离）──────────
print("\nA2 口径对比（新判据 vs 旧 author_id==0）")
n_old = sum(1 for r in rows if (r["author_id"] or 0) == 0)
# ⚠️ 2026-10-05：`n_old == 0`（干净库/CI 没有任何无主条目）⇒ A2/A3/A5b 的判据面不存在。
#   注意其中 A3a/A3b 原本是 `all(...)` / `==` 形式，在空集上**恒真** —— 假覆盖，
#   必须显式 SKIP 而不是让它"PASS"。
_HAS_SAMPLE = (n_old > 0)
if _HAS_SAMPLE:
    check("A2 旧判据（author_id==0）在本库仍有命中（两口径确实不同）", True, f"actual={n_old}")
else:
    skip("A2 旧判据（author_id==0）在本库仍有命中（两口径确实不同）",
         "本库无 author_id==0 的条目 ⇒ 无从比较两口径（判据面不存在）")
check("A2b 当前口径（标记停用）下新判据命中 0 条，与旧判据完全分离", n_new == 0,
      f"new={n_new} old={n_old}")

# ── A3 个人能力可删（**这条才是本门禁真正要保护的不变式**）──────────
print("\nA3 无主条目在当前口径下可删（内置标记停用 ⇒ 删除不再被拦截）")
flipped = []
for r in rows:
    if (r["author_id"] or 0) == 0 and not is_builtin_row(r):
        dto = plugin_dto(dict(r), ADMIN)
        flipped.append((r["plugin_id"], r["author_name"], dto["is_builtin"], dto["can_delete"]))
if _HAS_SAMPLE:
    check("A3a 差集条目数 == 旧判据命中数（停用后全部落入差集）", len(flipped) == n_old,
          f"差集={len(flipped)} 旧判据={n_old}")
    check("A3b 差集条目 is_builtin 全为 False",
          all(not f[2] for f in flipped),
          f"仍为True={[f[0] for f in flipped if f[2]][:3]}")
    can_del = [f for f in flipped if f[3]]
    check("A3c **全部**差集条目 can_delete=True（用户可见的修复已兑现到每一条）",
          len(can_del) == len(flipped),
          f"可删={len(can_del)}/{len(flipped)}")
    # 取差集首条（不写死历史 id —— 库数据会变，写死会抛 StopIteration 并崩掉整个门禁）
    sample = flipped[0]
    check("A3d 存在可复现样本且 can_delete=True（取差集首条，不写死历史 id）",
          sample[3] is True, f"sample={sample[:3]} can_delete={sample[3]}")
else:
    for _t in ("A3a 差集条目数 == 旧判据命中数（停用后全部落入差集）",
               "A3b 差集条目 is_builtin 全为 False",
               "A3c **全部**差集条目 can_delete=True（用户可见的修复已兑现到每一条）",
               "A3d 存在可复现样本且 can_delete=True（取差集首条，不写死历史 id）"):
        skip(_t, "本库无 author_id==0 的条目（差集为空）⇒ 判据面不存在；"
                 "原实现在此是**空集恒真的假覆盖**，已改为显式 SKIP")

# ── A4 真内置仍不可删 ──────────────────────────────────────────
print("\nA4 真内置条目仍不可删（防顺带放开）")
blt = [r for r in rows if is_builtin_row(r)]
dto_blt = [plugin_dto(dict(r), ADMIN) for r in blt]
# ⚠️ 内置数为 0 时，"全部 can_delete==False" / "全部 is_builtin==True" 是**空集恒真** ——
#   看着绿，实际什么都没验（假覆盖，与「变异 VACUOUS」同族）。
#   ⇒ 判据面不存在时记 SKIP，并另加「开关打开 ⇒ 判据面必须回来」的自证（见 A5b）。
if not blt:
    skip("全部内置条目 can_delete == False", "内置标记开关关闭 ⇒ 判据面为空集（0 条内置）")
    skip("全部内置条目 is_builtin == True", "内置标记开关关闭 ⇒ 判据面为空集（0 条内置）")
else:
    check("全部内置条目 can_delete == False",
          all(not d["can_delete"] for d in dto_blt),
          f"漏={sum(1 for d in dto_blt if d['can_delete'])}")
    check("全部内置条目 is_builtin == True",
          all(d["is_builtin"] for d in dto_blt))

# ── A5 can_delete 不随 scope 漂移 ──────────────────────────────
print("\nA5 内置判定与 scope 解耦")
scopes = sorted({r["scope"] for r in blt})
if not blt:
    skip("内置条目覆盖 multi-scope（样本多样性）", "内置标记开关关闭 ⇒ 判据面为空集（0 条内置）")
    skip("超管视角下内置条目 can_delete 恒 False", "内置标记开关关闭 ⇒ 判据面为空集（0 条内置）")
else:
    check("内置条目覆盖 multi-scope（样本多样性）", len(scopes) >= 1, f"scopes={scopes}")
    check("超管视角下内置条目 can_delete 恒 False",
          all(not plugin_dto(dict(r), ADMIN)["can_delete"] for r in blt))

# A5b 开关自证：把 BUILTIN_MARKING_ENABLED 临时打开，内置判据面**必须回来**
#   （证明上面几条 SKIP 是"判据面不存在"而非"判据被删了"）。
_saved_mark = _base.BUILTIN_MARKING_ENABLED
try:
    _base.BUILTIN_MARKING_ENABLED = True
    _blt_on = [r for r in rows if is_builtin_row(r)]
    _on_ns_bad = [r["plugin_id"] for r in _blt_on if r["plugin_id"].startswith("com.mbse.")]
    _on_all_nodel = _blt_on and all(not plugin_dto(dict(r), ADMIN)["can_delete"] for r in _blt_on)
    if _blt_on:
        check("A5b 开关打开 ⇒ 内置判据面回来且口径正确（证明 SKIP 是环境所致，非断言被废）",
              not _on_ns_bad and _on_all_nodel,
              f"打开后内置数={len(_blt_on)} 命名空间违例={_on_ns_bad[:3]} 全部不可删={_on_all_nodel}")
    else:
        skip("A5b 开关打开 ⇒ 内置判据面回来且口径正确（证明 SKIP 是环境所致，非断言被废）",
             "干净库既无无主条目、也无 author_name='平台内置'/legacy 前缀的条目 ⇒ "
             "即便打开开关也识别不到内置（判据面不存在）")
finally:
    _base.BUILTIN_MARKING_ENABLED = _saved_mark
check("A5c 开关已还原（门禁不改全局状态）", _base.BUILTIN_MARKING_ENABLED is _saved_mark)

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
    # 干净库/CI 上没有无主个人能力 ⇒ 判据面不存在，SKIP 而非 FAIL
    # （原实现判 FAIL，等于用不存在的数据证伪）
    skip("夹具自证：存在无主个人能力样本", "本库无 author_id==0 的条目（干净库/CI 常态）")

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
# ⚠️ 2026-10-05：干净库/CI 上**没有任何 author_id==0 的无主条目** ⇒ 变异体与正确实现
#   表现完全相同（命中数都是 0）⇒ 变异造不出差异，判据面不存在 ⇒ 整组 SKIP 而非 FAIL。
_NO_SAMPLE = (n_old == 0)
if _NO_SAMPLE:
    for _t in ("[变异] A1 翻转：内置数不再等于当前口径值",
               "[变异] A2b 翻转：新判据不再命中 0 条（变异绕过了停用开关）",
               "[变异] A3a 翻转：差集消失（被误伤条目数=0）",
               "[变异] A4 判据面回来：变异后内置条目全部 can_delete=False"):
        skip(_t, "本库无 author_id==0 的无主条目 ⇒ 变异体与正确实现无差异（判据面不存在）")
try:
    m_builtin = sum(1 for r in rows if _mutant(r))
    m_builtin_rows = [r for r in rows if _mutant(r)]
    m_flip = [r for r in rows
              if (r["author_id"] or 0) == 0 and not _mutant(r)]
    print(f"  变异后内置数 = {m_builtin}（当前口径基准 {n_new}）")
    # ⚠️ 2026-10-05：原变异段三处口径已随 A 段一并更新 ——
    #   ① 基准值不再写死 55/19，改用**当前口径实得值** n_new（否则基准本身已失效）；
    #   ② 删掉 `next(... == "com.mbse.local.agent.agent-362")` —— 该历史 id 已不在
    #      非 removed 集合里，next() 会抛 StopIteration 让整个变异段崩。
    m_blt_rows = list(m_builtin_rows)
    if _NO_SAMPLE:
        # 上面已整组 SKIP；此处不再重复判定（否则会把"无样本"误判成"变异未翻转"）
        pass
    else:
        check("[变异] A1 翻转：内置数不再等于当前口径值",
              m_builtin != n_new, f"mutant={m_builtin} 基准={n_new}（若 PASS 说明原断言空转）")
        check("[变异] A2b 翻转：新判据不再命中 0 条（变异绕过了停用开关）",
              m_builtin > 0, f"mutant={m_builtin}")
        check("[变异] A3a 翻转：差集消失（被误伤条目数=0）",
              len(m_flip) == 0, f"actual={len(m_flip)}")
        check("[变异] A4 判据面回来：变异后内置条目全部 can_delete=False",
              m_builtin > 0
              and all(not plugin_dto(dict(r), ADMIN)["can_delete"] for r in m_blt_rows),
              "可删漏数=%d" % sum(1 for r in m_blt_rows
                                  if plugin_dto(dict(r), ADMIN)["can_delete"]))
finally:
    _base.is_builtin_row = _orig_base
    _q.is_builtin_row = _orig_base

# 还原后自检
print("\n还原后自检")
# ⚠️ 2026-10-05：原写死 `com.mbse.local.agent.agent-362`，该历史 id 已不在非 removed 集合里
#   ⇒ next() 抛 StopIteration，整个门禁**在最后一步崩掉**（前面所有 PASS 都不算数）。
#   ⇒ 改为取差集首条（不写死 id，且判据面存在性自带）。
_restore_ok = bool(flipped) and flipped[0][3] is True
if _HAS_SAMPLE:
    check("还原 is_builtin_row 后差集样本恢复可删（取差集首条，不写死历史 id）",
          _restore_ok,
          f"sample={flipped[0][0] if flipped else None} can_delete={flipped[0][3] if flipped else None}")
else:
    skip("还原 is_builtin_row 后差集样本恢复可删（取差集首条，不写死历史 id）",
         "本库无无主条目 ⇒ 差集为空，无样本可验（判据面不存在）")

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
