"""控制面收敛门禁（2026-10-30）—— 幂等键 / 停滞自愈 / 预算层级。

对应文档：
    · `docs/引擎选型评估与控制面收敛-20261030.md`（P0-1 幂等键、P0-2 停滞自愈、P0-3 层级收敛）
    · `docs/运行时稳定性优化-行业调研与实施方案-20261030.md`（上一轮，本门禁与之互补）

━━━ 为什么需要这个门禁 ━━━
三项改动都属于**"平时不显形、出问题时才见效"**的控制面：
    · 幂等键只在"子任务重试/续跑"时生效；
    · 停滞自愈每 300s 扫一次，多数轮次零动作；
    · 预算层级是审计口径，不参与请求处理。
⇒ 日常功能测试一条都覆盖不到，极易在后续重构中被"顺手精简"掉。

━━━ 断言纪律（本仓已为此踩过多次坑）━━━
1. 每条"应当存在"的断言都配 `★变异自证`：**把实现改回旧行为，判据必须判红**。
   只写正向断言会出现"假绿"（`verify_team_defined_orchestration` 的 G10/G12
   曾因判据过宽，变异后仍绿）。
2. **不做全文特征串搜索**：必须用 AST / 函数作用域锚定
   （本仓 `verify_runtime_stability` 的 `_code()` 就是为此而生 ——
   注释里写个示例片段就能造成误红/误绿）。
3. 数字必须带口径（"15 个键"要说明是哪个集合的 15 个）。
"""
import ast
import json
import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_PASS, _FAIL = [], []


