# -*- coding: utf-8 -*-
"""D5 沙箱代码执行模块：进程隔离 + 超时控制 + 受限环境（Python/JS）。

对齐调研报告「代码节点在安全沙箱中运行代码（Python/JS 脚本执行、数据转换、自定义逻辑）」：
- 进程隔离：subprocess 启动独立解释器进程，恶意/崩溃代码不影响主服务
- 超时控制：subprocess.run(timeout=...) 强杀超时进程，防死循环
- 受限环境：builtins 白名单 + import 模块白名单 + 禁用反射/文件/网络原语
- 双语言：python（内置）+ js（Node vm 受限上下文，无 require/process/Buffer）

对外接口：
    run_code(code, language="python", inputs=None, timeout=10, max_output=2000) -> dict
    返回 {"ok", "outputs", "content", "error"}；error 非空表示失败（超时/语法/受限操作）。
"""
import base64
import json
import os
import shutil
import subprocess
import sys

# ── 允许的 builtins（纯计算，无反射/IO/执行能力）──
_PY_BUILTINS = (
    "abs", "all", "any", "bool", "bytes", "bytearray", "chr", "dict", "divmod",
    "enumerate", "filter", "float", "format", "hash", "hex", "int", "isinstance",
    "iter", "len", "list", "map", "max", "min", "next", "oct", "ord", "pow",
    "print", "range", "reversed", "round", "set", "slice", "sorted", "str",
    "sum", "tuple", "zip", "repr",
)
# 显式禁止（即使白名单漏判也不安全）：eval/exec/compile/open/input/__import__/
# getattr/setattr/delattr/globals/locals/vars/type/dir/object/super/memoryview
_PY_ALLOW_IMPORTS = {
    "json", "math", "re", "random", "datetime", "collections",
    "itertools", "statistics", "string", "functools", "operator", "typing",
}

_JS_TIMEOUT_MS = "timeout_ms"  # vm.runInContext 的 timeout 参数键（毫秒）


# ── Python 沙箱执行器（在子进程内运行；payload 经 stdin base64 传入）──
_PY_RUNNER = r'''
import base64, json, sys
payload = json.loads(base64.b64decode(sys.stdin.buffer.read()).decode("utf-8"))
code = payload.get("code") or ""
inputs = payload.get("inputs") or {}
_b = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
_allow = {!BUILTINS!}
safe = {k: _b[k] for k in _allow if k in _b}
_orig_imp = _b.get("__import__")
_allowed_mods = {!MODS!}
def _imp(name, *a, **kw):
    if name.split(".")[0] not in _allowed_mods:
        raise ImportError("沙箱禁止导入模块: " + name)
    return _orig_imp(name, *a, **kw)
safe["__import__"] = _imp
_in = inputs if isinstance(inputs, dict) else {}
_out = {}
_globs = {"__builtins__": safe, "_in": _in, "_out": _out,
          "json": _orig_imp("json"), "math": _orig_imp("math"),
          "re": _orig_imp("re"), "random": _orig_imp("random"),
          "collections": _orig_imp("collections"), "itertools": _orig_imp("itertools")}
try:
    exec(compile(code, "<sandbox>", "exec"), _globs)
    # 用户代码可能整体重绑定 _out（_out = {...}），须从执行命名空间读取而非局部变量
    _out = _globs.get("_out", {}) if isinstance(_globs.get("_out"), dict) else _out
    sys.stdout.write("__OUT__" + json.dumps(_out, ensure_ascii=False, default=str))
except BaseException as e:
    sys.stdout.write("__ERR__" + json.dumps(
        {"type": type(e).__name__, "msg": str(e)[:500]}, ensure_ascii=False))
'''.replace("{!BUILTINS!}", json.dumps(list(_PY_BUILTINS))).replace(
    "{!MODS!}", json.dumps(sorted(_PY_ALLOW_IMPORTS)))


