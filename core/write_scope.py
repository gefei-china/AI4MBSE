# -*- coding: utf-8 -*-
"""写入范围判定（write scope）：本轮用户**到底有没有**要求「写入平台」。

## 一、为什么要有这个模块（真机 conv 999999905，2026-11-19 归因）

用户目标（msg#7013）：「生成过程中经历四次校验与修正，是什么原因，我的提示词给的不清晰嘛，
帮我优化并验证提示词效果，提升AI代码生成的准确性」—— 通篇只要**文本**。

计划里 t4「用优化后提示词在既有模型 v0.1 上做增量验证」被分派给
「智源数据读写agent」（id=1032）⇒ 它先 `todo_write`「定位工程坐标三元组」，
再 `query_projects`（MCP 502）⇒ 拿不到三元组 ⇒ 另一个子任务在汇总前
`ask_user`「你希望怎么继续？」（#12509）⇒ 整轮变成「⏸ 暂停等你确认」，
用户真正要的「优化后的提示词全文」始终没拿到（msg#7016 只有 318 字）。

## 二、为什么不「按 Agent 是不是写类」来治（实测否决过的三条路）

| 判据 | 实测结果 | 结论 |
|---|---|---|
| `tools.side_effect` 分布 | id=1032 绑了 3 个工具（capability_query/code_exec/doc_parse），**write=0**；而它的 system_prompt 明写「新增、修改、删除模型元素」 | ❌ 它的写能力走 **MCP 工具**，MCP 不在 `tools` 表 ⇒ side_effect 为 NULL ⇒ 数据驱动**永远判它只读** |
| `agents.capabilities` / `intent_keywords` | id=1032 两者都是 `[]` | ❌ 无信号 |
| Agent 名字含「读写/写入」 | 能抓到 1032，但**新 Agent 叫「平台同步agent」就漏**；反过来「导入规范检查agent」（只读）会被误剔 | ⚠️ 只能当**辅助**信号，不能当主判据 |

⇒ 主判据必须是**任务层面**的：「这一轮用户要不要写平台」，而不是「派给谁」。
   因为同一个 Agent（design / 1032 / chat）在两类任务里都会出现。

## 三、三态（这是通用性的关键）

`scan_write_intent()` 返回 `yes` / `no` / `unknown`：

  · **no**：出现明确否定（「不要写入」「只输出文本」「先别导入」）⇒ 确定性只读；
  · **yes**：出现写入动词 + 平台/工程类宾语（「把模型**导入**智源」「在包中**新增**一个测试包」）
          或单独出现的强写入词（「写入智源」「落库」）⇒ 允许写入；
  · **unknown**：既没说写也没说不写（绝大多数对话）⇒ **保守只读，但不惩罚**
          （不剔除候选池、不阻断；只加「默认只读，需写入请明说」的任务约束）。

`unknown` 走保守路而不是放行路，依据是**代价不对称**：
  · 误判成写入 ⇒ 白跑一次平台查询 + 可能触发 502 + 用 `ask_user` 打断用户（实测就是这个）；
  · 误判成只读 ⇒ 模型在答复里说「本轮未写入，如需写入请确认」，用户再说一句即可。
后者可恢复，前者不可恢复（这一轮的预算和用户注意力已经花掉了）。

## 四、边界（刻意不做的事）

**不用于拦截「用户明确要求写入」的场景**。`yes` 时一律放行，写操作仍走
`subtask_artifacts` 暂存闸 + 人工确认队列。本模块只决定「**要不要拦**」，不决定「怎么写」。

执行侧硬拦只在 `no` / `unknown` 生效，且**豁免集与确认闸共用同一对常量**
（`_REPAIR_WRITE_EXEMPT` / `_PROPOSE_WRITE_EXEMPT`）—— 否则 `sysml_v2_autofix`
会被拦，校验-修复闭环直接卡死（本仓 2026-11-13 记过这个坑）。
"""
from __future__ import annotations

#: 明确否定/收窄信号。**优先于**任何写入动词（同一句里同时出现 ⇒ 判 no）。
NEGATORS = (
    "不要写入", "别写入", "先别写", "暂不写入", "不需要写入", "不用写入", "不写入",
    "不要导入", "别导入", "不要同步", "不要落库", "不要提交", "不要回写",
    "不要保存到", "不要动平台", "不要改平台", "不要写入智源", "不要修改平台",
    "只输出文本", "仅输出文本", "只给文本", "只要文本", "只导出文本",
    "只读", "不要碰平台", "不写平台", "别动模型",
)

#: **强写入词**：单独出现即足以判 yes（它们本身就包含"目标侧"语义，不需要宾语佐证）。
STRONG_WRITE_VERBS = (
    "写入", "写回", "回写", "落库", "导入智源", "导入到智源", "导入平台", "导入到平台",
    "同步到智源", "同步至智源", "上传到平台", "提交到智源", "提交到平台", "推到智源",
    "发布到智源", "部署到平台", "写进工程", "写进平台",
)

