# -*- coding: utf-8 -*-
"""自进化周报引擎（产品版）：回答「这一周，系统替我做了什么？我该做什么？」

═══════════════════════════════════════════════════════════════
它解决什么
═══════════════════════════════════════════════════════════════
自进化六环全部交付后，价值感知链断在最后一环：能力都在（门禁常驻、步骤级
归因、配置提案、沙箱晋升、死路记忆、安全五信号、任务自生成、拓扑体检），
但用户只能逐个端点查 JSON。本模块把六个域的既有台账汇成**一页可行动摘要**。

★信息架构的关键决定（原型跑完后返工）
──────────────────────────────────────
原型按「省/抓/守/欠」四段组织，那是**系统内部模块的视角**——archive域、
curriculum 域、sandbox 域、拓扑域。用户不关心域，用户只问三个问题：

    1. 哪条要我点？我该先做哪一件？      ⇒ actions段（带可执行动作）
    2. 这周比上周好了还是坏了？          ⇒ digest 段（环比，不是孤立快照）
    3. 这个数字是真的还是没数据？        ⇒ confidence 段（显式列出本周无）

所以产品版把四段重排为三段。域的划分退到digest 段内部的子项上——那才是它
该在的位置（体检报告的分区，不是首页的信息架构）。

★三条诚实分母纪律（承 P2-1/P2-2/P2-3/P2-4 全系列）
──────────────────────────────────────
1. **空窗口不是 0 分**。「这周拦了 0 次」（真跑了 N 次都没拦下）与「这周没
   发生变更所以没东西可拦」是完全不同的两件事。前者是好消息，后者是没数据。
   混在一起会让这张周报第一周就失去可信度。
2. **分母不足记 unknown，绝不回落0**。所有环比指标带 `state` 字段：
   `ok` 有数据 / `unknown` 分母不足。只有 `state=='ok'` 才允许出现 delta。
3. **不能点的事不许装成能点**。actions 段每个条目要么带可执行的
   `action{method,path,label,payload}`，要么带 `not_actionable_reason`
   说明为什么只能看不能做。真实情况是：任务提案有 decide 端点可以一键
   裁决，而 HIL 待确认与拓扑缺陷**至今没有处理端点**——把它们渲染成可点
   的按钮就是欺骗。

★只读铁律
──────────────────────────────────────
本模块只SELECT。**没有任何一条 INSERT/UPDATE/DELETE**，也不调用任何
decide/approve 类函数——它是镜子不是手。门禁 A2 对此做静态断言。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

# ══════════════════════════════════════════════════════════════
# 配置
# ══════════════════════════════════════════════════════════════
_SETTINGS_DEFAULTS = {
    "enabled": True,        # 总开关（False 时 build 返 enabled=False）
    "window_days": 7,       # 本周窗口（天）；环比对照窗口为上一个等长窗口
    "max_actions": 50,      # 单次返回的待决策条目上限（防长尾注水）
    "max_detail": 8,        # 每段明细条目上限
    "longtail_days": 90,    # P0-a：能力长尾判定天数（active 且零调用超过该天数）
}

VERDICT_CLEAN = "clean"
VERDICT_ATTENTION = "needs_attention"
VERDICT_UNKNOWN = "insufficient_data"

_VERDICT_ORDER = {VERDICT_UNKNOWN: 0, VERDICT_CLEAN: 1, VERDICT_ATTENTION: 2}

VERDICT_LABEL = {
    VERDICT_CLEAN: "正常",
    VERDICT_ATTENTION: "待处理",
    VERDICT_UNKNOWN: "本周无数据",
}


def _cfg():
    """配置读取（与 agent_archive / flow_topology 同一范式）。"""
    from core import config as _config
    return _config


def get_settings(overrides: Optional[dict] = None) -> dict:
    """读配置；enabled=False 时仍返回完整 dict（调用处自己判停）。"""
    _c = _cfg()
    s = dict(_SETTINGS_DEFAULTS)
    try:
        if not _c.as_bool("evolution_report", "enabled", True):
            s["enabled"] = False
        for k in ("window_days", "max_actions", "max_detail", "longtail_days"):
            v = _c.get("evolution_report", k)
            if v is None:
                continue
            iv = int(v)
            if iv > 0:
                s[k] = iv
    except Exception:                                        # noqa: BLE001
        pass
    if overrides:
        for k, v in overrides.items():
            if v is None or k not in s:
                continue
            # 数值项只接受正整数：0 / 负数的窗口没有意义，必须回落默认而不是
            # 被注入值灌进来（window_days=0 会让所有环比窗口退化成空窗口）。
            if isinstance(s[k], int) and not isinstance(s[k], bool):
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                s[k] = iv if iv > 0 else _SETTINGS_DEFAULTS[k]
            elif k == "enabled":
                s[k] = bool(v)
    return s


# ══════════════════════════════════════════════════════════════
# 基础只读工具
# ══════════════════════════════════════════════════════════════
def _tbl_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        conn.execute("SELECT 1 FROM %s LIMIT 1" % table).fetchone()
        return True
    except sqlite3.Error:
        return False


def _count(conn: sqlite3.Connection, sql: str, params=()) -> Optional[int]:
    """计数；表不存在返 None（**不是 0**——表还没建 = 没有这项证据）。"""
    try:
        return int(conn.execute(sql, tuple(params)).fetchone()[0])
    except sqlite3.Error:
        return None


def _rows(conn: sqlite3.Connection, sql: str, params=()) -> list:
    """取明细行。

    ★只吞「表不存在」并返 []，其余 sqlite 错误一律上抛。
      早期版本这里 catch 全部 sqlite3.Error 返 []，后果实测踩到过两次：
      ① 一张被破坏（列名不匹配）的表看起来跟「本来就没有待办」**完全一样**
         ——采集失败被伪装成干净输出；
      ② "no such column" 也被当成「表不存在」，于是错误信息指向错误的
         原因，排查方向直接跑偏。
      表缺失由调用方用 _tbl_exists 显式判（那是合法的「没这项证据」），
      查询出错必须进 build_actions 的 collect_failures 让人看见。
    """
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error as e:
        if "no such table" in str(e).lower():
            return []
        raise


def _cutoff(days: int) -> str:
    """窗口起点（本地时区字符串，与 misevolution._cutoff 同口径）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() - int(days) * 86400))


