"""LLM 配置域：/api/llm/providers, /api/integration/llm

说明：/api/integration/llm 是集成入口中查询 LLM Provider 的转发接口，
其逻辑完全依赖本域的 provider 查询，故归入本域（避免 routers 同层互引）。
"""
from typing import Optional
import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from core.deps import db_session, current_user
from repositories.llm_repo import LlmRepo
from llm import llm_client, llm_router
from core.audit import audit, audit_user
from models import ProviderIn, ProviderKeyIn

router = APIRouter(tags=["LLM 配置"])


def _check_context_window(cw) -> None:
    """上下文窗口轻量校验：合理范围 512 ~ 1000000，防误填为 0 导致调用约束异常。"""
    if cw is None:
        return
    if not (512 <= cw <= 1000000):
        raise ValueError("context_window 需在 512 ~ 1000000 之间")


def _mask_providers(providers: list) -> list:
    """API key 掩码（展示安全，原始值不回传）。"""
    for p in providers:
        if p.get("api_key"):
            p["api_key_masked"] = p["api_key"][:8] + "..." if len(p["api_key"]) > 8 else "***"
            p["api_key"] = ""
    return providers


@router.get("/api/llm/providers")
def list_providers(model_type: Optional[str] = None, conn=Depends(db_session)):
    """模型列表；?model_type=chat|embedding 可过滤（管理面板按类型切换）。

    D10：每条附带 tags/priority/budget_tokens 与已用 token（budget_used，供前端展示预算条）。
    """
    items = LlmRepo(conn).list_providers()
    for p in items:
        try:
            p["tags"] = json.loads(p.get("tags") or "[]")
        except Exception:
            p["tags"] = []
        try:
            p["model_params"] = json.loads(p.get("model_params") or "{}")
        except Exception:
            p["model_params"] = {}
        p["budget_used"] = llm_router.usage_of(conn, p["id"])
    if model_type:
        items = [p for p in items if p.get("model_type") == model_type]
    return _mask_providers(items)


@router.get("/api/llm/status")
def llm_status(conn=Depends(db_session)):
    """P0-2: 全局 AI 接入状态 —— 默认 provider、真实/Mock 模式、调用统计。

    供前端「AI 建模」顶部 chip 展示：真实 LLM 已接通 / Mock 降级中。
    """
    providers = _mask_providers(LlmRepo(conn).list_providers())
    # 顶部状态展示「对话默认模型」；向量默认由向量化服务（embedder）单独使用
    default = next((p for p in providers if p.get("model_type") == "chat" and p.get("is_default") == 1),
                   next((p for p in providers if p.get("model_type") == "chat"), providers[0] if providers else None))
    return {
        "connected": bool(default and default.get("api_key_masked")),
        "used_mock": llm_client.stats["last_used_mock"],
        "default_provider": default,
        "stats": dict(llm_client.stats),
    }


@router.get("/api/integration/llm")
def integration_llm(conn=Depends(db_session)):
    return _mask_providers(LlmRepo(conn).list_providers())


@router.post("/api/llm/providers")
def create_provider(p: ProviderIn, conn=Depends(db_session), user=Depends(current_user)):
    repo = LlmRepo(conn)
    try:
        _check_context_window(p.context_window)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    if p.is_default:
        repo.clear_default(p.model_type or "chat")
    pid = repo.create_provider(p.name, p.provider_type, p.base_url, p.api_key, p.model_name,
                               p.max_tokens, p.temperature, p.is_default, p.model_type, p.context_window,
                               json.dumps(p.tags or [], ensure_ascii=False),
                               int(p.priority or 0), int(p.budget_tokens or 0))
    audit(audit_user(user), "llm_create", f"添加模型: {p.name}（{p.model_type}）", conn=conn)
    return {"ok": True, "id": pid}


@router.put("/api/llm/providers/{pid}")
def update_provider(pid: int, p: ProviderIn, conn=Depends(db_session), user=Depends(current_user)):
    repo = LlmRepo(conn)
    try:
        _check_context_window(p.context_window)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    if p.is_default:
        repo.clear_default(p.model_type or "chat")
    repo.update_provider(pid, p.name, p.provider_type, p.base_url, p.model_name,
                         p.max_tokens, p.temperature, p.is_default, p.model_type, p.context_window,
                         json.dumps(p.tags or [], ensure_ascii=False),
                         int(p.priority or 0), int(p.budget_tokens or 0))
    if p.api_key:
        repo.update_provider_key(pid, p.api_key)
    audit(audit_user(user), "llm_update", f"更新模型#{pid}", conn=conn)
    return {"ok": True}


