"""store 包基础层：模块级常量、权限判定、状态机校验与公共小工具。

本模块被 store 包内其余**全部**模块依赖；**不得反向导入任何兄弟模块**
（既避免循环 import，也保证 `PLUGIN_DIR` 等常量单点定义）。

原 `plugin_system/store.py` 顶层导入中，本文件只保留自用者；其余按需下放到各兄弟模块。
"""
import os
from datetime import datetime

from core.config import BASE_DIR


PLUGIN_DIR = os.path.join(BASE_DIR, "data", "plugins")

TRANSITIONS = {
    # 2026-09-16（P1-7 解耦）：draft/rejected 可直接到 published = 「发布（自用）」，**免审核**。
    # 审核只保留给「上架公共市场」（走 scope，见下方 SHARE_TRANSITIONS）。
    #
    # 2026-09-17（P2-4 校准）：本表是 status 维度的**合法迁移表**（can_transition 据此校验），
    # 而非「调用方清单」。下列边无 transition() 调用方，已逐条标注 —— 评估可达性时勿当实操路径。
    "draft": {"published", "submitted", "rejected", "removed"},
    #          ^published=publish_self  ^submitted/^rejected/^removed=未使用（见下方说明）
    "submitted": {"published", "rejected", "draft", "removed"},
    #             ^published/^rejected=遗留数据审核（/review 兼容分支）
    #             ^draft=unpublish_self「撤回提交」（2026-09-17 P1-1 补：关闭准死锁）
    #             ^removed=未使用
    "rejected": {"draft", "published", "submitted", "removed"},
    #            ^published=publish_self  ^draft/^submitted/^removed=未使用
    "published": {"disabled", "draft", "removed"},
    #             ^disabled=set_enabled 直写（不经 transition）  ^draft=unpublish_self  ^removed=未使用
    "disabled": {"published", "removed"},
    #            ^published=publish_self（自用）/ set_enabled 直写  ^removed=未使用
    "removed": set(),
    # ──────────────────────────────────────────────────────────────────
    # 「未使用」边说明（2026-09-17 双轨合并 + P2-4 校准）：
    #   · * → removed   软删除由 soft_delete() 直接 UPDATE（含清理 installs/scope），
    #                   不经 transition()，故这些边不会被 can_transition 校验。
    #                   removed 仍是事实终态（get_plugin() 默认排除），只是不可经 transition 到达。
    #   · draft|rejected → submitted
    #                   旧「发布需审核」链的入口。双轨合并后新代码不再产生 submitted
    #                   （上架审核统一走 scope=pending_public），保留仅为旧数据兼容。
    #   · draft ↔ rejected
    #                   无任何编辑/审核路径使用；保留以免 rendered 可达图缺失节点，
    #                   但前端不提供该迁移入口。
    # 保留上述边而非删除的原因：可达性分析依赖表结构完整（删 disabled 相关边会使
    # disabled 从 draft 不可达，得出「存在不可达状态」的错误结论）。
    # ──────────────────────────────────────────────────────────────────
}

STATUS_LABELS = {
    "draft": "草稿", "submitted": "待审核", "rejected": "已驳回",
    "published": "已发布", "disabled": "已停用", "removed": "已删除",
}

SCOPES = ("personal", "pending_public", "public")

SCOPE_LABELS = {"personal": "仅自己", "pending_public": "申请上架中", "public": "已上架市场"}

SHARE_TRANSITIONS = {
    "personal": {"pending_public"},            # 申请上架（需审核）
    "pending_public": {"public", "personal"},  # 审核通过 / 驳回
    "public": {"personal"},                    # 撤回上架（免审核）
}

EDITABLE_STATUSES = ("draft", "rejected", "published", "disabled")

_SELF_PUBLISHABLE_FROM = ("draft", "rejected", "disabled")


BUILTIN_AUTHOR_NAMES = ("平台内置",)
BUILTIN_ID_PREFIXES = ("com.zhiyuan.legacy.",)

# 2026-09-29（用户第二轮指示）：「目前的插件先不标记内置，因此允许进行操作（删除/卸载）」。
# 本阶段插件市场全部条目（含 legacy 迁移的 55 条）一律**不**视为内置：
#   · is_builtin_row() 短路返回 False —— 前端 is_builtin 徽章、卸载守卫、删除守卫全部放行；
#   · 动作语义改为「来源决定按钮」：市场安装的 → 只卸载；自己创建的 → 只删除（见 36-capability.js）。
# 未来要恢复"内置不可删卸"时：把 BUILTIN_MARKING_ENABLED 改回 True 即可（判据本体保留，勿删）。
BUILTIN_MARKING_ENABLED = False