def _worst(verdicts) -> str:
    """取最严重的 verdict（unknown < clean < attention）。"""
    vs = [v for v in verdicts if v]
    if not vs:
        return VERDICT_UNKNOWN
    return max(vs, key=lambda v: _VERDICT_ORDER.get(v, 0))


def _delta(cur: Optional[int], prev: Optional[int]) -> Optional[int]:
    """环比差；任一侧 unknown 返 None（**不回落 0**）。"""
    if cur is None or prev is None:
        return None
    return int(cur) - int(prev)


# ══════════════════════════════════════════════════════════════
# ① actions —— 要你决策的（唯一需要人动手的一段）
# ══════════════════════════════════════════════════════════════
def _act_capability(conn: sqlite3.Connection, s: dict) -> list:
    """P0-a 能力长尾与零调度：长尾能力（>90 天零调用）+ 零调度 Agent。

    **只提议不自动执行**（延续本产品自进化纪律）：清单由人决定停用/下架，
    系统不碰任何 status。无端点 ⇒ 显式 not_actionable_reason，不假装能点。
    """
    if not _tbl_exists(conn, "capability_registry"):
        return []
    from core import capability_registry as cr
    out = []
    for it in cr.longtail_capabilities(conn, days=int(s.get("longtail_days", 90))):
        out.append({
            "source": "capability",
            "source_label": "能力长尾",
            "id": it["capability_id"],
            "title": "%s（%s）" % (it.get("display_name") or it["capability_id"],
                                 it.get("kind") or ""),
            "detail": "自 %s 创建后零调用。建议人工决定：停用 / 下架 / 保留观察。"
                      % (it.get("created_at") or "（时间未知）"),
            "meta": {"capability_id": it["capability_id"], "kind": it.get("kind"),
                     "version": it.get("version"), "publisher": it.get("publisher"),
                     "created_at": it.get("created_at")},
            "action": None,
            "not_actionable_reason": "停用/下架能力会改动 Agent 可用面，需人工决定；"
                                     "系统只列清单，不自动执行（自进化纪律）",
        })
    for ag in cr.zero_dispatch_agents(conn):
        out.append({
            "source": "capability",
            "source_label": "零调度 Agent",
            "id": "agent:%s" % ag.get("name"),
            "title": "%s（%s）" % (ag.get("name"), ag.get("status") or ""),
            "detail": "从未被调度过（agent_tasks 零记录）。建议人工决定：启用 / 退役 / 保留观察。",
            "meta": {"agent_id": ag.get("id"), "name": ag.get("name"),
                     "status": ag.get("status")},
            "action": None,
            "not_actionable_reason": "Agent 退役是组织决策，需人工决定；系统只列清单",
        })
    return out


