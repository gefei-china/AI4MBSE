"""M5 HIL 人机协作强制：L2 写操作确认队列（未确认不生效）。

背景：HIL 分级此前仅写入 prompt 文本，靠 LLM 自觉；本模块在执行路径强制——
- is_write_tool：识别写类工具（entity_create 等），非写工具不受影响。
- queue_confirmation：L2 上下文发起写操作 → 生成待确认记录（payload 完整参数快照）。
- approve/reject：人工决策（approve 后调用方按 payload 真正执行；reject 丢弃）。
- pending：供前端确认队列面板轮询。

与 agent_tasks 解耦：hil_confirmations 独立表，支持 conversation / flow 两种场景。

★ 2026-10-13（文章 §06 幂等）：本模块此前只有一句悬空注释
  「# 幂等 ID 命名空间（防生成重复确认）」—— **它描述的常量并不存在**，
  `queue_confirmation` 是**无条件 INSERT**，同一请求被重复提交会产生多条待确认记录。
  真库实证（2026-10-13 只读）：`hil_confirmations` 6 行里 **id 13/14 是完全重复的一对**
  （agent_id / action / payload 全同）⇒ 已经在产生重复确认。
  现改为按**幂等键**去重，键的构造见 `idempotency_key()`。
"""
import hashlib
import json
from datetime import datetime, timedelta

# 写类工具白名单（其余默认只读）。新增写工具在此登记。
WRITE_TOOLS = {"entity_create", "relation_create", "entity_update", "entity_delete", "relation_delete"}

#: ★ 2026-10-28（写前确认 HITL）：确认单**状态机**的合法取值。
#:
#: 此前只有 `pending / approved / rejected` 三个值 ⇒ **无法区分"批了但没跑"与"跑成了"**
#: （前者是"批准后执行失败/未执行"，后者是"真的写进去了"）⇒ 审计与前端都看不出真相。
#: 扩展后：
#:   pending    —— 已入队，等待人工决策
#:   approved   —— 已批准，**尚未执行**（中间态）
#:   executed   —— 已批准**且执行成功**（result_json 有回执）
#:   failed     —— 已批准但**执行失败**（result_json 有错误）
#:   rejected   —— 被人工拒绝
#:   expired    —— 超时未响应（fallback_level 生效后）
#:   superseded —— 被同批次的后续确认单取代（批次合并用）
STATUSES = ("pending", "approved", "rejected", "executed", "failed", "expired", "superseded")

#: 需要**人点头**的 level（其余放行）。沿用 services/hil_policy.LEVELS 的取值域。
BLOCKING_LEVELS = ("confirm", "approve")

#: 默认超时（分钟）。与既有 `external_effect` 策略种子的 1440 分钟一致。
DEFAULT_TIMEOUT_MIN = 1440

#: ★ 执行侧写类判定的 match_key（策略表按它取档位）。
#:   本地工具与 MCP 工具**分开取键** ⇒ 智源平台的写操作可单独一档（D-4）。
MATCH_LOCAL_DESTRUCTIVE = "destructive"
MATCH_LOCAL_WRITE = "write"
MATCH_MCP_DESTRUCTIVE = "mcp_destructive"
MATCH_MCP_WRITE = "mcp_write"
MATCH_READ = "read"


def classify_action(name: str, side_effect: str = "") -> dict:
    """★ 执行侧写类判定（**双源**）—— 写前确认闸的唯一判据入口。

    返回 `{"match_key", "kind", "source"}`：
      · `kind` ∈ read / write / destructive
      · `match_key` —— 策略表取档位用的键（本地与 MCP 分开 ⇒ D-4 智源单独一档）

    ## 为什么必须双源
    `tools.side_effect` 只覆盖 `tools` 表里的工具（本地工具与 HTTP 集成）；
    **MCP 工具不在 `tools` 表** ⇒ 查不到即返回 "read"
    ⇒ 实测（2026-10-27）`create_model_elements` / `delete_model_elements` 等
    **全部被当成只读放行**（零人工确认直接执行）。
    ⇒ 必须叠加 MCP 侧的动词族判据（`mcp_effect_level`）。

    ## 为什么本地优先
    本地工具的 `side_effect` 是**人工维护的权威标注**（用户在管理页配的），
    比动词族推断更可靠 ⇒ 本地命中时以它为准，MCP 判据只作为**兜底**。
    """
    nm = str(name or "")
    se = str(side_effect or "")
    if se == "destructive":
        return {"match_key": MATCH_LOCAL_DESTRUCTIVE, "kind": "destructive",
                "source": "tools.side_effect"}
    if se == "write":
        return {"match_key": MATCH_LOCAL_WRITE, "kind": "write", "source": "tools.side_effect"}
    # ── MCP 侧兜底 ──
    try:
        from agent.mcp_tool_meta import mcp_effect_level as _mel
        lvl = _mel(nm)
    except Exception:
        lvl = "read"
    if lvl == "destructive":
        return {"match_key": MATCH_MCP_DESTRUCTIVE, "kind": "destructive", "source": "mcp_verb"}
    if lvl == "write":
        return {"match_key": MATCH_MCP_WRITE, "kind": "write", "source": "mcp_verb"}
    return {"match_key": MATCH_READ, "kind": "read", "source": "default_read"}


