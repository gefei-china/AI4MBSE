# -*- coding: utf-8 -*-
"""AI 设计工坊（定制化中心）域：/api/studio/*

由 tools/split_router_studio.py 从 routers/studio.py 机械切分而成（本文件是各分片共享的 router 与 helper 装配点）；⚠️ 切分脚本**已一次性执行完毕、不可重跑**（重跑会以薄入口为输入、覆盖本目录）—— 此后本文件按普通源码维护。"""
import json
import os
import re
import threading
import time
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse

from core.deps import db_session, require_permission, current_user
from core.config import STATIC_DIR
from repositories.studio_repo import StudioRepo
from repositories.agent_repo import AgentRepo
from core.audit import audit, audit_user
from agent import AgentRegistry
from agent.registry import sanitize_description, sanitize_capabilities
from workflows import ToolRegistry, FlowExecutor
from skills.parser import parse_skill_zip, extract_skill_package, SkillParseError
from plugin_system import store as plugin_store
from plugin_system.manifest import MARKET_TYPES, PLUGIN_TYPES, CAP_SLOTS
from models import (
    PromptIn,
    SkillIn,
    MCPIn,
    ToolIn,
    AgentIn,
    AgentToolIn,
    A2AIn,
    EventSubIn,
)

router = APIRouter(tags=["AI 设计工坊"])


# ---- 模块级常量（拆分自原文件中部）----
_VALID_HOOK_ACTIONS = ("block", "require_confirm", "warn")

# 市场暴露类型 —— 单一真理源：权威定义见 plugin_system.manifest.MARKET_TYPES。
# 2026-09-16 修复：此前此处为 ("skill","mcp","tool") 字面量，与 manifest 的
# PLUGIN_TYPES=("skill","mcp","bundle") 不一致，导致 manifest 校验拒绝 type=tool，
# 市场 kind=tool 路由恒定查不到任何条目（死路由）。改为 re-export 以杜绝再次漂移。
_VALID_MARKET_KIND = MARKET_TYPES

# ---- 模块级私有 helper（拆分自原文件中部，供各分片共用）----

def _version_ge(v: str, req: str) -> bool:
    """版本比较（v1.2 ≥ v1.0）：非数字段忽略，解析失败放行（不做强阻断）。"""
    def _num(s: str):
        parts = []
        for p in re.split(r"[.\-_]", s):
            p = re.sub(r"\D", "", p)
            parts.append(int(p) if p else 0)
        return parts or [0]
    try:
        return _num(v) >= _num(req)
    except Exception:
        return True


def _intent_rule_err(trigger: str, intent: str, weight) -> str | None:
    """意图规则字段校验：trigger 非空 ≤50、intent 非空、weight 正数。返回错误文案（None=通过）。"""
    if not trigger or not trigger.strip():
        return "trigger 必填"
    if len(trigger) > 50:
        return "trigger 不能超过 50 字符"
    if not intent or not intent.strip():
        return "intent 必填"
    if weight is not None:
        try:
            w = float(weight)
        except (TypeError, ValueError):
            return "weight 必须是数字"
        if w <= 0:
            return "weight 必须大于 0"
    return None


def _invalidate_intent_rules() -> None:
    """规则表变更后让**所有** IntentRouter 实例下次 detect 重载规则（即时生效、无需重启）。

    P0-3（2026-09-24 修复）：原实现是 `_agent.router._rules = None`，而
    `AgentPipeline.__init__` 里 `self.router = IntentRouter()`（**每实例新建**），
    两者不是同一个对象 —— 失效作用在请求路径根本用不到的实例上，异常还被
    `except: pass` 吞掉，表现为「规则改了要重启」。现改用类级版本号，与实例数无关。
    异常一律留痕（本仓纪律：静默兜底会让人去追不存在的"偶发"）。
    """
    try:
        from agent.intent import IntentRouter
        IntentRouter.invalidate_rules()
    except Exception:
        import traceback as _tb
        _tb.print_exc()


def _sanitize_agent_meta(data: dict) -> bool:
    """Task 12 安全治理：Agent 能力/描述元数据入库前清洗（写回入参）。

    - description：sanitize_description（截断 ≤200 / 去控制字符 / 去脚本与模板注入标记）
    - capabilities：sanitize_capabilities(allow_extra=True)（枚举白名单 + 自定义短词清洗）
    清洗幂等无害（合法值原样保留）；返回是否实际发生清洗（供写审计，避免合法保存刷屏）。
    """
    desc_raw = data.get("description") or ""
    caps_raw = data.get("capabilities")
    desc_clean = sanitize_description(desc_raw)
    caps_clean = sanitize_capabilities(caps_raw, allow_extra=True)
    data["description"] = desc_clean
    data["capabilities"] = caps_clean
    caps_raw_norm = caps_raw if isinstance(caps_raw, (list, tuple)) else []
    return desc_clean != desc_raw or caps_clean != caps_raw_norm


def _hook_err(body: dict) -> str | None:
    """钩子表单校验：name/tool_pattern/action 必填；action 枚举；pattern 含 * 校验。"""
    if not (body or {}).get("name", "").strip():
        return "钩子名称必填"
    if not (body or {}).get("tool_pattern", "").strip():
        return "匹配工具必填"
    action = (body or {}).get("action", "block")
    if action not in _VALID_HOOK_ACTIONS:
        return f"动作必须为 {'/'.join(_VALID_HOOK_ACTIONS)}"
    return None


