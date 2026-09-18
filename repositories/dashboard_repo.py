"""仪表盘域 Repository：跨表聚合统计（只读）。

对应 routers/dashboard.py 的数据访问。跨 generation_history /
conversations / relations / entities / merge_requests / documents 等表，
独立成 repo 避免塞入 meta。
"""
from repositories.base import BaseRepo


class DashboardRepo(BaseRepo):
    """工作台仪表盘统计。"""

    def dashboard_stats(self, actor_name: str | None = None) -> dict:
        """一次请求聚合全部统计指标（KPI + 待办 + 资产 + 最近对话 + 最近操作）。

        - KPI：待确认 AI 生成项 / 进行中对话 / 冲突告警 / 预评审评分
        - todo：合并请求待评审、知识评审待办（domain_review_queue + v2g_candidates）
        - assets：实体 / 关系 / 文档 / 模型统计
        - recent_conversations：最近更新的对话（工作台一键进入）
        - recent_activities：当前用户的最近操作轨迹（audit_logs，按显示名匹配）
        """
        pending_confirm = self.count("generation_history", "status='pending'")
        active_convs = self.count("conversations", "status='active'")
        conflicts = self.count("relations", "relation_type='CONFLICTS' AND status='candidate'")
        pending_merge = self.count("merge_requests", "status IN ('draft','open')")
        kb_review = self.count("domain_review_queue", "status='pending'") + \
            self.count("v2g_candidates", "status='pending'")
        reviewed = self.count("entities", "status='reviewed'")
        candidates = self.count("entities", "status='candidate'")
        total_entities = self.count("entities")
        total_relations = self.count("relations")
        total_docs = self.count("documents")
        total_models = self.count("llm_providers")
        recent_conversations = self.rows(
            "SELECT id, title, intent, status, updated_at FROM conversations "
            "ORDER BY updated_at DESC, id DESC LIMIT 5"
        )
        # 最近操作轨迹：当前用户最近 5 条审计记录（audit_logs.user_name = 显示名）
        recent_activities = []
        if actor_name:
            recent_activities = self.rows(
                "SELECT event_type, detail, created_at FROM audit_logs "
                "WHERE user_name=? ORDER BY id DESC LIMIT 5",
                (actor_name,),
            )
        return {
            "pending_confirm": pending_confirm,
            "active_conversations": active_convs,
            "conflicts": conflicts,
            "review_score": 82,
            "kb_stats": {
                "reviewed": reviewed,
                "candidate": candidates,
                "total": total_entities,
            },
            "todo": {
                "pending_generate": pending_confirm,
                "pending_merge_review": pending_merge,
                "conflicts": conflicts,
                "knowledge_review": kb_review,
            },
            "assets": {
                "entities": total_entities,
                "relations": total_relations,
                "documents": total_docs,
                "models": total_models,
            },
            "recent_conversations": recent_conversations,
            "recent_activities": recent_activities,
        }
