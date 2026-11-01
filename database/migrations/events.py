# -*- coding: utf-8 -*-
"""事件驱动底座迁移（2026-11-01）—— ev_sources / ev_triggers / ev_workitems。

对应设计：docs/Super-agent方案评估与落地建议-20261101.md §3 P0-2 / P0-4。

纯新表 ⇒ 幂等 `CREATE TABLE IF NOT EXISTS`，老库启动即建，无补列迁移路径
（与 `_migrate_tool_idempotency` / `_migrate_agent_todos` 同范式）。

━━━ 三张表的职责边界（不要混）━━━
· `ev_sources`   —— **来源配置**：收谁、收什么范围、observe 还是 execute、策略版本。
                    这是"策略"，可被改。
· `ev_triggers`  —— **事件台账**：每个不可变事件一行，含处置结论（disposition）。
                    这是"事实"，**只增不改**（除了补 disposition/reason）。
· `ev_workitems` —— **工作单元**：一个被授权的动作，含授权快照。

⚠️ **为什么 Trigger 与 WorkItem 要分两张表**：
一个 Trigger（事件）可能**没有**对应 WorkItem（被过滤了），
一个 Trigger 也可能因策略放宽而**先观察后执行**（同一 Trigger 对应多个 WorkItem）。
合表 ⇒ 无法表达"事件存在但没开工"，也就无法回答"这条变更为什么没反应"。

━━ disposition 取值（每个都要能解释给人听）━━━
· received —— 已落库，尚未判定
· admitted —— 准入通过（可能已开工单）
· filtered —— 被准入规则滤掉（**必须带 reason**）
· duplicate—— 幂等命中（同一外部事件重复投递）
"""
from __future__ import annotations


