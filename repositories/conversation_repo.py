"""对话域 Repository：conversations / messages / feedback 表。

对应 routers/conversations.py 的全部数据访问。
"""
from repositories.base import BaseRepo


class ConversationRepo(BaseRepo):
    """对话与消息数据访问。"""

    # ── conversations ──
    def list_conversations(self) -> list:
        """会话列表 + `grouped` 标记（1 = 已归属某个**仍存在**的项目）。

        2026-09-24 左侧项目管理：项目下建立的任务**只在项目分组内展示**，
        下方「任务」列表降级为「未分组任务」兜底 —— 判定必须由后端给，
        不能让前端拿 project_id 非空来自判：项目被移除（或历史脏数据指向已删项目）时
        project_id 可能成为**孤儿值**，前端无 projects 清单就分不清「该收」与「该显示」。
        """
        return self.rows("""
            SELECT c.*, u.display_name as user_name,
            (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) as msg_count,
            CASE WHEN TRIM(COALESCE(c.project_id,'')) <> ''
                  AND EXISTS(SELECT 1 FROM projects p WHERE p.id=c.project_id)
                 THEN 1 ELSE 0 END as grouped
            FROM conversations c LEFT JOIN users u ON c.user_id=u.id
            ORDER BY c.updated_at DESC""")

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

    def delete_conversation(self, conv_id: int) -> None:
        """删除会话：级联清理 feedback → messages → artifacts → conversations（V2.3 优化：会话管理-删除）。"""
        msg_ids = [r["id"] for r in self.rows("SELECT id FROM messages WHERE conversation_id=?", (conv_id,))]
        for mid in msg_ids:
            self.execute("DELETE FROM feedback WHERE message_id=?", (mid,))
        self.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
        self.execute("DELETE FROM artifacts WHERE conversation_id=?", (conv_id,))
        self.execute("DELETE FROM conversations WHERE id=?", (conv_id,))

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

    def create_feedback(self, msg_id: int, fb_type: str, context: str, created_by: str = "王工") -> None:
        self.execute(
            "INSERT INTO feedback (message_id, feedback_type, context, created_by) VALUES (?,?,?,?)",
            (msg_id, fb_type, context, created_by),
        )
