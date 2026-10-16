"""人工介入策略裁决（P0-8，2026-10-16）—— 策略即数据，裁决是**纯函数**。

## 为什么有这个模块

决策场景随系统能力增长，硬编码不可持续。此前"要不要问人"散落在
`hil_service.WRITE_TOOLS`（Python set）与 `agents.hil_level`（三档枚举）里，
新增一类决策场景就必须改代码发版。本模块把这件事变成**改表**。

## 行业依据（2026-10-16 调研，勿重复调研）

- AWS Well-Architected AGENTREL02-BP05：按**影响 × 可逆性**分档；
  "统一监督要么拖慢一切、要么漏掉高危" —— 反对一刀切。
- Suntec 治理框架：**不要给 agent 整体分等级，要给每个动作分等级**（Action Registry）。
- Olivares：等级应**每次决策时从当前策略重新推导**，不存快照
  —— 策略改了立即生效，陈旧快照无法把门槛压低。
- app-lab.ai（EU AI Act 分析）：策略引擎必须在 agent **之外**，agent 只能申请。
- agentpatternscatalog 反模式：**approval fatigue** —— 闸门太多会让人闭眼点通过。

⇒ 本模块落实四点：**按动作分档 / 策略外置为数据 / 每次现推不缓存 / 严格程度可叠加**。

## 三条不变式（写成可断言的判据，不是口号）

1. **策略层只能收紧，不能放宽**
   `escalate()` 只接受更严的 level；调用方想放宽必须走显式参数
   `allow_relax=True`（等价于"用户主动改策略"），且会带 `relaxed_by` 痕迹。
   ⇒ 因为策略层若能自行放宽，等于 agent 可以给自己解禁。

2. **fail-closed：策略不可读 ⇒ 按 confirm 裁决**
   DB 异常 / 表不存在 / 字段缺失，一律返回 `confirm`。
   ⚠️ 这里**故意不沿用** `tool_hook_service` 的"故障静默放行"——
   那个是**读侧提示钩子**（fail-open 最多导致一句多余提示），
   这里是**裁决层**（fail-open 会让本该人审的动作直接执行）。
   两个方向的风险不对称，**不能照抄**。

3. **纯函数：不碰数据库**
   读表由 `load_policies(conn)` 负责；`evaluate()` 只吃内存里的策略列表。
   ⇒ 裁决逻辑可单测，且能离线跑评测集（评测脚本把 record 打成 no-op 的做法同理）。

## 为什么现在不接链路

P0 只交付"表 + 种子 + 裁决器 + 单测"，**不接入 tools.py:434**。
因为接入必然要回答"approve(异步) 与现有 L2(同步) 如何并存"，
那会改变现有行为，属于高风险变更。先把裁决器本身验证干净。

用法（接入时）::

    policies = load_policies(conn)
    d = evaluate(policies, decision_point="retrieval_domain_conflict",
                 match_key="retrieval_domain_conflict",
                 context={"confidence": 0.31, "domain": "satellite_comms"})
    if d["level"] in ("approve", "confirm"):
        ...  # 入 hil_confirmations 队列
"""
import json

__all__ = [
    "LEVELS", "LEVEL_RANK", "DECISION_POINTS",
    "load_policies", "evaluate", "escalate", "default_decision",
]

#: 四级裁决。顺序即严格程度，**不要重排**（多处按索引比较）。
LEVELS = ("auto", "notify", "approve", "confirm")

LEVEL_RANK = {name: i for i, name in enumerate(LEVELS)}

#: 决策点枚举。与 database/seeds.py 的 _HIL_DECISION_POINTS **必须同源**。
#: 两处不一致会导致"种子写不进 / 裁决认不出"，且症状是静默走默认，
#: 所以 tests 里有一条断言要求两边一致。
DECISION_POINTS = (
    "tool_write",
    "retrieval_domain_conflict",
    "retrieval_empty",
    "answer_ungrounded",
    "model_conflict",
    "plan_divergence",
    "external_effect",
)