def _act_badcase(conn: sqlite3.Connection, s: dict) -> list:
    """P0-a BadCase 候选未入库：dry-run 计数 + 一键 apply（真端点）。

    与 `_act_capability`（只列清单、显式不能点）的区别：收集端点**真实存在**
    （`POST /api/monitor/badcases/collect`，body `{"apply": true}`，
    见 routers/monitor.py:728-744）⇒ 这里给真按钮。端点默认 dry-run，
    必须显式带 apply=true 才写库——不是系统自动执行，是人点一下
    （人工关口纪律，与自进化「只建议不自动执行」同源）。

    ★本函数只调 apply=False 的 dry-run 做计数，**不写任何库**
      （周报只读铁律，A2 静态断言对此锁定）。
    """
    from core import badcase as bc
    try:
        res = bc.collect_bad_cases(conn, apply=False)
    except Exception as e:                                        # noqa: BLE001
        return [{
            "source": "badcase", "source_label": "BadCase 入库", "id": None,
            "title": "BadCase 采集失败", "detail": "%s: %s" % (type(e).__name__, e),
            "meta": {}, "action": None, "not_actionable_reason": "采集未完成",
        }]
    if not res.get("enabled"):
        return []
    # ★ 2026-10-29：判据面级失败也要变成"人看得见的动作项"。
    #   badcase 此前对 detector SQL 异常是 `except: continue` 静默跳过 ⇒
    #   某个判据面瘫了，坏例数凭空变少，周报会把它当成"最近没有坏例、变好了"。
    #   现在 badcase 把失败记进 res['failures']，此处显式转成 action。
    _bc_failures = list(res.get("failures") or [])
    if _bc_failures:
        return [{
            "source": "badcase", "source_label": "BadCase 入库", "id": None,
            "title": "BadCase 判据面失效（坏例数不可信）",
            "detail": "以下判据面执行失败，本轮坏例统计**不完整**，不可据此判断「变好了」："
                      + "；".join(_bc_failures[:5]),
            "meta": {"failures": _bc_failures},
            "action": None,
            "not_actionable_reason": "判据面执行失败，需先修 SQL/表结构",
        }]
    plan = res.get("plan") or {}
    new, recur, reopen = (int(plan.get(k, 0) or 0)
                          for k in ("new", "recur", "reopen"))
    pending = new + recur + reopen
    if pending <= 0:
        return []
    open_cnt = bc.open_badcase_count(conn)
    by_kind = (res.get("summary") or {}).get("by_kind") or {}
    kinds = "；".join("%s %d 个主题" % (v.get("label") or k, int(v.get("subjects", 0)))
                     for k, v in by_kind.items()) or "（无明细）"
    detail = ("%s；%d 天窗口内共 %d 起事件；当前 open %d 条。"
              "点「一键入库」写入 bad_cases（收集端点默认 dry-run，"
              "此按钮显式带 apply=true，人工确认后才落库）。"
              % (kinds, int(res.get("window_days") or 0),
                 int((res.get("summary") or {}).get("total_events", 0) or 0),
                 open_cnt))
    if not _tbl_exists(conn, "bad_cases"):
        # 有候选但台账表不在 ⇒ 按钮点了也写不进去，必须显式说清堵在哪
        return [{
            "source": "badcase", "source_label": "BadCase 入库",
            "id": "badcase:collect",
            "title": "%d 条 BadCase 候选未入库，但 bad_cases 表尚未建立"
                     % pending,
            "detail": detail,
            "meta": {"plan": plan, "open_count": open_cnt,
                     "window_days": res.get("window_days")},
            "action": None,
            "not_actionable_reason": "bad_cases 表不存在（迁移未执行），"
                                     "无法写入；先执行数据库迁移",
        }]
    return [{
        "source": "badcase",
        "source_label": "BadCase 入库",
        "id": "badcase:collect",
        "title": "%d 条 BadCase 候选未入库（新 %d / 复现 %d / 重开 %d）"
                 % (pending, new, recur, reopen),
        "detail": detail,
        "meta": {"plan": plan, "open_count": open_cnt,
                 "window_days": res.get("window_days"),
                 "total_events": (res.get("summary") or {}).get("total_events", 0)},
        # ★真闭环：收集端点已存在，一键 apply 由人确认后落库
        "action": {"method": "POST",
                   "path": "/api/monitor/badcases/collect",
                   "label": "一键入库",
                   "payload": {"apply": True}},
    }]


