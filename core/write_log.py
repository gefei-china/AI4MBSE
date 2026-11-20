# -*- coding: utf-8 -*-
"""关键写操作的毫秒级运行日志（2026-11-19 P2-1）。

## 为什么要这个模块（真库事故 conv 999999928/929）

双会话事故发生在 08:43:56，但 uvicorn 日志**只有秒级、且没有 request_id**：
    INFO: 127.0.0.1:64550 - "POST /api/conversations HTTP/1.1" 200 OK
无法回答「两次 POST 是不是同一次发送的重复提交」「谁先谁后、差几毫秒」——
最后只能靠 DB 落库时间（只到秒）和消息内容逐字节比对来反推。

这不是「没有审计」：`audit_logs` 已经记了 request_id/IP/UA，但它是**审计表**
（哈希链、合规用），不是**排障用的即时运行日志**——审计记录要事后查表、
且没有毫秒时间。本模块补的是后者：

  · 毫秒时间戳 + request_id + 操作名 + 关键 id，一行一条，直接进 logs/；
  · 只记**写操作**（建会话 / 落消息 / 固化 / 恢复），高频读路径不记，避免刷屏；
  · fail-open：日志失败绝不影响主链路（与 stream_checkpoint 同纪律）。

## 与 audit 的分工

  audit_logs（core/audit.py）  = 谁、从哪来、做了什么（合规/溯源，事后查表）
  write_log（本模块）          = 什么时候、哪次请求、产生了哪个 id（排障/时序，即时 stdout）
"""
from __future__ import annotations

import datetime
import logging

_logger = logging.getLogger("mbse.write")


def request_id() -> str:
    """当前请求的 request_id；无 HTTP 语境时返回 '-'（线程/后台任务）。"""
    try:
        from core.audit import request_context
        return str(request_context().get("request_id") or "-")
    except Exception:
        return "-"


def stamp() -> str:
    """毫秒时间戳（本地时间）。不用 time.time() 手算，避免时区/精度歧义。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def wrote(op: str, **fields) -> None:
    """记一条关键写操作。字段用 `k=v` 拼接，值一律截断防刷屏。

    用法：wrote("conv_create", conv_id=cid, client_rid=rid[:8])
    输出：2026-11-19 17:20:31.042 [a1b2c3d4] conv_create conv_id=123 client_rid=ab12cd34
    """
    try:
        parts = []
        for k, v in fields.items():
            s = str(v).replace("\n", " ")[:80]
            parts.append("%s=%s" % (k, s))
        tail = (" " + " ".join(parts)) if parts else ""
        _logger.warning("%s [%s] %s%s", stamp(), request_id(), op, tail)
    except Exception:
        pass   # 日志失败绝不影响写操作本身