def _batch_target(arguments: dict) -> str:
    """从参数里提取**受影响对象**的名称（批次摘要要展示"这轮要动哪些东西"）。

    ## ⚠️ 2026-10-28 真机实测（清库任务）暴露的缺陷
    初版只查 `name/title/label/...` ⇒ **删除类工具的 `target` 全是空串**，
    批次摘要渲染成「删除「（未命名对象）」× 3」——
    用户看到三行一模一样的"未命名对象"，等于**盲批**。
    这正是 OWASP ASI09 点名的失效模式：
    "**A human in the loop is not a control if the UI conceals the exact effect.**"

    ⇒ 必须补 **ID 类键**（`dataId/dataID/elementId/id/uuid/...`）：
    删除/移动类操作的参数里**只有 ID，没有 name**（智源的 `delete_model_elements` 就是如此）。
    """
    args = arguments or {}
    # ① 语义名优先（人最容易识别的）
    for _k in ("name", "title", "label", "elementName", "packageName", "viewName",
               "newName", "targetName"):
        _v = args.get(_k)
        if isinstance(_v, (str, int)) and str(_v).strip():
            return str(_v).strip()[:120]
    # ② ★ ID 类键（删除/移动类的唯一标识 —— 初版漏了这批，导致摘要空白）
    for _k in ("dataId", "dataID", "data_id", "elementId", "elementID", "viewId",
               "presentingId", "packageId", "targetId", "id", "uuid"):
        _v = args.get(_k)
        if isinstance(_v, (str, int)) and str(_v).strip():
            _s = str(_v).strip()
            return "#" + _s[-12:] if len(_s) > 12 else "#" + _s
    # ③ 列表型：报条数 + 首项
    for _k in ("elements", "items", "elementIds", "operations", "changes",
               "presentingIds", "targets", "relationships", "dataIds"):
        _v = args.get(_k)
        if isinstance(_v, (list, tuple)) and _v:
            return "%d 项（首个：%s）" % (len(_v), str(_v[0])[:60])
    return ""


def _extract_version(payload: dict) -> str:
    """从工具参数里**递归**提取 versionId（乐观锁用）。

    ## 为什么必须递归（实测踩到）
    智源 MCP 的坐标是**嵌套**的：`{"version": {"projectId":..., "branchId":..., "versionId":...}}`
    ⇒ 只查顶层 `versionId` 会**恒为空** ⇒ 乐观锁被静默跳过 ⇒
    "确认单挂起期间版本已变、批准后写到错的地方"这个风险完全没被防住。
    （2026-10-28 离线测试抓到：payload 只有顶层 name 时校验不生效。）

    ## 搜索顺序（先精确后宽松）
      ① 顶层 `versionId` / `version_id` / `version`
      ② 嵌套一层 `version.*` / `workspace.*`
      ③ 任何 dict 值里的 `versionId`（最多 2 层，防无限递归）
    """
    if not isinstance(payload, dict):
        return ""
    for k in ("versionId", "version_id", "version"):
        v = payload.get(k)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
        if isinstance(v, dict):
            for kk in ("versionId", "version_id", "id"):
                vv = v.get(kk)
                if isinstance(vv, (str, int)) and str(vv).strip():
                    return str(vv).strip()
    # ③ 一层下钻（覆盖 {"data": {"version": {...}}} 这类包壳）
    for _k, _v in payload.items():
        if isinstance(_v, dict):
            r = _extract_version(_v)
            if r:
                return r
    return ""


