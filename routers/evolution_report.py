# -*- coding: utf-8 -*-
"""自进化周报 API：把六个域的既有台账汇成**一页可行动摘要**（只读）。

## 产品口径（2026-10-20 价值感知收官）
自进化六环全部交付后，能力都在，但用户只能逐个端点查 JSON——没有任何一个
地方能回答「这一周系统替我做了什么、我该做什么」。本路由是那一页。

## 三段信息架构（产品版，取代原型的四段「省/抓/守/欠」）
原型按系统内部模块分四段（archive域 / curriculum域 / sandbox域 / 拓扑域）。
那是**模块视角不是用户视角**——用户不关心域，只问三件事：

    1. 哪条要我点？        ⇒ GET /api/evolution-report 的 actions 段
    2. 这周比上周好了吗？  ⇒ digest 段（环比，不是孤立快照）
    3. 这数字是真的吗？    ⇒ confidence 段（显式列出所有「本周无」）

域的划分退到 digest 段内部作体检分区——那才是它该在的位置。

## 铁律
**只读。** 本路由与引擎合起来没有任何写路径——周报是镜子不是手。它也**不
代替人做决定**：只有 curriculum 有真正的 decide 端点，所以只有那批条目
带可执行 action；HIL 待确认与拓扑缺陷至今**没有**处理端点，引擎显式标注
`not_actionable_reason`，前端也不渲染成可点按钮。把不能点的事装成能点的
按钮就是欺骗。

## 失败纪律
承 P2-1 教训：端点失败一律 `ok:False + error` 可见，不裸奔 500。

端点：
    GET  /api/evolution-report              周报总览（三段+ 置信度声明）
    GET  /api/evolution-report/text         文本版（CLI / 日志 / 冒烟用）
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.deps import db_session

router = APIRouter(tags=["自进化周报"])


@router.get("/api/evolution-report")
def evolution_report(conn=Depends(db_session)):
    """自进化周报总览：actions（要你决策的）/ digest（本周体检+环比）/
    confidence（哪些是真数据）。

    enabled=False 时返回 `{"ok":True,"enabled":False,"reason":...}`——
    这是配置关闭，**不是失败**，前端据此隐藏整块而不是显示红色错误条。
    """
    from core import evolution_report as er
    try:
        return er.build_report(conn)
    except Exception as e:                                   # noqa: BLE001
        return JSONResponse({"ok": False,
                             "error": "周报生成失败: %s: %s"
                                      % (type(e).__name__, e)}, 500)


@router.get("/api/evolution-report/text")
def evolution_report_text(conn=Depends(db_session)):
    """文本版周报（供 CLI 冒烟 / 运维日志 / 无JS 环境读取）。"""
    from core import evolution_report as er
    try:
        rep = er.build_report(conn)
    except Exception as e:                                   # noqa: BLE001
        return JSONResponse({"ok": False,
                             "error": "周报生成失败: %s: %s"
                                      % (type(e).__name__, e)}, 500)
    return {"ok": True, "enabled": rep.get("enabled", True),
            "text": er.render_text(rep)}