def _uniq_cast_name(conn, kind, base):
    repo = StudioRepo(conn); arepo = AgentRepo(conn)
    def clash(n):
        if kind == 'skill': return repo.get_skill_by_name(n)
        if kind == 'tool':  return repo.get_tool_by_name(n)
        if kind == 'agent': return arepo.get_agent_by_name(n)
        return None
    if not clash(base): return base
    i = 2
    while clash(f"{base}{i}"): i += 1
    return f"{base}{i}"


def _plugin_to_market_dto(it: dict, user: dict | None = None) -> dict:
    """统一插件行 → 旧市场 API 响应结构（前端 loadMarketManage/loadSkillCombined 兼容）。

    数据源已切换为 plugins 表（市场统一，2026-08-31）：kind=type、name 用 label.zh_CN 显示名，
    builtin 判定：legacy 迁移种子（com.zhiyuan.legacy 或 平台内置）视为内置。
    2026-09-17：新增 is_mine（当前用户是否为提供者）—— 提供者在市场目录中
    视为「已安装」（消费判定本就包含自建已发布能力，无需重复安装），前端据此显示 ✓ 已安装。
    """
    m = it.get("manifest") or {}
    label = m.get("label") or {}
    src_ref = str(it.get("source_ref") or "")
    builtin = 1 if (src_ref.startswith("com.zhiyuan.legacy") or it.get("author_name") in ("平台内置",)) else 0
    _uid = (user or {}).get("id")
    is_mine = bool(_uid and it.get("author_id") and int(it["author_id"]) == int(_uid))
    return {
        "id": it.get("plugin_id"),
        "plugin_id": it.get("plugin_id"),
        "name": label.get("zh_CN") or it.get("name"),
        "slug": it.get("name"),
        "kind": it.get("type"),
        "type": it.get("type"),
        "description": it.get("description") or m.get("description", ""),
        "category": it.get("category") or label.get("category", "") or "未分类",
        "version": it.get("current_version"),
        "builtin": builtin,
        "pinned": it.get("pinned", 0),
        "install_count": it.get("install_count", 0),
        "installed": it.get("installed", False),
        "is_mine": is_mine,
        "status": it.get("status"),
        "status_label": it.get("status_label"),
        "scope": it.get("scope") or "personal",
        # 上架申请状态（2026-09-17 双轨合并）：新模型由 scope 表达待审，
        # 旧模型由 status='rejected' 表达驳回；统一映射为旧前端契约的
        # submitted / rejected，供分享审核列表与卡片徽标复用。
        "share_status": ("submitted" if it.get("scope") == "pending_public"
                         else "rejected" if it.get("status") == "rejected"
                         else ""),
        "source_ref": src_ref,
        "origin": "share" if it.get("scope") == "pending_public" or it.get("status") in ("submitted", "rejected") else "admin",
    }


def _find_plugin_by_display(conn, kind: str, name: str):
    """按显示名（label.zh_CN / name slug）或 plugin_id 与类型在 plugins 表查找（未删除）。

    2026-09-17 第二刀：兼容 plugin_id 入参。遗留前端（28-studio.js 卡片、分享审核表）
    一直传 plugins 表数值主键，而本函数只比对 name/label → 一律 404，
    表现为「按钮在、点了报未找到插件」。放宽到 plugin_id 后旧调用点也能命中；
    新前端统一改用 plugin_id，语义唯一。
    """
    if not name:
        return None
    # 先试 plugin_id（精确、索引命中）；再回退到遍历比对显示名
    hit = conn.execute(
        "SELECT * FROM plugins WHERE plugin_id=? AND type=? AND status!='removed'",
        (str(name), kind)).fetchone()
    if hit:
        d = dict(hit)
        try:
            d["manifest"] = json.loads(d.get("manifest_json") or "{}")
        except Exception:
            d["manifest"] = {}
        return d
    rows = conn.execute(
        "SELECT * FROM plugins WHERE type=? AND status!='removed'", (kind,)).fetchall()
    for r in rows:
        d = dict(r)
        try:
            m = json.loads(d.get("manifest_json") or "{}")
        except Exception:
            m = {}
        zh = ((m.get("label") or {}).get("zh_CN") or "")
        if name in (d.get("name"), zh):
            d["manifest"] = m
            return d
    return None


def _find_plugin_by_id(conn, pid: str) -> dict | None:
    row = conn.execute("SELECT * FROM plugins WHERE plugin_id=? AND status!='removed'", (pid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["manifest"] = json.loads(d.get("manifest_json") or "{}")
    except Exception:
        d["manifest"] = {}
    return d

__all__ = ['router', 'json', 'os', 're', 'threading', 'time', 'uuid', 'List', 'Optional', 'APIRouter', 'Depends', 'UploadFile', 'File', 'JSONResponse', 'StreamingResponse', 'db_session', 'require_permission', 'current_user', 'STATIC_DIR', 'StudioRepo', 'AgentRepo', 'audit', 'audit_user', 'AgentRegistry', 'sanitize_description', 'sanitize_capabilities', 'ToolRegistry', 'FlowExecutor', 'parse_skill_zip', 'extract_skill_package', 'SkillParseError', 'plugin_store', 'PromptIn', 'SkillIn', 'MCPIn', 'ToolIn', 'AgentIn', 'AgentToolIn', 'A2AIn', 'EventSubIn', '_VALID_HOOK_ACTIONS', '_VALID_MARKET_KIND', '_version_ge', '_intent_rule_err', '_invalidate_intent_rules', '_sanitize_agent_meta', '_hook_err', '_uniq_cast_name', '_plugin_to_market_dto', '_find_plugin_by_display', '_find_plugin_by_id']