# ── JS 沙箱执行器（Node vm 受限上下文；payload 经 stdin JSON 传入）──
_JS_RUNNER = r"""
const fs = require('fs');
let payload;
try { payload = JSON.parse(fs.readFileSync(0, 'utf8')); }
catch(e){ console.log('__ERR__'+JSON.stringify({type:'PayloadError', msg:'输入解析失败'})); process.exit(0); }
const vm = require('vm');
const sb = {
  _in: (payload.inputs && typeof payload.inputs==='object') ? payload.inputs : {},
  _out: {},
  JSON, Math, Number, String, Boolean, Array, Object,
  parseInt, parseFloat, isNaN, isFinite, encodeURIComponent, decodeURIComponent,
};
sb.console = { log: (...a)=>console.log(...a), warn: ()=>{} };
sb._out = {};
try {
  const t = payload.timeout_ms || {!JS_TIMEOUT!};
  vm.runInContext(payload.code || '', vm.createContext(sb), { timeout: t });
  console.log('__OUT__'+JSON.stringify(sb._out));
} catch(e){
  console.log('__ERR__'+JSON.stringify({type: e.name||'Error', msg: String(e.message).slice(0,500)}));
}
""".replace("{!JS_TIMEOUT!}", str(10000))


def _spawn(language: str):
    """检查运行时可执行文件是否存在。"""
    if language == "python":
        return sys.executable, True
    if language == "js":
        node = shutil.which("node")
        return node, bool(node)
    return None, False


def _run_python(code: str, inputs: dict, timeout: float, max_output: int) -> dict:
    payload = base64.b64encode(
        json.dumps({"code": code, "inputs": inputs or {}}, ensure_ascii=False).encode("utf-8")).decode()
    # 强制子进程 UTF-8 模式：Windows 管道 stdout 默认按 locale 编码（cp936），
    # 中文内容会产出非 UTF-8 字节导致父进程 decode 乱码
    _env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    try:
        p = subprocess.run(
            [sys.executable, "-c", _PY_RUNNER], input=payload.encode("utf-8"),
            capture_output=True, timeout=timeout, env=_env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"执行超时（>{timeout}s），已强制终止", "outputs": {}}
    return _parse_runner_output(p, max_output)


def _run_js(code: str, inputs: dict, timeout: float, max_output: int) -> dict:
    node = shutil.which("node")
    if not node:
        return {"ok": False, "error": "未检测到 Node.js 运行时，无法执行 JS（可改用 python）", "outputs": {}}
    payload = json.dumps({"code": code, "inputs": inputs or {},
                          "timeout_ms": int(timeout * 1000)}, ensure_ascii=False)
    try:
        p = subprocess.run(
            [node, "-e", _JS_RUNNER], input=payload.encode("utf-8"),
            capture_output=True, timeout=timeout + 2)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"执行超时（>{timeout}s），已强制终止", "outputs": {}}
    return _parse_runner_output(p, max_output)


def _parse_runner_output(p, max_output: int) -> dict:
    out = (p.stdout or b"").decode("utf-8", "replace")
    if p.returncode != 0 and not out:
        err = (p.stderr or b"").decode("utf-8", "replace")[:300]
        return {"ok": False, "error": f"解释器异常退出（code={p.returncode}）: {err}", "outputs": {}}
    if out.startswith("__ERR__"):
        try:
            e = json.loads(out[len("__ERR__"):])
            return {"ok": False, "error": f"{e.get('type')}: {e.get('msg')}", "outputs": {}}
        except Exception:
            return {"ok": False, "error": out[len("__ERR__"):][:300], "outputs": {}}
    outputs = {}
    if out.startswith("__OUT__"):
        try:
            outputs = json.loads(out[len("__OUT__"):])
        except Exception:
            outputs = {}
    content = json.dumps(outputs, ensure_ascii=False) if outputs else "代码执行完成（未写入 _out）"
    if len(content) > max_output:
        content = content[:max_output] + "…(已截断)"
    return {"ok": True, "outputs": outputs, "content": content, "error": ""}


def run_code(code: str, language: str = "python", inputs: dict | None = None,
             timeout: float = 10, max_output: int = 2000) -> dict:
    """沙箱执行用户代码。

    - code: 源码；Python 用 _in/_out，JS 用 _in/_out（语义一致）
    - language: python | js（node）
    - inputs: 注入 _in 的字典
    - timeout: 超时秒数（默认 10，>0 生效；防死循环）
    - max_output: 输出 content 截断长度
    返回 {"ok", "outputs", "content", "error"}。
    """
    lang = (language or "python").strip().lower()
    if lang in ("js", "javascript", "node"):
        lang = "js"
    if lang != "python" and lang != "js":
        return {"ok": False, "error": f"暂不支持语言: {language}（支持 python / js）", "outputs": {}}
    if not code or not code.strip():
        return {"ok": False, "error": "代码为空", "outputs": {}}
    timeout = max(float(timeout or 10), 0.5)
    if lang == "python":
        return _run_python(code, inputs, timeout, max_output)
    return _run_js(code, inputs, timeout, max_output)