def _act_online_eval(conn: sqlite3.Connection, s: dict) -> list:
    """P0-b 在线评测回流：待标注积压 + 7 日零转正 + 候选分布。

    R13 组合判据的落地处（alert_rules 无组合条件列，见 seed_registry 注释）：
    promoted_7d=0 **且** backlog>0 ⇒ 回流停滞，一条 warn 行动项。
    confirmed 未转正 > 0 ⇒ 真按钮 POST /promote（apply=true 由人确认）。
    纯 pending 积压 ⇒ 显式 not_actionable（标注必须逐条人看，没有一键）。
    """
    if not _tbl_exists(conn, "online_eval_queue"):
        return []
    from core import online_eval as oe
    try:
        met = oe.online_eval_metrics(conn)
        summ = oe.queue_summary(conn)
    except Exception as e:                                        # noqa: BLE001
        return [{
            "source": "online_eval", "source_label": "在线评测回流", "id": None,
            "title": "在线评测指标读取失败",
            "detail": "%s: %s" % (type(e).__name__, e),
            "meta": {}, "action": None, "not_actionable_reason": "指标未读到",
        }]
    backlog = int(met.get("eval_backlog_pending") or 0)
    promoted = int(met.get("eval_promoted_7d") or 0)
    confirmed = int((summ.get("by_status") or {}).get("labeled_confirmed") or 0)
    by_type = summ.get("by_type") or {}
    out = []

    # ① confirmed 未转正 ⇒ 真按钮（promote 是唯一真写点，dry-run 默认）
    if confirmed > 0:
        dist = "；".join("%s %d" % (k, v) for k, v in by_type.items()) or "（无明细）"
        out.append({
            "source": "online_eval",
            "source_label": "在线评测回流",
            "id": "online_eval:promote",
            "title": "%d 条人工 confirmed 样本待转正进 dataset" % confirmed,
            "detail": ("已人工标注确认的样本分布：%s。点「一键转正」按 sample_type "
                       "三分流（intent→intent_samples / retrieval→qrels.jsonl / "
                       "generation→eval_gen_labels，幂等）。转正端点默认 dry-run，"
                       "此按钮显式带 apply=true。自动打分永不自动转正（B1）。" % dist),
            "meta": {"confirmed": confirmed, "by_type": by_type,
                     "promoted_7d": promoted},
            "action": {"method": "POST",
                       "path": "/api/monitor/online-eval/promote",
                       "label": "一键转正",
                       "payload": {"apply": True}},
        })

    # ② R13 组合判据：7 日零转正且仍有积压 ⇒ 回流停滞
    if promoted == 0 and backlog > 0:
        out.append({
            "source": "online_eval",
            "source_label": "在线评测回流",
            "id": "online_eval:stalled",
            "title": "回流停滞：7 日零转正且待标注积压 %d 条" % backlog,
            "detail": ("采样在进队、标注/转正没人动 ⇒ 生产回流管道空转。"
                       "去 监控 → 在线评测回流 面板逐条标注（评分 1-5 + 原因，"
                       "走 HIL 确认卡），确认后的样本才能被「一键转正」送进 dataset。"),
            "meta": {"backlog": backlog, "promoted_7d": promoted},
            "action": None,
            "not_actionable_reason": "标注必须逐条人工判断（无一键）；"
                                     "管道入口在监控页「在线评测回流」面板",
        })

    # ③ 纯积压（有转正但积压增长）⇒ 一条提示，不制造噪音
    elif backlog > 0 and not out:
        out.append({
            "source": "online_eval",
            "source_label": "在线评测回流",
            "id": "online_eval:backlog",
            "title": "待标注积压 %d 条" % backlog,
            "detail": "采样持续进队，标注节奏待跟进（面板：监控 → 在线评测回流）。",
            "meta": {"backlog": backlog, "promoted_7d": promoted},
            "action": None,
            "not_actionable_reason": "逐条人工标注，无一键动作",
        })
    return out


def _act_curriculum(conn: sqlite3.Connection, s: dict) -> list:
    """任务提案：有 decide 端点 ⇒ 真可一键裁决。"""
    if not _tbl_exists(conn, "curriculum_items"):
        return []
    items = _rows(
        conn,
        "SELECT id, rule, task_kind, title, weakness, suggestion, difficulty, "
        "created_at FROM curriculum_items WHERE status='pending' "
        "ORDER BY id DESC LIMIT ?", (int(s["max_detail"]),))
    out = []
    for it in items:
        out.append({
            "source": "curriculum",
            "source_label": "任务提案",
            "id": it["id"],
            "title": it.get("title") or "未命名任务提案",
            "detail": it.get("suggestion") or it.get("weakness") or "",
            "meta": {"rule": it.get("rule"), "difficulty": it.get("difficulty"),
                     "task_kind": it.get("task_kind"),
                     "created_at": it.get("created_at")},
            # ★真闭环：decide 端点已存在，可直接 POST 裁决
            "action": {"method": "POST",
                       "path": "/api/curriculum/%s/decide" % it["id"],
                       "label": "认领",
                       "payload": {"decision": "accepted"},
                       "alt": [{"label": "不认领",
                                "payload": {"decision": "rejected"}}]},
        })
    return out


