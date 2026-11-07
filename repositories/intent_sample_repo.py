# -*- coding: utf-8 -*-
"""意图样本池仓储（2026-09-26）：intent_samples 的读写 + 候选导入 + 评测集导出。

## 状态机（评测只吃 confirmed，这是本模块最要紧的约束）
    new ──suggest──> suggested ──confirm──> confirmed   （进评测集）
     │                  │                     │
     └──── reject ──────┴────────────────────> rejected（确认不是有效样本，永不进评测）
`intent` 在 suggested 阶段存的是**系统判定**（建议值，仅供人参考）；到 confirmed 时才是**人工标签**。
把两者分开存（`intent` vs `hit_intent`）是为了能在页面上直观看到"人标了什么 / 系统当时判了什么"，
也为了标完之后能复盘系统行为——若混用一列，事后无法区分"这是人标的还是机器猜的"。
"""
import json
from datetime import datetime

# 历史内置意图（**仅作为兜底**；不是白名单，见 `_dynamic_intents`）
# ⚠️ B5-2 之前这里是**唯一事实源**（硬编码 9 个），而库里 active Agent 有 19 个
#   ⇒ 页面上标注下拉**选不到** 10 个真实意图（含 8 阶段 Agent 全家 + 主 Agent + 多方案生成）。
#   且**写入侧从不校验** intent（只有 status 校验）⇒ 这不是安全边界，只是"下拉候选集"。
#   ⇒ 现在改为**从平台能力表动态派生**，这份常量只保证「数据库不可用时页面仍能渲染」。
VALID_INTENTS = ("requirement_analysis", "requirement_quality", "design", "impact",
                 "review", "report_generation", "system_mgmt", "knowledge_qa", "chat")
VALID_STATUS = ("new", "suggested", "silver", "synth", "confirmed", "rejected")

# ── 标注档位（2026-11-07 第 19 轮）──────────────────────────────────────────
#   new       仅采集，系统还没给建议
#   suggested 系统给了建议（弱标注），**等人确认** —— 这才是真正的人工待办
#   silver    双视图独立标注一致（`created_by='dual_label_v1'`），已定档，用于**回归集**
#   synth     手写改写（`source='aug'`），标签由构造决定，用于**边界覆盖**
#   confirmed 人工确认 —— **唯一**的金标、**唯一**进评测集
#   rejected  人工判定无效
#
# 为什么必须把 silver/synth 从 suggested 里拆出来（实测依据）：
#   491 条 `suggested` 里 **347 条是银标、17 条是合成**，真待人工的只有 **127 条**。
#   挤在一个状态里 ⇒ 页面「待确认 491」把两档已定档样本也算成人工待办，
#   而评测跑批器（`tools/intent_eval_snapshot.py`）早已按 gold/silver/synth **三集分报**。
#   状态字段与评测口径必须同构，否则"待办数"和"评测集"各说一套、都不可信。
#: 状态 → 评测档位（与 `tools/intent_eval_snapshot.py::SETS` 一一对应）
STATUS_TIER = {"confirmed": "gold", "silver": "silver", "synth": "synth"}


def tier_of(source: str = "", created_by: str = "") -> str:
    """按**标签来源**决定一条新样本落在哪一档。纯函数、无副作用，便于门禁直接断言。

    判定顺序**不可交换**：`aug` 先判。评测跑批器的 `silver` 集显式排除 `aug`
    （`COALESCE(source,'')<>'aug'`）；若先判 `dual_label`，那 183 条「aug + 双视图一致」
    会同时落进 silver 与 synth 两集 ⇒ 同一批题在两条指标里各算一次，分数互相污染。
    """
    if (source or "").strip() == "aug":
        return "synth"
    if (created_by or "").strip() == "dual_label_v1":
        return "silver"
    return "suggested"