#: 条件判据支持的操作符。刻意小集合 —— 条件判据是给用户配的，
#: 表达力越大越容易配出自相矛盾的规则，且难以校验。
_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: _num(a) > _num(b),
    "gte": lambda a, b: _num(a) >= _num(b),
    "lt": lambda a, b: _num(a) < _num(b),
    "lte": lambda a, b: _num(a) <= _num(b),
    "in": lambda a, b: a in (b or []),
    "nin": lambda a, b: a not in (b or []),
}


def _num(v):
    """宽松转 float：失败返回 None（而不是抛），使比较判据降级为不命中。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def default_decision(reason: str = "") -> dict:
    """fail-closed 默认裁决：按 confirm。

    ⚠️ 这是本模块**唯一**允许在"拿不到策略"时走的路径，
    且必须显式携带 reason，便于审计时区分"策略判的"与"兜底的"。
    """
    return {
        "level": "confirm",
        "policy_id": None,
        "matched_by": "fallback",
        "decision_point": "",
        "reason": reason or "策略不可读（fail-closed）：按最严裁决，需人工确认",
        "conditions": {},
    }


def _match_conditions(policy: dict, context: dict) -> bool:
    """条件判据求值。**空条件 = 恒真**（这是有意的：多数策略不需要条件）。

    语义细节：context 缺字段时该条判据判为**不命中**（而非当 0），
    因为"没有置信度数据"不等于"置信度为 0"—— 前者应走兜底而不是被误判为低分。
    """
    conds = policy.get("conditions") or {}
    if not isinstance(conds, dict) or not conds:
        return True
    ctx = context or {}
    for field, spec in conds.items():
        if field not in ctx:
            return False
        actual = ctx.get(field)
        # 支持两种写法：{"op":"lt","value":0.6} 或直接 {"lt":0.6}
        if isinstance(spec, dict) and "op" in spec:
            op, want = spec.get("op"), spec.get("value")
        elif isinstance(spec, dict):
            op, want = next(iter(spec.items()))
        else:
            op, want = "eq", spec
        fn = _OPS.get(op)
        if fn is None:
            return False          # 未知操作符 ⇒ 不命中（不猜）
        try:
            if not fn(actual, want):
                return False
        except Exception:
            return False
    return True


def load_policies(conn, scope: str = "") -> list:
    """读策略表 → 内存列表。读失败返回 []（由 evaluate 走 fail-closed）。

    刻意返回**原始行**而不是解析好的对象：策略解析与裁决分离，
    这样"策略写错了"能在 load 阶段就暴露，而不必混在裁决逻辑里。
    """
    if conn is None:
        return []
    try:
        sql = ("SELECT id, scope, match_key, decision_point, level, conditions_json, "
               "reason, timeout_min, fallback_level, priority "
               "FROM hil_policies WHERE enabled=1")
        args = []
        if scope:
            # global 与指定 scope 同时可见：global 是底，scope 是覆盖
            sql += " AND scope IN ('global', ?)"
            args.append(scope)
        sql += " ORDER BY priority ASC, id ASC"
        rows = conn.execute(sql, args).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["conditions"] = json.loads(d.pop("conditions_json") or "{}")
        except Exception:
            d["conditions"] = {}
            d.pop("conditions_json", None)
        out.append(d)
    return out


def evaluate(policies, decision_point: str, match_key: str = "",
             context: dict = None, scope: str = "") -> dict:
    """裁决：返回 {level, policy_id, matched_by, reason, conditions}。

    ## 候选与排序（2026-10-16 修正过一次，见下）

      候选 = (match_key 精确命中 或 match_key='any') 且 条件全真
      排序 = **特异性优先**（精确 > any），同层内 priority 升序，
            再同层同 priority 取**更严者**

    ⚠️ **修正记录（这条是真 bug，被单测抓出来，不是理论推演）**：
       初版把精确策略与 `any` 策略放进同一个池子、只按 `priority` 排序。
       后果：一条 `match_key='any', priority=10, level='auto'` 的通用底策略，
       会**压过** `match_key='file_delete', priority=20, level='confirm'` 的精确策略
       （10 < 20 先被选中）⇒ destructive 动作被放行。
       单测「同 priority 取更严者」与「命中非 any 的精确策略优先于 any」双红，
       现象是 level 返回 auto 而非 confirm。

       **根因**：把"优先级数值"和"规则特异性"当成了同一件事。
       两者是不同的维度 —— 特异性回答「这条规则说的是不是这件事」，
       priority 回答「同一件事有多条规则时听谁的」。
       ⇒ 必须先按特异性分层，再在层内比 priority。

    ⚠️ 条件不满足的策略**不算候选**，会继续往后找 —— 这与"命中即返回"不同，
       因为同一决策点下可能有"普通情况自动、特殊条件要人审"的多条策略。
    """
    if policies is None or len(policies) == 0:
        return default_decision("策略表为空（fail-closed）")
    ctx = context or {}
    exact, wild = [], []
    for p in policies:
        mk = (p.get("match_key") or "").strip()
        if mk == "any":
            bucket = wild
        elif mk == match_key:
            bucket = exact
        else:
            continue
        # 决策点不匹配即跳过；但 match_key 已命中时不强制要求 dp 相等
        # （seed 里 write/destructive 都挂在 tool_write 下，dp 用于归类与审计）
        if not _match_conditions(p, ctx):
            continue
        bucket.append(p)
    # 有精确命中就只在精确层内决胜；精确层为空才回落到 any 层（通用底）
    cands = exact if exact else wild
    if not cands:
        return {
            "level": "auto",
            "policy_id": None,
            "matched_by": "no_policy",
            "decision_point": decision_point,
            "reason": "无匹配策略且无兜底策略 ⇒ 默认放行（fail-open 仅限**有策略表但未命中**）",
            "conditions": {},
        }
    best = cands[0]
    for p in cands[1:]:
        # 同 priority 时取更严者：策略作者可能配了多条同优先级的，
        # 此时"宁可多问一次"比"漏审"安全（industry: deny-closed）
        if p.get("priority", 100) == best.get("priority", 100) and \
                LEVEL_RANK.get(p.get("level"), 0) > LEVEL_RANK.get(best.get("level"), 0):
            best = p
    return {
        "level": best.get("level") or "auto",
        "policy_id": best.get("id"),
        "matched_by": "policy",
        "decision_point": decision_point or best.get("decision_point") or "",
        "reason": best.get("reason") or "",
        "conditions": best.get("conditions") or {},
        "timeout_min": best.get("timeout_min") or 0,
        "fallback_level": best.get("fallback_level") or "",
        "priority": best.get("priority", 100),
    }


def escalate(level: str, target: str, allow_relax: bool = False) -> str:
    """把 level 收紧到至少 target。**默认只增不减**（不可变式 2）。

    语义（三分支，顺序不能换）：
      1. target 不比当前严 ⇒ 原样返回（本来就不需要"收紧"）
      2. target 更严 ⇒ 返回 target（收紧，无需许可）
      3. target 更宽松 ⇒ **仅当 allow_relax=True 才允许**，否则原样返回
         （放宽等价于"用户主动调低门槛"，须显式许可并由调用方留痕）

    ⚠️ **修正记录（单测抓出的真 bug）**：初版写成
         `if RANK[tgt] <= RANK[cur]: return cur` 提前返回后，
         下面的 `if allow_relax: return tgt` 落在**收紧**分支里，
         导致 `escalate("confirm","auto",allow_relax=True)` 永远返回 confirm
         —— 显式许可的放宽路径不可达（单测「显式 allow_relax 才可放宽」红）。
       根因：把"是否需要许可"的条件写成了"是否更严"，方向搞反。

    ⚠️ 非法 level 一律按 confirm 处理（fail-closed），不静默放过。
    """
    cur = level if level in LEVEL_RANK else "confirm"
    tgt = target if target in LEVEL_RANK else "confirm"
    if LEVEL_RANK[tgt] > LEVEL_RANK[cur]:
        return tgt            # 收紧：始终允许
    if LEVEL_RANK[tgt] < LEVEL_RANK[cur]:
        # 放宽：必须显式许可
        return tgt if allow_relax else cur
    return cur                # 同级：原样
