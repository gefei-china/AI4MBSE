# -*- coding: utf-8 -*-
"""skill_script_run —— 真正执行技能包自带的 `scripts/*.py`（2026-11-07，P1）

## 为什么需要（上一轮的实测缺口）

7 个已发布技能声明了 `scripts/main.py` / `dump_tools.py` / `lookup.py`，文件都真实存在，
但 `agent/pipeline_parts/skills.py:355` 只往上下文里注入一行：

    ⚙ 脚本（需要时执行）：scripts/main.py

**没有任何执行入口** ⇒ 模型拿到的是"一句话提示"，做不了任何事。
所以"技能自己带逻辑"这件事在此前是**假的**：技能 = 一段 Markdown 正文，
脚本 = 磁盘上的死文件。

## 关键设计取舍：**不能**复用 code_exec 的沙箱

第一版想法是"读脚本源码 → 丢给 `sandbox.run_code`"。**实测不成立**：
技能脚本是**平台受信脚本**，它们就是 `import os` / `sys.path.insert` /
`from core import config` / `from zhiyuan_client import ZhiyuanClient` 写的
（见 `zhiyuan_mgmt/scripts/main.py`）。沙箱的 import 白名单只有
json/math/re/random/datetime/collections/itertools/statistics/string/functools/operator/typing，
且 builtins 里没有 open ⇒ **每一个现有脚本都会在 import 行直接失败**。

⇒ 因此本工具走**独立的受信执行通道**，并把这个越权**显式标注**出来：

| 维度 | code_exec（不可信） | skill_script_run（受信） |
|---|---|---|
| 解释器 | 沙箱 runner（受限 builtins） | 平台同一 venv 的真解释器 |
| 文件/网络 | 无 | **有**（脚本可以读写、可以发 HTTP） |
| 权限档位 | `read` | **`write`**（绝不标 read） |
| 谁能调 | 所有 Agent | 仅当技能命中且白名单含本工具 |
| 路径 | 无（不碰文件） | **强制限定在该技能 `package_path` 内** |

`side_effect='write'` 是有意的：脚本能产生持久副作用，标 read 等于骗过写前确认闸。
代价是**没有技能语境时不注入**（写意图闸），这正是想要的。

## 四道安全边界

1. **路径限定**：`realpath` 前缀校验 + 只允许 `.py` + 只允许技能 manifest 声明过的路径
   （不接受任意路径，防「技能名 + ../../ 敏感文件」）
2. **独立进程 + 超时强杀**：`subprocess.run(timeout=)`，与主服务隔离，死循环不留残骸
3. **输出截断**：stdout/stderr 各 20K 字符
4. **不留 shell**：执行的是"脚本文件路径"，不是模型拼的命令行字符串
   （`[python, runner, script_path]` 参数分离，模型无法注入额外参数）

## 调用协议（两段式，兼容现有 7 个脚本）

脚本有两种写法，本工具都支持：

| 脚本形态 | args 为空 | args 非空 |
|---|---|---|
| 有 `run(**kwargs)`（现有 7 个都是） | 调 `run()` 并打印返回值 | 调 `run(**args)` |
| 只有 `if __name__ == "__main__"` 的顶层代码 | `runpy.run_path(run_name="__main__")` | 同左（args 忽略，会明确提示） |

这样"直接跑脚本"和"带参数跑脚本"都能用，不用改造已有技能。
"""
from __future__ import annotations

import json
import os

__all__ = ["exec_skill_tool", "SKILL_TOOL_NAMES", "SKILL_TOOL_DEFS"]

#: 超时默认/上限（秒）。技能脚本常含 HTTP 往返（智源连通性自检），故默认比 code_exec 大。
DEFAULT_TIMEOUT = 30.0
MAX_TIMEOUT = 120.0
#: stdout / stderr 各自截断上限（字符）
MAX_OUTPUT = 20_000

SKILL_TOOL_NAMES = ("skill_script_run",)

