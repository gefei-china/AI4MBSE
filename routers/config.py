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
from core.deps import current_user, db_session, require_permission
from models import StaticConfigIn, RuntimeConfigIn

router = APIRouter()

# ── P0-5（2026-11-15）：配置读写权限门 + 变更留痕 ────────────────────────
# 权限域刻意**不放在 admin 域**：`require_permission` 把「admin 列表非空」视为超级用户，
# 给设计师补一个 admin 权限就等于放行全站所有管理操作。故新建独立域 system_config。
# 真库现状（2026-11-15）：17 个用户全是设计师 preset、且没有任何 role_id=82 的账号
# ⇒ 若不给 preset 补授权位，启用门的当天就是全员 403（D3 明确要求避免）。
# 匿名在 auth.enforce_login=False 下仍按既有兼容态放行；验收**不得**用匿名判门。
CFG_READ = require_permission("system_config", "read")
CFG_WRITE = require_permission("system_config", "write")


def _who(user) -> str:
    """留痕操作者；匿名不编造人名。"""
    if not user:
        return "未登录"
    return str(user.get("username") or user.get("display_name")
               or user.get("id") or "未登录")


def _mask(dotted: str, value) -> str:
    """secret 类配置的 before/after 一律脱敏，台账里绝不落明文密钥。"""
    sec, _, key = str(dotted).partition(".")
    meta = config.CONFIG_SCHEMA.get(sec, {}).get(key) or {}
    if str(meta.get("type") or "") == "secret":
        return "****"
    return "" if value is None else str(value)


def _log_changes(conn, entries, source: str, user, note: str) -> list:
    """逐键落 config_history（before / after / 操作者）。

    entries: [(section, key, old, new, applied)]；old/new 必须已由 _mask 脱敏。
    skipped_masked 之类的「未真正写入」用 applied=0 留痕，台账不撒谎。
    """
    from services.config_promotion import _insert_history
    ids = []
    for sec, key, old, new, applied in entries:
        ids.append(_insert_history(conn, sec, key, old, new, source, 0,
                                   _who(user), note, applied=bool(applied)))
    return ids


def _audit_best_effort(user, event: str, detail: str, conn) -> None:
    """审计旁路：审计失败不阻断配置写入，但必须留痕（兜底 except 不许静默）。"""
    try:
        audit(_who(user), event, detail, conn=conn)
    except Exception as e:
        print("[config] 审计写入失败（不阻断配置变更）：%s" % e)


@router.get("/api/system/config")
def get_system_config(conn=Depends(db_session), user=Depends(CFG_READ)):
    """当前生效配置：静态（脱敏）+ 动态（settings）+ 配置来源说明。"""
    from core import test_mode
    return {
        "ok": True,
        "static": config.dump(mask_secrets=True),
        "runtime": config.runtime_settings(conn),
        "config_path": config.CONFIG_PATH,
        # ★ 2026-11-27：本进程是否测试态、force_mock 到底生不生效。
        #   需求原话「mock 数据在真实使用中不要使用」——这里给出**可核的生效口径**，
        #   避免"配置文件里写着 true"被当成"真的在用 Mock"（反之亦然）。
        "guards": test_mode.describe(),
        "levels": ["shadow(MBSE_SHADOW_CONFIG, 仅沙箱子进程)", "env", "system_config_file", "defaults"],
        "note": "host/port/database.path 为启动级参数，修改后需重启服务完全生效；其余项即时生效",
    }


@router.get("/api/system/config/schema")
def get_config_schema(user=Depends(CFG_READ)):
    """配置项元数据（前端渲染配置表单用）。"""
    return {"ok": True, "schema": config.schema()}


@router.get("/api/system/budget-policy")
def get_budget_policy():
    """预算层级审计（P0-3，2026-10-30）。

    回答三个问题，每个都有机器可核的判据：
      1. **谁是真源** —— 四层（L1 整轮 / L2 编排批次 / L3 子任务 / L4 单次调用）
         每层的权威键、别名键、当前值、实际生效上限（同层取最严）；
      2. **哪些是假权威** —— 同层别名比真源更严（真源形同虚设）/ 父层严于子层
         （子层额度永远用不满）；
      3. **哪些是配置谎言** —— 零消费点的键（改了了不起任何作用）。
    """
    try:
        from core import budget_policy
        return budget_policy.audit()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"预算层级审计失败: {str(e)[:200]}"}, 500)