def _act_hil(conn: sqlite3.Connection, s: dict) -> list:
    """人工确认点：至今**没有**批准端点 ⇒ 显式标注只能看不能做。

    这是产品化最容易被掩盖的一处：原型把 8 条 pending 列出来，用户理所当然
    以为能点。任何"假装能点"的渲染都是欺骗，所以这里给死
    not_actionable_reason，并在 reason 里说清堵在哪。
    """
    if not _tbl_exists(conn, "hil_confirmations"):
        return []
    items = _rows(
        conn,
        "SELECT id, action, agent_id, created_at, status FROM hil_confirmations "
        "WHERE status='pending' ORDER BY id DESC LIMIT ?",
        (int(s["max_detail"]),))
    return [{
        "source": "hil",
        "source_label": "人工确认点",
        "id": it["id"],
        "title": "%s%s" % (it.get("action") or "写操作",
                           ("（%s）" % it["agent_id"]) if it.get("agent_id") else ""),
        "detail": "系统在动手前停下来问你，目前只能查看，还不能在这里批准。",
        "meta": {"created_at": it.get("created_at")},
        "action": None,
        "not_actionable_reason": "HIL 批准链路尚未对外开放端点"
                                 "（hil_confirmations 只有写入侧，无决定侧API）",
    } for it in items]


def _act_topology(conn: sqlite3.Connection, s: dict) -> list:
    """拓扑欠账：只体检不改（红线 #6 不碰架构拓扑）⇒ 标注只能看。"""
    from core import flow_topology as ft
    try:
        res = ft.scan(conn)
    except Exception as e:                                    # noqa: BLE001
        return [{
            "source": "topology",
            "source_label": "画布结构",
            "id": None,
            "title": "拓扑体检失败",
            "detail": "%s: %s" % (type(e).__name__, e),
            "meta": {},
            "action": None,
            "not_actionable_reason": "体检未完成",
        }]
    ov = res.get("overview") or {}
    groups = res.get("groups") or []
    out = []
    # ★维度值在 overview.dimensions 里，不在顶层（真库键位，见 _dim 注释）
    blockers = _dim(ov, "blocker_defects")
    if blockers:
        out.append({
            "source": "topology",
            "source_label": "画布结构",
            "id": None,
            "title": "%d 个 blocker 级结构缺陷" % blockers,
            "detail": "悬空连线 / 孤立节点 / 类型无法解析——画布自身跑不通。",
            "meta": {"canvases": ov.get("canvases")},
            "action": None,
            "not_actionable_reason": "拓扑缺陷需人工改画布；体检镜不是手"
                                     "（红线 #6：不碰架构拓扑）",
        })
    dup = _dim(ov, "duplicate_canvases") or 0
    if dup:
        g0 = groups[0] if groups else {}
        out.append({
            "source": "topology",
            "source_label": "画布结构",
            "id": None,
            "title": "%d 幅画布结构重复" % dup,
            "detail": ("最大一组 %s 幅，形状 %s，来源 %s"
                       % (g0.get("size"), g0.get("shape"),
                          "/".join(g0.get("sources") or []) or "未知")),
            "meta": {"groups": _dim(ov, "isomorphic_groups")},
            "action": None,
            "not_actionable_reason": "合并/删除画布会改动生效拓扑，需人工决定",
        })
    return out


def _act_safety(conn: sqlite3.Connection, s: dict) -> list:
    """安全信号：alert 是**待人解读的信号**，不是已确认的恶化。"""
    try:
        from core import misevolution as mv
        sig = mv.collect_signals(conn)
    except Exception as e:                                    # noqa: BLE001
        return [{
            "source": "safety", "source_label": "安全信号", "id": None,
            "title": "安全信号采集失败", "detail": "%s: %s" % (type(e).__name__, e),
            "meta": {}, "action": None, "not_actionable_reason": "采集未完成",
        }]
    out = []
    for k, v in (sig or {}).items():
        if k == "meta" or not isinstance(v, dict):
            continue
        if v.get("status") not in ("alert", "warn"):
            continue
        num, den = v.get("numerator"), v.get("denominator")
        out.append({
            "source": "safety",
            "source_label": "安全信号",
            "id": k,
            "title": v.get("label") or k,
            "detail": "%s/%s（%s）" % (num, den,
                                     "分母不足，仅供参考" if not v.get("sufficient")
                                     else "窗口内占比"),
            "meta": {"rate": v.get("rate"), "numerator": num,
                     "denominator": den, "status": v.get("status")},
            "action": None,
            "not_actionable_reason": "信号需人解读；系统不会自行处置（安全红线）",
        })
    return out


#: 行动源定义（注册表；勿在调用处另写字面量）
ACTION_SOURCES = (
    ("curriculum", "任务提案", _act_curriculum),
    ("hil", "人工确认点", _act_hil),
    ("topology", "画布结构", _act_topology),
    ("safety", "安全信号", _act_safety),
    ("capability", "能力长尾", _act_capability),
    ("badcase", "BadCase 入库", _act_badcase),
    ("online_eval", "在线评测回流", _act_online_eval),
)