SKILL_TOOL_DEFS = [
    {
        "name": "skill_script_run",
        "description": (
            "执行某个技能包自带的脚本（技能 manifest 里声明过的 scripts/*.py）。"
            "用途：技能正文写着「脚本（需要时执行）」时，用它真正跑起来"
            "（例如「智源平台管理」的连通性自检、「智源MCP工具手册」的工具目录生成）。"
            "约定：不传 args 则调用脚本的 run() 或直接执行脚本；传 args 则调用 run(**args)。"
            "返回脚本的 stdout 与 return 值。"
            "限制：路径限定在该技能包目录内；超时默认 30 秒（上限 120）；"
            "脚本是平台受信脚本，**有文件与网络能力**，属写类操作。"
        ),
        # ★ 绝不能标 read：脚本可写文件、可发 HTTP，标 read 会绕过写前确认闸
        "side_effect": "write",
        "risk_level": "medium",
        "version": "v1.0",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string",
                          "description": "技能名（如「智源平台管理」）或技能 id"},
                "script": {"type": "string",
                           "description": "脚本相对路径，默认 scripts/main.py；"
                                          "必须在该技能 manifest 的 scripts 里声明过"},
                "args": {"type": "object",
                         "description": "传给脚本 run(**args) 的键值对；不传则调用 run()"},
                "argv": {"type": "array", "items": {"type": "string"},
                         "description": "按命令行参数传入（脚本用 sys.argv[1:] 解析时用这个，"
                                        "例如 [\"查询工程列表\"] 或 [\"--list\"]）；"
                                        "与 args 二选一，args 优先"},
                "timeout": {"type": "number",
                            "description": "超时秒数，默认 30，上限 120"},
            },
            "required": ["skill"],
        },
    },
]


# ── 子进程 runner ────────────────────────────────────────────────────────
# 为什么用 runpy 而不是 importlib：runpy 能复现"直接 python script.py"的语义
# （含 __name__ == "__main__"），且不需要 sys.path 里能 import 到脚本包。
# 为什么 output 用 base64：脚本 print 的中文在 Windows 管道默认 cp936，
#   混进 JSON 会让父进程 decode 失败（与 sandbox.py 同一坑）。
_RUNNER = r'''
import base64, io, json, os, runpy, sys, traceback

payload = json.loads(base64.b64decode(sys.stdin.buffer.read()).decode("utf-8"))
path = payload["path"]
argv = payload.get("args") or {}
# 脚本内可能读 sys.argv 做参数解析（lookup.py 就是 `sys.argv[1]` 当查询词）。
# ⚠️ argv 是**列表**，直接用 —— 绝不拼成命令行字符串再交给 shell。
sys.argv = [path] + [str(x) for x in (payload.get("argv") or [])]

_buf_out, _buf_err = io.StringIO(), io.StringIO()
_old_out, _old_err = sys.stdout, sys.stderr
sys.stdout, sys.stderr = _buf_out, _buf_err
ret = None
try:
    if argv:
        # 带参数：按模块加载并 run(**args)
        import importlib.util
        spec = importlib.util.spec_from_file_location("_skill_script", path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_skill_script"] = mod
        spec.loader.exec_module(mod)          # 只执行定义，不跑 __main__
        fn = getattr(mod, "run", None)
        if not callable(fn):
            raise RuntimeError(
                "该脚本没有可调用的 run()，不支持带参数执行；"
                "请把 args 置空以直接执行脚本")
        ret = fn(**argv)
        print(ret)
    else:
        # 等价于 `python <script>`：__main__ 里的 print 已被 _buf_out 捕获
        runpy.run_path(path, run_name="__main__")
        ret = None
except SystemExit as _se:
    # ★★ 绝不能把 SystemExit 一律当失败：本仓 7 个技能脚本几乎都写成
    #   `if __name__ == "__main__": sys.exit(main())`，而 `main()` 正常返回 0
    #   ⇒ 每个**成功**的执行都会抛 SystemExit(0)。
    #   实测踩坑：lookup.py --list 明明输出了 71 个工具，却被判 ok=false
    #   —— "把成功判成失败"比"失败没被发现"更伤信任。
    #   约定：code ∈ (None, 0) = 正常退出；其它非零 = 真失败。
    _code = getattr(_se, "code", None)
    if _code not in (None, 0):
        sys.stdout, sys.stderr = _old_out, _old_err
        print("__ERR__" + json.dumps({
            "type": "SystemExit", "msg": str(_code),
            "traceback": "", "stdout": _buf_out.getvalue()[-8000:],
            "stderr": _buf_err.getvalue()[-4000:],
        }, ensure_ascii=False))
        raise SystemExit(0)
except BaseException as e:
    sys.stdout, sys.stderr = _old_out, _old_err
    print("__ERR__" + json.dumps({
        "type": type(e).__name__, "msg": str(e)[:800],
        "traceback": traceback.format_exc()[-1500:],
        "stdout": _buf_out.getvalue()[-4000:],
        "stderr": _buf_err.getvalue()[-4000:],
    }, ensure_ascii=False))
    raise SystemExit(0)
finally:
    sys.stdout, sys.stderr = _old_out, _old_err

print("__RES__" + json.dumps({
    "stdout": _buf_out.getvalue(),
    "stderr": _buf_err.getvalue(),
    "return": (None if ret is None else str(ret)[:4000]),
}, ensure_ascii=False))
'''