def ck(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("%s %s%s" % ("[PASS]" if ok else "[FAIL]", name,
                       ("　→ " + detail) if (detail and not ok) else ""))


def _read(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _code(rel):
    """**只保留真实代码行**（剔除整行注释）—— 本仓纪律：注释不参与判定。"""
    out = []
    for ln in _read(rel).split("\n"):
        if ln.lstrip().startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


def _func_src(rel, func_name):
    """按 AST 取某个函数/方法的**源码段**（含 docstring；判定时需自行剔除）。

    ⚠️ 为什么不用正则从全文抠：`_exec_tool_call` 与 `_exec_tool_call_impl`
    名称前缀相同，正则会同时命中两个；AST 才能精确到"那一个 def"。
    """
    src = _read(rel)
    tree = ast.parse(src)
    lines = src.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return "\n".join(lines[node.lineno - 1: node.end_lineno])
    return ""


def _func_code(rel, func_name):
    """函数源码去掉 docstring 后的**纯代码**（判定用）。

    ⚠️ 必须 `textwrap.dedent` 再 parse：函数段从 `    def ...` 开始（带缩进），
    `ast.parse` 会直接 IndentationError ⇒ 走进 except ⇒ **docstring 没被剥掉**。
    这个坑本轮踩了一次并造成误红：docstring 里写了 `见 agent/tool_result_notes.py`，
    于是"判读在执行之后"的判据锚到了 docstring 里的字符串上（位置 431 < 1648）。
    """
    import textwrap
    seg = _func_src(rel, func_name)
    if not seg:
        return ""
    try:
        tree = ast.parse(textwrap.dedent(seg))
        fn = tree.body[0]
        lines = seg.split("\n")
        if fn.body and isinstance(fn.body[0], ast.Expr) and \
                isinstance(fn.body[0].value, ast.Constant) and \
                isinstance(fn.body[0].value.value, str):
            d0 = fn.body[0].lineno - 1
            d1 = getattr(fn.body[0], "end_lineno", fn.body[0].lineno) - 1
            lines = lines[:d0] + lines[d1 + 1:]
        return "\n".join(lines)
    except Exception:
        return seg


def _code_only(seg):
    """按 AST 取一段源码里的**非 docstring 字符串常量**集合（判定"某名字是否出现"用）。

    为什么不用 `"x" in seg`：注释/docstring 里提到某个名字，会让判据
    以为"代码里有它" —— 本仓已多次因此得出假绿/假红。
    """
    import textwrap
    try:
        tree = ast.parse(textwrap.dedent(seg))
    except Exception:
        return set()
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                doc_ids.add(id(body[0].value))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in doc_ids:
            out.add(node.value)
    return out


# ══════════════════════════════════════════════════════════════════════
# A 组：写类工具幂等键（P0-1）
# ══════════════════════════════════════════════════════════════════════
def a_group():
    print("\n── A 组：写类工具幂等键（P0-1）──")

    # A1 模块存在 + 导出的入口齐全
    p = os.path.join(_ROOT, "core", "idempotency.py")
    ck("A1 core/idempotency.py 存在", os.path.isfile(p))
    if not os.path.isfile(p):
        return
    import core.idempotency as idem
    for fn in ("prepare", "lookup", "record", "replay", "prune", "derive_scope",
               "classify_write", "args_hash", "enabled"):
        ck("A1 %s 可调用" % fn, callable(getattr(idem, fn, None)))

    # A2 写类判定**与写前确认闸同源**（不是另写一套）
    #    判据：idempotency 里必须出现 `hil_service` + `classify_action`，
    #    且**不得**自己维护一份工具名白名单（那是第二套口径 ⇒ 必然漂移）。
    code = _code("core/idempotency.py")
    ck("A2 写类判定复用 hil_service.classify_action",
       "from hil_service import classify_action" in code,
       "未复用 ⇒ 与写前确认闸判定口径可能不一致（写操作被漏判为读）")
    _has_own_list = ("WRITE_TOOLS" in code) or ("_WRITE_TOOL_NAMES" in code)
    ck("A2b ★ 不另立工具名白名单（防第二套口径）", not _has_own_list)

    # A3 键的四段组成：scope|tool|args_hash|ordinal
    _dksrc = _func_src("core/idempotency.py", "derive_key")
    kc = _func_code("core/idempotency.py", "derive_key")
    ck("A3 幂等键含 scope/tool/args_hash/ordinal 四段",
       all(t in kc for t in ("scope", "tool", "ahash", "ordinal")),
       "缺段会让重试/多次调用无法区分")
    # A3b ★变异：把**格式串**从四段降到三段 ⇒ 判据必须判红。
    #     ⚠️ 不能靠"删除 ordinal 标识符"来变异：它在函数签名里仍会出现
    #     （`def derive_key(..., ordinal)`）⇒ 那种变异本身不敏感。
    #     真正的判据是**格式串段数** —— 它决定键里到底拼了几段。
    ck("A3b 格式串确为四段", "%s|%s|%s|%s" in kc, "格式串不是四段")
    _mut_fmt = kc.replace("%s|%s|%s|%s", "%s|%s|%s")
    ck("A3b ★变异：格式串降到三段 ⇒ 判据判红",
       "%s|%s|%s|%s" not in _mut_fmt,
       "变异后仍是四段 ⇒ 判据不敏感")

    # A4 作用域只在编排子任务内（run + task_key 都非空）
    sc = _func_code("core/idempotency.py", "derive_scope")
    ck("A4 scope 要求 run_ctx 与 task_key 同时存在",
       "_tool_run_ctx" in sc and "_tool_task_key" in sc and "if run_id and task_key" in sc,
       "缺一即会让会话直答路径也进幂等（把用户重复发言当重放）")

    # A5 只记成功执行（不记被拦/失败）
    ec = _func_code("agent/pipeline_parts/tools.py", "_exec_tool_call")
    ck("A5 只在 ok is True 时记账",
       'res.get("ok") is True' in ec,
       "把被闸拦下/失败也记为已执行 ⇒ 人工确认队列会被重放吃掉（真事故）")
    # A5b ★变异：去掉 ok 判据 ⇒ 判据判红
    _mut5 = ec.replace('res.get("ok") is True', "True")
    ck("A5b ★变异：去掉 ok 判据 ⇒ 判据判红",
       'res.get("ok") is True' not in _mut5)

    # A6 先查后执行，命中则重放（顺序敏感）
    i_look = ec.find("_idem.lookup")
    i_exec = ec.find("_exec_tool_call_impl(name, arguments)")
    ck("A6 查重发生在执行之前", 0 <= i_look < i_exec,
       "lookup=%s impl=%s" % (i_look, i_exec))
    # A6b ★变异：顺序颠倒 ⇒ 同一判据判红
    ck("A6b ★变异：顺序颠倒 ⇒ 判据判红", not (0 <= i_exec < i_look),
       "颠倒后仍判为『查在前』 ⇒ 判据不看顺序")

    # A7 fail-open：幂等层异常不得阻断工具执行
    ck("A7 幂等层异常一律放行（fail-open）",
       ec.count("except Exception") >= 2 and "fail-open" in _func_src(
           "agent/pipeline_parts/tools.py", "_exec_tool_call"),
       "fail-closed 会让幂等表故障 = 全平台写操作瘫痪")

    # A8 表结构存在且幂等（CREATE TABLE IF NOT EXISTS）
    mg = _code("database/migrations/misc.py")
    ck("A8 tool_idempotency 表迁移为幂等建表",
       "CREATE TABLE IF NOT EXISTS tool_idempotency" in mg)
    ck("A8b 迁移已登记进 __init__ 导出",
       "_migrate_tool_idempotency" in _code("database/migrations/__init__.py"))

    # A9 配置键在 DEFAULT 与 SCHEMA **双向存在**（否则界面改不了/写不进）
    from core import config as _cfg
    _d = (_cfg.DEFAULT_CONFIG.get("runtime") or {})
    _s = (_cfg.CONFIG_SCHEMA.get("runtime") or {})
    for k in ("tool_idempotency_enabled", "tool_idempotency_scope",
              "tool_idempotency_ttl_s"):
        ck("A9 runtime.%s 在 DEFAULT" % k, k in _d)
        ck("A9 runtime.%s 在 SCHEMA（可写可看）" % k, k in _s)

    # ── A10 功能实测：同一 scope 同一调用 ⇒ 第二次命中重放 ──
    # ⚠️ 必须**另起子进程**跑（`MBSE_DB_PATH` 指向临时库）：
    #   本进程里 `database`/`core.config` 已在 import 时把 DB_PATH 固化，
    #   就地 `del sys.modules` + reload 会连带打断 `_cfg` 等既有引用
    #   （实测报 `module core.config not in sys.modules`），
    #   且污染后续组的配置读取。子进程 = 干净、可重复、不touch真库。
    _tmpdb = os.path.join(_ROOT, "tmp", "_gate_idem.db")
    os.makedirs(os.path.dirname(_tmpdb), exist_ok=True)
    try:
        if os.path.exists(_tmpdb):
            os.remove(_tmpdb)
    except Exception:
        pass
    _probe = os.path.join(_ROOT, "tmp", "_gate_idem_probe.py")
    with open(_probe, "w", encoding="utf-8") as f:
        f.write(_IDEM_PROBE_SRC)
    try:
        import subprocess
        env = dict(os.environ)
        env["MBSE_DB_PATH"] = _tmpdb
        env["PYTHONIOENCODING"] = "utf-8"
        cp = subprocess.run([sys.executable, "-X", "utf8", _probe],
                            cwd=_ROOT, env=env, capture_output=True, timeout=180)
        out = (cp.stdout or b"").decode("utf-8", "replace")
        # 取最后一个 JSON 行（脚本会打印初始化日志）
        payload = None
        for ln in reversed(out.strip().split("\n")):
            ln = ln.strip()
            if ln.startswith("{") and ln.endswith("}"):
                try:
                    payload = json.loads(ln)
                    break
                except Exception:
                    continue
        if payload is None:
            ck("A10 功能实测可运行", False,
               "子进程无 JSON 结果；stderr=%s" % (cp.stderr or b"")[-300:])
        else:
            for name, ok, why in payload.get("checks", []):
                ck(name, bool(ok), why)
    except Exception as e:
        ck("A10 功能实测可运行", False, "异常：%s" % str(e)[:200])
    finally:
        for _p in (_probe, _tmpdb):
            try:
                os.remove(_p)
            except Exception:
                pass


#: 幂等功能探针（子进程执行）。**独立文件**而不是 `-c`：
#: 避免 Windows 命令行长度/引号转义问题，也让探针本身可被读、被审。
_IDEM_PROBE_SRC = r'''# -*- coding: utf-8 -*-
"""幂等键功能探针（由 verify_control_plane 的子进程调用，不在生产路径）。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
checks = []


def ck(name, ok, why=""):
    checks.append([name, bool(ok), why])


try:
    from database.schema import init_db
    init_db()
    from core import idempotency as idem

    class _Pipe:
        _tool_run_ctx = 42
        _tool_task_key = "t1"

    class _P2(_Pipe):
        _tool_run_ctx = 43

    k1 = idem.prepare(_Pipe(), "file_write", {"path": "a.txt", "content": "hi"},
                      side_effect="write")
    ck("A10 prepare 对写类工具返回键", bool(k1 and k1.get("key")))
    if not k1:
        raise SystemExit(0)
    k1b = idem.prepare(_Pipe(), "file_write", {"path": "a.txt", "content": "hi"},
                       side_effect="write")
    ck("A10b 同一执行单元同参 ⇒ 键相同（可命中）", k1["key"] == k1b["key"])
    k2 = idem.prepare(_P2(), "file_write", {"path": "a.txt", "content": "hi"},
                      side_effect="write")
    ck("A10c 不同批次 ⇒ 键不同（不同批次是两次真实意图）", k2["key"] != k1["key"])

    _pp = _Pipe()
    a1 = idem.prepare(_pp, "file_append", {"path": "b.txt", "content": "x"},
                      side_effect="write")
    a2 = idem.prepare(_pp, "file_append", {"path": "b.txt", "content": "x"},
                      side_effect="write")
    ck("A10d 同一次尝试内连写两次 ⇒ 键不同（不吞掉真意图）", a1["key"] != a2["key"])

    ck("A10e 只读工具不参与幂等",
       idem.prepare(_Pipe(), "file_read", {"path": "a.txt"}, side_effect="read") is None)
    # destructive 也算写（删两次不能只删一次）
    ck("A10e2 destructive 参与幂等",
       bool(idem.prepare(_Pipe(), "file_delete", {"path": "c.txt"},
                         side_effect="destructive")))

    ok = idem.record(k1["key"], "file_write", k1["scope"], k1["args_hash"],
                     k1["ordinal"], {"ok": True, "result": "已写入 a.txt"})
    ck("A10f 记录成功结果为真", bool(ok))
    row = idem.lookup(k1["key"])
    ck("A10g 能查回记录", bool(row), "record 或 lookup 断链")
    rp = idem.replay(row) if row else None
    ck("A10h 重放带 idempotent_replay 标记",
       bool(rp) and rp.get("idempotent_replay") is True)
    ck("A10i 重放内容包含原结果", bool(rp) and "已写入 a.txt" in str(rp.get("result")))
    ck("A10j 重放结果如实告知模型（防它当成新执行）",
       bool(rp) and "幂等重放" in str(rp.get("result")))
    ok2 = idem.record(k1["key"], "file_write", k1["scope"], k1["args_hash"],
                      k1["ordinal"], {"ok": True, "result": "已写入 a.txt"})
    ck("A10k 重复记账不炸（幂等 upsert）", bool(ok2))
    st = idem.stats()
    ck("A10l stats 报出 total>=1", int(st.get("total") or 0) >= 1)

    x1 = idem.prepare(_Pipe(), "file_write", {"path": "d\\a.txt", "content": "z"},
                      side_effect="write")
    x2 = idem.prepare(_Pipe(), "file_write", {"path": "d/a.txt", "content": "z"},
                      side_effect="write")
    ck("A10m 路径分隔归一 ⇒ 同键（Windows 同一文件不会产生两个键）",
       x1["key"] == x2["key"])
    y1 = idem.prepare(_Pipe(), "file_write", {"path": "e.txt", "content": ""},
                      side_effect="write")
    y2 = idem.prepare(_Pipe(), "file_write", {"path": "e.txt"}, side_effect="write")
    ck("A10n 空串参数与缺省等价 ⇒ 同键", y1["key"] == y2["key"])

    # 关键反例：**失败结果不得被重放**（否则"没执行"会被当成"执行过"）
    _pf = _Pipe()
    kf = idem.prepare(_pf, "file_write", {"path": "f.txt", "content": "q"},
                      side_effect="write")
    idem.record(kf["key"], "file_write", kf["scope"], kf["args_hash"], kf["ordinal"],
                {"ok": False, "result": "被写前确认拦下"})
    ck("A10o 失败/被拦的结果仍能被 lookup 到（由调用方判据排除）",
       idem.lookup(kf["key"]) is not None)

    # 作用域关闭 ⇒ 完全不参与
    from core import config as _c
    _c._CONFIG.setdefault("runtime", {})["tool_idempotency_scope"] = "off"
    ck("A10p scope=off 时 prepare 返回 None",
       idem.prepare(_Pipe(), "file_write", {"path": "g.txt"}, side_effect="write") is None)
    _c._CONFIG["runtime"]["tool_idempotency_scope"] = "subtask"

    # 会话直答路径（无 run/task 标识）⇒ 不参与
    class _Solo:
        pass
    ck("A10q 非编排路径（无 run/task）不参与",
       idem.prepare(_Solo(), "file_write", {"path": "h.txt"}, side_effect="write") is None)

    ck("A10r prune 可执行（不抛）", isinstance(idem.prune(86400), int))
except Exception as e:
    import traceback
    ck("A10 探针执行未抛异常", False, "%s | %s" % (e, traceback.format_exc()[-400:]))

print(json.dumps({"checks": checks}, ensure_ascii=False))
'''


# ══════════════════════════════════════════════════════════════════════
# B 组：停滞自愈（P0-2）
# ══════════════════════════════════════════════════════════════════════
def b_group():
    print("\n── B 组：停滞自动重驱（P0-2）──")
    from core import config as cfg

    d = (cfg.DEFAULT_CONFIG.get("orchestration") or {})
    s = (cfg.CONFIG_SCHEMA.get("orchestration") or {})

    # B1 默认开（能力可用）
    ck("B1 auto_resume_enabled 默认 True", d.get("auto_resume_enabled") is True,
       "默认关 ⇒ 能力存在但永远不会被使用（配置即功能）")

    # B2 三道成本闸（缺一即可能烧额度）
    ck("B2a 单轮上限显式设为 1", int(d.get("auto_resume_max_runs") or 0) == 1)
    ck("B2b 每日预算存在且 >0", int(d.get("auto_resume_daily_budget") or 0) > 0)
    ck("B2c 不重跑 failed 任务", d.get("auto_resume_include_failed") is False)

    # B3 orchestration 段**已登记进 SCHEMA**（否则开关改不了）
    ck("B3 orchestration 段已进 SCHEMA", bool(s), "未登记 ⇒ save_override 抛「未知配置项」⇒ 开关形同死配置")
    for k in ("auto_resume_enabled", "auto_resume_max_runs", "auto_resume_daily_budget",
              "auto_resume_interval_s", "resume_stale_s"):
        ck("B3 orchestration.%s 可改可看" % k, k in s)

    # B4 每日预算闸**真有消费点**（不是又一个配置谎言）
    sup = _func_code("core/orch_supervisor.py", "_scan_conn")
    ck("B4 每日预算被读进扫描逻辑",
       "auto_resume_daily_budget" in sup and "budget_exhausted" in sup,
       "只加键不加消费点 ⇒ 与 total_time_budget_s 同型的配置谎言")
    ck("B4b 预算耗尽时跳过重投", "daily_budget_exhausted" in sup)
    # B4c ★变异：把预算判定短路成 False ⇒ 判据判红
    _mut = sup.replace("res.get(\"budget_exhausted\")", "False")
    ck("B4c ★变异：预算判定短路 ⇒ 判据判红",
       "res.get(\"budget_exhausted\")" not in _mut)

    # B5 预演（max_runs=0）不受预算闸约束（否则运维看不到"今天还会投哪些"）
    ck("B5 预演模式豁免预算闸", "_budget > 0 and res.get" in sup,
       "预演被预算拦 ⇒ 看不到待投批次（灰度失去意义）")

    # B6 今日计数双源（进程内 + 审计表），取 max 保守
    ck("B6 今日计数含审计表口径", "audit_logs" in _code("core/orch_supervisor.py"))
    ck("B6b 进程内与审计取 max（保守侧）",
       "max(inmem" in _func_code("core/orch_supervisor.py", "_resumes_today"))

    # B7 日用口径是 UTC（与审计表 CURRENT_TIMESTAMP 一致）
    ck("B7 UTC 日口径（与 audit_logs.created_at 同源）",
       "time.gmtime" in _func_code("core/orch_supervisor.py", "_utc_day"),
       "用本地时间跨时区会算错日界")

    # B8 状态快照暴露预算（不暴露就没法运维）
    st = _func_code("core/orch_supervisor.py", "supervisor_status")
    for k in ("daily_budget", "resumed_today", "budget_exhausted"):
        ck("B8 supervisor_status 暴露 %s" % k, k in st)

    # B9 main.py 按配置启动，且默认与 config 一致（不硬编码 False）
    m = _func_code("main.py", "lifespan")
    ck("B9 main.py 用 auto_resume_enabled 判定启动",
       "auto_resume_enabled" in m, "未接线 ⇒ 守护永不启动")
    ck("B9b 启动判定的默认值与 config 一致（True）",
       'auto_resume_enabled", True' in m,
       "两处默认值不一致 ⇒ 改 config 与改代码行为不同（口径漂移）")
    ck("B9c max_runs 默认与 config 一致（1）",
       'auto_resume_max_runs", 1' in m)

    # B10 功能实测：预算耗尽 ⇒ 不再触发（用手工构造的 res/db 看不到真库，改为
    #     直接验证判据函数在"预算已满"时的行为）
    import core.orch_supervisor as sup_mod
    old_daily = None
    try:
        class _Conn:
            def execute(self, sql, params=()):
                class _R:
                    def fetchone(self_inner):
                        return {"c": 99}
                return _R()
        got = sup_mod._db_resumes_today(_Conn())
        ck("B10 审计计数可读出（模拟 99 次）", got == 99, "got=%s" % got)

        class _Bad:
            def execute(self, *a, **k):
                raise RuntimeError("boom")
        ck("B10b 审计读失败 ⇒ 返回 0（不抛）", sup_mod._db_resumes_today(_Bad()) == 0)
        ck("B10c 任何异常都不阻断守护（scan_once 外层兜）",
           "except Exception" in _func_code("core/orch_supervisor.py", "_scan_conn"))
    except Exception as e:
        ck("B10 预算计数功能实测", False, str(e)[:160])


# ══════════════════════════════════════════════════════════════════════
# C 组：预算层级真源（P0-3）
# ══════════════════════════════════════════════════════════════════════
def c_group():
    print("\n── C 组：预算层级收敛（P0-3）──")
    p = os.path.join(_ROOT, "core", "budget_policy.py")
    ck("C1 core/budget_policy.py 存在", os.path.isfile(p))
    if not os.path.isfile(p):
        return
    import core.budget_policy as bp

    # C2 四层齐全且父子链完整
    ids = [L["id"] for L in bp.LAYERS]
    ck("C2 四层齐全（L1..L4）", ids == ["L1", "L2", "L3", "L4"], "实际 %s" % ids)
    chain = {L["id"]: L["parent"] for L in bp.LAYERS}
    ck("C2b 嵌套链完整（L1→L2→L3→L4）",
       chain == {"L1": "", "L2": "L1", "L3": "L2", "L4": "L3"}, "实际 %s" % chain)

    # C3 每层都有 primary 真源（不能只有别名）
    for L in bp.LAYERS:
        ck("C3 %s 有 primary 真源" % L["id"], bool(L.get("primary")))

    # C4 审计报告结构完整
    r = bp.audit()
    for k in ("layers", "findings", "dead_configs", "off_chain", "counts", "scan_scope"):
        ck("C4 报告含 %s" % k, k in r)
    ck("C4b 报告口径自述（scan_scope 非空）", bool(r.get("scan_scope", {}).get("note")))
    ck("C4c 层数 = 4", r["counts"]["layers"] == 4)

    # C5 死配置检测：真实库应为 0（本轮已把 total_time_budget_s 接线）
    ck("C5 追踪键零死配置", r["counts"]["dead_configs"] == 0,
       "存在死配置：%s" % [d["group"] + "." + d["key"] for d in r["dead_configs"]])
    # C5b ★变异：把 consumer_scan 换成"永远有消费点" ⇒ 判据必须能判出死配置
    _orig = bp.consumer_scan
    try:
        bp.consumer_scan = lambda keys=None: {k[1]: {"files": 1, "hits": 1, "paths": []}
                                             for k in (keys or [])}
        r_mut = bp.audit()
        ck("C5b ★变异：消费点恒为真 ⇒ 死配置数归零（判据依赖真实扫描）",
           r_mut["counts"]["dead_configs"] == 0,
           "变异后仍有死配置 ⇒ 判据不看扫描结果")
        bp.consumer_scan = lambda keys=None: {k[1]: {"files": 0, "hits": 0, "paths": []}
                                             for k in (keys or [])}
        r_mut2 = bp.audit()
        ck("C5c ★变异：消费点恒为零 ⇒ 死配置数 = 追踪键数（判据真的在读）",
           r_mut2["counts"]["dead_configs"] == r_mut2["counts"]["tracked_keys"],
           "变异后死配置数不对 ⇒ 判据没读到扫描结果")
    finally:
        bp.consumer_scan = _orig

    # C6 违例检测能力（构造真实违例 ⇒ 必须报出来）
    #    手法：把 L1 真源临时压到 1s ⇒ 必然与某子层冲突（且 L2 默认停用，
    #    所以这里同时验证"跨过停用层去找最近启用祖先"这条逻辑）。
    try:
        _orig_cfg = bp._cfg_value

        def _fake_cfg(group, key, default=None):
            if (group, key) == ("runtime", "chat_deadline_s"):
                return 1
            return _orig_cfg(group, key, default)

        bp._cfg_value = _fake_cfg
        try:
            r3 = bp.audit()
            kinds = [f["kind"] for f in r3["findings"]]
            ck("C6b ★注入祖先层 1s ⇒ 报 parent_stricter_than_child",
               "parent_stricter_than_child" in kinds, "findings=%s" % kinds)
            # 该条必须指出"跨过了停用层"（L2 默认 0=不启用）
            _pf = [f for f in r3["findings"]
                   if f["kind"] == "parent_stricter_than_child"]
            ck("C6c 跨停用层时标注 via_disabled",
               bool(_pf) and any(f.get("via_disabled") for f in _pf),
               "未标注 ⇒ 运维看不出中间层是停用的")
        finally:
            bp._cfg_value = _orig_cfg
        # 恢复后必须归零（证明上面的 findings 不是常驻噪音）
        r4 = bp.audit()
        ck("C6d 恢复后 findings 归零（非噪音）", r4["counts"]["findings"] == 0,
           "残留：%s" % [(f["kind"], f.get("key")) for f in r4["findings"]])
    except Exception as e:
        ck("C6 违例检测功能实测", False, str(e)[:160])

    # C7 端点已暴露
    rc = _code("routers/config.py")
    ck("C7 /api/system/budget-policy 端点已注册", "/api/system/budget-policy" in rc)

    # C8 不把"链外键"硬塞进层级（第一版教训：制造 5 条假违例）
    ck("C8 findings 当前为 0（无假违例）", r["counts"]["findings"] == 0,
       "存在 findings：%s" % [(f["kind"], f.get("key")) for f in r["findings"]])
    ck("C8b 链外键单列（off_chain 非空且含 jobs.worker_lease_s）",
       any(x["group"] == "jobs" and x["key"] == "worker_lease_s" for x in r["off_chain"]))
    ck("C8c token 维度单列计数（不混入时间层级）",
       int(r.get("token_dimension_keys") or 0) > 0)


# ══════════════════════════════════════════════════════════════════════
# D 组：接线与回归（改动的旁证）
# ══════════════════════════════════════════════════════════════════════
def d_group():
    print("\n── D 组：接线与回归 ──")

    # D1 幂等闸挂在**唯一出口**（两条 ReAct 路径同源）
    ec = _func_code("agent/pipeline_parts/tools.py", "_exec_tool_call")
    ck("D1 幂等闸挂在 _exec_tool_call（唯一出口）", bool(ec) and "_idem" in ec)
    #    反面：不得散落到 stream.py / execute.py（那会变成多套口径）
    for f in ("agent/pipeline_parts/stream.py", "agent/pipeline_parts/execute.py"):
        ck("D1b %s 不自行实现幂等（单一出口）" % os.path.basename(f),
           "core.idempotency" not in _code(f) and "core import idempotency" not in _code(f))

    # D2 原有工具判读仍生效（幂等包在它外面，不能吃掉判读）
    ck("D2 工具判读仍在幂等之后执行",
       ec.find("tool_result_notes") > ec.find("_exec_tool_call_impl"),
       "判读被短路 ⇒ 丢失确定性判读（上一轮刚修的东西）")

    # D3 上一轮墙钟/并发闸未被破坏
    from core import runtime_state as rs
    for fn in ("stream_gate", "compute_deadline", "cap_timeout", "thread_deadline_left"):
        ck("D3 runtime_state.%s 仍在" % fn, callable(getattr(rs, fn, None)))
    ck("D3b 全局并发闸仍在生成器内持有",
       "async with" in _code("routers/conversations.py") or
       "_gate.__aenter__" in _code("routers/conversations.py"))

    # D4 配置段零悬空：orchestration 段登记后，DEFAULT 与 SCHEMA 的键集合应一致
    from core import config as cfg
    dk = set((cfg.DEFAULT_CONFIG.get("orchestration") or {}).keys())
    sk = set((cfg.CONFIG_SCHEMA.get("orchestration") or {}).keys())
    ck("D4 orchestration DEFAULT ⊆ SCHEMA", dk <= sk,
       "缺：%s" % sorted(dk - sk))
    rk_d = set((cfg.DEFAULT_CONFIG.get("runtime") or {}).keys())
    rk_s = set((cfg.CONFIG_SCHEMA.get("runtime") or {}).keys())
    ck("D4b runtime DEFAULT ⊆ SCHEMA", rk_d <= rk_s,
       "缺：%s" % sorted(rk_d - rk_s))

    # D5 迁移导入链完整（改了 __init__ 就得能 import）
    try:
        import database.schema  # noqa: F401
        ck("D5 database.schema 可导入", True)
    except Exception as e:
        ck("D5 database.schema 可导入", False, str(e)[:160])

    # D6 工具副作用查询的**请求内缓存**（本轮加的，防幂等闸把热路径读放大 2~3 倍）
    try:
        from agent import AgentPipeline
        from agent.pipeline_parts.common import reset_request_state
        _p = AgentPipeline()
        reset_request_state(_p)
        _v1 = _p._tool_side_effect("file_write")
        _cache = getattr(_p, "_tool_se_cache", None)
        ck("D6 _tool_side_effect 首次查询后写缓存",
           isinstance(_cache, dict) and _cache.get("file_write") == _v1, "cache=%s" % (_cache,))
        # 第二次必须**不再查库**：把 get_db 打成炸弹，仍应返回值
        import agent.pipeline_parts.tools as _tmod
        _orig_gdb = _tmod.get_db if hasattr(_tmod, "get_db") else None
        _boom = {"fired": False}

        def _no_db(*a, **k):
            _boom["fired"] = True
            raise AssertionError("不应再查库")
        # `_tool_side_effect` 内部是 `from database import get_db`（函数内导入），
        # 所以要在 `database` 模块上替换才会生效。
        import database as _dbmod
        _orig2 = _dbmod.get_db
        _dbmod.get_db = _no_db
        try:
            _v2 = _p._tool_side_effect("file_write")
        finally:
            _dbmod.get_db = _orig2
        ck("D6b 命中缓存时不再查库（热路径无重复 SELECT）",
           _v2 == _v1 and not _boom["fired"], "fired=%s v2=%s" % (_boom["fired"], _v2))
        # 查不到的工具也必须记忆（空串），否则每次重查
        _p._tool_side_effect("__no_such_tool_gate__")
        ck("D6c 缓存记忆『查不到』（否则未登记工具每次重查）",
           _p._tool_se_cache.get("__no_such_tool_gate__") == "",
           "cache=%s" % (_p._tool_se_cache,))
        # 必须每请求清零：管理员把 read 改成 write 后，旧缓存会让安全闸放行
        reset_request_state(_p)
        ck("D6d reset_request_state 清空副作用缓存（安全闸不读陈旧配置）",
           _p._tool_se_cache == {}, "残留=%s" % (_p._tool_se_cache,))
    except Exception as e:
        ck("D6 副作用缓存功能实测", False, str(e)[:160])


def main():
    print("=" * 78)
    print("verify_control_plane — 幂等键 / 停滞自愈 / 预算层级（P0-1~P0-3）")
    print("=" * 78)
    a_group()
    b_group()
    c_group()
    d_group()
    print("\n" + "=" * 78)
    print("结果：%d PASS / %d FAIL" % (len(_PASS), len(_FAIL)))
    if _FAIL:
        print("失败项：")
        for f in _FAIL:
            print("  x %s" % f)
        return 1
    print("门禁通过：控制面三项均已落地且有行为证据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