#: **弱写入动词**：必须与「平台/工程类宾语」共现才判 yes。
#: ⚠️ 这些词单独出现时**不算**写入 —— 「新增一条待办」「创建一个目录」「删除临时文件」
#:   都是本地/会话内动作，判成写平台会误伤（真机 todo_write 每轮都在调）。
#: 「新增」这条是 2026-11-19 补的：真库 msg#4899「在"通信子系统用例"包中**新增**一个"测试"包」
#: 是明确的平台写入，而旧词表只有「写入/导入/回写/落库」⇒ 判 no ⇒ 会被只读闸误拦。
WEAK_WRITE_VERBS = (
    "新增", "添加", "追加", "创建", "新建", "建立", "删除", "移除", "修改", "更新",
    "改一下", "改成", "重命名", "移动", "替换", "补充", "补齐", "改成",
    "导入", "同步", "上传", "提交", "发布", "部署", "推", "写",
)

#: 宾语侧的「平台/工程」信号：出现即视为"目标在平台上"。
PLATFORM_OBJECTS = (
    "智源", "平台", "工程", "项目", "分支", "版本", "模型", "包", "元素", "视图",
    "projectid", "branchid", "versionid", "sysml", "zhiyuan", "mcp",
)

#: Agent 名称里出现这些字样 ⇒ **辅助信号**（不是主判据，理由见模块头表格）。
WRITE_AGENT_MARKS = ("写入", "读写", "回写", "落库", "导入")

#: 兼容旧调用点的别名（旧名 → 新名）。
WRITE_VERBS = STRONG_WRITE_VERBS


def scan_write_intent(text: str) -> dict:
    """扫描写入意图，返回 `{verdict, hits, negated, reason}`，verdict ∈ yes/no/unknown。"""
    s = str(text or "").strip().lower()
    out = {"verdict": "unknown", "hits": [], "negated": [], "reason": ""}
    if not s:
        out["reason"] = "empty"
        return out

    neg = [n for n in NEGATORS if n.lower() in s]
    if neg:
        out.update({"verdict": "no", "negated": neg, "reason": "negated:%s" % neg[0]})
        return out

    strong = [v for v in STRONG_WRITE_VERBS if v.lower() in s]
    if strong:
        out.update({"verdict": "yes", "hits": strong, "reason": "strong:%s" % strong[0]})
        return out

    weak = [v for v in WEAK_WRITE_VERBS if v.lower() in s]
    obj = [o for o in PLATFORM_OBJECTS if o.lower() in s]
    if weak and obj:
        out.update({"verdict": "yes", "hits": weak[:4],
                    "reason": "weak+object:%s/%s" % (weak[0], obj[0])})
        return out
    if weak and not obj:
        out["reason"] = "weak_only:%s（无平台宾语，按本地动作处理）" % weak[0]
        return out
    out["reason"] = "no_signal"
    return out


def needs_platform_write(text: str) -> tuple:
    """兼容旧签名：返回 (need: bool, 依据: str)。**只有 yes 返回 True**。

    ⚠️ 供历史调用点使用；新代码请用 `scan_write_intent()` —— 它能把
       「不明说」与「明确不要」区分开，而这两者在执行侧的处理完全不同。
    """
    r = scan_write_intent(text)
    return (r["verdict"] == "yes", r["reason"] if r["verdict"] == "yes" else "")


def is_write_agent(name: str, keywords=None) -> bool:
    """**辅助信号**：名字/关键词带写类字样。不要当唯一判据（理由见模块头表格）。

    真机事实：id=1032「智源数据读写agent」`capabilities=[]`、`intent_keywords=[]`、
    绑定工具 write=0，但 system_prompt 写着「新增、修改、删除模型元素」——
    数据侧完全没有信号，只有名字可用。所以它仍然保留，但只用于**降低概率**，
    真正的闸在任务层（`readonly_task_note` + 执行侧只读闸）。
    """
    s = str(name or "")
    if any(m in s for m in WRITE_AGENT_MARKS):
        return True
    if keywords:
        try:
            joined = keywords if isinstance(keywords, str) else " ".join(
                str(x) for x in (keywords or []))
        except Exception:
            joined = ""
        if any(v.lower() in str(joined).lower() for v in STRONG_WRITE_VERBS):
            return True
    return False


def split_pool(pool, need_write: bool) -> tuple:
    """按写入范围切分候选池。返回 (保留, 剔除)。

    `need_write=True` 原样返回。**安全阀**：剔除后池子会空 ⇒ 退回原池
    （宁可多一个候选，也不能给模型一个空池）。

    ⚠️ 2026-11-19 起这只在 `verdict == "no"` 时调用 —— `unknown` 不剔除（保守但不惩罚）。
    """
    items = [str(x) for x in (pool or []) if str(x or "").strip()]
    if need_write or not items:
        return items, []
    keep = [x for x in items if not is_write_agent(x)]
    drop = [x for x in items if is_write_agent(x)]
    if not keep:
        return items, []
    return keep, drop


def readonly_task_note(verdict: str) -> str:
    """给子任务的只读约束文本；空串表示不加约束（`yes`）。

    `no`   ⇒ 确定性措辞（用户明确说了不要写）
    `unknown` ⇒ 条件措辞（默认只读，明确要求才写）
    """
    if verdict == "yes":
        return ""
    if verdict == "no":
        return ("\n【写入范围 · 硬约束】用户本轮**明确不需要写入平台**。"
                "本任务只做只读查询与文本产出；调用任何写入/导入/同步/删除类工具都会被拒，"
                "不要尝试，也不要因此中断——直接按 expected_output 交付文本。")
    return ("\n【写入范围】本轮用户未说明是否要写入平台 ⇒ **默认只读**："
            "不要为了「取基线」去查工程三元组或读写平台数据；"
            "若确实必须写入，先用一句话说明原因再执行；取不到数据不得中断交付。")
