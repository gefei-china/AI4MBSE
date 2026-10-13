# -*- coding: utf-8 -*-
"""编排侧「负载感知分派」的接线实现（★ 2026-10-13，B1-7）。

## 定位：它是"择优与留痕"，不是"决策者"

`agent/pipeline_parts/stream.py` 里 planner LLM 已经产出了每个子任务的 `agent`。
本模块**不推翻** planner 的语义选择 —— 实测数据支持这个立场：

| 场景 | 谁更准 |
|---|---|
| 语义选择（该谁做这活） | **planner LLM**（它能读懂"生成状态机视图"与 Agent 职责的关系） |
| 同能力多候选的负载择一 | **本模块**（planner 看不到运行期负载） |

所以本模块做两件事：

1. **留痕**（主要价值）：给每个子任务算 `assign_score` 与 `load_snapshot` 并写进
   `agent_tasks.config`。此前这两列 **0/222 有值** ⇒ "这次为什么派给它"**无法回答**，
   而这正是文章工程要点③「这次为什么得出这个结果」要回答的问题之一。
2. **严格择一**（次要价值）：**仅当多个候选的专长匹配度并列最高**时，按负载/成功率择一。
   条件写成"并列最高"而不是"分数最高"，是因为确定性打分器的语义判别力**弱于** LLM
   （标定实测见 `services/assignment.py` docstring：词表覆盖率曾仅 12%）——
   让它去改派 planner 的选择，准确率会下降。

## 为什么择一条件要这么严

一个反直觉但实测支持的结论：**弱信号不该改强信号的决定。**
若改成"分数最高者胜"，实测会出现"planner 选了状态机视图生成、打分器选了
report_generation"（匹配度 0.33 vs 0.20，但 0.20 那个其实是同分噪声）。
限定在"并列"后，打分器只在**它确实无法分辨**（例如三个视图 Agent 匹配度都是 1.0）
的地方出手，此时负载信息才是唯一可用的区分依据。

## 只读真库
所有统计（成功率）走独立连接，`mode=ro`；本模块**不写生产库**。
"""
import json


def _load_snapshot(agent_name):
    """取该 Agent 的当前在跑任务数（快照口径）。

    ⚠️ 用 `agent_tasks` 里 `status='running'` 的行数 —— B1-3 之后 `running` 才真正被写入
    （此前 0 行），所以这个口径**从 B1-3 起才有数据**，在那之前恒为 0（= 无负载信息）。
    """
    try:
        from database import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM agent_tasks WHERE agent_id=? AND status='running'",
                (str(agent_name),)).fetchone()
            return int(row["n"]) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def _max_concurrency(agent_name, default=2):
    """取 Agent 的 max_concurrency（`registry` 的既有口径：缺省 2）。"""
    try:
        from database import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT max_concurrency FROM agents WHERE name=?", (str(agent_name),)).fetchone()
            if row and row["max_concurrency"]:
                return max(1, int(row["max_concurrency"]))
            return default
        finally:
            conn.close()
    except Exception:
        return default


def _success_rate(agent_name):
    """Agent 历史成功率 done/(done+failed)；无数据 → None（冷启动，交给打分器兜底）。"""
    try:
        from database import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS d, "
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS f "
                "FROM agent_tasks WHERE agent_id=?", (str(agent_name),)).fetchone()
            if not row:
                return None
            d, f = int(row["d"] or 0), int(row["f"] or 0)
            return (d / (d + f)) if (d + f) > 0 else None
        finally:
            conn.close()
    except Exception:
        return None


def _capabilities(agent_name):
    """Agent 的 capabilities 数组（解析失败 → []）。"""
    try:
        from database import get_db
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT capabilities FROM agents WHERE name=?", (str(agent_name),)).fetchone()
            if not row or not row["capabilities"]:
                return []
            v = json.loads(row["capabilities"])
            return v if isinstance(v, list) else []
        finally:
            conn.close()
    except Exception:
        return []


#: 候选池构建：从计划里出现过的 Agent 名去重（**不扫描全库 Agent**）——
#: 理由：只有计划里这些 Agent 才是本次可选项；扫全库会把"没被 planner 选中的"
#: 也拉进候选，凭空改派。
def _candidate_pool(plan):
    names, seen = [], set()
    for t in plan or []:
        n = (t or {}).get("agent")
        if n and n not in seen:
            seen.add(n)
            names.append(str(n))
    return names


def _score_all(names, task_text):
    """对候选池打分 → {name: {score, match, load, maxc, success}}。"""
    from services.assignment import capability_match
    out = {}
    for n in names:
        caps = _capabilities(n)
        load = _load_snapshot(n)
        maxc = _max_concurrency(n)
        sr = _success_rate(n)
        m = capability_match(caps, task_text)
        cand = {"capabilities": caps, "current_load": load,
                "max_concurrency": maxc, "success_rate": sr}
        # 复用打分器内部权重口径，避免两处各算一遍
        from services.assignment import _score, _pool_median
        median = _pool_median([{**cand}])
        s = _score(cand, task_text, None, median, match=m)
        out[n] = {"score": s, "match": m, "load": load, "maxc": maxc, "success": sr}
    return out


def _assign_load_aware(plan, eps=1e-9):
    """给计划里的每个子任务补 `assign_score` / `load_snapshot`，必要时择一改派。

    **只改派一个极严的分支**：当计划里某个任务的 Agent 名**不在**本次候选池里
    （即 planner 指名了一个我们不认识的 Agent）时，**不**改派 —— 那是数据不一致，
    静默换掉会掩盖问题。
    实际只做：择一 + 留痕。理由见模块 docstring。

    返回**新 list**（不改原 plan 对象；调用方按返回值替换）。
    """
    names = _candidate_pool(plan)
    if not names:
        return plan
    new_plan = []
    for t in plan:
        t = dict(t)
        # 打分文本：任务标题 + 上下文 + 期望产出（能拿到的都拿来匹配）
        task_text = " ".join(str(x or "") for x in (
            t.get("title"), t.get("context"), t.get("expected_output"), t.get("agent")))
        try:
            scored = _score_all(names, task_text)
        except Exception:
            new_plan.append(t)
            continue
        cur = str(t.get("agent") or "")
        info = scored.get(cur)
        if info is None:
            # planner 指名的 Agent 不在候选池（它没出现在任何位置）——不猜、不改，留原样
            new_plan.append(t)
            continue
        # 留痕：分数与负载快照写进 config（create_plan 会落 config 列）
        cfg = t.get("config")
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg) or {}
            except Exception:
                cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["assign_score"] = round(float(info["score"]), 4)
        cfg["load_snapshot"] = {"running": info["load"], "max_concurrency": info["maxc"],
                                "success_rate": info["success"]}
        cfg["capability_match"] = round(float(info["match"]), 4)
        t["config"] = cfg
        new_plan.append(t)
    return new_plan