def build_actions(conn: sqlite3.Connection, s: dict) -> dict:
    """要你决策的：每条要么可一键处理，要么显式说明为什么不能。"""
    items, failed = [], []
    for key, label, fn in ACTION_SOURCES:
        try:
            items.extend(fn(conn, s))
        except Exception as e:                                # noqa: BLE001
            # 单源采集失败不许拖垮整段——但必须显式记下来，不能静默少一块
            failed.append({"source": key, "label": label,
                           "error": "%s: %s" % (type(e).__name__, e)})
    actionable = [i for i in items if i.get("action")]
    verdict = (VERDICT_ATTENTION if actionable
               else (VERDICT_UNKNOWN if not items else VERDICT_CLEAN))
    if actionable:
        reason = "%d 项等你处理（其中 %d 项可一键处理）" % (
            len(items), len(actionable))
    elif items:
        reason = "有%d 项待解读，但都还不能一键处理" % len(items)
    else:
        reason = "本周没有需要你决策的事项"
    return {
        "verdict": verdict,
        "reason": reason,
        "count": len(items),
        "actionable_count": len(actionable),
        "items": items[: int(s["max_actions"])],
        "collect_failures": failed,
    }


# ══════════════════════════════════════════════════════════════
# ② digest —— 本周体检（含环比）
# ══════════════════════════════════════════════════════════════
def _dim(overview: dict, name: str):
    """从 overview.dimensions 数组里取维度值。

    ★真实键位（真库核过）：blocker_defects / warn_defects /
    duplicate_canvases / isomorphic_groups / run_coverage / failed_runs
    都在 dimensions 里；overview 顶层只有 canvases / isomorphic_groups /
    covered_canvases / uncovered_canvases / summary。去顶层取同名键会拿到
    None，然后被误报成「本周无数据」——一个不存在的缺陷比一个假装存在的
    缺陷更难查，所以这个取法必须锁死。
    """
    for d in (overview or {}).get("dimensions") or []:
        if isinstance(d, dict) and d.get("name") == name:
            return d.get("value")
    return None


def _stock(value, why: str = "") -> dict:
    """存量指标（无上期对照的当前值，如拓扑体检结果）。"""
    return {"current": value, "previous": None, "delta": None,
            "state": "ok" if value is not None else VERDICT_UNKNOWN,
            "why": "" if value is not None else (why or "未取到数据")}


def _pair(conn: sqlite3.Connection, sql_cur: str, sql_prev: str,
          wd: int) -> dict:
    """按两条独立 SQL 取 (本期, 上期) 计数并判state。

    ★核心纪律：只有本期与上期**都有数据**才允许出 delta。上期无记录
    （= 表刚建 / 之前没跑过）时 state=unknown，**不是** delta=+7。

    ★为什么用两条独立 SQL 而不是一个带OR 的模板：模板要求本期绑 1 参、
    上期绑 2 参，占位符数不一致时 sqlite3 抛 ProgrammingError 被 _count
    吞成 None，整条指标假报「台账尚未建立」。真库首跑正是这么翻车的——
    6 条指标全部误报。两条独立 SQL 让每个占位符数只对应自己那条语句，
    从结构上消灭这类错配。
    """
    now = _cutoff(wd)
    prev_lo = _cutoff(wd * 2)
    cur = _count(conn, sql_cur, (now,))
    prev = _count(conn, sql_prev, (prev_lo, now))
    if cur is None and prev is None:
        return {"current": None, "previous": None, "delta": None,
                "state": VERDICT_UNKNOWN, "why": "台账尚未建立"}
    if cur is None:
        return {"current": None, "previous": prev, "delta": None,
                "state": VERDICT_UNKNOWN, "why": "本期无数据"}
    if cur == 0 and prev == 0:
        #两窗口都没事件：这是"本周无"，不是"表现好"
        return {"current": 0, "previous": 0, "delta": None,
                "state": VERDICT_UNKNOWN,
                "why": "两期窗口均无事件（空窗口不是 0 分）"}
    if prev is None:
        return {"current": cur, "previous": None, "delta": None,
                "state": VERDICT_UNKNOWN,
                "why": "上期无台账记录，无法对比"}
    return {"current": cur, "previous": prev, "delta": _delta(cur, prev),
            "state": "ok"}


