# -*- coding: utf-8 -*-
"""预算层级真源 + 审计（2026-10-30，P0-3）。

━━━ 为什么需要这个模块 ━━━
《引擎选型评估与控制面收敛-20261030》§1.1 的实测结论：本工程涉及
"时间/预算/重试"的配置键共 **38 个**，分散在 **12 个配置段**。
这不是"键多"的问题，而是**同一语义被反复实现 ⇒ 口径必然漂移**。
仓库里已经付出过两次代价：
    ① `delegation.total_time_budget_s` —— 注释写"超限终止剩余任务"，
       **全仓零消费点** ⇒ 改了不起任何作用（配置谎言，2026-10-02 发现，一直没处理）；
    ② 11 个 `context_audit.*` 键 —— 经 settings 表写入返回 `ok=True`，
       但消费点只读配置文件 ⇒ 静默无效（2026-10-29 修复）。

引擎（Temporal/DBOS）能提供的核心价值就是"**单一真源 + 明确层级**"，
而这一半**不需要引擎就能拿到**。本模块就是那一半：
    · `LAYERS` —— 四层预算的**唯一真源**；
    · `audit()` —— 把"当前配置 + 消费点"摊开成一份可核对的报告。

━━━ 四层模型（只覆盖"请求链路"上的时间预算）━━━
    L1 整轮     —— 一次请求的全部过程（意图识别 + 编排 + 汇总）
    L2 编排批次 —— 单次编排批次（planner 出图 → 派发 → 汇总）
    L3 子任务   —— 单个子任务（一个 agent 的一次执行）
    L4 单次调用 —— 一次 LLM 调用（含重试）
嵌套关系：L1 ⊃ L2 ⊃ L3 ⊃ L4。

⚠️ **不在这条链路上**的键（后台作业租约、审计 TTL、DB 连接超时、离线 OCR 预算…）
**不硬塞进四层** —— 它们没有"被包含"关系，塞进去只会制造假违例。
它们被归入 `off_chain` 并**只做死配置检测**。

━━━ 三条角色（把"同层"再分细，否则审计会满屏假违例）━━━
P1 `canonical` —— 该层的权威预算。每个条目带一个 **aspect**（同一层内的不同侧面）：
      L1: wall（整轮墙钟）      L3: hard（硬上限）/ idle（无产出判超时）
      L4: single（单次尝试）/ total（连重试的总预算）
P2 `aliases` —— **同一 aspect**、同一层的另一个键。它与 canonical 表达同一件事，
      因此**不得比 canonical 更严** —— 否则 canonical 就是**假权威**。
P3 `independent` —— 同类但**不同调用链**（外部子系统超时、并行路径的预算），
      **不可比**，只参与死配置检测。
      ⚠️ 这条区分是本模块第一版的教训：初版把 `integration.timeout`(15s)、
      `mcp.timeout`(15s) 等外部子系统超时当成 `llm.http_timeout_s`(180s) 的别名，
      审计立刻报出 5 条"别名严于真源" —— **全是模型自己的错，不是配置的问题**。

━━━ 第三条判据：死配置（配置谎言的机器可检形态）━━━
一个配置键在业务代码里**零消费点**，就说明它"改了了不起任何作用"
—— 与 `total_time_budget_s` 同型。扫描口径：把每个 `.py` 解析成 AST，
取**非 docstring** 的字符串常量（真实消费点必然是代码里的字符串字面量；
注释/docstring 不算）⇒ 避免"注释里提了一句就判定为有消费点"的假阳性。

⚠️ 口径声明（引用数字必须带口径）：
    · 扫描范围 = 仓库根下 `*.py`，**排除** `tmp/ docs/ tests/ backups/ static/ data/
      outputs/ logs/ .venv/ __pycache__/ 与 `core/config.py` 自身
      （定义处不算消费点；`tools/verify/` 是"断言"不是"消费"，同样排除）；
    · "有消费点" = 至少 1 个文件里出现该键的**代码字符串字面量**；
    · 本报告只覆盖**时间维度**；token / 字符 / 比例类预算（`context.*`）属**另一维度**，
      单列 `token_dimension_keys` 计数，不混进层级判定。
"""
import ast
import os
import warnings