@router.put("/api/system/config/static")
def update_static_config(body: StaticConfigIn, conn=Depends(db_session),
                         user=Depends(CFG_WRITE)):
    """修改静态配置：校验 → 落盘 → 即时 reload → **逐键落 config_history**。

    P0-5：before 在写之前取（不是事后补算），secret 类前后都记 ****，
    被掩码护栏跳过的键记 applied=0，台账与真实落盘状态一致。
    """
    keys = [str(k) for k in (body.updates or {})]
    # ★ 2026-11-27（P0）：**服务态进程禁止**把 llm.force_mock 写成 true。
    #   事故复盘（conv 1000000246）：验证脚本在用户正在跑真实任务的服务上开了这个开关，
    #   该会话其后 11 次 LLM 调用全部变成 Mock 样板，用户拿到 841 字无关内容。
    #   开关没有作用域就是缺陷本身 ⇒ 此处在写入侧直接拒绝（生效侧 llm.chat 另有第二道闸）。
    #   需要 Mock 的场景（回归/门禁）请以 MBSE_TEST_MODE=1 启独立实例。
    from core import test_mode
    _bad = []
    for _k in keys:
        if _k.strip() != "llm.force_mock":
            continue
        _v = (body.updates or {}).get(_k)
        if str(_v).strip().lower() in ("1", "true", "yes", "on") and not test_mode.is_test_mode():
            _bad.append(_k)
    if _bad:
        return JSONResponse({
            "ok": False,
            "error": ("拒绝在服务态进程开启 llm.force_mock：Mock 内容一旦进入真实用户请求，"
                      "等于交付编造内容（2026-11-27 已发生一次：11 次真实调用被替换成固定样板）。"
                      "回归/门禁请以 MBSE_TEST_MODE=1 启动独立实例后测试。"),
            "rejected": _bad,
            "guards": test_mode.describe(),
        }, 409)
    before = {d: _mask(d, config.get(*d.split(".", 1))) if "." in d else ""
              for d in keys}
    try:
        result = config.save_override(body.updates)
        skipped = set(result.get("skipped_masked") or [])
        entries = []
        for d in keys:
            sec, _, key = d.partition(".")
            entries.append((sec, key, before.get(d, ""),
                            _mask(d, config.get(sec, key)), d not in skipped))
        ids = _log_changes(conn, entries, "api:static", user,
                           "PUT /api/system/config/static；共 %d 项" % len(entries))
        _audit_best_effort(user, "config_static_update",
                           "静态配置变更 %d 项：%s" % (len(entries), ",".join(keys[:20])), conn)
        return {"ok": True, **result, "history_ids": ids,
                "static": config.dump(mask_secrets=True),
                "note": "已即时生效；host/port/database.path 需重启服务完全生效"}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"保存配置失败: {str(e)[:200]}"}, 500)


@router.get("/api/system/config/think-suggest")
def get_think_suggestions(user=Depends(CFG_READ)):
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
def update_runtime_config(body: RuntimeConfigIn, conn=Depends(db_session),
                          user=Depends(CFG_WRITE)):
    """修改动态配置（settings 表，界面可调参数）→ 逐键落 config_history。"""
    keys = [str(k) for k in (body.updates or {})]
    before = config.runtime_settings(conn)
    try:
        result = config.update_runtime_settings(body.updates, conn)
        after = config.runtime_settings(conn)
        entries = [("__settings__", k,
                    str((before.get(k) or {}).get("value", "")),
                    str((after.get(k) or {}).get("value", "")), True)
                   for k in keys]
        ids = _log_changes(conn, entries, "api:runtime", user,
                           "PUT /api/system/config/runtime；共 %d 项" % len(entries))
        _audit_best_effort(user, "config_runtime_update",
                           "动态配置变更 %d 项：%s" % (len(entries), ",".join(keys[:20])), conn)
        return {"ok": True, **result, "history_ids": ids,
                "runtime": config.runtime_settings(conn)}
    except ValueError as e:
        # 白名单拒绝（未知/无消费点键）—— 不是「更新失败」，是「不允许写」
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"更新动态配置失败: {str(e)[:200]}"}, 400)


@router.post("/api/system/config/reload")
def reload_config(conn=Depends(db_session), user=Depends(CFG_WRITE)):
    """手动重载静态配置；重载本身也是一次配置动作，同样留痕。"""
    try:
        config.reload()
        ids = _log_changes(conn, [("__system__", "reload", "", "ok", True)],
                           "api:reload", user, "POST /api/system/config/reload")
        _audit_best_effort(user, "config_reload", "手动重载静态配置", conn)
        return {"ok": True, "history_ids": ids,
                "static": config.dump(mask_secrets=True)}
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


@router.post("/api/config/effect-check")
def config_effect_check(body: dict = Body(default={}),
                        conn=Depends(db_session), user=Depends(current_user)):
    """配置变更「改后效果」判定（2026-10-29）。

    补的是 P1-4 之后的空档：沙箱只证明"改前没改坏"，改完**有没有真的变好**
    此前无人回答。本端点用 `eval_metrics` 做前后等长窗口对比（方向敏感），
    给出 effective / ineffective / mixed / no_data / inconclusive。

    · 单条：`{history_id: 12}` → 判定该行（默认 dry-run，`apply=true` 才落库）
    · 批量：不传 history_id → 判定全部"已应用但未判定"的行
    """
    from core import config_effect as _ce
    apply = bool(body.get("apply"))
    hid = int(body.get("history_id") or 0)
    if hid:
        res = _ce.evaluate_effect(conn, hid,
                                  window_days=int(body.get("window_days") or
                                                  _ce.DEFAULT_WINDOW_DAYS))
        if apply and res.get("ok"):
            conn.execute(
                "UPDATE config_history SET effect_verdict=?, effect_detail=?, "
                "effect_checked_at=? WHERE id=?",
                (res["verdict"], res["reason"], res["checked_at"], hid))
            conn.commit()
        return res
    return _ce.evaluate_pending(conn, apply=apply,
                                limit=int(body.get("limit") or 20),
                                window_days=int(body.get("window_days") or
                                                _ce.DEFAULT_WINDOW_DAYS))


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
