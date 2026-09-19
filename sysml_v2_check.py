# -*- coding: utf-8 -*-
"""SysML v2 生成代码本地校验（`checker.jar` 薄封装）：项目级合并 + 语法/语义双路计数。

放在工程根，与 `model_quality.py` 同级。三份文档的结论收敛在这里：

  · `docs/SysML-v2-AI建模知识文档.md` §8.6 —— 给了可直接落地的骨架（本文件的基线实现）；
  · `docs/SysML-v2-知识文档与校验器-工程落地集成指南.md` §2.2 接入点②/③ + §3 P1/P2/P3
    —— 给了门禁判据与接入位置（本文件 + cards.py + agent/utils.py 三处就是 P1/P2/P3）；
  · `docs/V2代码自动校验与修复闭环-方案评估-20260918.md` §2.2/§2.3 —— 给了调用契约
    （`-i` 参数、退出码、`stdin` 文件名、噪声过滤），本文件按其逐条对齐。

零新依赖（只用 `subprocess` + `re` + 标准库）。

────────────────────────────────────────────────────────────────────────────
四条硬纪律（都是踩过坑才有的，改本文件前先读）
────────────────────────────────────────────────────────────────────────────
① **必须分「语法错 / 语义错」两路计数**。语法错会**遮蔽**语义错：语法修好后原本被遮蔽的
   语义错才暴露出来，ERROR 总数可能**反向上升**（实测某文件 19 → 37）。所以
   「ERROR 总数下降」**不能**当修复有效的判据，只能对 `n_syntax` 要求单调严格下降。
② **工程门禁必须用项目级合并口径**。单文件校验会把「就在另一个文件里」的包报成
   `Couldn't resolve reference`（实测：同一工程单文件口径 137 错 / 合并口径 47 错，
   约 2/3 是伪错）。
③ **`n_syntax > 0` 时不要去看语义错**。实测 14 条语义错逐条回溯：名字真的不存在的 **0 条**，
   全部是语法错破坏包结构后的级联产物。修复顺序永远是「先语法，后语义」。
④ **临时文件必须 UTF-8 无 BOM**。带 BOM 直接触发词法错（`no viable alternative at character '?'`）。

────────────────────────────────────────────────────────────────────────────
门禁判据（`verdict()`，与集成指南 §2.2 判据表逐条一致）
────────────────────────────────────────────────────────────────────────────
    | 情形                          | verdict       | 处置                                        |
    |-------------------------------|---------------|---------------------------------------------|
    | 校验器不可用（缺 java/jar/库）| `unavailable` | 放行，只记原因（不因环境问题阻断建模主链路）|
    | 超时 / 进程异常               | `unavailable` | 同上                                        |
    | `rc == 0`                     | `pass`        | 放行，无 ERROR                              |
    | `rc == 1` 且 `n_syntax == 0`  | `report`      | **报告但不阻断**（只剩语义错；很多是「引用 |
    |                               |               | usage 而非 def」这类改文本解决不了的问题，  |
    |                               |               | 属建模决策，必须人判断）                    |
    | `rc == 1` 且 `n_syntax > 0`   | `block`       | **标记待人工**（语法未清零时语义错全不可信，|
    |                               |               | 且语法错会级联污染命名空间）                |

⚠️ 本模块只**判错**，不**修复**。「确定性修复」属集成指南 §3 的 P4（可选、本轮未做）——
   自动改写代码会掩盖真实建模缺陷，边界见集成指南 §5「什么不要做」。

────────────────────────────────────────────────────────────────────────────
两种口径，用途不同（别混用）
────────────────────────────────────────────────────────────────────────────
  · `check_code(code)` —— **单产物口径**（只校验刚生成的这一段代码）。
    用作**生成质量反馈**（快且轻）。会有跨文件伪错（引用了上一轮生成的类型时）。
    → 关键设计：**单产物口径的伪错几乎全落在「语义路」**（语法错与兄弟文件无关），
      而门禁只认语法路（`block` 仅由 `n_syntax > 0` 触发）→ **伪错不影响门禁判定**。
      这是「接入点②用单产物口径」与「只对语法路设硬门槛」两条结论能同时成立的原因。
  · `check_project(paths)` —— **项目级合并口径**（把多个文件合并成一个再校验）。
    用作**工程门禁**（集成指南 §4 的 A/B 对拍、CI、已入库 `sysml_models/` 体检）。

性能与预算门控（防反复重跑）：
  · 单次 4~6 s（其中库加载固定约 2.3 s）；
  · **hash 短路**：同样内容的第二次调用直接返回缓存，零成本（`_CACHE`，LRU 上限 32 条）；
  · 超时上限 `timeout`（默认 90 s，实测 4~6 s，留足冷启动余量），超时即降级 `unavailable`。

用法：
    python sysml_v2_check.py                      # 默认校验 sysml_models/ev_thermal_mgmt/
    python sysml_v2_check.py a.sysml b.sysml      # 指定文件（项目级合并口径）

    from sysml_v2_check import check_code, verdict, summarize
    r = check_code(code)
    if verdict(r) == "block":
        ...   # 先修语法，别去动语义错
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.abspath(__file__))
JAVA = os.path.join(REPO, "java-runtime", "bin", "java.exe")
JAR = os.path.join(REPO, "checker.jar")
LIB = os.path.join(REPO, "sysml.library")

DEFAULT_TIMEOUT = 90          # 单次上限（秒）。实测 4~6 s；留足 JVM 冷启动与被抢占的余量
_CACHE_LIMIT = 32             # hash 短路缓存条数（每次 4~6 s，重复调用必须免费）
_TOP_N = 5                    # summarize() 里保留的诊断条数（防止 element_summary 膨胀）
_MSG_CAP = 200                # 单条诊断消息截断长度

# 语法错的典型特征（集成指南 §8.2：命中即归「语法路」）
SYNTAX_SIGNS = re.compile(
    r"no viable alternative|mismatched input|extraneous input|"
    r"mismatched character|missing |token recognition error"
)
# 诊断行格式（§2.4）：`[ERROR] stdin:<行>:<列>: <消息>`；文件名字段被工具硬编码为 stdin
DIAG = re.compile(r"^\[(ERROR|WARN)\]\s+stdin:(\d+):(\d+):\s*(.*)$")

_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()


# ────────────────────────────── 可用性 ──────────────────────────────
def available() -> bool:
    """校验器三件套是否齐备（java 运行时 / checker.jar / sysml.library）。

    缺任一即视为不可用 → `verdict()` 返回 `unavailable` → 调用方放行。
    这样「机上没装/没放校验器」不会把建模主链路拖垮（宁可漏检，不可阻断出模型）。
    """
    return os.path.isfile(JAVA) and os.path.isfile(JAR) and os.path.isdir(LIB)


# ────────────────────────────── 内部实现 ──────────────────────────────
def _merge(paths):
    """按顺序读入并合并；返回 (合并文本, 行号映射表)。

    行号映射表用于把诊断里的合并行号反查回源文件（工具硬编码文件名 `stdin`，见 §2.4 坑 1）。
    每段都 `.lstrip("\\ufeff")` 去 BOM —— 带 BOM 会触发词法错（纪律 ④）。
    """
    parts, merge_map, line = [], [], 0
    for p in paths:
        with open(p, "rb") as f:
            src = f.read().decode("utf-8", "replace")
        src = src.lstrip("\ufeff")
        if not src.endswith("\n"):
            src += "\n"
        parts.append(src)
        n = src.count("\n")
        merge_map.append((str(p), line + 1, line + n))
        line += n
    return "".join(parts), merge_map


def _which(merge_map, line_no):
    for p, a, b in merge_map:
        if a <= line_no <= b:
            return os.path.basename(p)
    return "?"


def _run(merged, timeout):
    """写无 BOM 临时文件 → 调 checker.jar → (rc, stdout, stderr, 耗时)。"""
    fd, tmp = tempfile.mkstemp(suffix=".sysml")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:                     # 无 BOM 写出（纪律 ④）
            f.write(merged.encode("utf-8"))
        t0 = time.time()
        proc = subprocess.run(
            [JAVA, "-jar", JAR, "-i", tmp, LIB],       # 第 2 参数 = 库路径（缺省回退 ./sysml.library）
            cwd=REPO, capture_output=True, timeout=timeout)
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"),
                time.time() - t0)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _parse(stdout, merge_map):
    """只提取 `[ERROR]` / `[WARN]` 行（先过滤 100+ 行 `Reading ...` 库加载噪声，见 §2.4 坑 2）。"""
    diags = []
    for ln in stdout.splitlines():
        m = DIAG.match(ln.strip())
        if not m:
            continue
        sev, no, col, msg = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        diags.append({
            "sev": sev, "line": no, "col": col, "msg": msg,
            "file": _which(merge_map, no),
            "is_syntax": bool(SYNTAX_SIGNS.search(msg)),
        })
    return diags


def _empty(source, error):
    """降低级结果（校验没真正执行）。"""
    return {
        "ok": False, "error": error, "rc": None, "source": source,
        "errors": [], "warns": [],
        "n_error": 0, "n_syntax": 0, "n_semantic": 0, "n_warn": 0,
        "verdict": "unavailable", "elapsed": 0.0, "cached": False,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _cache_get(key):
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is None:
        return None
    out = dict(hit)
    out["cached"] = True
    return out


def _cache_put(key, result):
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.pop(next(iter(_CACHE)))      # 简易 FIFO（够用；只求不无限增长）
        _CACHE[key] = {k: v for k, v in result.items() if k != "cached"}


def _check(merged, merge_map, source, timeout, use_cache):
    """共用心跳：合并文本 → 校验 → 双路计数 → 判据。"""
    key = hashlib.sha256(merged.encode("utf-8")).hexdigest()
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            return hit
    if not available():
        return _empty(source, "checker 不可用：缺 java-runtime/bin/java.exe / checker.jar / sysml.library")
    if not merged.strip():
        # 空文件是「通过」的（§2.3），不能当有效产出 —— 显式登记，别让空内容伪装成 rc=0
        return _empty(source, "待校验内容为空")
    try:
        rc, out, err, dt = _run(merged, timeout)
    except subprocess.TimeoutExpired:
        return _empty(source, f"校验超时（>{timeout}s）")
    except Exception as exc:                                  # noqa: BLE001
        return _empty(source, f"校验进程异常：{type(exc).__name__}: {exc}")

    diags = _parse(out, merge_map)
    errs = [d for d in diags if d["sev"] == "ERROR"]
    warns = [d for d in diags if d["sev"] == "WARN"]
    result = {
        "ok": True, "error": "", "rc": rc, "source": source,
        "errors": errs, "warns": warns,
        "n_error": len(errs),
        # ★ 双路计数（纪律 ①）：只对 n_syntax 设门禁，n_error 仅供参考
        "n_syntax": len([d for d in errs if d["is_syntax"]]),
        "n_semantic": len([d for d in errs if not d["is_syntax"]]),
        "n_warn": len(warns),
        "elapsed": round(dt, 2), "cached": False,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["verdict"] = verdict(result)
    if use_cache:
        _cache_put(key, result)
    return result


# ────────────────────────────── 对外接口 ──────────────────────────────
def check_project(paths, *, timeout=DEFAULT_TIMEOUT, use_cache=True):
    """**项目级合并口径**校验：把 `paths` 按顺序合并成一个文件再送 `checker.jar`。

    用于工程门禁（集成指南 §4 A/B 对拍、CI、已入库 `sysml_models/` 体检）。
    单文件口径约 2/3 是跨文件伪错，**门禁必须用本口径**（纪律 ②）。
    """
    paths = [str(p) for p in (paths or [])]
    if not paths:
        return _empty("project", "未提供文件")
    missing = [p for p in paths if not os.path.isfile(p)]
    if missing:
        return _empty("project", f"文件不存在：{missing[0]}")
    merged, merge_map = _merge(paths)
    return _check(merged, merge_map, "project", timeout, use_cache)


def check_code(code, *, timeout=DEFAULT_TIMEOUT, use_cache=True):
    """**单产物口径**校验：只校验刚生成的这一段代码。

    用于生成质量反馈（`cards.py::_gen_sysml_views` 接入点②）。跨文件伪错会落在语义路，
    不影响门禁判定（`block` 只由 `n_syntax > 0` 触发）—— 理由见模块 docstring。
    """
    text = code if isinstance(code, str) else str(code or "")
    if not text.strip():
        return _empty("code", "待校验内容为空")
    merge_map = [("<generated>", 1, text.count("\n") + 1)]
    return _check(text, merge_map, "code", timeout, use_cache)


def verdict(result):
    """按集成指南 §2.2 判据表给三档判据：`pass` / `report` / `block`（+ 环境降级 `unavailable`）。"""
    if not result or not result.get("ok"):
        return "unavailable"
    if result.get("rc") == 0:
        return "pass"
    if not result.get("n_syntax"):
        return "report"
    return "block"


def summarize(result, *, top_n=_TOP_N):
    """压成可长期留痕的摘要（进 `sysml_versions.element_summary["check"]`，别塞全量诊断）。"""
    if not result:
        return {}
    errs = result.get("errors") or []
    return {
        "rc": result.get("rc"),
        "verdict": result.get("verdict") or verdict(result),
        "blocked": (result.get("verdict") or verdict(result)) == "block",
        "n_error": result.get("n_error", 0),
        "n_syntax": result.get("n_syntax", 0),
        "n_semantic": result.get("n_semantic", 0),
        "n_warn": result.get("n_warn", 0),
        "scope": result.get("source", ""),       # 'code' 单产物 / 'project' 合并口径
        "top": [f"{d.get('file')}:{d.get('line')}:{d.get('col')} {str(d.get('msg'))[:_MSG_CAP]}"
                for d in errs[:top_n]],
        "error": result.get("error", ""),
        "at": result.get("at", ""),
    }


def line_text(result):
    """一行日志文本（给 print / 后端日志用）。"""
    r = result or {}
    if not r.get("ok"):
        return f"[sysml-check] 未执行：{r.get('error', '')}"
    flag = {"pass": "OK", "report": "语义错(不阻断)", "block": "语法错(待人工)",
            "unavailable": "未执行"}.get(r.get("verdict"), r.get("verdict"))
    extra = " cached" if r.get("cached") else f" {r.get('elapsed', 0)}s"
    return (f"[sysml-check] {flag}: rc={r.get('rc')} ERROR={r.get('n_error')}"
            f"（语法 {r.get('n_syntax')} / 语义 {r.get('n_semantic')}）WARN={r.get('n_warn')}{extra}")


def clear_cache():
    """清空 hash 短路缓存（自检脚本用；生产无需调用）。"""
    with _CACHE_LOCK:
        _CACHE.clear()


# ────────────────────────────── CLI ──────────────────────────────
def _default_paths():
    d = os.path.join(REPO, "sysml_models", "ev_thermal_mgmt")
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".sysml")]


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    paths = argv or _default_paths()
    if not paths:
        print("没有可校验的 .sysml 文件")
        return 2
    print(f"校验器可用：{available()}")
    for p in paths:
        print(f"  - {p}")
    r = check_project(paths)
    print()
    print(line_text(r))
    print(f"判据：{r.get('verdict')}")
    for d in (r.get("errors") or [])[:20]:
        tag = "语法" if d["is_syntax"] else "语义"
        print(f"  [{tag}] {d['file']}:{d['line']}:{d['col']} {d['msg']}")
    return 0 if r.get("verdict") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