__all__ = ["LAYERS", "layer_of", "audit", "consumer_scan", "effective_budget",
           "scan_sources"]

#: 仓库根（core/ 的上一级）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 扫描时跳过的目录（生成物 / 文档 / 断言脚本 / 依赖 / 归档）
_SKIP_DIRS = {
    "tmp", "docs", "tests", "backups", "static", "data", "outputs", "screenshots",
    "node_modules", ".venv", "venv", "__pycache__", ".git", ".workbuddy", "logs",
    "checker", "sysml.library", "java-runtime", "seeds_data",
}

#: 四层预算真源定义。
#: primary    = 该层用于**跨层比较**的权威预算（(group, key)）
#: canonical  = 权威预算条目（可含多个 aspect）
#: aliases    = 同 aspect 的另一个键（不得比 canonical 更严）
#: independent= 同类但不同调用链（不可比，只做死配置检测）
LAYERS = [
    {
        "id": "L1", "title": "整轮", "parent": "",
        "scope": "一次请求的全部过程（意图识别 + 编排 + 汇总）",
        "primary": ("runtime", "chat_deadline_s"),
        "canonical": [{"group": "runtime", "key": "chat_deadline_s", "aspect": "wall"}],
        "aliases": [{"group": "agent", "key": "auto_continue_deadline_s",
                     "aspect": "wall", "against": "chat_deadline_s"}],
        "independent": [("agent", "fill_deadline_s", "自动补齐子任务的墙钟（独立子流程）")],
    },
    {
        "id": "L2", "title": "编排批次", "parent": "L1",
        "scope": "单次编排批次（planner 出图 → 派发 → 汇总）",
        "primary": ("delegation", "total_time_budget_s"),
        "canonical": [{"group": "delegation", "key": "total_time_budget_s", "aspect": "wall"}],
        "aliases": [],
        "independent": [("replanning", "probe_time_budget_sec", "编排健康探针的探针预算（独立探针）")],
    },
    {
        "id": "L3", "title": "子任务", "parent": "L2",
        "scope": "单个子任务（一个 agent 的一次执行）",
        "primary": ("delegation", "subtask_timeout_s"),
        "canonical": [{"group": "delegation", "key": "subtask_timeout_s", "aspect": "hard"},
                      {"group": "delegation", "key": "subtask_idle_timeout_s", "aspect": "idle"}],
        "aliases": [],
        "independent": [("delegation", "worker_timeout_s", "非流式 planner 并行 worker 的超时（另一条执行路径）")],
    },
    {
        "id": "L4", "title": "单次调用", "parent": "L3",
        "scope": "一次 LLM 调用（含重试）",
        "primary": ("llm", "http_timeout_s"),
        "canonical": [{"group": "llm", "key": "http_timeout_s", "aspect": "single"},
                      {"group": "llm", "key": "total_timeout_budget", "aspect": "total"}],
        "aliases": [],
        "independent": [("integration", "timeout", "外部集成 HTTP 超时（独立子系统）"),
                        ("mcp", "timeout", "MCP 服务器超时（独立子系统）"),
                        ("sysml", "check_timeout", "本地 SysML 校验器超时（独立子系统）"),
                        ("zhiyuan", "timeout", "智源平台超时（独立子系统）"),
                        ("sandbox", "gate_timeout_s", "沙箱门控超时（独立子系统）")],
    },
]

#: 反查表：`(group, key)` → 层 id
_BY_KEY = {}
for _L in LAYERS:
    for _c in _L["canonical"]:
        _BY_KEY[(_c["group"], _c["key"])] = _L["id"]
    for _a in _L["aliases"]:
        _BY_KEY[(_a["group"], _a["key"])] = _L["id"]
    for _i in _L["independent"]:
        _BY_KEY[(_i[0], _i[1])] = _L["id"]

#: 时间维度的键名特征
_TIME_PAT = ("timeout", "deadline", "ttl", "stale", "lease", "interval",
             "wait_s", "budget_s", "time_budget", "drain")
#: token / 字符维度（**另一维度**，不参与层级判定）
_TOKEN_PAT = ("token", "chars", "ratio", "window", "thinking_budget")


