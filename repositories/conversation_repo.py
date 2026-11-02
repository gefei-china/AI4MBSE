"""对话域 Repository：conversations / messages / feedback 表。

对应 routers/conversations.py 的全部数据访问。
"""
from datetime import datetime
import json

from repositories.base import BaseRepo

#: 「该会话属于用户 u」判定的**放行域**。
#: ⚠️ 口径来源 = 2026-10-13 与米爸确认的三条（勿擅改，改前先看门禁
#:    `tools/verify/verify_conv_scope_guard.py` 的 E1~E4）：
#:  ① **user_id 为 NULL/0 的历史会话对所有人可见** —— 真库 316 条里 276 条 user_id 为 NULL，
#:     若严格过滤，米爸现有的历史会话会**当场从界面消失**。这类无归属数据没有"原主"可言，
#:     按"谁都能看"处理是唯一不丢数据的选择；后续可一键回填归属后再收紧。
#:  ② **匿名/本机模式（user 为 None）放行全部** —— 私有化离线部署是本仓既有形态
#:     （AGENTS.md 铁律），本机单用户使用不该被隔离挡住。
#:  ③ 其余情况严格 `c.user_id = ?`。
VISIBLE_USER_IDS_ALL = None   # 语义标记：user is None ⇒ 全部可见


def conv_visible_to(row: dict | None, user) -> bool:
    """★ 2026-10-13（B1-9）：会话可见性判据（单一事实源，端点与列表共用）。

    `user` 形如 `{"id": 1, ...}`（current_user 返回值）或 None（匿名/本机模式）。
    ⚠️ **只判"看得到"，不判"能不能改"** —— 写操作的鉴权仍走 require_permission，
    这里只解决"能不能看到别人的会话"（此前任何 conv_id 都可读/可删）。
    """
    if user is None:
        return True                       # 口径②：匿名/本机模式放行
    if not row:
        return False                      # 会话不存在 → 不可见
    uid = row.get("user_id")
    if not uid:                            # 口径①：历史无归属会话对所有人可见
        # ⚠️ 必须区分「**该字段为 NULL**」与「**整行取不到**」：`_own_conv` 在会话不存在时
        #   会先 `if not row` 兜住，但 `{}`（空 dict）也会落到这里。
        #   把"取不到行"当成"无归属"⇒ 放行，等于给不存在的 conv_id 开了一个可见性口子。
        #   判据用键是否存在：真实行必有 id 列，空 dict 没有。
        return "id" in row or "conversation_id" in row
    try:
        return int(uid) == int(user.get("id") or 0)
    except Exception:
        return False