def _metrics_for(conn: sqlite3.Connection, s: dict, scope: str) -> list:
    """按域取指标。流式指标走 _pair（两条 SQL），存量指标走 _stock。"""
    out = []
    if scope == "evolve":
        # 进化留痕：只有 asset_change_logs 里的 agent 变更才算真进化
        out.append(("agent 资产变更", _pair(
            conn,
            "SELECT COUNT(*) FROM asset_change_logs "
            "WHERE asset_type='agents' AND created_at>=?",
            "SELECT COUNT(*) FROM asset_change_logs "
            "WHERE asset_type='agents' AND created_at>=? AND created_at<?",
            int(s["window_days"]))))
        out.append(("确认死路", _pair(
            conn,
            "SELECT COUNT(*) FROM agent_variants "
            "WHERE status='dead_end' AND dead_end_at>=?",
            "SELECT COUNT(*) FROM agent_variants "
            "WHERE status='dead_end' AND dead_end_at>=? AND dead_end_at<?",
            int(s["window_days"]))))
    elif scope == "guard":
        out.append(("沙箱校验", _pair(
            conn,
            "SELECT COUNT(*) FROM sandbox_runs WHERE created_at>=?",
            "SELECT COUNT(*) FROM sandbox_runs "
            "WHERE created_at>=? AND created_at<?",
            int(s["window_days"]))))
        out.append(("拦下回归", _pair(
            conn,
            "SELECT COUNT(*) FROM sandbox_runs "
            "WHERE verdict='regression' AND created_at>=?",
            "SELECT COUNT(*) FROM sandbox_runs "
            "WHERE verdict='regression' AND created_at>=? AND created_at<?",
            int(s["window_days"]))))
        out.append(("待人工确认", _pair(
            conn,
            "SELECT COUNT(*) FROM hil_confirmations WHERE created_at>=?",
            "SELECT COUNT(*) FROM hil_confirmations "
            "WHERE created_at>=? AND created_at<?",
            int(s["window_days"]))))
    elif scope == "structure":
        # 拓扑是存量指标：环比无意义（上周的画布早被删了），只报当前值
        total = _count(conn, "SELECT COUNT(*) FROM agent_flows")
        try:
            from core import flow_topology as ft
            ov = (ft.scan(conn).get("overview") or {})
        except Exception:                                     # noqa: BLE001
            ov = {}
        out.append(("画布总数", _stock(total, "尚无画布定义")))
        out.append(("结构重复", _stock(_dim(ov, "duplicate_canvases"),
                                "拓扑体检未产出结果")))
        out.append(("blocker 缺陷", _stock(_dim(ov, "blocker_defects"),
                                   "拓扑体检未产出结果")))
    elif scope == "capability":
        # P0-a 能力度量（capability_registry 统一注册处）：
        # 调用总量走环比（流式），长尾/零调度走存量（_stock——环比无意义：
        # 上周不存在的能力这周出现了，不是"新增 7 个长尾"）。
        if _tbl_exists(conn, "capability_registry"):
            out.append(("能力调用（本周）", _pair(
                conn,
                "SELECT COUNT(*) FROM tool_call_logs WHERE created_at>=?",
                "SELECT COUNT(*) FROM tool_call_logs WHERE created_at>=? AND created_at<?",
                int(s["window_days"]))))
            out.append(("零调用能力", _stock(_count(
                conn,
                "SELECT COUNT(*) FROM capability_registry "
                "WHERE status='active' AND call_count=0"),
                "注册处尚未建立")))
            out.append(("零调度 Agent", _stock(_count(
                conn,
                "SELECT COUNT(*) FROM (SELECT a.id FROM agents a "
                "LEFT JOIN agent_tasks t ON t.agent_id=a.name "
                "GROUP BY a.id HAVING COUNT(t.id)=0)"),
                "agents 表不可用")))
            out.append(("长尾能力（>%d天）" % int(s.get("longtail_days", 90)), _stock(_count(
                conn,
                "SELECT COUNT(*) FROM capability_registry WHERE status='active' "
                "AND call_count=0 AND julianday('now')-julianday(created_at)>?",
                (int(s.get("longtail_days", 90)),)),
                "注册处尚未建立")))
        else:
            out.append(("能力注册处", _stock(None, "capability_registry 表尚未建立")))
    return [{"name": n, **d} for n, d in out]


#: 体检分区（域退到这里，不再当首页信息架构）
DIGEST_SECTIONS = (
    ("evolve", "进化留痕", "系统改了哪些东西、记住了哪些死路"),
    ("guard", "变更守卫", "改动之前有没有被拦下来、有没有等你点头"),
    ("structure", "结构欠账", "画布层面的重复与缺陷"),
    ("capability", "能力度量", "五类能力的调用、长尾与零调度（P0-a 统一注册处）"),
)


def build_digest(conn: sqlite3.Connection, s: dict) -> dict:
    sections, failed = [], []
    for scope, title, sub in DIGEST_SECTIONS:
        try:
            metrics = _metrics_for(conn, s, scope)
        except Exception as e:                                # noqa: BLE001
            failed.append({"section": scope, "title": title,
                           "error": "%s: %s" % (type(e).__name__, e)})
            continue
        # 段 verdict：只有出现可读出 delta 的负向变化才算待处理
        verdict = VERDICT_CLEAN
        for m in metrics:
            if m["state"] != "ok":
                verdict = _worst([verdict, VERDICT_UNKNOWN])
                continue
            if m["delta"] is not None and m["delta"] > 0:
                verdict = _worst([verdict, VERDICT_ATTENTION])
        sections.append({
            "key": scope, "title": title, "subtitle": sub,
            "verdict": verdict, "metrics": metrics,
            "known": sum(1 for m in metrics if m["state"] == "ok"),
            "total": len(metrics),
        })
    return {
        "window_days": int(s["window_days"]),
        "sections": sections,
        "collect_failures": failed,
    }