def exec_idem_key(conf_id: int, payload: dict) -> str:
    """★ 执行幂等键：sha1(确认单 id + 规范化 payload) 前 24 位。

    ## 为什么必须有它（不是可选优化）
    入队幂等（`idem_key`）只防"同一次操作被重复**提交**"；
    但**批准后执行**是另一个入口 —— 用户点两下「批准」就会**执行两次写操作**
    ⇒ 在智源平台上产生**重复包**（真库实证：平台侧确实已积累 3 个重复测试包）。
    ⇒ 执行侧必须再有一道幂等：执行前先查 `result_json` 是否已有回执，
      有 ⇒ 直接返回缓存结果，不重复执行。

    ⚠️ 与 `idempotency_key` 的区别：那个是**入队**去重（agent+action+payload+run+task），
      这个是**执行**去重（conf_id+payload）—— 同一笔确认单只能被真正执行一次。
    """
    try:
        _p = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False,
                        separators=(",", ":"))
    except Exception:
        _p = str(payload or "")
    return hashlib.sha1(("%d|%s" % (int(conf_id or 0), _p)).encode("utf-8")).hexdigest()[:24]


#: ★ 幂等键的**命名空间前缀**（原为悬空注释，2026-10-13 落实为真实常量）。
#: 前缀的作用：让不同来源的确认单不撞键 —— 同一 action+payload 在
#: 「编排子任务暂存」与「L2 强制拦截」两条路径下是不同的业务意图，不该互相当成重复。
IDEM_NS_CONFIRM = "hilq"

#: 幂等窗口（小时）：只在这个窗口内去重。
#: ⚠️ **不设永久唯一约束**的理由：确认单是"待办"，人工可能隔很久才处理；
#:   永久去重会让"用户昨天拒绝、今天想重新提交同一操作"被静默吞掉（返回那条已拒绝的旧记录）——
#:   那比产生重复确认更糟（用户永远做不成事，且看不出原因）。
#:   窗口内的重复才是"同一次操作的重复提交"，这是真正要挡的那类。
IDEM_WINDOW_HOURS = 24

_PREVIEW_LEN = 400


def idempotency_key(agent_id: str, action: str, payload: dict,
                    run_id: int = 0, task_key: str = "") -> str:
    """★ 构造写操作的幂等键（文章 §06）。

    键的构成（每段都对应"两次提交是否应当视为同一次操作"的判断）：
      · `agent` —— 谁发起的。不同 Agent 对同一工具的调用是**不同意图**（多 Agent 协作场景常见）。
      · `action` —— 调哪个工具。
      · `payload` —— 参数**规范化后**的哈希。规范化 = `sort_keys=True` + 紧凑分隔符，
        否则 `{"a":1,"b":2}` 与 `{"b":2,"a":1}` 会算出两个键，而它们显然是同一次操作。
      · `run` + `task` —— **刻意纳入**：编排子任务暂存（`store_write_request` 路径）里
        run/task 是溯源必需，去重窗口内的同一 run 同一 task 同一 payload 视为重复；
        而跨 run 的同参调用（两个批次都试图建同一个实体）**不该去重** ——
        那可能确实是两次独立决策，拦掉会让第二批批次"看起来成功但什么都没做"。
    """
    try:
        _p = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False,
                        separators=(",", ":"))
    except Exception:
        _p = str(payload or "")
    _raw = "|".join([IDEM_NS_CONFIRM, str(agent_id or ""), str(action or ""),
                     _p, str(int(run_id or 0)), str(task_key or "")])
    return hashlib.sha1(_raw.encode("utf-8")).hexdigest()[:24]