def layer_of(group, key):
    """返回 `(group, key)` 所属层 id；未归层返回 `""`。"""
    return _BY_KEY.get((group, key), "")


def _cfg_value(group, key, default=None):
    """读配置当前值（异常退回 default）。"""
    try:
        from core import config as _c
        v = _c.get(group, key, default)
        return default if v is None else v
    except Exception:
        return default


def _as_number(v):
    """把配置值转成可比较的秒数；非数值返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except Exception:
        return None


# ── 消费点扫描 ────────────────────────────────────────────────────
def scan_sources():
    """列出参与扫描的 `.py` 文件（已排除生成物/文档/断言脚本）。

    ⚠️ 排除 `tools/verify/` 是**有意的**：验证脚本是"断言键存在"，
    不是"消费键" —— 把它们算成消费点会让"死配置"检测永远为假绿。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(_ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        rel_top = os.path.relpath(dirpath, _ROOT).split(os.sep)[0]
        if rel_top == "tools" and "verify" in dirnames:
            dirnames.remove("verify")
        for fn in filenames:
            if fn.endswith(".py"):
                p = os.path.join(dirpath, fn)
                if os.path.abspath(p) == os.path.abspath(
                        os.path.join(_ROOT, "core", "config.py")):
                    continue          # 定义处不算消费点
                out.append(p)
    return out


def _code_strings(path):
    """取文件里**非 docstring** 的字符串常量（真实消费点的形态）。"""
    try:
        with open(path, "rb") as f:
            src = f.read().decode("utf-8", "replace")
        # ⚠️ 必须屏蔽 SyntaxWarning：被扫描的第三方/历史文件里可能含
        #    非法转义序列（如 docstring 里的 `\|`）⇒ ast.parse 会打印告警，
        #    污染调用方输出（本仓 `tools/migrate_*.py` 就有一处）。
    #    告警与本模块的判定无关 ⇒ 直接静音。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(src)
    except Exception:
        return []
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                doc_ids.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc_ids:
            out.append(node.value)
    return out


def consumer_scan(keys=None):
    """统计每个键的**消费点**（文件数 + 命中次数）。

    :param keys: `[(group, key), ...]`；缺省 = 层级定义里的全部键。
    :return: `{key_string: {"files": n, "hits": m, "paths": [...]}}`
    """
    if keys is None:
        keys = list(_BY_KEY.keys())
    targets = {}
    for g, k in keys:
        targets.setdefault(k, []).append(g)
    res = {k: {"files": 0, "hits": 0, "paths": []} for k in targets}
    for p in scan_sources():
        strings = _code_strings(p)
        if not strings:
            continue
        for k in targets:
            n = sum(1 for s in strings if s == k)
            if n:
                res[k]["files"] += 1
                res[k]["hits"] += n
                rel = os.path.relpath(p, _ROOT).replace("\\", "/")
                if len(res[k]["paths"]) < 6:
                    res[k]["paths"].append(rel)
    return res


# ── 层级预算 ──────────────────────────────────────────────────────
def _layer_keys(L):
    """展开某层的全部键（canonical + aliases + independent）。"""
    out = []
    for c in L["canonical"]:
        out.append((c["group"], c["key"]))
    for a in L["aliases"]:
        out.append((a["group"], a["key"]))
    for i in L["independent"]:
        out.append((i[0], i[1]))
    return out


def effective_budget(layer_id, aspect=None):
    """某层（可选某 aspect）的"实际生效上限" = 参与比较的启用键的**最小值**。

    为什么取最小：更严格者先生效 ⇒ 真正起作用的是最严的那个。
    `0` 表示"不限"，不参与取最小。

    :param aspect: 限定某个侧面（如 L3 的 "hard" / "idle"）；缺省 = 该层 primary 侧面。
    """
    for L in LAYERS:
        if L["id"] != layer_id:
            continue
        cands = []
        if aspect is None:
            g, k = L["primary"]
            v = _as_number(_cfg_value(g, k, 0))
            if v is not None and v > 0:
                cands.append((v, "%s.%s" % (g, k)))
            for a in L["aliases"]:
                if a.get("against") != k:
                    continue
                v = _as_number(_cfg_value(a["group"], a["key"], 0))
                if v is not None and v > 0:
                    cands.append((v, "%s.%s" % (a["group"], a["key"])))
        else:
            for c in L["canonical"]:
                if c.get("aspect") != aspect:
                    continue
                v = _as_number(_cfg_value(c["group"], c["key"], 0))
                if v is not None and v > 0:
                    cands.append((v, "%s.%s" % (c["group"], c["key"])))
            for a in L["aliases"]:
                if a.get("aspect") != aspect:
                    continue
                v = _as_number(_cfg_value(a["group"], a["key"], 0))
                if v is not None and v > 0:
                    cands.append((v, "%s.%s" % (a["group"], a["key"])))
        if not cands:
            return {"budget_s": None, "source": "", "note": "无启用中的预算键（不限）"}
        v, src = min(cands)
        return {"budget_s": v, "source": src,
                "note": "同层同侧面所有启用键取最严者（更严格者先生效）"}
    return {"budget_s": None, "source": "", "note": "未知层"}


# ── 审计 ──────────────────────────────────────────────────────────
def audit():
    """产出预算层级审计报告。

    报告五部分：
      1. `layers`      —— 每层的真源 / 别名 / 独立键 / 当前值 / 实际生效上限；
      2. `findings`    —— ① 同层同侧双真源（值不同 ⇒ 实际取最严）
                        ② 别名严于真源（真源成假权威）
                        ③ 父层严于子层（子层额度永远用不满）；
      3. `dead_configs` —— **零消费点**的键（改了了不起作用 = 配置谎言）；
      4. `off_chain`    —— 时间维度但**不在请求链路上**的键（不硬塞进层级）；
      5. `token_dimension_keys` —— token/字符维度键数（另一维度，单列计数）。
    """
    layers_out = []
    all_keys = []
    for L in LAYERS:
        canon = []
        for c in L["canonical"]:
            v = _cfg_value(c["group"], c["key"], None)
            canon.append({"group": c["group"], "key": c["key"], "aspect": c["aspect"],
                          "value": v, "seconds": _as_number(v)})
            all_keys.append((c["group"], c["key"]))
        aliases = []
        for a in L["aliases"]:
            v = _cfg_value(a["group"], a["key"], None)
            aliases.append({"group": a["group"], "key": a["key"], "aspect": a.get("aspect"),
                            "against": a.get("against"), "value": v, "seconds": _as_number(v)})
            all_keys.append((a["group"], a["key"]))
        indep = []
        for i in L["independent"]:
            v = _cfg_value(i[0], i[1], None)
            indep.append({"group": i[0], "key": i[1], "value": v,
                          "seconds": _as_number(v), "note": i[2]})
            all_keys.append((i[0], i[1]))
        layers_out.append({
            "id": L["id"], "title": L["title"], "parent": L["parent"],
            "scope": L["scope"],
            "primary": {"group": L["primary"][0], "key": L["primary"][1],
                        "value": _cfg_value(L["primary"][0], L["primary"][1], None)},
            "canonical": canon, "aliases": aliases, "independent": indep,
            "effective": effective_budget(L["id"]),
        })

    findings = []
    # ── ① 同层同侧双真源 ──
    for L in LAYERS:
        by_aspect = {}
        for c in L["canonical"]:
            v = _as_number(_cfg_value(c["group"], c["key"], None))
            by_aspect.setdefault(c["aspect"], []).append(
                (c["group"] + "." + c["key"], v))
        for a in L["aliases"]:
            v = _as_number(_cfg_value(a["group"], a["key"], None))
            by_aspect.setdefault(a.get("aspect"), []).append(
                (a["group"] + "." + a["key"], v))
        for aspect, items in by_aspect.items():
            vals = [(n, v) for n, v in items if v is not None and v > 0]
            if len(vals) < 2:
                continue
            uniq = sorted({v for _n, v in vals})
            if len(uniq) <= 1:
                continue
            eff = min(vals)
            findings.append({
                "kind": "multi_true_source", "layer": L["id"], "aspect": aspect,
                "keys": [{"name": n, "value_s": v} for n, v in vals],
                "effective_s": eff[0], "effective_source": eff[1],
                "detail": "同层同侧面有多个键表达同一预算且值不同 ⇒ 实际生效取最严者",
            })

    # ── ② 别名严于真源 ──
    for L in LAYERS:
        for a in L["aliases"]:
            av = _as_number(_cfg_value(a["group"], a["key"], 0))
            if av is None or av <= 0:
                continue
            cv = None
            cname = ""
            for c in L["canonical"]:
                if c["key"] == a.get("against"):
                    cv = _as_number(_cfg_value(c["group"], c["key"], 0))
                    cname = c["group"] + "." + c["key"]
                    break
            if cv is None or cv <= 0:
                continue
            if av < cv:
                findings.append({
                    "kind": "alias_stricter_than_canonical", "layer": L["id"],
                    "key": a["group"] + "." + a["key"], "value_s": av,
                    "canonical": cname, "canonical_s": cv,
                    "detail": "同层别名比真源更严 ⇒ 真源是假权威（实际生效的是别名）",
                })

    # ── ③ 父层严于子层 ──
    # ⚠️ 必须**跨过停用层**去比最近的启用祖先：L2（编排批次）默认 `0=不启用`，
    #    若只比直接父层就会得到"L2 无限预算" ⇒ L1(900) vs L3(600) 这对真实约束
    #    永远比不出来（自证时暴露：注入 L1=1s 后审计仍零 findings）。
    _BY_ID = {L["id"]: L for L in LAYERS}
    for L in LAYERS:
        chain_parents = []
        p = L["parent"]
        while p:
            chain_parents.append(p)
            p = _BY_ID.get(p, {}).get("parent", "")
        c_eff = effective_budget(L["id"])
        cs = c_eff.get("budget_s")
        if cs is None:
            continue
        for pid in chain_parents:
            p_eff = effective_budget(pid)
            ps = p_eff.get("budget_s")
            if ps is None:
                continue      # 该祖先未启用预算 ⇒ 继续往上找
            if ps < cs:
                findings.append({
                    "kind": "parent_stricter_than_child", "layer": L["id"],
                    "parent_layer": pid, "key": p_eff.get("source"), "value_s": ps,
                    "child_source": c_eff.get("source"), "child_s": cs,
                    "via_disabled": pid != L["parent"],
                    "detail": "祖先层预算严于子层 ⇒ 子层额度永远用不满（更严格者先生效）",
                })
            break             # 只与**最近一个启用祖先**比（更远的是它的父层，由它自己去比）

    # ── 死配置（零消费点）──
    cons = consumer_scan(all_keys)
    dead = []
    for g, k in all_keys:
        c = cons.get(k) or {"files": 0, "hits": 0, "paths": []}
        if int(c.get("files") or 0) == 0:
            dead.append({
                "group": g, "key": k, "layer": layer_of(g, k),
                "detail": "全仓业务代码零消费点 ⇒ 改了不起任何作用（配置谎言）",
            })

    # ── 链外 / token 维度 ──
    off_chain = []
    token_dim = 0
    try:
        from core import config as _c
        for grp, d in (_c.DEFAULT_CONFIG or {}).items():
            if not isinstance(d, dict):
                continue
            for k, v in d.items():
                kl = str(k).lower()
                if any(p in kl for p in _TOKEN_PAT):
                    token_dim += 1
                    continue
                if not any(p in kl for p in _TIME_PAT):
                    continue
                if layer_of(grp, k):
                    continue
                off_chain.append({"group": grp, "key": k, "value": v,
                                  "detail": "时间维度但不在请求链路上（后台/租约/基础设施类）"})
    except Exception:
        pass

    return {
        "ok": True,
        "layers": layers_out,
        "findings": findings,
        "dead_configs": dead,
        "off_chain": off_chain,
        "token_dimension_keys": token_dim,
        "counts": {
            "layers": len(layers_out),
            "tracked_keys": len(all_keys),
            "findings": len(findings),
            "dead_configs": len(dead),
            "off_chain": len(off_chain),
            "token_dimension_keys": token_dim,
        },
        "scan_scope": {
            "excluded_dirs": sorted(_SKIP_DIRS),
            "note": "消费点 = 非 docstring 的代码字符串字面量；core/config.py 定义处不计；"
                    "tools/verify 是断言不是消费",
        },
    }
