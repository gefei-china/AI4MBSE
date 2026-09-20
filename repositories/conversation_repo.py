"""对话域 Repository：conversations / messages / feedback 表。

对应 routers/conversations.py 的全部数据访问。
"""
from repositories.base import BaseRepo


class ConversationRepo(BaseRepo):
    """对话与消息数据访问。"""

    # ── conversations ──
    def list_conversations(self) -> list:
        return self.rows("""
            SELECT c.*, u.display_name as user_name,
            (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) as msg_count
            FROM conversations c LEFT JOIN users u ON c.user_id=u.id
            ORDER BY c.updated_at DESC""")

    def create_conversation(self, title: str, intent: str, user_id: int = 1,
                            project_id: str | None = None) -> int:
        """新建会话。

        2026-09-20：project_id **不再依赖列的默认值**（原为硬编码 `'project-satnet-broadband'`，
        使全部会话被静默归入一个已归档项目）。未显式指定时取用户配置的默认项目；
        未配置则为空串 = 不归属任何项目（见 repositories.project_repo.resolve_project_id）。
        """
        if project_id is None:
            from repositories.project_repo import resolve_project_id
            project_id = resolve_project_id(self.conn)
        return self.execute(
            "INSERT INTO conversations (title, intent, user_id, project_id) VALUES (?,?,?,?)",
            (title, intent, user_id, project_id),
        )

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
