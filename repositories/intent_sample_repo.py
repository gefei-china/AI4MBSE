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

# 池内合法意图（与 IntentRouter.INTENTS 对齐；chat 也算有效样本，它是"该走闲聊"的正例）
VALID_INTENTS = ("requirement_analysis", "requirement_quality", "design", "impact",
                 "review", "report_generation", "system_mgmt", "knowledge_qa", "chat")
VALID_STATUS = ("new", "suggested", "confirmed", "rejected")


class IntentSampleRepo:
    def __init__(self, conn):
        self.conn = conn

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
            # 待标注的排前面（高频优先：seen_count 高说明是真正常用的说法），再按新近
            "CASE status WHEN 'new' THEN 0 WHEN 'suggested' THEN 1 ELSE 2 END, "
            "seen_count DESC, id DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        items = [dict(r) for r in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "stats": self.stats(), "intents": list(VALID_INTENTS)}

    def stats(self) -> dict:
        g = lambda sql: {r[0]: r[1] for r in self.conn.execute(sql).fetchall()}
        return {
            "by_status": g("SELECT status, COUNT(*) FROM intent_samples GROUP BY status"),
            "by_intent": g("SELECT intent, COUNT(*) FROM intent_samples "
                           "WHERE status='confirmed' GROUP BY intent"),
            "by_source": g("SELECT source, COUNT(*) FROM intent_samples GROUP BY source"),
            "confirmed": self.conn.execute(
                "SELECT COUNT(*) FROM intent_samples WHERE status='confirmed'").fetchone()[0],
        }

    def confirmed_cases(self) -> list:
        """评测集：**(text, intent) 列表**——只取人工确认过的，按 id 升序（与历史脚本逐例顺序对齐）。"""
        return [(r["text"], r["intent"]) for r in self.conn.execute(
            "SELECT text, intent FROM intent_samples WHERE status='confirmed' AND intent<>'' "
            "ORDER BY id").fetchall()]

    # ── 写入 ──
    def upsert(self, text: str, intent="", status="new", source="manual",
               hit_intent="", hit_route="", hit_conf=0.0, note="", created_by="") -> dict:
        """按 text 幂等写入：已存在则 seen_count+1 并（仅在传入非空时）更新字段。

        为什么按 text 去重：样本池的价值在"**不同**说法的覆盖度"，同一句话重复进来只会虚高样本量；
        seen_count 保留频次（页面据此把高频真实说法排前面，标注性价比最高）。
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reason": "empty_text"}
        if status not in VALID_STATUS:
            return {"ok": False, "reason": "bad_status"}
        cur = self.conn.execute("SELECT id, status FROM intent_samples WHERE text=?", (text,)).fetchone()
        if cur:
            sets, args = ["seen_count=seen_count+1", "updated_at=CURRENT_TIMESTAMP"], []
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
        """对 status='new' 的前 N 条跑**一次真实意图识别**，写入建议标签（status→suggested）。

        刻意做成"小批量、可重复触发"：一次跑全池会卡住请求（每条都要 embedding，冷启更慢），
        而人工确认本来就该分批进行。返回明细供页面逐条展示"系统建议 vs 人工待确认"。
        """
        rows = self.conn.execute(
            "SELECT id, text FROM intent_samples WHERE status='new' ORDER BY seen_count DESC, id LIMIT ?",
            (limit,)).fetchall()
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
            res = self.upsert(text, intent="", status=st, source="prod_import" if source != "messages" else "prod",
                              hit_intent=h[0], hit_route=h[1], hit_conf=h[2])
            n_new += (not res.get("updated")); n_upd += bool(res.get("updated"))
        return {"ok": True, "added": n_new, "updated": n_upd, "source": source,
                "scanned": len(rows), "hint_hit": sum(1 for r in rows if hints.get((r["t"] or "").strip()))}

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