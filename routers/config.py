"""系统配置管理路由：静态配置（core.config）+ 动态配置（settings 表）统一维护入口。

接口：
- GET  /api/system/config            查看当前生效配置（静态脱敏 + 动态 runtime + 来源说明）
- GET  /api/system/config/schema     配置项元数据（类型/描述/当前值/默认值，供前端渲染表单）
- PUT  /api/system/config/static     修改静态配置（写系统级配置文件并即时 reload）
- PUT  /api/system/config/runtime    修改动态配置（upsert settings 表）
- POST /api/system/config/reload     手动重载静态配置（配置文件被外部修改后使用）

静态配置项（zhiyuan.* / llm.* / mcp.* / app.* / database.*）写 ~/.workbuddy/mbse_config.json，
优先级低于环境变量（环境变量仍可覆盖）；host/port/db path 等启动级参数需重启完全生效。
"""
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse

from core import config
from core.audit import audit
from core.deps import current_user, db_session
from models import StaticConfigIn, RuntimeConfigIn

router = APIRouter()


@router.get("/api/system/config")
def get_system_config(conn=Depends(db_session)):
    """当前生效配置：静态（脱敏）+ 动态（settings）+ 配置来源说明。"""
    return {
        "ok": True,
        "static": config.dump(mask_secrets=True),
        "runtime": config.runtime_settings(conn),
        "config_path": config.CONFIG_PATH,
        "levels": ["shadow(MBSE_SHADOW_CONFIG, 仅沙箱子进程)", "env", "system_config_file", "defaults"],
        "note": "host/port/database.path 为启动级参数，修改后需重启服务完全生效；其余项即时生效",
    }


@router.get("/api/system/config/schema")
def get_config_schema():
    """配置项元数据（前端渲染配置表单用）。"""
    return {"ok": True, "schema": config.schema()}


@router.put("/api/system/config/static")
def update_static_config(body: StaticConfigIn):
    """修改静态配置：校验 → 落盘系统级配置文件 → 即时 reload。"""
    try:
        result = config.save_override(body.updates)
        return {"ok": True, **result,
                "static": config.dump(mask_secrets=True),
                "note": "已即时生效；host/port/database.path 需重启服务完全生效"}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"保存配置失败: {str(e)[:200]}"}, 500)


@router.get("/api/system/config/think-suggest")
def get_think_suggestions():
    """任务级思考：按真库实测数据给出「建议关思考」的意图清单（**只读，不改任何配置**）。

    ★ 为什么要这个接口（而不是让用户自己查 SQL）：
      自校正判据的三条阈值（思考占比 / 平均产出 / 样本量）都是**可配**的，
      用户在界面上调完阈值后需要**立刻看到结果**，否则只能靠猜。

    ★ 为什么「只建议不自动改」：
      一次统计口径错误（比如某天embedding 降级导致 reasoning 全为 0）
      若直接改配置，就是**把统计错误变成线上行为变更**，风险不可控。
      ⇒ 采纳与否由人决定（界面点「加入 off 名单」也只是填进输入框，仍需手动保存）。
    """
    try:
        from llm.thinking_autotune import suggest_thinking_off
        items = suggest_thinking_off()
        return {"ok": True,
                "count": len(items),
                "suggestions": [{"intent": s["intent"], "n": s["n"],
                                 "ratio": round(float(s["ratio"]), 4),
                                 "avg_completion": round(float(s["avg_completion"]), 1)}
                                for s in items],
                "thresholds": {
                    "min_ratio": config.get("llm", "autotune_min_ratio", 0.5),
                    "max_output": config.get("llm", "autotune_max_output", 1200),
                    "min_samples": config.get("llm", "autotune_min_samples", 5),
                },
                "note": "只建议不自动改；采纳请用 POST /api/system/config/static 改 llm.thinking_policy"}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"计算建议清单失败: {str(e)[:200]}"}, 500)


@router.put("/api/system/config/runtime")
def update_runtime_config(body: RuntimeConfigIn, conn=Depends(db_session)):
    """修改动态配置（settings 表，界面可调参数）。"""
    try:
        result = config.update_runtime_settings(body.updates, conn)
        return {"ok": True, **result, "runtime": config.runtime_settings(conn)}
    except ValueError as e:
        # 白名单拒绝（未知/无消费点键）—— 不是"更新失败"，是"不允许写"
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"更新动态配置失败: {str(e)[:200]}"}, 400)


@router.post("/api/system/config/reload")
def reload_config():
    """手动重载静态配置（编辑配置文件后无需重启）。"""
    try:
        config.reload()
        return {"ok": True, "static": config.dump(mask_secrets=True)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"重载配置失败: {str(e)[:200]}"}, 500)


# ── P1-4：配置变更台账 + 回滚（回滚同样经 HIL 批准，不开自动后门）────────
@router.get("/api/config/history")
def config_history(section: str = "", limit: int = 100,
                   conn=Depends(db_session), user=Depends(current_user)):
    """配置变更台账（config_history）：每次经 HIL 批准的变更一行，旧值→新值可追溯。

    回滚的数据源：对某行发起 POST /api/config/rollback {history_id}。
    """
    from core import config_promotion as _promo
    rows = _promo.list_history(conn, limit=limit, section=section)
    return {"ok": True, "history": rows, "sources": [_promo.SRC_PROPOSAL,
                                                     _promo.SRC_ROLLBACK,
                                                     _promo.SRC_MANUAL]}


@router.post("/api/config/rollback")
def config_rollback(body: dict = Body(default={}),
                    conn=Depends(db_session), user=Depends(current_user)):
    """回滚某条台账：把该行 new_value 改回 old_value。**同样入 HIL 待批准队列**
    ——回滚也是配置变更，不留自动后门；批准后经 config_promotion 执行并落新台账。
    """
    from core import config_promotion as _promo
    history_id = int((body or {}).get("history_id") or 0)
    note = str((body or {}).get("note") or "").strip()
    if not history_id:
        return JSONResponse({"ok": False, "error": "缺少 history_id"}, 400)
    out = _promo.request_rollback(conn, history_id, note=note)
    if not out.get("ok"):
        return JSONResponse({"ok": False, "error": out.get("error")}, 400)
    audit(user, "config_rollback_request",
          "回滚台账 #%s → HIL 确认单 #%s" % (history_id, out.get("confirmation_id")),
          conn=conn)
    return {"ok": True, "confirmation_id": out.get("confirmation_id"),
            "note": "已入 HIL 待批准队列；批准后配置才回滚"}
