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

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core import config
from core.deps import db_session
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
        "levels": ["env", "system_config_file", "defaults"],
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


@router.put("/api/system/config/runtime")
def update_runtime_config(body: RuntimeConfigIn, conn=Depends(db_session)):
    """修改动态配置（settings 表，界面可调参数）。"""
    try:
        result = config.update_runtime_settings(body.updates, conn)
        return {"ok": True, **result, "runtime": config.runtime_settings(conn)}
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