def _clamp_timeout(v) -> float:
    """夹取超时：0/负数/非法 ⇒ 默认值（0 在 subprocess 里等于无限等待，比不传更危险）。"""
    try:
        t = float(v)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    if t <= 0:
        return DEFAULT_TIMEOUT
    return min(max(t, 1.0), MAX_TIMEOUT)


def _load_skill(skill: str) -> dict | None:
    """按名字或 id 取技能行（只要 published/active 可用的）。"""
    from database import get_db

    conn = get_db()
    try:
        row = None
        s = str(skill or "").strip()
        if s.isdigit():
            row = conn.execute(
                "SELECT id,name,package_path,scripts,status FROM skills WHERE id=?",
                (int(s),)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT id,name,package_path,scripts,status FROM skills WHERE name=? "
                "ORDER BY CASE status WHEN 'published' THEN 0 ELSE 1 END, id LIMIT 1",
                (s,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _resolve_script(row: dict, script: str) -> tuple[str, str]:
    """解析并校验脚本路径。返回 (绝对路径, 错误信息)。错误信息非空即拒绝。"""
    pkg = str(row.get("package_path") or "").strip()
    if not pkg:
        return "", "该技能没有 package_path（没有技能包目录），无法执行脚本"
    try:
        declared = [str(x) for x in json.loads(row.get("scripts") or "[]")]
    except Exception:
        declared = []
    if not declared:
        return "", "该技能没有声明任何脚本（scripts 为空）"

    rel = str(script or "").strip() or "scripts/main.py"
    # 归一化：允许模型写 "scripts\main.py" / "./scripts/main.py"
    rel = rel.replace("\\", "/").lstrip("./")
    if not rel:
        rel = "scripts/main.py"
    if not rel.lower().endswith(".py"):
        return "", "只允许执行 .py 脚本，收到：%s" % rel

    # ① 必须在 manifest 声明过的清单里（不接受任意脚本名）
    norm_declared = {d.replace("\\", "/").lstrip("./") for d in declared}
    if rel not in norm_declared:
        return "", ("脚本 %s 不在该技能的 scripts 声明里（已声明：%s）。"
                    "不在声明清单内的脚本不允许执行。"
                    % (rel, ", ".join(sorted(norm_declared)) or "无"))

    # ② realpath 前缀校验（防 ../ 穿越；即使声明表被污染也挡住）
    pkg_real = os.path.realpath(pkg)
    full = os.path.realpath(os.path.join(pkg_real, rel))
    if not (full == pkg_real or full.startswith(pkg_real + os.sep)):
        return "", "脚本路径越出技能包目录，已拒绝：%s" % rel
    if not os.path.isfile(full):
        return "", "脚本文件不存在：%s" % full
    return full, ""


def exec_skill_tool(name: str, arguments: dict | None = None) -> dict:
    """统一入口：执行技能脚本。返回 {"ok": bool, "result": str}（与 file_tools 同形态）。"""
    import base64
    import subprocess
    import sys

    args = arguments or {}
    if name not in SKILL_TOOL_NAMES:
        return {"ok": False, "result": "未知技能脚本工具: %s" % name}

    skill = str(args.get("skill") or "").strip()
    if not skill:
        return {"ok": False, "result": "skill 不能为空（技能名或技能 id）"}

    row = _load_skill(skill)
    if not row:
        return {"ok": False, "result": "找不到技能：%s" % skill}

    path, err = _resolve_script(row, args.get("script") or "")
    if err:
        return {"ok": False, "result": err}

    kwargs = args.get("args")
    if kwargs is not None and not isinstance(kwargs, dict):
        return {"ok": False, "result": "args 必须是对象（键值对）"}
    argv_list = args.get("argv")
    if argv_list is not None:
        if not isinstance(argv_list, list) or not all(
                isinstance(x, (str, int, float)) for x in argv_list):
            return {"ok": False, "result": "argv 必须是字符串数组"}
    timeout = _clamp_timeout(args.get("timeout"))

    payload = base64.b64encode(json.dumps(
        {"path": path, "args": kwargs or {}, "argv": argv_list or []},
        ensure_ascii=False).encode("utf-8")).decode()
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    try:
        p = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", _RUNNER],
            input=payload.encode("utf-8"),
            capture_output=True, timeout=timeout, env=env,
            cwd=os.path.dirname(path),      # 脚本按包相对路径找同级文件
        )
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "result": "脚本执行超时（>%ss），已强制终止：%s / %s"
                          % (timeout, row.get("name"), os.path.basename(path))}

    raw = (p.stdout or b"").decode("utf-8", "replace")
    err_raw = (p.stderr or b"").decode("utf-8", "replace")

    if "__ERR__" in raw:
        try:
            e = json.loads(raw.split("__ERR__", 1)[1].split("__RES__")[0])
        except Exception:
            e = {"type": "ScriptError", "msg": raw[:500]}
        _etype = str(e.get("type") or "")
        _emsg = str(e.get("msg") or "")
        # SystemExit 不是"崩溃"，是脚本**主动**退出（argparse 缺参、usage 分支）。
        # 直接抛 `SystemExit: 2` 对模型毫无操作性 ⇒ 翻成人话并指路。
        if _etype == "SystemExit":
            _msg = ("脚本以退出码 %s 结束 —— 通常是**缺少命令行参数**。"
                    "请读上面的 stdout（多数脚本在缺参时会打印用法），"
                    "再用 argv 传参重试（例：argv=[\"查询工程列表\"] 或 [\"--list\"]）。"
                    % (_emsg or "?"))
        else:
            _msg = "%s: %s" % (_etype, _emsg)
        detail = {
            "ok": False,
            "skill": row.get("name"),
            "script": os.path.basename(path),
            "error": _msg,
            "stdout": (e.get("stdout") or "")[:MAX_OUTPUT],
            "stderr": (e.get("stderr") or err_raw)[:MAX_OUTPUT],
            "traceback": (e.get("traceback") or "")[-2000:],
        }
        return {"ok": False,
                "result": json.dumps(detail, ensure_ascii=False, default=str)[:MAX_OUTPUT]}

    if "__RES__" not in raw:
        # 解释器级失败（语法错、进程被杀）—— 不能当成功
        return {"ok": False, "result": json.dumps({
            "ok": False, "skill": row.get("name"), "script": os.path.basename(path),
            "error": "解释器异常退出（code=%s），未产出结果" % p.returncode,
            "stdout": raw[:4000], "stderr": err_raw[:4000],
        }, ensure_ascii=False)[:MAX_OUTPUT]}

    try:
        res = json.loads(raw.split("__RES__", 1)[1])
    except Exception:
        return {"ok": False, "result": "结果解析失败：%s" % raw[:500]}

    out = {
        "ok": True,
        "skill": row.get("name"),
        "script": os.path.basename(path),
        "return": (res.get("return") or "")[:4000],
        "stdout": (res.get("stdout") or "")[:MAX_OUTPUT],
    }
    if res.get("return") is None and not (res.get("stdout") or "").strip():
        out["note"] = "脚本执行完成，但没有输出（既无 print 也无返回值）"
    return {"ok": True, "result": json.dumps(out, ensure_ascii=False, default=str)[:MAX_OUTPUT]}