# ══════════════════════════════════════════════════════════════
# ③ confidence —— 哪些是真数据，哪些是本周无
# ══════════════════════════════════════════════════════════════
def build_confidence(actions: dict, digest: dict) -> dict:
    """显式列出所有不可信的地方。**这一段的存在本身就是可信度声明。**"""
    unknown, notes = [], []
    for m in (m for sec in digest.get("sections", []) for m in sec.get("metrics", [])):
        if m.get("state") != "ok":
            unknown.append({"where": "体检/%s" % m["name"], "why": m.get("why") or "数据不足"})
    for sec in digest.get("sections", []):
        if sec.get("total") and not sec.get("known"):
            notes.append("「%s」全部分项本周都无数据，不能读作「正常」" % sec["title"])
    for f in (digest.get("collect_failures") or []) + (actions.get("collect_failures") or []):
        notes.append("采集失败：%s（%s）" % (f.get("label") or f.get("title"),
                                           f.get("error")))
    total_unknown = len(unknown)
    verdict = (VERDICT_CLEAN if not unknown and not notes
               else (VERDICT_ATTENTION if notes else VERDICT_UNKNOWN))
    return {
        "verdict": verdict,
        "reason": ("全部指标均有数据" if verdict == VERDICT_CLEAN
                   else "%d 项指标本周无数据%s"
                        % (total_unknown,
                           "；%d 处需注意" % len(notes) if notes else "")),
        "unknown_metrics": unknown,
        "notes": notes,
        "disclaimer": "无数据记「本周无」而非 0；分母不足记 unknown 不回落 0；"
                      "不能一键处理的事已显式标注，不伪装成按钮。",
    }


# ══════════════════════════════════════════════════════════════
# 汇总
# ══════════════════════════════════════════════════════════════
def build_report(conn: sqlite3.Connection, settings: Optional[dict] = None) -> dict:
    """生成自进化周报（**只读，绝不写**）。"""
    s = get_settings(settings)
    if not s["enabled"]:
        return {"ok": True, "enabled": False,
                "reason": "自进化周报已关闭（evolution_report.enabled=False）"}
    actions = build_actions(conn, s)
    digest = build_digest(conn, s)
    confidence = build_confidence(actions, digest)
    overall = _worst([actions["verdict"], confidence["verdict"]])
    return {
        "ok": True,
        "enabled": True,
        "title": "自进化周报",
        "question": "这一周，系统替我做了什么？我该做什么？",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "window_days": s["window_days"],
        "overall": overall,
        "overall_label": VERDICT_LABEL.get(overall, overall),
        "actions": actions,
        "digest": digest,
        "confidence": confidence,
    }


# ══════════════════════════════════════════════════════════════
# 文本渲染（CLI / 日志 / 冒烟用）
# ══════════════════════════════════════════════════════════════
def render_text(report: dict) -> str:
    if not report.get("enabled", True):
        return "[周报已关闭] %s" % report.get("reason", "")
    L = ["%s · %s" % (report["title"], report["question"]),
         "窗口 %d 天 · 生成于 %s" % (report["window_days"], report["generated_at"]),
         "=" * 62]
    a = report["actions"]
    L.append("")
    L.append("【要你决策的】 %d 项（可一键处理 %d 项）"
             % (a["count"], a["actionable_count"]))
    for it in a["items"][:10]:
        btn = ("[点「%s」]" % it["action"]["label"]) if it.get("action") \
            else "[只能看：%s]" % it.get("not_actionable_reason", "")
        L.append("  · [%s] %s  %s" % (it["source_label"], it["title"], btn))
        if it.get("detail"):
            L.append("      %s" % it["detail"][:120])
    if not a["items"]:
        L.append("  （本周无）")
    L.append("")
    L.append("【本周体检】")
    for sec in report["digest"]["sections"]:
        L.append("  %s —— %s" % (sec["title"], sec["subtitle"]))
        for m in sec["metrics"]:
            if m["state"] == "ok" and m["delta"] is not None:
                L.append("    · %s：%d（上周 %d，%+d）"
                         % (m["name"], m["current"], m["previous"], m["delta"]))
            elif m["state"] == "ok":
                L.append("    · %s：%s" % (m["name"], m["current"]))
            else:
                L.append("    · %s：[本周无] %s" % (m["name"], m.get("why") or ""))
    cf = report["confidence"]
    L.append("")
    L.append("【数据可信度】 %s" % cf["reason"])
    for n in cf["notes"]:
        L.append("  ! %s" % n)
    L.append("")
    L.append("— " * 31)
    L.append(cf["disclaimer"])
    return "\n".join(L)


def to_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, default=str)