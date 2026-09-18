"""监控域模型（自 routers/monitor.py 原样搬移，P1-2 Models 外置）。"""
from pydantic import BaseModel


class AlertRuleIn(BaseModel):
    name: str
    metric: str = "success_rate"        # success_rate | avg_latency | error_count | mock_rate
    operator: str = ">"                 # > | >= | < | <=
    threshold: float = 0
    level: str = "warning"              # info | warning | critical
    notify_url: str = ""                # 告警 webhook（A2A 事件结构，空=仅记录）
    secret: str = ""
    status: str = "active"              # active | paused