#: 「需要补建议」的候选集：**判据是"有没有标签"**，不是"状态是不是 new"。
#: 抽成模块常量（而不是写在方法体里）的唯一理由：门禁可以用**同一条 SQL** 做行为级断言
#: （造一行 suggested+空 intent，断言它被选中、silver/synth 不被选中），
#: 不必把 SQL 抄第二份 —— 抄一份就等于埋一处漂移。
PENDING_SUGGEST_SQL = (
    "SELECT id, text FROM intent_samples "
    "WHERE status='new' OR (status='suggested' AND COALESCE(intent,'')='') "
    "ORDER BY seen_count DESC, id LIMIT ?")


class IntentSampleRepo:
    def __init__(self, conn):
        self.conn = conn

    # ── 意图候选集（B5-2：动态派生）────────────────────────────────────
    def _dynamic_intents(self) -> list:
        """标注/筛选下拉的候选意图 = **意图字典 ∪ 平台能力 ∪ 已在用的历史值**。

        ★ 2026-11-07（米爸第 17 轮）：**首选 `intent_taxonomy`（意图字典）**。
          此前只有 agents 派生 ⇒ 用户反馈"只能单选、无法新增和编辑"：
          因为意图不是独立实体，想加一个"还没对应 Agent 的意图"根本无处可存。
          现在字典是主来源（可增可改可停用），agents 与历史值作补齐，常量作兜底。

        四源合并（任一即可，避免单一来源漏）：
          ① `intent_taxonomy`（active/deprecated 都出，deprecated 供历史标注复核）
          ② `agents.status='active'` 的 name —— 字典还没播种到的新 Agent
          ③ `intent_samples.intent` 的 DISTINCT 值 —— **存量样本一个都不能丢**
          ④ `VALID_INTENTS` 常量兜底 —— 数据库不可用时页面仍能渲染
        ⚠️ 查询失败**静默降级**到常量，不抛：这是列表接口的附属信息。
        """
        out = set(VALID_INTENTS)
        try:
            for r in self.conn.execute(
                    "SELECT name FROM intent_taxonomy WHERE status IN ('active','deprecated')"):
                if r[0]:
                    out.add(r[0])
        except Exception:
            pass
        try:
            for r in self.conn.execute(
                    "SELECT name FROM agents WHERE status='active'"):
                if r[0]:
                    out.add(r[0])
            for r in self.conn.execute(
                    "SELECT DISTINCT intent FROM intent_samples "
                    "WHERE intent IS NOT NULL AND intent!=''"):
                if r[0]:
                    out.add(r[0])
        except Exception:
            return sorted(out)
        return sorted(out)

    def _intent_labels(self) -> dict:
        """意图 → 中文显示名。**首选意图字典**（用户可编辑），再回落 agents.display_name。

        B5-2：动态加进来的意图在没有中文标签时前端只能显示英文名 ⇒ 后端一并给出。
        2026-11-07：字典的 label 是权威（用户可改），agents 只是它的初始来源。
        """
        out = {}
        try:
            for r in self.conn.execute(
                    "SELECT name, label FROM intent_taxonomy "
                    "WHERE label IS NOT NULL AND TRIM(label)<>''"):
                if r[0]:
                    out[r[0]] = str(r[1]).strip()
        except Exception:
            pass
        try:
            for r in self.conn.execute(
                    "SELECT name, display_name FROM agents WHERE status='active'"):
                # 字典没给中文名的才用 Agent 显示名
                if r[0] and r[1] and str(r[1]).strip() and r[0] not in out:
                    out[r[0]] = str(r[1]).strip()
        except Exception:
            pass
        return out

    # ── 查询 ──
    def list(self, status=None, intent=None, source=None, q=None, limit=200, offset=0) -> dict:
        """分页列表 + 计数统计（页面顶部徽章与筛选器用它，避免前端多次请求）。"""
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if intent:
            where.append("intent=?")
            args.append(intent)
        if source:
            where.append("source=?")
            args.append(source)
        if q:
            where.append("text LIKE ?")
            args.append("%" + q + "%")
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.conn.execute(f"SELECT COUNT(*) FROM intent_samples {w}", args).fetchone()[0]
        rows = self.conn.execute(
            f"SELECT * FROM intent_samples {w} ORDER BY "
            # 待标注的排前面（高频优先：seen_count 高说明是真正常用的说法），再按新近；
            # 定档但非金标（silver/synth）排在人工待办之后、已决（confirmed/rejected）之前。
            "CASE status WHEN 'new' THEN 0 WHEN 'suggested' THEN 1 "
            "WHEN 'silver' THEN 2 WHEN 'synth' THEN 3 ELSE 4 END, "
            "seen_count DESC, id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        items = [dict(r) for r in rows]
        # ★ 2026-11-07：附上**真实使用次数**（messages 表 role=user，排除测试会话）。
        #   原因见 usage_audit()：seen_count 曾因回填被刷高（71% 条目虚高），
        #   页面若只显示 seen_count 会误导标注优先级。两列并排，用户一眼看出差异。
        real = self.real_usage_counts()
        for it in items:
            it["real"] = int(real.get(it.get("text") or "", 0))
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "stats": self.stats(),
                # B5-2：动态候选集（不再硬编码 9 个）+ 中文标签
                "intents": self._dynamic_intents(),
                "intent_labels": self._intent_labels()}

    def stats(self) -> dict:
        g = lambda sql: {r[0]: r[1] for r in self.conn.execute(sql).fetchall()}
        by_status = g("SELECT status, COUNT(*) FROM intent_samples GROUP BY status")
        # 人工待办的**唯一定义**：既没定成金标、也没定档为银标/合成、也没被驳回的行。
        # 页面禁止自己再写一遍 `new+suggested` —— 口径写两处必然漂移（本轮就是因为
        # 一处把 silver/synth 混进 suggested，才让"485 条待审"这个数字虚高 3.8 倍）。
        pending = sum(v for k, v in by_status.items() if k in ("new", "suggested"))
        return {
            "by_status": by_status,
            "by_intent": g("SELECT intent, COUNT(*) FROM intent_samples "
                           "WHERE status='confirmed' GROUP BY intent"),
            "by_source": g("SELECT source, COUNT(*) FROM intent_samples GROUP BY source"),
            "confirmed": self.conn.execute(
                "SELECT COUNT(*) FROM intent_samples WHERE status='confirmed'").fetchone()[0],
            # 评测三档（与 tools/intent_eval_snapshot.py::SETS 同构）
            "by_tier": {tier: by_status.get(st, 0) for st, tier in STATUS_TIER.items()},
            "pending_human": pending,
        }

    def confirmed_cases(self) -> list:
        """评测集：**(text, intent) 列表**——只取人工确认过的，按 id 升序（与历史脚本逐例顺序对齐）。"""
        return [(r["text"], r["intent"]) for r in self.conn.execute(
            "SELECT text, intent FROM intent_samples WHERE status='confirmed' AND intent<>'' "
            "ORDER BY id").fetchall()]

    # ── 写入 ──
    def upsert(self, text: str, intent="", status="new", source="manual",
               hit_intent="", hit_route="", hit_conf=0.0, note="", created_by="",
               bump_seen: bool = True) -> dict:
        """按 text 幂等写入：已存在则 seen_count+1 并（仅在传入非空时）更新字段。

        为什么按 text 去重：样本池的价值在"**不同**说法的覆盖度"，同一句话重复进来只会虚高样本量；
        seen_count 保留频次（页面据此把高频真实说法排前面，标注性价比最高）。

        ★ 2026-11-07（米爸第 17 轮）：**`bump_seen` 必须区分"真实使用"与"回填导入"**。
          实测缺陷：`text='请检索标准库并生成视图'` 的 `messages` 真实出现次数是 **0**
          （仅在 2 个测试会话里各 1 次），但 `intent_samples.seen_count=61` ——
          因为 `import_source` 每导入一次就对已存在的行 +1，反复导入把频次刷高了 60 次。
          全池统计：**390/547 条（71%）看到达次数高于真实出现次数**，seen 总和 1098
          而 messages 真实 user 消息只有 473。
          ⇒ 用 seen_count 当"真实使用频次"排序是错的（上一轮审核清单就排错了）。
          修法：`import_source`（回填）传 `bump_seen=False`，只有真实请求路径
          （`collect()` / `record_override()`）才 +1。
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reason": "empty_text"}
        if status not in VALID_STATUS:
            return {"ok": False, "reason": "bad_status"}
        cur = self.conn.execute("SELECT id, status FROM intent_samples WHERE text=?", (text,)).fetchone()
        if cur:
            sets = ["updated_at=CURRENT_TIMESTAMP"]
            if bump_seen:
                sets.insert(0, "seen_count=seen_count+1")
            args = []
            # 已确认的行**不被自动采集覆盖**（人标过优先级最高）；其余允许补建议值
            if cur["status"] != "confirmed":
                if intent:
                    sets += ["intent=?", "status=?"] if status in ("confirmed", "suggested") \
                        else ["hit_intent=?"]
                    args += [intent, status] if status in ("confirmed", "suggested") else [intent]
                if hit_intent:
                    sets.append("hit_intent=?"); args.append(hit_intent)
                if hit_route:
                    sets.append("hit_route=?"); args.append(hit_route)
                if hit_conf:
                    sets.append("hit_conf=?"); args.append(float(hit_conf))
                if note:
                    sets.append("note=?"); args.append(note)
            self.conn.execute("UPDATE intent_samples SET " + ", ".join(sets) + " WHERE text=?",
                              args + [text])
            self.conn.commit()
            return {"ok": True, "id": cur["id"], "updated": True}
        cur = self.conn.execute(
            "INSERT INTO intent_samples (text, intent, status, source, hit_intent, hit_route, "
            "hit_conf, note, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (text, intent, status, source, hit_intent, hit_route, float(hit_conf or 0), note, created_by))
        self.conn.commit()
        return {"ok": True, "id": cur.lastrowid, "updated": False}

    def update(self, sid: int, intent=None, status=None, note=None) -> dict:
        """改标注/状态（页面行内编辑、勾选批量确认都走它）。"""
        if status is not None and status not in VALID_STATUS:
            return {"ok": False, "reason": "bad_status"}
        # 置为 confirmed 必须有标签：空标签的"确认"会污染评测集（会出现期望为空的用例）
        if status == "confirmed":
            cur = self.conn.execute("SELECT intent FROM intent_samples WHERE id=?", (sid,)).fetchone()
            if not cur:
                return {"ok": False, "reason": "not_found"}
            if not (intent or cur["intent"]):
                return {"ok": False, "reason": "confirm_requires_intent"}
        sets, args = ["updated_at=CURRENT_TIMESTAMP"], []
        for col, val in (("intent", intent), ("status", status), ("note", note)):
            if val is not None:
                sets.append(col + "=?"); args.append(val)
        self.conn.execute("UPDATE intent_samples SET " + ", ".join(sets) + " WHERE id=?", args + [sid])
        self.conn.commit()
        return {"ok": True}

    def delete(self, sid: int) -> dict:
        self.conn.execute("DELETE FROM intent_samples WHERE id=?", (sid,))
        self.conn.commit()
        return {"ok": True}

    def suggest_batch(self, limit: int = 40) -> dict:
        """对**还没有建议标签**的前 N 条跑一次真实意图识别，写入建议标签（→ suggested）。

        刻意做成"小批量、可重复触发"：一次跑全池会卡住请求（每条都要 embedding，冷启更慢），
        而人工确认本来就该分批进行。返回明细供页面逐条展示"系统建议 vs 人工待确认"。

        ★ 2026-11-07（第 19 轮）：**补上「suggested 但 intent 为空」的死区**。
          原查询只取 `status='new'`，而实测有 **118 条**处于「已是 suggested、却没有标签」
          （回填时带着 history hint 进了 suggested，但 hint 为空 ⇒ 既不会被补建议，
          也无法被人工确认 —— `update()` 要求 confirmed 必须有标签）。这批行**卡死在中间态**：
          页面上有、点"确认"报 `confirm_requires_intent`、点"补建议"扫不到。
          修法：判据从"看状态"改为"看**有没有标签**"（无标签即需要建议），
          与 tier 拆分后新增的 silver/synth 天然互斥（它们必有标签）。
        """
        rows = self.conn.execute(PENDING_SUGGEST_SQL, (limit,)).fetchall()
        if not rows:
            return {"ok": True, "updated": 0, "items": []}
        from agent.pipeline import AgentPipeline
        from agent.intent import IntentRouter
        pipe = AgentPipeline()
        pipe._load_db_agents()
        rt = pipe.router
        # 纪律（AGENTS.md 坑 26）：取"当前实现"的判定必须**绕开意图缓存**，否则拿到的是历史结论
        og, os_ = IntentRouter._cache_get, IntentRouter._cache_set
        IntentRouter._cache_get = lambda self, *a, **k: None
        IntentRouter._cache_set = lambda self, *a, **k: None
        out = []
        try:
            for r in rows:
                got = rt.detect(r["text"])
                meta = rt.get_last_meta()
                self.conn.execute(
                    "UPDATE intent_samples SET intent=?, status='suggested', hit_intent=?, hit_route=?, "
                    "hit_conf=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (got, got, meta.get("route", ""), float(meta.get("confidence") or 0), r["id"]))
                out.append({"id": r["id"], "text": r["text"], "suggested": got,
                            "route": meta.get("route", ""), "confidence": meta.get("confidence")})
        finally:
            IntentRouter._cache_get, IntentRouter._cache_set = og, os_
            self.conn.commit()
        return {"ok": True, "updated": len(out), "items": out}

    # ── 候选导入 ──
    def import_source(self, source: str, limit: int = 200, has_intent_hint=True) -> dict:
        """从既有数据回填候选（样本池"从哪里采样"的落地实现）。

        - `builtin`：内置 29 例评测集（硬编码在 tests/manual_verify/eval_intent_routing.py）
          → 以 **confirmed** 入库（它们本就是人工整理的标注，直接可评测）
        - `messages`：真实用户输入（`messages.content`，role='user'）→ suggested/new
        - `query_trace`：检索层记录的查询文本（含内部查询，需过滤）→ suggested/new
        可复用的历史判定：`intent_cache`（已判定过的 query→intent/route）直接作为建议值，省一次识别。
        """
        if source == "builtin":
            import importlib.util
            import os
            # 用**文件路径**加载而非 `import tests.manual_verify...`：tests/ 不是包（无 __init__.py），
            # 且私有化部署可能不带 tests/ —— 故按路径加载并容错，失败时给明确原因而不是抛栈。
            p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "tests", "manual_verify", "intent_cases.py")
            if not os.path.exists(p):
                return {"ok": False, "reason": "builtin_cases_unavailable", "path": p}
            spec = importlib.util.spec_from_file_location("_intent_cases", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            n_new = n_upd = 0
            for text, want in mod.BUILTIN_PAIRS:
                r = self.upsert(text, intent=want, status="confirmed", source="seed")
                n_new += (not r.get("updated")); n_upd += bool(r.get("updated"))
            return {"ok": True, "added": n_new, "updated": n_upd, "source": source}

        table = "messages" if source == "messages" else "query_trace"
        col = "content" if source == "messages" else "query"
        extra = " AND role='user'" if source == "messages" else ""
        rows = self.conn.execute(
            f"SELECT {col} AS t FROM {table} WHERE {col} IS NOT NULL AND LENGTH(TRIM({col}))>=4{extra} "
            f"GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT ?", (limit,)).fetchall()
        hints = {r[0]: (r[1], r[2], r[3]) for r in self.conn.execute(
            "SELECT query, intent, route, confidence FROM intent_cache").fetchall()}
        n_new = n_upd = 0
        for r in rows:
            text = (r["t"] or "").strip()
            # 长文本（需求书粘贴）与**系统包装文本**（澄清续跑）都不进单句样本池
            if not text or len(text) > 200 or _is_system_wrapper(text):
                continue
            h = hints.get(text.lower()) or hints.get(text) or ("", "", 0)
            st = "suggested" if (has_intent_hint and h[0]) else "new"
            # ★ bump_seen=False：回填是"发现了一条新说法"，不是"用户又说了一次"。
            #   详见 upsert 文档里 2026-11-07 的实测说明（seen_count 曾被刷到 61 而真实为 0）。
            res = self.upsert(text, intent="", status=st, source="prod_import" if source != "messages" else "prod",
                              hit_intent=h[0], hit_route=h[1], hit_conf=h[2],
                              bump_seen=False)
            n_new += (not res.get("updated")); n_upd += bool(res.get("updated"))
        return {"ok": True, "added": n_new, "updated": n_upd, "source": source,
                "scanned": len(rows), "hint_hit": sum(1 for r in rows if hints.get((r["t"] or "").strip()))}

    def real_usage_counts(self, exclude_test_convs: bool = True) -> dict:
        """★ 2026-11-07：返回 `{text: 真实出现次数}` —— 从 `messages` 表 `role='user'` 统计。

        为什么必须另算（而不是用 `intent_samples.seen_count`）：
          实测 390/547 条（71%）的 seen_count 高于真实出现次数（最极端的一条 61 vs 0），
          因为回填导入曾无脑 +1。用 seen_count 当"真实高频"排序会把
          测试脚本刷出来的句子排到最前，标注性价比全错。

        `exclude_test_convs=True` 时排除标题含 verify/测试/probe/验证 的会话 ——
          这些是验证脚本建的一次性会话（实测「请检索标准库并生成视图」的 2 次全在这里）。
          只影响**统计口径**，不删数据（保留可回溯）。
        """
        sql = ("SELECT m.content AS t, COUNT(*) AS n FROM messages m "
               "LEFT JOIN conversations cv ON cv.id = m.conversation_id "
               "WHERE m.role='user' AND m.content IS NOT NULL AND TRIM(m.content)<>''")
        if exclude_test_convs:
            sql += (" AND COALESCE(cv.title,'') NOT LIKE '%verify%' "
                    " AND COALESCE(cv.title,'') NOT LIKE '%测试%' "
                    " AND COALESCE(cv.title,'') NOT LIKE '%probe%' "
                    " AND COALESCE(cv.title,'') NOT LIKE '%验证%'")
        sql += " GROUP BY m.content"
        try:
            return {r[0]: int(r[1] or 0) for r in self.conn.execute(sql).fetchall()}
        except Exception:
            return {}

    def usage_audit(self) -> dict:
        """体检：`seen_count` 与真实出现次数的偏离程度（供门禁/页面显示"数据可信度"）。"""
        real = self.real_usage_counts()
        rows = self.conn.execute(
            "SELECT id, text, seen_count, status FROM intent_samples").fetchall()
        inflated, total_seen, total_real = [], 0, 0
        for r in rows:
            sc = int(r["seen_count"] or 0)
            rc = int(real.get(r["text"], 0))
            total_seen += sc
            total_real += rc
            if sc > rc:
                inflated.append({"id": r["id"], "text": (r["text"] or "")[:60],
                                 "seen": sc, "real": rc, "delta": sc - rc,
                                 "status": r["status"]})
        inflated.sort(key=lambda x: -x["delta"])
        return {"samples": len(rows), "inflated": len(inflated),
                "inflated_ratio": round(len(inflated) / max(len(rows), 1), 3),
                "seen_sum": total_seen, "real_sum": total_real,
                "top_inflated": inflated[:20]}

    def export(self) -> dict:
        """导出评测集（JSON）：既含 confirmed（可直接评测），也含待标注工作量统计。"""
        return {"schema": "intent-samples@1", "exported_at": datetime.now().isoformat(timespec="seconds"),
                "cases": [{"text": t, "intent": i} for t, i in self.confirmed_cases()],
                "stats": self.stats()}


def _is_system_wrapper(text: str) -> bool:
    """系统自己生成的"包装文本"，**不是用户原话** → 不得进样本池。

    实测来源（2026-09-26 人工核对候选时查出）：澄清卡片被回答后，续跑的请求会以
    `【澄清补充】用户已补充以下建模信息（请据此继续，无需再确认）：问题「…」回答「…」`
    的形态**存成 role='user' 的消息**，于是被「从历史回填」当成用户输入采了进来。
    这类文本进池的坏处：① 它不是任何人的真实说法，标签无从谈起（评测集会被污染）；
    ② 它极长且同构，会在列表里挤占位置；③ 拿它去标定，等于给"系统造的句子"调参。
    故在**采集与回填两处**都拦掉（而不是事后人工删）。
    """
    t = (text or "").lstrip()
    # 2026-09-26 补：`[任务上下文快照]` 是**编排子任务**的内部上下文（worker 用 `sub.execute_stream`
    # 跑子任务时同样会经过采集钩子）→ 实测被采进池 8 条，污染了"真实说法"样本集。
    return (t.startswith("【澄清补充】") or t.startswith("【系统】")
            or t.startswith("[任务上下文快照]") or t.startswith("[任务]"))


def collect(text: str, intent: str = "", route: str = "", conf: float = 0.0,
            source: str = "prod") -> bool:
    """真实请求路径上的**候选采集**（样本池的入口）——由 stream.py 在意图识别后调用。

    设计要点（每条都是踩过坑才这么写）：
    - **绝不抛异常、绝不拖慢请求**：自己在 try 里开短连接，任何失败静默返回 False。
      采集是"顺手做的事"，不能因为样本池写失败而让一次对话崩掉。
    - **只记弱标注**：写入的是当时系统自己的判定（hit_intent/hit_route/hit_conf），
      status='suggested' 而不是 'confirmed' —— 拿系统判定当标签去评测系统是自我循环，
      人工确认才是标签（详见模块头注释）。
    - **UNIQUE(text) 幂等**：同一句话只留一行并累加 seen_count，样本量按"不同说法"计
      （页面按 seen_count 降序 → 先标高频真实说法）。
    - 开关 `intent.sample_collect`：可整体关掉（压测/演示时不想污染样本池）。
    - 过滤：太短（<4 字符，多为"好""哦"）与过长（>200，粘贴的需求书）都不进**单句**样本池。
    """
    t = (text or "").strip()
    if not (4 <= len(t) <= 200) or _is_system_wrapper(t):
        return False
    try:
        from core import config as _cfg
        if not bool(_cfg.get("intent", "sample_collect", True)):
            return False
    except Exception:
        pass
    try:
        from database import get_db
        conn = get_db()
        try:
            IntentSampleRepo(conn).upsert(t, intent="", status="suggested" if intent else "new",
                                          source=source, hit_intent=intent or "",
                                          hit_route=route or "", hit_conf=conf or 0)
            return True
        finally:
            conn.close()
    except Exception:
        return False


def metrics(y_true: list, y_pred: list) -> dict:
    """与 `tests/manual_verify/eval_intent_routing.py` **同一口径**的指标（accuracy / macro-F1 / 混淆矩阵）。

    刻意与脚本保持同构 —— 页面看到的数字必须能和命令行复跑的结论对上，否则"两套指标"必然互相打架。
    """
    from collections import Counter, defaultdict
    labels = sorted(set(y_true) | set(y_pred))
    cm = defaultdict(Counter)
    for a, b in zip(y_true, y_pred):
        cm[a][b] += 1
    per, f1s = [], []
    for lb in labels:
        tp = cm[lb][lb]
        fp = sum(cm[a][lb] for a in labels if a != lb)
        fn = sum(cm[lb][b] for b in labels if b != lb)
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        f1s.append(f1)
        per.append({"intent": lb, "precision": round(p, 3), "recall": round(r, 3),
                    "f1": round(f1, 3), "n": tp + fn})
    acc = sum(1 for a, b in zip(y_true, y_pred) if a == b) / len(y_true) if y_true else 0.0
    return {"accuracy": round(acc, 4), "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
            "per_class": per, "confusion": {a: dict(cm[a]) for a in labels}, "n": len(y_true)}