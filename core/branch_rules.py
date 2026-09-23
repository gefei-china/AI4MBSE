"""分支保护规则（P0-2 配置化）：集中解析 + 三项守卫（writable / renamable / deletable）。

依据《版本管理优化实施方案与实施计划-20260923》§4.2（决策 D5：按类型给默认 + 分支级覆盖）。

规则 schema（branches.protection_rules，JSON）::

    {
      "writable": true,          # 是否允许直写实体/关系/文档
      "deletable": false,        # 是否允许删除分支
      "renamable": false,        # 是否允许改名
      "required_reviews": 0,     # 合并前必需评审数（0=不要求）
      "allow_direct_push": true  # 是否允许绕过 MR 直接提交
    }

解析优先级（后覆盖前）::

    基础默认 _RULE_BASE → 类型默认 _TYPE_RULES → PROTECTED 名称兜底 → 分支级 protection_rules

设计约束：
- **零回归**：内置 `release` / `dev` / `personal` 仍不可删、不可改名；`release` 仍不可直写。
- **fail-safe**：分支级 JSON 损坏时忽略覆盖、保持兜底 —— 解析失败绝不放开保护。
- **无外部依赖**：本模块只 import 标准库，故可同时被 `routers.branches` 与
  `routers.knowledge_parts.*` 复用而不产生循环导入。

消费点（截至 2026-09-23）：
- `routers/branches.py` —— 同步(writable) / 改名(renamable) / 删除(deletable) / 规则配置端点
- `routers/knowledge_parts/shared.py::_release_guard` —— 实体·关系·文档写入门(writable)，10 处调用共用
"""
import json

RELEASE_BRANCH = "release"
DEV_BRANCH = "dev"
PERSONAL_BRANCH = "personal"

# 受保护分支名（内置三类）：不可删除、不可改名。分支级规则可显式覆盖（管理端可解锁）。
PROTECTED_NAMES = frozenset({RELEASE_BRANCH, DEV_BRANCH, PERSONAL_BRANCH})

# 规则键全集（白名单：写入端点只接受这些键）
RULE_KEYS = ("writable", "deletable", "renamable", "required_reviews", "allow_direct_push")

# 基础默认：自定义分支默认可写可删可改名、合并不强制评审
RULE_BASE = {
    "writable": True,
    "deletable": True,
    "renamable": True,
    "required_reviews": 0,
    "allow_direct_push": True,
}

# 类型默认：release 只读发布（需评审、禁直推）、dev 可写但属主干不可删改、
# personal/local 为工作分支可写可删（名称恰为 personal 的内置分支由名称兜底收紧）
TYPE_RULES = {
    RELEASE_BRANCH: {
        "writable": False, "deletable": False, "renamable": False,
        "required_reviews": 1, "allow_direct_push": False,
    },
    DEV_BRANCH: {"writable": True, "deletable": False, "renamable": False},
    "personal": {"writable": True, "deletable": True, "renamable": True},
    "local": {"writable": True, "deletable": True, "renamable": True},
}

# 内置分支的显式默认规则（迁移回填用）：让生效规则在库/接口中可见可编辑，而非隐含
BUILTIN_DEFAULTS = {
    RELEASE_BRANCH: {
        "writable": False, "deletable": False, "renamable": False,
        "required_reviews": 1, "allow_direct_push": False,
    },
    DEV_BRANCH: {"writable": True, "deletable": False, "renamable": False},
    # 名称恰为 personal 的内置工作分支：可写但不允许删除/改名（防误删工作分支）
    PERSONAL_BRANCH: {"writable": True, "deletable": False, "renamable": False},
}


def is_release_family(name: str | None, branch_type: str = "") -> bool:
    """是否属「已发布(release)」家族：类型为 release，或分支名等于/前缀 release。"""
    nm = (name or "").strip()
    if branch_type == RELEASE_BRANCH:
        return True
    return nm == RELEASE_BRANCH or nm.startswith(RELEASE_BRANCH + "/")