def _json_canonical(payload: dict) -> str:
    """payload → 规范化 JSON 字符串（用于**SQL 层**比对，见 queue_confirmation）。

    ⚠️ 必须与 `idempotency_key` 用**同一套规范化规则**，否则两处口径漂移。
    """
    try:
        return json.dumps(payload or {}, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
    except Exception:
        return str(payload or "")


class HILService:
    """L2 写操作强制确认队列。"""

    @staticmethod
    def is_write_tool(tool_name: str) -> bool:
        return (tool_name or "").strip() in WRITE_TOOLS

    @staticmethod
    def queue_confirmation(conn, agent_id: str, action: str, payload: dict,
                           preview: str = "", conversation_id: int = 0,
                           run_id: int = 0, task_key: str = "",
                           tool_type: str = "", level: str = "",
                           batch_id: str = "", timeout_min: int = 0) -> dict | None:
        """L2 写操作拦截：写入待确认队列。返回确认记录（含 id）。

        task_key：所属子任务键（默认 ''，既有调用方零改动）；
        供编排子任务写操作暂存批量挂确认队列时溯源（subtask_artifacts 绑定）。

        ★ 2026-10-13（§06 幂等）：**同一次操作被重复提交时返回已有记录，不新建**。
        判据 = `idem_key`（见 `idempotency_key`）+ 窗口（`IDEM_WINDOW_HOURS`）+ 状态 pending。
        ⚠️ 只对 **pending** 去重：已 approved/rejected 的旧记录不再挡新提交
        （否则"上次被拒、这次想重来"会被误判成重复而静默丢弃 —— 用户永远做不成事且看不出原因）。
        ⚠️ 返回的 dict 带 `idempotent_hit` 标记（True=命中已有，False=本次新建），
        供调用方区分；⚠️ 不改变既有返回结构的其他键。
        ⚠️ `idem_key` 列缺失（旧库）时**自动降级为不启用幂等**，行为与改动前一致 ——
        不能因为一个新列没补上就让整个写确认链路挂掉。

        ★ 2026-10-28（写前确认 HITL）新增参数：
          · `tool_type` —— local / mcp / http（批准后决定走哪个执行器）
          · `level`     —— confirm / approve（决定审批权限门）
          · `batch_id`  —— 批次合并键（**同批次的同类写操作合并成一张卡**）
          · `timeout_min` —— 超时分钟数（默认 `DEFAULT_TIMEOUT_MIN`）
        """
        _idem = ""
        try:
            _idem = idempotency_key(agent_id, action, payload, run_id, task_key)
            try:
                conn.execute("SELECT idem_key FROM hil_confirmations LIMIT 1").fetchone()
            except Exception:
                _idem = ""      # 旧库无该列 → 本次不启用幂等
            if _idem:
                try:
                    dup = conn.execute(
                        "SELECT * FROM hil_confirmations WHERE idem_key=? AND status='pending' "
                        "AND created_at >= datetime('now', ?) LIMIT 1",
                        (_idem, "-%d hours" % IDEM_WINDOW_HOURS)).fetchone()
                except Exception:
                    dup = None
                if dup:
                    d = dict(dup)
                    try:
                        d["payload"] = json.loads(d.get("payload") or "{}")
                    except Exception:
                        pass
                    d["idempotent_hit"] = True
                    return d
        except Exception:
            _idem = ""
        # ── 批次合并（2026-10-28）：同批次同类写 ⇒ 并入已有确认单，不新建 ──
        #   为什么必须有：实测一次建包 = 19 次工具调用；每次都弹卡 ⇒ 人烦到闭眼点通过
        #   ⇒ HITL 退化成形式主义（行业依据：approval fatigue 反模式，
        #   见 services/hil_policy.py 模块头「行业依据」段）。
        #   ⚠️ **批次键含 match_key（动词族）** ⇒ 跨族**不合并**
        #   （arXiv:2604.04918 实测：upfront approval 会**抑制运行时的再审查**——
        #    "the main behavioral benefit of oversight occurred **upstream**"；
        #    因此**计划外的新动作类型必须重新审批**，不能并入原批次）。
        if batch_id:
            try:
                _b = conn.execute(
                    "SELECT * FROM hil_confirmations WHERE batch_id=? AND status='pending' "
                    "ORDER BY id DESC LIMIT 1", (batch_id,)).fetchone()
                if _b:
                    bd = dict(_b)
                    _n = int(bd.get("batch_count") or 1) + 1
                    # 批次对象清单**追加**（计划级摘要要用）——只增不改，摘要随批次完整
                    _items = []
                    try:
                        _items = json.loads(bd.get("batch_items") or "[]")
                        if not isinstance(_items, list):
                            _items = []
                    except Exception:
                        _items = []
                    # ★ 2026-10-28（真机清库实测）：**必须存完整参数**。
                    #   初版只存 {tool, preview, target} ⇒ 批准后只能执行主单那 1 笔，
                    #   合并进来的第 2..N 笔**永远不执行**，而用户以为"批准了 N 项"
                    #   ⇒ 数据丢失型缺陷（静默少做）。`args` 是执行时的唯一依据。
                    _items.append({"tool": action,
                                   "preview": (preview or "")[:300],
                                   "target": _batch_target(payload or {}),
                                   "args": payload or {}})
                    _try_items = json.dumps(_items, ensure_ascii=False)[:8000]
                    try:
                        conn.execute(
                            "UPDATE hil_confirmations SET batch_count=?, batch_items=? WHERE id=?",
                            (_n, _try_items, bd.get("id")))
                        conn.commit()
                    except Exception:
                        pass
                    out = HILService.get(conn, int(bd.get("id") or 0)) or {}
                    out["batch_merged"] = True
                    out["idempotent_hit"] = True
                    try:
                        out["batch_items"] = _items
                    except Exception:
                        pass
                    return out
            except Exception:
                pass
        _tmo = int(timeout_min or 0) or DEFAULT_TIMEOUT_MIN
        _timeout_at = (datetime.now() + timedelta(minutes=_tmo)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            cur = conn.execute(
                "INSERT INTO hil_confirmations (agent_id, conversation_id, run_id, action, payload, preview, status, task_key, idem_key, tool_type, level, batch_id, batch_count, timeout_at, batch_items) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (agent_id or "", conversation_id or 0, run_id or 0, action,
                 json.dumps(payload or {}, ensure_ascii=False),
                 (preview or "")[:_PREVIEW_LEN], "pending", task_key or "", _idem,
                 tool_type or "", level or "", batch_id or "", 1, _timeout_at,
                 json.dumps([{"tool": action, "preview": (preview or "")[:300],
                              "target": _batch_target(payload or {}),
                              "args": payload or {}}], ensure_ascii=False)))
            conn.commit()
            out = HILService.get(conn, cur.lastrowid)
            if isinstance(out, dict):
                out["idempotent_hit"] = False
            return out
        except Exception as _ie:
            # ⚠️ 降级分支**必须留痕**：静默降级的后果是"确认单进了队列，但 tool_type/level/
            #   batch_id 全是空默认值" ⇒ 批准时不知道走哪个执行器、批次合并失效
            #   ⇒ 症状是"确认单能生成、但功能全不对"，且**看不出原因**。
            #   （2026-10-28 实测踩到：临时库缺列 ⇒ 全部落此分支 ⇒ 批次合并不生效。）
            try:
                import sys as _sys
                print("[hil] ⚠️ 新列插入失败，退回旧列插入（tool_type/level/batch_id 将为空）："
                      "%s: %s" % (type(_ie).__name__, str(_ie)[:200]), file=_sys.stderr)
            except Exception:
                pass
            # 旧库缺新列 ⇒ 退回不写新列的插入（**不丢确认单**）
            # ★ 2026-10-30 修：退回分支**必须仍写 `idem_key`**（以及能写就写的 run/task）。
            #   为什么（真缺陷，非洁癖）：幂等去重读的是 `idem_key` 列，
            #   而本分支此前**只写 8 个旧列** ⇒ 该行 `idem_key=''`
            #   ⇒ 下一次同样操作查不到它 ⇒ **重复确认单**（正是 §06 幂等要治的病）。
            #   实测复现：把表建到缺 `tool_type` 的最小 schema 上，
            #   同一操作连提交两次 → 2 行，且两行 idem_key 都是空。
            #   ⇒ 退回的语义是"少写**增强**列"，不是"少写**控制**列"。
            #   `idem_key` 属控制列（决定去重）；`tool_type/level/batch_id` 属增强列。
            try:
                _cols = set()
                try:
                    _cols = {r[1] for r in conn.execute(
                        "PRAGMA table_info(hil_confirmations)").fetchall()}
                except Exception:
                    _cols = set()
                _names = ["agent_id", "conversation_id", "run_id", "action", "payload",
                          "preview", "status", "task_key"]
                _vals = [agent_id or "", conversation_id or 0, run_id or 0, action,
                         json.dumps(payload or {}, ensure_ascii=False),
                         (preview or "")[:_PREVIEW_LEN], "pending", task_key or ""]
                # 控制列：有则必写（缺列只是老到没有这套机制，不是"故意不写"）
                for _c, _v in (("idem_key", _idem), ("timeout_at", _timeout_at),
                               ("batch_count", 1)):
                    if _c in _cols:
                        _names.append(_c)
                        _vals.append(_v)
                cur = conn.execute(
                    "INSERT INTO hil_confirmations (%s) VALUES (%s)"
                    % (", ".join(_names), ",".join("?" * len(_names))), tuple(_vals))
                conn.commit()
                return HILService.get(conn, cur.lastrowid)
            except Exception:
                return None

    @staticmethod
    def mark_executed(conn, conf_id: int, ok: bool, result: str = "") -> dict:
        """★ 执行后回写：status → executed / failed，并存执行回执。

        回执存在 `result_json`（**完整**回执，不截断）——
        它的两个用途：① 前端展示"到底写成没有"；
        ② **执行幂等的依据**（`result_json` 非空 ⇒ 已执行过 ⇒ 不重复跑）。
        """
        try:
            _rid = conn.execute(
                "SELECT idem_key FROM hil_confirmations LIMIT 1").fetchone()
        except Exception:
            _rid = None
        _st = "executed" if ok else "failed"
        _blob = json.dumps({"ok": bool(ok), "result": str(result or "")[:4000],
                            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                           ensure_ascii=False)
        try:
            conn.execute(
                "UPDATE hil_confirmations SET status=?, result_json=? WHERE id=?",
                (_st, _blob, int(conf_id or 0)))
            conn.commit()
        except Exception:
            # 旧库无 result_json 列 ⇒ 至少把状态落上（**状态比回执更重要**）
            try:
                conn.execute("UPDATE hil_confirmations SET status=? WHERE id=?",
                             (_st, int(conf_id or 0)))
                conn.commit()
            except Exception:
                return {"ok": False, "status": "update_failed"}
        return {"ok": bool(ok), "status": _st, "result_json": _blob}

    @staticmethod
    def already_executed(conn, conf_id: int) -> dict | None:
        """★ 执行幂等判据：若该确认单**已有回执**，返回它（调用方直接复用，不重复执行）。"""
        try:
            row = conn.execute(
                "SELECT result_json, status FROM hil_confirmations WHERE id=?",
                (int(conf_id or 0),)).fetchone()
        except Exception:
            return None
        if not row:
            return None
        try:
            rj = row["result_json"]
        except Exception:
            rj = None
        if not rj:
            return None
        try:
            d = json.loads(rj)
        except Exception:
            d = {"ok": True, "result": str(rj)}
        try:
            d["status"] = row["status"]
        except Exception:
            pass
        return d


    @staticmethod
    def get(conn, conf_id: int) -> dict | None:
        try:
            row = conn.execute("SELECT * FROM hil_confirmations WHERE id=?", (conf_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["payload"] = json.loads(d.get("payload") or "{}")
            return d
        except Exception:
            return None

    @staticmethod
    def pending(conn, limit: int = 50) -> list:
        try:
            rows = conn.execute(
                "SELECT * FROM hil_confirmations WHERE status='pending' ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.get("payload") or "{}")
                out.append(d)
            return out
        except Exception:
            return []

    @staticmethod
    def decide(conn, conf_id: int, approve: bool, decided_by: str = "",
               expected_version: str = "") -> dict:
        """人工决策。approve=True → 返回 {'status':'approved','payload':{...}}，调用方执行后才算生效；
        approve=False → 丢弃，返回 {'status':'rejected'}。

        ★ 2026-10-28（写前确认 HITL）两处加固：
        ① **条件更新防并发双批**：`UPDATE ... WHERE id=? AND status='pending'`，
           靠 `rowcount` 判定"谁抢到"。此前是无条件 UPDATE ⇒ 两个并发 decide 请求
           都会"成功"，随后**执行两次写操作**（重复包）。
        ② **乐观锁 `expected_version`**：批准时校验 payload 里的坐标与调用方期望一致
           （防"确认单挂起期间平台坐标已变，批准后写到错的地方"）。
           不一致 ⇒ 返回 `version_mismatch`，**不执行**，要求模型重新定位。
        """
        row = HILService.get(conn, conf_id)
        if not row:
            return {"status": "not_found", "payload": {}}
        if row["status"] != "pending":
            return {"status": row["status"], "payload": row["payload"], "row": row}
        # ① 乐观锁校验（**递归**取坐标：智源是嵌套 version:{...}，见 _extract_version 注释）
        if expected_version:
            _pv = _extract_version(row.get("payload") or {})
            if _pv and _pv != str(expected_version):
                return {"status": "version_mismatch", "payload": row["payload"], "row": row,
                        "expected_version": str(expected_version),
                        "payload_version": _pv}
        new_status = "approved" if approve else "rejected"
        try:
            cur = conn.execute(
                "UPDATE hil_confirmations SET status=?, decided_by=?, decided_at=? "
                "WHERE id=? AND status='pending'",
                (new_status, decided_by or "", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 conf_id))
            conn.commit()
            if cur.rowcount == 0:
                # 并发：已被别人决策过 ⇒ 返回现状，**不执行**
                _r2 = HILService.get(conn, conf_id) or {}
                return {"status": _r2.get("status") or "pending",
                        "payload": _r2.get("payload") or {}, "row": _r2,
                        "raced": True}
        except Exception:
            # 旧库/异常 ⇒ 退回无条件更新（**不丢决策**）
            conn.execute(
                "UPDATE hil_confirmations SET status=?, decided_by=?, decided_at=? WHERE id=?",
                (new_status, decided_by or "", datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 conf_id))
            conn.commit()
        return {"status": new_status, "payload": row["payload"], "row": row}


# ══════════════════════════════════════════════════════════════════════
# ★ 2026-10-28（写前确认 HITL）统一执行器 + 回灌文案构造
# ══════════════════════════════════════════════════════════════════════

def execute_confirmation(conn, conf: dict, user=None) -> dict:
    """批准后**真正执行**该笔写操作（统一执行器）。

    ## ★ 与旧流程的根本差别（本轮修复的核心缺陷）
    旧 decide 端点只真执行 `kind='http'` 的工具 + `save_artifact` + `eval_label`，
    其余一律落 else 分支「非 HTTP 集成类型，不支持批准后自动执行，请手动操作」
    ⇒ **MCP 写操作（智源的全部建模写动作）批准了也不执行**。
    ⇒ 现在统一走 `AgentPipeline._exec_tool_call` —— **与模型调用工具是同一份代码**
    ⇒ MCP / HTTP / local 三类天然全覆盖，且**权限闸、Hook、寻址链全部复用**。

    ## 三道保护
    ① **执行幂等**：`result_json` 已有回执 ⇒ 直接复用，不重复执行
      （用户点两下「批准」就会执行两次写操作 ⇒ 智源平台上产生重复包）。
    ② **批准旁路**：置 `_hil_authorized_conf_id`，让统一闸识别"这是被批准的那一笔"
      ⇒ 不会二次入队（否则无限循环）。
    ③ **回执回写**：结果写 `result_json`，status → executed / failed
      ⇒ 审计与前端都能区分"批了但没跑"与"跑成了"。

    返回 `{"ok", "executed", "status", "result", "message"}`。
    """
    cid = int((conf or {}).get("id") or 0)
    action = str((conf or {}).get("action") or "")
    payload = (conf or {}).get("payload") or {}
    if not cid or not action:
        return {"ok": False, "executed": False, "status": "invalid",
                "result": "确认单缺少 id 或 action，无法执行"}
    # ① 执行幂等
    try:
        done = HILService.already_executed(conn, cid)
    except Exception:
        done = None
    if done:
        return {"ok": bool(done.get("ok", True)), "executed": False,
                "status": "already_executed", "result": str(done.get("result") or ""),
                "message": "该确认单此前已执行过（幂等命中），本次未重复执行"}
    # ② 统一执行器
    #   ★ 2026-10-28（真机清库实测修 X1）：**批次合并的每一项都必须执行**。
    #   初版只执行主确认单的 `payload`（= 合并进来的第 1 笔）⇒ 合并的第 2..N 笔
    #   **永远不会被执行**，而用户以为"批准了 N 项" ⇒ 静默少做（数据丢失型）。
    #   现在：`batch_items` 里每条都带 `args` ⇒ 逐条执行并汇总。
    try:
        from agent.pipeline import AgentPipeline
        pipe = AgentPipeline()
        pipe._tool_intent_ctx = {"intent": str((conf or {}).get("agent_id") or "")}
        pipe._tool_agent_ctx = {"agent": str((conf or {}).get("agent_id") or "")}
        pipe._tool_conv_ctx = int((conf or {}).get("conversation_id") or 0)
        pipe._tool_user = user
        pipe._tool_whitelist = None          # 直发执行：无委派白名单

        # ── 构造执行清单：优先用 batch_items（带完整 args），退回单条 payload ──
        _items = []
        try:
            _bi = conf.get("batch_items")
            _items = json.loads(_bi) if isinstance(_bi, str) else (_bi or [])
            if not isinstance(_items, list):
                _items = []
        except Exception:
            _items = []
        _todo = []
        for _it in _items:
            if not isinstance(_it, dict):
                continue
            _a = _it.get("args")
            if isinstance(_a, dict) and _a:
                _todo.append({"tool": str(_it.get("tool") or action), "args": _a,
                              "target": _it.get("target") or ""})
        if not _todo:
            # 单条 / 老数据（无 batch_items 或无 args）⇒ 退回主 payload（逐字节兼容）
            _todo = [{"tool": action, "args": payload, "target": ""}]

        _results, _any_fail, _ok_n = [], False, 0
        for _i, _t in enumerate(_todo, 1):
            pipe._hil_authorized_conf_id = cid   # 批准旁路（每笔都要，防二次入队）
            try:
                _r = pipe._exec_tool_call(_t["tool"], _t["args"])
            except Exception as _ie:
                _r = {"ok": False, "result": "执行异常：%s" % str(_ie)[:200]}
            _ok1 = bool((_r or {}).get("ok"))
            if not _ok1:
                _any_fail = True
            else:
                _ok_n += 1
            _results.append({"seq": _i, "tool": _t["tool"], "target": _t.get("target") or "",
                             "ok": _ok1,
                             "result": str((_r or {}).get("result") or "")[:1200]})
        pipe._hil_authorized_conf_id = None
        _n = len(_todo)
        if _n > 1:
            _summary = ("批次执行完成：%d/%d 项成功" % (_ok_n, _n)
                        + ("" if not _any_fail else "（%d 项失败，详见明细）" % (_n - _ok_n)))
            _blob = json.dumps(_results, ensure_ascii=False, indent=1)
            res = {"ok": not _any_fail, "result": _summary + "\n" + _blob}
        else:
            res = {"ok": bool(_results[0]["ok"]), "result": _results[0]["result"]}
    except Exception as _ex:
        try:
            HILService.mark_executed(conn, cid, False, "执行异常：%s" % str(_ex)[:300])
        except Exception:
            pass
        return {"ok": False, "executed": False, "status": "exec_error",
                "result": "执行异常：%s" % str(_ex)[:300],
                "message": "写操作执行失败"}
    _ok = bool((res or {}).get("ok"))
    _txt = str((res or {}).get("result") or "")
    # ③ 回执回写
    try:
        HILService.mark_executed(conn, cid, _ok, _txt)
    except Exception:
        pass
    return {"ok": _ok, "executed": True, "status": "executed" if _ok else "failed",
            "result": _txt,
            "message": ("写操作「%s」已执行成功" % action) if _ok
            else ("写操作「%s」执行失败" % action)}


def build_resume_text(action: str, exec_res: dict, approved: bool,
                      reason: str = "", original_input: str = "") -> str:
    """★ 2026-10-28：构造**回灌给模型**的文案（D-2）。

    ## 为什么必须回灌（这是"批准即执行"闭环的最后一环）
    批准执行发生在**原 ReAct 循环之外**（一次独立的 HTTP 请求）⇒
    **LLM 永远收不到"工具已执行"的回执** ⇒ 它会重发同一操作，或宣称"尚未执行"。
    （真机实测 E8：`create_model_elements` 回执 `SUCCEEDED`，模型却断言
    「尚未写入平台……因轮次用尽，写操作没有执行」—— **与事实相反**。）
    ⇒ 把**真实执行结果**显式回灌，模型才有事实依据。

    ## 两条必须遵守的纪律（实测踩过）
    ① **必须带"原请求"复述**：`messages` 表**不存 tool_calls / tool_result**
       ⇒ ReAct 状态是纯内存 ⇒ 回灌必然是"**重新起一轮**"而不是"恢复状态机"
       ⇒ 不复述原请求，模型会丢失上下文（`ask_user` 范式里已这么做，见
       `routers/conversations.py:695`）。
    ② **必须明说"不要重复发起"**：否则模型会把"已执行"当成"没执行"再跑一遍
       ⇒ 在平台上产生重复包。
    """
    if not approved:
        return ("【人工确认结果】用户**拒绝**了写操作「%s」。%s\n"
                "请**不要重试**该操作，直接向用户说明情况。\n%s"
                % (action, ("拒绝原因：%s" % reason) if reason else "",
                   ("\n原请求：%s" % original_input[:2000]) if original_input else ""))
    _r = str((exec_res or {}).get("result") or "")
    _st = "成功" if (exec_res or {}).get("ok") else "失败"
    return ("【人工确认结果】你请求的写操作「%s」已获用户**批准并执行**，执行结果：**%s**。\n"
            "执行回执：%s\n"
            "请据此继续，**不要重复发起该操作**（它已经执行过了）。\n%s"
            % (action, _st, (_r[:1500] if _r else "（无回执正文）"),
               ("\n原请求：%s" % original_input[:2000]) if original_input else ""))