@router.patch("/api/llm/providers/{pid}/key")
def update_provider_key(pid: int, body: ProviderKeyIn, conn=Depends(db_session), user=Depends(current_user)):
    """仅更新 API 密钥（不回传明文；PUT 全量更新时 api_key 留空表示不改）。"""
    if not body.api_key.strip():
        return JSONResponse({"error": "api_key 不能为空"}, 400)
    LlmRepo(conn).update_provider_key(pid, body.api_key.strip())
    audit(audit_user(user), "llm_key_update", f"更新LLM#{pid} API密钥", conn=conn)
    return {"ok": True}


@router.patch("/api/llm/providers/{pid}/status")
def update_provider_status(pid: int, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """停用/启用模型：body={"status":"active"|"disabled"}。

    停用后 llm 客户端/智能路由均按 status='active' 过滤，不会继续被选中调用。
    """
    status = (body or {}).get("status")
    if status not in ("active", "disabled"):
        return JSONResponse({"error": "status 必须是 active 或 disabled"}, 400)
    repo = LlmRepo(conn)
    row = conn.execute("SELECT id,name FROM llm_providers WHERE id=?", (pid,)).fetchone()
    if not row:
        return JSONResponse({"error": "模型不存在"}, 404)
    repo.update_provider_status(pid, status)
    audit(audit_user(user), "llm_status_update",
          f"{'启用' if status=='active' else '停用'}模型#{pid}: {row['name']}", conn=conn)
    return {"ok": True, "status": status}


@router.patch("/api/llm/providers/{pid}/params")
def update_provider_params(pid: int, body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """保存模型参数模板（P0 平台化）：body={"model_params": {"temperature":0.3,"max_tokens":8192,"top_p":0.9}}"""
    import json as _json
    mp = (body or {}).get("model_params")
    if not isinstance(mp, dict):
        return JSONResponse({"error": "model_params 必须是 JSON 对象"}, 400)
    LlmRepo(conn).update_provider_params(pid, _json.dumps(mp, ensure_ascii=False))
    audit(audit_user(user), "llm_params_update", f"更新LLM#{pid} 模型参数", conn=conn)
    return {"ok": True, "model_params": mp}


@router.delete("/api/llm/providers/{pid}")
def delete_provider(pid: int, conn=Depends(db_session), user=Depends(current_user)):
    LlmRepo(conn).delete_provider(pid)
    audit(audit_user(user), "llm_delete", f"删除LLM#{pid}", conn=conn)
    return {"ok": True}


@router.post("/api/llm/providers/{pid}/test")
def test_provider(pid: int, conn=Depends(db_session), user=Depends(current_user)):
    result = llm_client.test_connection(pid)
    audit(audit_user(user), "llm_test", f"测试LLM#{pid}: {'成功' if result.get('ok') else '失败'}", conn=conn)
    return result


@router.post("/api/llm/providers/test")
def test_provider_form(body: dict, conn=Depends(db_session), user=Depends(current_user)):
    """编辑表单「测试」入口：用表单当前配置（未保存）直接连通性测试。

    配置来源为前端表单字段（name/base_url/api_key/model_name/model_type），不落库。
    """
    base_url = (body.get("base_url") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    model_name = (body.get("model_name") or "").strip()
    if not base_url or not model_name:
        return JSONResponse({"ok": False, "error": "Base URL 与模型名必填"}, 400)
    if not api_key:
        return JSONResponse({"ok": False, "error": "API Key 未配置，请先填写再测试", "mock": True}, 400)
    cfg = {
        "name": body.get("name") or "form-test",
        "provider_type": "openai_compat",
        "base_url": base_url,
        "api_key": api_key,
        "model_name": model_name,
        "model_type": body.get("model_type") or "chat",
        "context_window": 8192, "max_tokens": 512, "temperature": 0.3,
    }
    try:
        from llm.providers.openai_compat import OpenAICompatProvider
        impl = OpenAICompatProvider(cfg)
        result = impl.test_connection(cfg)
    except Exception as e:
        result = {"ok": False, "error": str(e)}
    audit(audit_user(user), "llm_test_form", f"表单测试LLM: {model_name}（{cfg['model_type']}）: {'成功' if result.get('ok') else '失败'}", conn=conn)
    return result