def is_builtin_row(row):
    """平台内置能力的**唯一判据**（2026-09-29 收敛，供 store/router/前端 DTO 共用）。

    ⚠️ 2026-09-29 二轮：按用户指示，**当前阶段整体停用内置标记**
    （BUILTIN_MARKING_ENABLED=False，is_builtin_row 恒 False）——
    此前一轮把 55 条 legacy 迁移条目标为内置，导致插件市场里"无法删除（内置的除外）"
    反向命中这批条目、删除/卸载全禁，与用户预期相反。恢复开关即可回到内置口径。

    背景：此前两处口径不一致，导致「能力中心里 19 条个人能力点不了删除」：
      - plugin_system/store/queries.py:75 用 `author_id == 0` → 实测命中 74/77 条；
      - routers/studio_parts/shared.py:154 用 `author_name == '平台内置'` → 命中 55 条。
    真库实测（只读）：
      author_name 分布 = 平台内置 55 / 历史迁移 18 / 王工 3 / 本地创建 1；
      plugin_id 前缀分布 = com.zhiyuan.legacy 55 / com.mbse.personal 18 / 其余 4；
      「平台内置」与「com.zhiyuan.legacy. 前缀」两判据**完全等价**（都是同样 55 条），
      且与旧表 builtin 标记严格对应（tools 28 / agents 9 / skills 4 / mcp 1
      + 由它们投影出的 prompt 8 条 ≈ 55）。

    因此内置口径收敛为「作者名为平台内置 **或** plugin_id 落在平台 legacy 命名空间」，
    两条取或以防单字段被改写。`author_id == 0` 不再作为内置判据 ——
    历史迁移与本地创建的个人能力同样落在 author_id=0，属实是"有主但无 id"，
    用户明确要求"目前的插件先不标记内置"。

    幂等/纯函数：不触碰数据库，可安全用于 DTO 计算与测试断言。
    """
    if not BUILTIN_MARKING_ENABLED:
        return False
    if not row:
        return False
    try:
        author_name = row.get("author_name") if hasattr(row, "get") else row["author_name"]
        plugin_id = row.get("plugin_id") if hasattr(row, "get") else row["plugin_id"]
    except Exception:
        return False
    if (author_name or "") in BUILTIN_AUTHOR_NAMES:
        return True
    return any((plugin_id or "").startswith(p) for p in BUILTIN_ID_PREFIXES)


def is_admin(user):
    """管理员判定（统一口径，供 store 与 router 共用）。

    口径与 routers.plugins._as_admin 一致：admin 域权限非空，或 ai_studio 含 publish。
    2026-09-16 修复：此前 set_enabled / soft_delete 各自用 bool(permissions["admin"])，
    口径更严 —— 设计师（ai_studio 含 publish、admin 为空）点「停用」会被 403，
    与其在编辑/删除上被认可的管理员身份自相矛盾。

    ⚠️ 2026-09-17（P1 权限拆分）起：is_admin 保留给「系统级/内容级」操作
    （编辑/删除他人个人能力、个人插件启停等）；**市场治理动作**
    （上架审核、公共能力全局启停、撤回上架、已上架能力改删、grant）
    一律改用 is_market_admin，创作者（publish）不再自动享有市场治理权。
    """
    if not user:
        return False
    perms = user.get("permissions") or {}
    if perms.get("admin"):
        return True
    return "publish" in (perms.get("ai_studio") or [])


def is_market_admin(user):
    """市场治理管理员判定（2026-09-17 P1，权限点拆分）。

    口径：admin 域权限非空（超级用户放行，与 require_permission 同理），
    或 ai_studio 含 **market_admin** 权限点。
    ai_studio:publish（能力创作者）被刻意排除 —— 对齐「市场数据由专门
    管理人员管理」的治理目标：创作者可创作、发布自用、申请上架，
    但不能审核上架、不能全局启停/下架/删除已上架能力、不能分配授权。

    迁移提示：需为承担市场治理的角色在权限矩阵中配置 ai_studio:market_admin；
    未配置前，只有 admin 域用户能执行市场治理动作。
    """
    if not user:
        return False
    perms = user.get("permissions") or {}
    if perms.get("admin"):
        return True
    return "market_admin" in (perms.get("ai_studio") or [])


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_plugin_dir(plugin_id):
    d = os.path.join(PLUGIN_DIR, plugin_id)
    os.makedirs(d, exist_ok=True)
    return d


def can_transition(status, target):
    return target in TRANSITIONS.get(status, set())


def _is_owner(p, user):
    """作者或管理员（与 routers.plugins._author_or_admin 同口径）。"""
    if not user or not user.get("id"):
        return False
    if p and p.get("author_id") and int(p["author_id"]) == int(user["id"]):
        return True
    return is_admin(user)