def guess_type(name: str | None) -> str:
    """未登记分支的类型推断：release 家族按 release 处理，其余留空（走基础默认，保持既有行为）。"""
    return RELEASE_BRANCH if is_release_family(name) else ""


def parse_rules(raw) -> dict:
    """解析分支级规则 JSON。损坏/非 dict → 返回 {}（调用方保持兜底，不放开保护）。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def effective_rules(branch, branch_type: str | None = None) -> dict:
    """解析分支生效保护规则。

    branch 可传分支行 dict（含 name/branch_type/protection_rules）或分支名 str；
    传 str 时可用 branch_type 补充类型信息。

    类型判定顺序：行内 branch_type（已登记分支为准）→ 入参 branch_type →
    **按名称推断**（`release` 与 `release/*` 视为 release 类型）。
    ⚠️ 名称推断这一环必须有：未登记的 `release/xxx` 分支在旧实现里靠名称前缀被判只读，
    去掉它会丢掉既有保护（实测踩过）。
    """
    if isinstance(branch, dict):
        name = (branch.get("name") or "").strip()
        btype = (branch.get("branch_type") or branch_type or "").strip() or guess_type(name)
        raw = branch.get("protection_rules")
    else:
        name = (branch or "").strip()
        btype = (branch_type or guess_type(name) or "").strip()
        raw = None

    rules = dict(RULE_BASE)
    rules.update(TYPE_RULES.get(btype, {}))
    if name in PROTECTED_NAMES:          # 名称兜底：内置分支不可删/不可改名（零回归）
        rules["deletable"] = False
        rules["renamable"] = False
    override = parse_rules(raw)
    for k in RULE_KEYS:
        if k in override:
            rules[k] = override[k]
    return rules


def load_branch(conn, name: str | None):
    """按名读分支行（dict）；不存在或查询异常 → None。供各守卫共用，避免各自写 SQL。"""
    nm = (name or "").strip()
    if not nm:
        return None
    try:
        row = conn.execute(
            "SELECT name, branch_type, protection_rules FROM branches WHERE name=?", (nm,)
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    try:
        return dict(row)
    except (TypeError, ValueError):      # 连接未设 row_factory：sqlite3.Row 才可 dict()
        return {"name": nm, "branch_type": "", "protection_rules": ""}


def rules_for(conn, name: str | None) -> dict:
    """取某分支生效规则（未登记分支按名称推断类型，走基础默认）。"""
    row = load_branch(conn, name)
    if row is not None:
        return effective_rules(row)
    return effective_rules({"name": name, "branch_type": guess_type(name)})


def check_writable(conn, branch: str | None) -> str | None:
    """直写守卫：返回错误文案则拒绝写实体/关系/文档；None 表示放行。

    原 `shared._release_guard` 的规则化实现（10 处写入门共用）。
    """
    nm = (branch or "").strip()
    if not nm:
        return None                      # 与原实现一致：未指定分支不拦
    row = load_branch(conn, nm)
    if row is None:
        row = {"name": nm, "branch_type": "", "protection_rules": ""}
    rules = effective_rules(row)
    if rules.get("writable", True):
        return None
    if is_release_family(nm, row.get("branch_type") or ""):
        return "已发布(release)分支为只读发布分支，不可直接编辑/写入；请切换到 dev 编辑后通过合并更新"
    return f"{nm} 分支受保护（只读），不可直接编辑/写入；请切换到可写分支编辑后通过合并更新"


def check_deletable(conn, name: str | None) -> str | None:
    """删除守卫：返回错误文案则拒绝删除；None 表示放行。"""
    row = load_branch(conn, name) or {"name": name, "branch_type": "", "protection_rules": ""}
    if effective_rules(row).get("deletable", True):
        return None
    return f"{name} 是受保护分支，不可删除"


def check_renamable(conn, name: str | None) -> str | None:
    """改名守卫：返回错误文案则拒绝改名；None 表示放行。"""
    row = load_branch(conn, name) or {"name": name, "branch_type": "", "protection_rules": ""}
    if effective_rules(row).get("renamable", True):
        return None
    return f"{name} 是受保护分支，不可改名（release 仅通过 dev 合并更新）"