class ConversationRepo(BaseRepo):
    """对话与消息数据访问。"""

    # ── conversations ──
    def list_conversations(self, user=None, project_id: str | None = None,
                            limit: int | None = None, offset: int = 0) -> list:
        """会话列表 + `grouped` 标记（1 = 已归属某个**仍存在**的项目）。

        2026-09-24 左侧项目管理：项目下建立的任务**只在项目分组内展示**，
        下方「任务」列表降级为「未分组任务」兜底 —— 判定必须由后端给，
        不能让前端拿 project_id 非空来自判：项目被移除（或历史脏数据指向已删项目）时
        project_id 可能成为**孤儿值**，前端无 projects 清单就分不清「该收」与「该显示」。

        ★ 2026-10-13（B1-9）新增两个**收窄**参数（默认 None = 行为不变，便于回退）：
          · `user`：按 user_id 收窄（口径见模块级 `conv_visible_to`）。**在 SQL 里过滤**，
            而不是取回全量再在 Python 里筛 —— 后者会让"看不到"变成"看不到但仍在库里可被逐个 id 捞"。
          · `project_id`：**只传具体值**时按项目收窄；传 `None` 表示不按项目过滤（现状），
            传 `""` 表示只看未分组会话。前端"当前工程"切换时传该参数。

        ★ 2026-10-16 性能优化（P1 后端）：
          · 把每行关联的 `(SELECT COUNT(*) FROM messages ...)` 相关子查询改为一次性
            `LEFT JOIN (SELECT conversation_id, COUNT(*) cnt ... GROUP BY) mc` 聚合 ——
            325 个会话原本要跑 325 次子查询，现在 messages 表只扫描一次。
          · 排序改为「置顶优先（pinned_at 非空在前），再按 updated_at 倒序」，支撑前端置顶。
          · 新增可选游标分页 `limit`/`offset`（默认 None = 全量，向后兼容 renderGnavProjects）。
        """
        sql = """
            SELECT c.*, u.display_name as user_name,
            COALESCE(mc.cnt, 0) as msg_count,
            CASE WHEN TRIM(COALESCE(c.project_id,'')) <> ''
                  AND EXISTS(SELECT 1 FROM projects p WHERE p.id=c.project_id)
                 THEN 1 ELSE 0 END as grouped
            FROM conversations c
            LEFT JOIN users u ON c.user_id=u.id
            LEFT JOIN (SELECT conversation_id, COUNT(*) cnt FROM messages GROUP BY conversation_id) mc
                   ON mc.conversation_id=c.id"""
        args: list = []
        wheres: list = []
        # 口径②：匿名/本机模式不加 user 条件（与改动前一致）
        if user is not None:
            wheres.append("(c.user_id IS NULL OR c.user_id=0 OR c.user_id=?)")
            args.append(int(user.get("id") or 0))
        if project_id is not None:
            if str(project_id).strip() == "":
                wheres.append("TRIM(COALESCE(c.project_id,'')) = ''")
            else:
                wheres.append("c.project_id=?")
                args.append(str(project_id))
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY (c.pinned_at IS NOT NULL) DESC, c.updated_at DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args += [int(limit), int(offset)]
        return self.rows(sql, args)

    def create_conversation(self, title: str, intent: str, user_id: int = 1,
                            project_id: str | None = None) -> int:
        """新建会话。**归属完全由调用方显式决定，后端不猜。**

        2026-09-20：project_id 不再依赖列的默认值（原为硬编码 `'project-satnet-broadband'`，
        使全部会话被静默归入一个已归档项目）。

        ⚠️ 2026-09-28（多工程 P0-2）**行为变更**：未显式指定时**不再回落**全局
        `settings.default_project_id`，直接落空串 = 「无工程会话」。
        原因：那个全局指针是**单行**、不分用户/标签页/会话 —— 两个标签页同时开着时，
        后切换工程的一方会覆盖它，另一方新建的会话就**静默错归属**到别人的工程下
        （这正是「多工程并发不闭环」的断点②）。现在「当前工程」是**页面级**状态
        （前端 `window._curProjectId`），新建会话时随请求显式带上；没带就是真的不需要工程
        （知识检索 / 问答等会话本来就不该被塞进某个工程 —— 用户 2026-09-28 口径）。
        """
        return self.execute(
            "INSERT INTO conversations (title, intent, user_id, project_id) VALUES (?,?,?,?)",
            (title, intent, user_id, project_id if project_id is not None else ""),
        )

    def set_conversation_project(self, conv_id: int, project_id: str) -> bool:
        """把会话归入项目（**收敛入口**：无工程会话 → 归属某项目）。

        与 create 时的归属是同一列，但语义不同：这是**事后**补归属（用户口径「一旦基于工程的
        会话，则需要收敛进项目中」）。传空串 = 解除归属（回到未分组任务）。
        返回是否命中（会话不存在 → False，由路由层转 404）。
        """
        n = self.execute(
            "UPDATE conversations SET project_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (project_id or "", conv_id),
        )
        return bool(n)

    def get_conversation(self, conv_id: int) -> dict | None:
        return self.one("SELECT * FROM conversations WHERE id=?", (conv_id,))

    def rename_conversation(self, conv_id: int, title: str) -> None:
        """会话重命名（V2.3 优化：会话管理-重命名）。"""
        self.execute(
            "UPDATE conversations SET title=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (title, conv_id),
        )

    # ── 2026-10-16 R1：会话级主 Agent（执行主体持久化）──
    def get_main_agent(self, conv_id: int) -> str:
        """读会话绑定的主 Agent 名（空串=未绑定）。"""
        try:
            row = self.one("SELECT main_agent FROM conversations WHERE id=?", (int(conv_id),))
            return (row or {}).get("main_agent") or ""
        except Exception:
            return ""

    def set_main_agent(self, conv_id: int, main_agent: str) -> bool:
        """写/覆盖会话绑定的主 Agent。传空串 = 解除绑定（回到自动识别）。

        ⚠️ 不动 `updated_at`：它与「重命名/发消息」同类语义都会扰乱"最近更新"排序基线，
           而改选主 Agent 是**配置动作**，不该把老会话顶到列表最前面（与 set_pinned 同口径）。
        """
        self.execute("UPDATE conversations SET main_agent=? WHERE id=?",
                     (str(main_agent or ""), int(conv_id)))
        return True

    def delete_conversation(self, conv_id: int) -> None:
        """删除会话：级联清理 feedback → messages → artifacts → conversations（V2.3 优化：会话管理-删除）。"""
        msg_ids = [r["id"] for r in self.rows("SELECT id FROM messages WHERE conversation_id=?", (conv_id,))]
        for mid in msg_ids:
            self.execute("DELETE FROM feedback WHERE message_id=?", (mid,))
        self.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
        self.execute("DELETE FROM artifacts WHERE conversation_id=?", (conv_id,))
        self.execute("DELETE FROM conversations WHERE id=?", (conv_id,))

    def set_pinned(self, conv_id: int, pinned: bool) -> bool:
        """置顶/取消置顶（P2 收藏）。pinned_at 非空=已置顶，存置顶时间戳；
        取消置顶置 NULL。不动 updated_at（置顶不应改变"最近更新"排序基线）。"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if pinned else None
        self.execute("UPDATE conversations SET pinned_at=? WHERE id=?", (ts, conv_id))
        return True

    # ── 会话维度 LLM 参数（2026-10-31）────────────────────────────────────
    # 与「模型维度 = 系统默认」严格分开：这里**只动 conversations**，
    # **绝不写 llm_providers**。米爸的原话：「基于会话进行设置时，只会对当前会话有影响，
    # 不会反向影响 LLM 维度的配置」。所以两个写入口在代码层就得是两条独立的 SQL，
    # 不能共用一个"保存参数"函数（共用就迟早有人把会话设置写进 provider）。
    # ── 会话 × 模型 维度的 LLM 参数（2026-11-01 修正）─────────────────────
    # ★ 米爸澄清：「会话中的设置是按照（会话+模型）维度来设置的，
    #   不是一个会话中所有模型按照同一套设置控制」。
    #   ⇒ 存储必须按 provider 分桶，否则"在 A 会话给 deepseek 设了关闭，
    #     切到 qwen 还是关闭" —— 那是会话维度，不是（会话×模型）维度。
    #
    # 结构（`by_model` 包一层是为了将来加 schema 版本不破坏解析）：
    #   {"by_model": {"71": {"thinking_level": "关闭"}, "104": {"thinking_level": "高"}}}
    #
    # ⚠️ 兼容老数据（2026-10-31 那版是**扁平** {"thinking_level": "高"}，不分模型）：
    #   读到扁平结构时按 legacy 处理 —— 视为"该会话所有模型共用"，
    #   但**只在读取时兼容，写入一律升级为 by_model**。存量实测 0 行，仍保留兼容
    #   以防生产库有数据（清空等于丢用户已做出的设置）。
    _LEGACY_KEYS = ("thinking_level", "context_window", "max_tokens", "temperature")

    @classmethod
    def _normalize_overrides(cls, raw: dict) -> tuple:
        """把存储结构归一化为 `(by_model_dict, is_legacy)`。"""
        if not isinstance(raw, dict) or not raw:
            return {}, False
        bm = raw.get("by_model")
        if isinstance(bm, dict):
            return {str(k): (v if isinstance(v, dict) else {}) for k, v in bm.items()}, False
        # 扁平（老格式）：键是参数名而非 provider id
        if any(k in cls._LEGACY_KEYS for k in raw.keys()):
            return {"__legacy__": dict(raw)}, True
        # 其它：当作已经是 {provider_id: {...}} 的裸映射（防御性兼容）
        numeric = {str(k): v for k, v in raw.items() if str(k).isdigit() and isinstance(v, dict)}
        if numeric:
            return numeric, False
        return {}, False

    def get_llm_overrides(self, conv_id: int, provider_id=None) -> dict:
        """读**该会话 × 该模型**的参数覆盖（`{}` = 全部沿用系统默认）。

        provider_id 为空时返回 `{}`（无法确定模型就无法确定该用哪套设置）——
        这比"回退到某套默认值"安全：回退会让用户看到不属于该模型的值。
        """
        row = self.one("SELECT llm_overrides FROM conversations WHERE id=?", (conv_id,))
        if not row:
            return {}
        try:
            raw = json.loads((row.get("llm_overrides") or "{}"))
        except Exception:
            return {}
        by_model, is_legacy = self._normalize_overrides(raw)
        if is_legacy:
            # 老数据：不分模型，对任何 provider 都生效（保持升级前行为）
            return dict(by_model.get("__legacy__") or {})
        if provider_id is None:
            return {}
        return dict(by_model.get(str(provider_id)) or {})

    def get_llm_overrides_all(self, conv_id: int) -> dict:
        """读该会话**全部模型**的覆盖（供接口一次性返回，前端切模型不用再请求）。"""
        row = self.one("SELECT llm_overrides FROM conversations WHERE id=?", (conv_id,))
        if not row:
            return {}
        try:
            raw = json.loads((row.get("llm_overrides") or "{}"))
        except Exception:
            return {}
        by_model, is_legacy = self._normalize_overrides(raw)
        if is_legacy:
            return {"__legacy__": dict(by_model.get("__legacy__") or {})}
        return by_model

    def set_llm_overrides(self, conv_id: int, provider_id, overrides: dict) -> None:
        """写**该会话 × 该模型**的参数（只动 conversations，**绝不写 llm_providers**）。

        ⚠️ 必须带 provider_id：不带就退化成"会话维度"，那是这次要修正的语义。
        不动 `updated_at`：改会话参数不算"会话有新内容"，
        否则用户只是看一眼设置，会话就会被顶到列表最前面。
        """
        if provider_id is None:
            raise ValueError("set_llm_overrides 需要 provider_id（会话×模型维度）")
        cur = self.get_llm_overrides_all(conv_id)
        # 老数据（__legacy__）在首次写入时按"迁移到当前模型"处理：
        #   用户既然在给某个模型调参，就把他之前那套值带到这个模型上，
        #   避免"改一个模型，之前的设置凭空消失"。
        legacy = cur.pop("__legacy__", None)
        bucket = dict(cur.get(str(provider_id)) or {})
        if legacy:
            merged = dict(legacy)
            merged.update(bucket)
            bucket = merged
        bucket.update(overrides or {})
        # 空值语义：值为 None/""/0 的键视为"恢复系统默认"⇒ 从桶里删掉
        for k in list(bucket.keys()):
            v = bucket[k]
            if v is None or v == "" or v == 0:
                bucket.pop(k, None)
        if bucket:
            cur[str(provider_id)] = bucket
        else:
            cur.pop(str(provider_id), None)
        payload = json.dumps({"by_model": cur}, ensure_ascii=False)
        self.execute("UPDATE conversations SET llm_overrides=? WHERE id=?", (payload, conv_id))

    def clear_llm_overrides_for(self, conv_id: int, provider_id) -> None:
        """只清**该模型**的会话参数（其它模型保持不动）。

        为什么不用 set_llm_overrides(conv_id, pid, {}) 代替：
          那个方法在 bucket 变空时会弹掉该键 —— 结果是对的，但它同时会把
          legacy 数据"迁移"到本模型再删，语义绕。这里写成显式删除，意图更清楚。
        """
        cur = self.get_llm_overrides_all(conv_id)
        cur.pop("__legacy__", None)
        cur.pop(str(provider_id), None)
        payload = json.dumps({"by_model": cur}, ensure_ascii=False)
        self.execute("UPDATE conversations SET llm_overrides=? WHERE id=?", (payload, conv_id))

    def set_llm_overrides_all(self, conv_id: int, by_model: dict) -> None:
        """整体覆盖该会话的 by_model 映射（供 clear_all 使用）。"""
        payload = json.dumps({"by_model": by_model or {}}, ensure_ascii=False)
        self.execute("UPDATE conversations SET llm_overrides=? WHERE id=?", (payload, conv_id))

    def search_conversations(self, user=None, q: str = "", limit: int = 100) -> list:
        """内容检索（P1 列表搜索「标题+内容」）：标题/意图 LIKE 或消息正文 LIKE。
        一次性聚合消息数（与 list_conversations 同款 LEFT JOIN），结果按 updated_at 倒序。"""
        like = "%" + (q or "") + "%"
        sql = """
            SELECT c.*, u.display_name as user_name,
                   COALESCE(mc.cnt, 0) as msg_count,
                   CASE WHEN TRIM(COALESCE(c.project_id,'')) <> ''
                        AND EXISTS(SELECT 1 FROM projects p WHERE p.id=c.project_id)
                       THEN 1 ELSE 0 END as grouped
            FROM conversations c
            LEFT JOIN users u ON c.user_id=u.id
            LEFT JOIN (SELECT conversation_id, COUNT(*) cnt FROM messages GROUP BY conversation_id) mc
                   ON mc.conversation_id=c.id
            WHERE (c.title LIKE ? OR c.intent LIKE ? OR EXISTS(
                    SELECT 1 FROM messages m WHERE m.conversation_id=c.id AND m.content LIKE ?))"""
        args = [like, like, like]
        if user is not None:
            sql += " AND (c.user_id IS NULL OR c.user_id=0 OR c.user_id=?)"
            args.append(int(user.get("id") or 0))
        sql += " ORDER BY c.updated_at DESC LIMIT ?"
        args.append(int(limit))
        return self.rows(sql, args)

    # ── messages ──
    def add_assistant_message(self, conv_id: int, content: str, msg_type: str = "text",
                              card_data: str = "{}") -> int:
        """追加一条 assistant 消息，返回 message_id（并把会话 updated_at 前移）。

        2026-09-25 新增用途：**流被中断/被新消息覆盖时的「已生成内容固化」**。
        此前中断只在浏览器 DOM 里渲染（`id:0`，无落库），刷新即丢——用户反馈
        「重新输入提交会终止之前的输出」的实际损失面就在这里。内容为空时由调用方
        先行拦截（此处不静默写空消息）。
        """
        mid = self.execute(
            "INSERT INTO messages (conversation_id, role, content, msg_type, card_data) "
            "VALUES (?, 'assistant', ?, ?, ?)",
            (conv_id, content, msg_type, card_data),
        )
        self.execute("UPDATE conversations SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (conv_id,))
        return int(mid)

    def list_messages(self, conv_id: int, limit: int | None = None, before_id: int | None = None) -> list:
        """按时间正序返回会话消息。

        性能分页（P0 修复：1565 条消息全量返回导致前端渲染卡死）：
        - limit：最多返回最近 limit 条（时间正序排列，即截取尾部）
        - before_id：只返回 id < before_id 的消息（向上翻页游标），与 limit 组合可逐批加载更早消息
        - 均不传 → 全量（兼容旧调用方行为）
        """
        if limit is None and before_id is None:
            return self.rows(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at",
                (conv_id,),
            )
        # 先取"最近 limit 条"（倒序取尾部再反转，保持时间正序）
        if before_id is not None and limit is not None:
            rows = self.rows(
                "SELECT * FROM messages WHERE conversation_id=? AND id<? ORDER BY id DESC LIMIT ?",
                (conv_id, before_id, limit),
            )
            return list(reversed(rows))
        if before_id is not None:
            rows = self.rows(
                "SELECT * FROM messages WHERE conversation_id=? AND id<? ORDER BY id DESC",
                (conv_id, before_id),
            )
            return list(reversed(rows))
        if limit is not None:
            rows = self.rows(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                (conv_id, limit),
            )
            return list(reversed(rows))
        return []

    def count_messages(self, conv_id: int) -> int:
        """会话消息总数（供前端判断是否还有更早消息可加载）。"""
        v = self.scalar("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conv_id,))
        return int(v or 0)

    def update_message_feedback(self, msg_id: int, fb_type: str) -> None:
        self.execute("UPDATE messages SET feedback=? WHERE id=?", (fb_type, msg_id))

    def create_feedback(self, msg_id: int, fb_type: str, context: str, created_by: str = "未登录") -> None:
        self.execute(
            "INSERT INTO feedback (message_id, feedback_type, context, created_by) VALUES (?,?,?,?)",
            (msg_id, fb_type, context, created_by),
        )

    # ── 2026-10-29：质量信号（quality）与评审动作（feedback）分列 ──
    def update_message_quality(self, msg_id: int, quality: str) -> None:
        """写消息级质量信号（up/down）。空串=清除评价。"""
        self.execute("UPDATE messages SET quality=? WHERE id=?", (quality, msg_id))

    def create_quality_feedback(self, msg_id: int, quality: str, context: str,
                                created_by: str = "未登录", conversation_id: int = 0) -> None:
        """落质量信号历史（signal_kind='quality'，与评审行可区分）。

        `conversation_id` 单列入库：online_eval 采样时按会话回查助手消息
        （ux_metrics 只有 conversation_id），质量信号自带 message_id ⇒ 采样更准。
        """
        self.execute(
            "INSERT INTO feedback (message_id, conversation_id, feedback_type, quality, "
            "signal_kind, context, created_by) VALUES (?,?,?,?,?,?,?)",
            (msg_id, conversation_id or None, "", quality, "quality", context, created_by),
        )

    def get_message_quality(self, msg_id: int) -> str:
        v = self.scalar("SELECT quality FROM messages WHERE id=?", (msg_id,))
        return str(v or "")