def _migrate_event_tables(conn):
    """事件驱动底座三表（幂等建表）。

    ## 主键口径（为什么 id 是 TEXT）
    Trigger/WorkItem 的 ID 由外部稳定 ID 派生（sha256），天然是长字符串。
    用 INTEGER 主键会迫使我们另开一列存外部 ID ⇒ "去重判据"出现两处真源
    （主键 + 外部 ID 列），两者一旦漂移就会出现"同一事件长出两个工单"。
    ⇒ 直接用派生 ID 作主键：**去重 = 主键唯一性**，只有一处真源。

    ## 为什么 `INSERT OR IGNORE` 而不是"先查再插"
    读-判-写三步在并发下有 TOCTOU 窗口（两个线程都判"不存在"⇒ 都插 ⇒ 两行）。
    主键唯一约束 + OR IGNORE 让并发下也只有一个赢家。
    （与 `core/job_queue.py` 的 `job_key` 唯一约束同源判据。）

    ## 时间字段口径
    一律用 SQLite 的 `CURRENT_TIMESTAMP`（**UTC**），与本仓其它表一致。
    ⚠️ SQLite 的 CURRENT_TIMESTAMP 是 UTC，而 Python `datetime.now()` 是本地时间
    （本机 UTC+8）—— 两者相减会凭空多出 8 小时。
    ⇒ 任何跨表的时间比较必须在 SQL 内用 `julianday` 完成，
    不要把时间戳取回 Python 做差（同 `agent/orch_checkpoint.py` 的时区坑）。
    """
    c = conn.cursor()

    # ── 来源配置（策略）──
    c.execute("""CREATE TABLE IF NOT EXISTS ev_sources (
        kind TEXT PRIMARY KEY,              -- 来源种类（稳定标识，一旦有数据不可改语义）
        title TEXT DEFAULT '',             -- 展示名
        listen TEXT DEFAULT 'mentions',    -- mentions=只收明确@ | all=收全部
        allowed_json TEXT DEFAULT '[]',    -- 白名单（发起者 ID 数组，小写比较）
        scope_keys_json TEXT DEFAULT '[]', -- 只接收这些 scope（空数组=不接收任何 scope）
        mode TEXT DEFAULT 'observe',       -- observe=只记录不执行 | execute=真执行
        policy_version TEXT DEFAULT 'v1',  -- 策略版本（授权快照要记录它）
        enabled INTEGER DEFAULT 1,         -- 0=停用（消息继续等待，不丢）
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # ── 事件台账（事实，只增）──
    c.execute("""CREATE TABLE IF NOT EXISTS ev_triggers (
        id TEXT PRIMARY KEY,               -- 派生 ID（source|external_id|version 的 sha256）
        source TEXT NOT NULL,              -- 来源种类
        external_id TEXT DEFAULT '',       -- 外部系统原始 ID（人类可读，排障用）
        thread_key TEXT DEFAULT '',        -- 归属线程 source:scope
        actor TEXT DEFAULT '',             -- 发起者 ID（授权判定的唯一身份依据）
        actor_type TEXT DEFAULT 'human',   -- human | bot | system
        addressed TEXT DEFAULT '',         -- mention | direct | ''（是否明确@了 Agent）
        text TEXT DEFAULT '',              -- 事件正文（**不可信，不参与授权判定**）
        payload_json TEXT DEFAULT '{}',    -- 结构化附加数据（同样不可信）
        at TEXT DEFAULT '',                -- 事件发生时刻
        disposition TEXT DEFAULT 'received', -- received|admitted|filtered|duplicate
        reason TEXT DEFAULT '',            -- 过滤原因（filtered 时必填，供人追问）
        hit_count INTEGER DEFAULT 0,       -- 幂等命中次数（**观测**，不改写首次 disposition）
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_evtrg_thread ON ev_triggers(thread_key)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_evtrg_source ON ev_triggers(source)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_evtrg_disp ON ev_triggers(disposition)")

    # ── 补列（老库兼容）：CREATE TABLE IF NOT EXISTS 不会给已有表加列 ──
    # hit_count：幂等命中次数。**与首次 disposition 分开**——
    # 首次处置结论是事实（不可变），重投次数是观测（可累加）。
    # 混在一列 ⇒ 重投即抹掉事实（2026-11-01 实测抓到的真实缺陷）。
    cols = [r[1] for r in c.execute("PRAGMA table_info(ev_triggers)").fetchall()]
    if "hit_count" not in cols:
        c.execute("ALTER TABLE ev_triggers ADD COLUMN `hit_count` INTEGER DEFAULT 0")
        print("[init_db] 迁移: ev_triggers 增加列 hit_count")

    # ── 工作单元（授权动作）──
    c.execute("""CREATE TABLE IF NOT EXISTS ev_workitems (
        id TEXT PRIMARY KEY,               -- 派生 ID（trigger_id|scope 的 sha256）
        trigger_id TEXT DEFAULT '',        -- 来源 Trigger
        source TEXT DEFAULT '',            -- 来源种类
        thread_key TEXT DEFAULT '',        -- 归属线程
        scope TEXT DEFAULT '',             -- 允许范围（= thread_key 或更细）
        title TEXT DEFAULT '',             -- 人类可读标题
        action TEXT DEFAULT '',            -- 建议动作（observe 模式下**不执行**）
        actor TEXT DEFAULT '',             -- 授权快照①：谁触发的
        actor_type TEXT DEFAULT 'human',
        policy_version TEXT DEFAULT 'v1',  -- 授权快照②：依据哪一版策略
        strength TEXT DEFAULT 'none',      -- 授权快照③：explicit（明确请求）| inferred（推断）
        mode TEXT DEFAULT 'observe',       -- observe | execute
        state TEXT DEFAULT 'open',         -- open|running|done|failed|stopped
        result TEXT DEFAULT '',            -- 执行结果
        error TEXT DEFAULT '',             -- 失败原因
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_evwi_thread ON ev_workitems(thread_key)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_evwi_state ON ev_workitems(state)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_evwi_mode ON ev_workitems(mode)")

    # ── 补列（老库兼容）──
    # conversation_id：工单当前挂载的会话（跨会话接力时更新）。
    # ⚠️ 语义是"**并行认领**"而非"转移"：原会话仍可继续推进，
    #    新会话是第二个认领者 —— 两个会话同时跑同一工单 ⇒ 重复写图谱，
    #    所以接力必须由人显式发起，且工单状态机仍是唯一裁决。
    _wi_cols = [r[1] for r in c.execute("PRAGMA table_info(ev_workitems)").fetchall()]
    if "conversation_id" not in _wi_cols:
        c.execute("ALTER TABLE ev_workitems ADD COLUMN `conversation_id` INTEGER DEFAULT 0")
        print("[init_db] 迁移: ev_workitems 增加列 conversation_id")
    # updated_at：停滞检测的判据基准（看门狗按"多久没动"判停滞）。
    # ⚠️ 必须有：没有它就只能用 created_at ⇒ 工单一旦被推进过就永远不算停滞。
    if "updated_at" not in _wi_cols:
        c.execute("ALTER TABLE ev_workitems ADD COLUMN `updated_at` TEXT DEFAULT ''")
        print("[init_db] 迁移: ev_workitems 增加列 updated_at")

    conn.commit()
    print("[init_db] 迁移: ev_sources / ev_triggers / ev_workitems 已建立（事件驱动底座）")
