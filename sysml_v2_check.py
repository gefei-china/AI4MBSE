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
① **必须分「词法 / 语法 / 语义」三路计数**（2026-09-19 由双路升级）。语法错会**遮蔽**语义错：
   语法修好后原本被遮蔽的语义错才暴露出来，ERROR 总数可能**反向上升**（实测某文件 19 → 37）。所以
   「ERROR 总数下降」**不能**当修复有效的判据，只能对 `n_hard`（= 词法 + 语法）要求单调严格下降。
   词法单列一档而非并入语法，是因为**修复动作不同**（词法=「字符/名字不合法」，语法=「结构写错」），
   把路别告知 LLM 能让它少走弯路；门禁上两者**同门槛**（都是硬错，都会级联）。
② **工程门禁必须用项目级合并口径**。单文件校验会把「就在另一个文件里」的包报成
   `Couldn't resolve reference`（实测：同一工程单文件口径 137 错 / 合并口径 47 错，
   约 2/3 是伪错）。
③ **`n_syntax > 0` 时不要去看语义错**。实测 14 条语义错逐条回溯：名字真的不存在的 **0 条**，
   全部是语法错破坏包结构后的级联产物。修复顺序永远是「先语法，后语义」。
④ **临时文件必须 UTF-8 无 BOM**。带 BOM 直接触发词法错（`no viable alternative at character '?'`）。

────────────────────────────────────────────────────────────────────────────
门禁判据（`verdict()`，与集成指南 §2.2 判据表逐条一致）
────────────────────────────────────────────────────────────────────────────
    | 情形                            | verdict       | 处置                                        |
    |---------------------------------|---------------|---------------------------------------------|
    | 校验器不可用（缺 java/jar/库）  | `unavailable` | 放行，只记原因（不因环境问题阻断建模主链路）|
    | 超时 / 进程异常                 | `unavailable` | 同上                                        |
    | `rc == 0`                       | `pass`        | 放行，无 ERROR                              |
    | `rc == 1` 且 `n_hard == 0`      | `report`      | **报告但不阻断**（只剩语义错；很多是「引用 |
    |                                 |               | usage 而非 def」这类改文本解决不了的问题，  |
    |                                 |               | 属建模决策，必须人判断）                    |
    | `rc == 1` 且 `n_hard > 0`       | `block`       | **标记待人工**（硬错未清零时语义错全不可信，|
    |                                 |               | 且硬错会级联污染命名空间）                  |

    `n_hard` = `n_lexical + n_syntax`（词法与语法同级，均为硬错）。

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

from core.fs_guard import bounded_unlink

REPO = os.path.dirname(os.path.abspath(__file__))
JAVA = os.path.join(REPO, "java-runtime", "bin", "java.exe")
JAR = os.path.join(REPO, "checker.jar")
LIB = os.path.join(REPO, "sysml.library")

DEFAULT_TIMEOUT = 90          # 单次上限（秒）。实测 4~6 s；留足 JVM 冷启动与被抢占的余量
_CACHE_LIMIT = 32             # hash 短路缓存条数（每次 4~6 s，重复调用必须免费）
_TOP_N = 5                    # summarize() 里保留的诊断条数（防止 element_summary 膨胀）
_MSG_CAP = 200                # 单条诊断消息截断长度

# ══════════════════ 三路分类规则（2026-09-19 三路化；**全部实测取证**）══════════════════
# 取证脚本 `tmp/lexprobe/probe.py`（16 个最小用例跑真校验器 → `tmp/lexprobe/probe.out`）。
# 三条实测依据（改规则前必须重跑它复核，别照抄规范或记忆）：
#   ① `no viable alternative at character '温'`  —— 中文标识符 / BOM（`:1:1 ... at character '?'`）→ **词法**
#   ② `no viable alternative at input '$'`       —— 未定义单字符 token → **词法**
#   ③ `no viable alternative at input 'typed'`   —— 合法 token 位置错 → **语法**
#
# ⚠️ 曾用过一条启发式并已**废弃**（记录在此，防止有人"想当然"加回来）：
#    启发式：「`at input` 引号内是单个非字母数字字符 → 词法」，其唯一依据是 `$` 报 `at input '$'`。
#    2026-09-19 实测**推翻**：`attribut y : ;` 报 `at input ';'` —— `;` 是**合法 token**、
#    只是位置不对（`:` 后面缺类型），属**语法**错，却被该启发式误判成词法（门禁断言当场抓到）。
#    结论：**"单字符"不是可靠判据**。
#
# 正解 —— 按 ANTLR 的**报错层次**分工（有理论依据，不是猜的）：
#    · `at character 'X'` / `token recognition error` / `mismatched character`
#      来自 **lexer**：字符根本组不成 token → **词法**；
#    · `no viable alternative at input 'X'` / `mismatched input` / `extraneous input` / `missing `
#      来自 **parser**：token 合法但语法位置不对 → **语法**。
# 代价：`$` 这类真·非法字符会归到语法路（它的文案走 parser 通道）。可接受 ——
#   门禁上词法与语法**同门槛**（都计入 `n_hard`），路别只影响给 LLM 的标签精度，不影响拦不拦。
_LEX_DEFAULT = r"no viable alternative at character|token recognition error|mismatched character"
_SYN_DEFAULT = r"no viable alternative|mismatched input|extraneous input|missing "

# 诊断行格式（§2.4）：`[ERROR] stdin:<行>:<列>: <消息>`；文件名字段被工具硬编码为 stdin
DIAG = re.compile(r"^\[(ERROR|WARN)\]\s+stdin:(\d+):(\d+):\s*(.*)$")

_SIGNS = None   # 惰性加载的分类正则（内置默认 + config 覆盖/追加）


def _join_pattern(base: str, extra: str) -> str:
    """把追加规则并进基础正则（追加项本身即正则片段，供现场补规则而不改代码）。"""
    return f"{base}|{extra}" if extra else base


def _signs():
    """分类正则：内置默认 → config 覆盖 → config 追加。

    为什么走 config：分类表属**可演进的领域知识**，不该焊死在代码里（新增一类诊断文案
    不该要求改 Python）。但本模块须能独立运行（CLI / 自检脚本），故 config 缺失时静默回退。
    """
    global _SIGNS
    if _SIGNS is None:
        lex, syn, lex_x, syn_x = _LEX_DEFAULT, _SYN_DEFAULT, "", ""
        try:
            from core import config as _cfg
            lex = str(_cfg.get("sysml", "lexical_signs", "") or "").strip() or lex
            syn = str(_cfg.get("sysml", "syntax_signs", "") or "").strip() or syn
            lex_x = str(_cfg.get("sysml", "lexical_signs_extra", "") or "").strip()
            syn_x = str(_cfg.get("sysml", "syntax_signs_extra", "") or "").strip()
        except Exception:
            pass
        _SIGNS = (re.compile(_join_pattern(lex, lex_x)), re.compile(_join_pattern(syn, syn_x)))
    return _SIGNS


def reload_signs() -> None:
    """丢弃分类正则缓存（改完 config 后调用；自检脚本 A/B 与现场热调规则用）。"""
    global _SIGNS
    _SIGNS = None


def classify(msg: str) -> str:
    """单条诊断归路：`lexical` | `syntax` | `semantic`（**顺序敏感：词法优先**）。

    依据 ANTLR 报错层次：lexer 文案 → 词法；parser 文案 → 语法；其余 → 语义。详见模块头说明。
    """
    lex, syn = _signs()
    if lex.search(msg):
        return "lexical"
    if syn.search(msg):
        return "syntax"
    return "semantic"


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
            bounded_unlink(tmp)   # 看门狗删除：防 tsbx 沙箱钩子死锁阻塞（见 core/fs_guard.py）
        except OSError:
            pass


def _parse(stdout, merge_map, merged=""):
    """只提取 `[ERROR]` / `[WARN]` 行（先过滤 100+ 行 `Reading ...` 库加载噪声，见 §2.4 坑 2）。

    三路化（2026-09-19）：每条附 `path`（`lexical`/`syntax`/`semantic`；WARN 不带路）。
    每条另附 `source` = **该行源文本**（取自合并文本，不取自 stdout）——
    校验器 stdout 非 UTF-8，中文字符会回显成乱码（实测 `at character '温'` → `'??'`）；
    而合并文本在我们手上是正确解码的。回喂 LLM 必须用后者，否则 LLM 拿到的提示无法定位。
    """
    lines = merged.splitlines() if merged else []
    diags = []
    for ln in stdout.splitlines():
        m = DIAG.match(ln.strip())
        if not m:
            continue
        sev, no, col, msg = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
        src = ""
        if 1 <= no <= len(lines):
            # ⚠️ **不要 strip**：诊断列号是按**原始行**（含缩进）算的，strip 会让列偏移失效，
            #    而下游要用 `source[col-1]` 回填乱码字符（见 diagnostics._fix_char_msg）。
            src = lines[no - 1][:200]
        diags.append({
            "sev": sev, "line": no, "col": col, "msg": msg,
            "file": _which(merge_map, no),
            "path": classify(msg) if sev == "ERROR" else "",
            "source": src,
        })
    return diags


def _empty(source, error):
    """降低级结果（校验没真正执行）。"""
    return {
        "ok": False, "error": error, "rc": None, "source": source,
        "errors": [], "warns": [],
        "n_error": 0, "n_lexical": 0, "n_syntax": 0, "n_semantic": 0, "n_warn": 0,
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

    diags = _parse(out, merge_map, merged)
    errs = [d for d in diags if d["sev"] == "ERROR"]
    warns = [d for d in diags if d["sev"] == "WARN"]
    result = {
        "ok": True, "error": "", "rc": rc, "source": source,
        "errors": errs, "warns": warns,
        "n_error": len(errs),
        # ★ 三路计数（2026-09-19）：词法 / 语法 / 语义。
        #   门禁只看**硬错** = 词法 + 语法（`n_hard`）；语义错单独计数、只报告。
        #   词法单列一档的理由：它的**修复动作**与语法错不同（词法=「字符/名字不合法」，
        #   语法=「结构写错」），回喂 LLM 时给出路别能让它少走弯路。
        "n_lexical": len([d for d in errs if d["path"] == "lexical"]),
        "n_syntax": len([d for d in errs if d["path"] == "syntax"]),
        "n_semantic": len([d for d in errs if d["path"] == "semantic"]),
        "n_warn": len(warns),
        "elapsed": round(dt, 2), "cached": False,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["n_hard"] = result["n_lexical"] + result["n_syntax"]
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
    """三档判据：`pass` / `report` / `block`（+ 环境降级 `unavailable`）。

    门禁口径（2026-09-19 三路化）：**词法 + 语法 = 硬错**（`n_hard`），任一 > 0 → `block`。
    词法错与语法错**同门槛**：`at character '温'`（中文标识符）本身就是模型不合法，
    且词法错会**级联**成后续语法/语义错（同纪律 ③ 的机制），放行它是自欺。
    兼容：历史结果可能只填了 `n_syntax`（无 `n_lexical`）→ 回退按 `n_syntax` 判。
    """
    if not result or not result.get("ok"):
        return "unavailable"
    if result.get("rc") == 0:
        return "pass"
    _hard = result.get("n_hard")
    if _hard is None:
        _hard = (result.get("n_lexical") or 0) + (result.get("n_syntax") or 0)
    if not _hard:
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
        "n_lexical": result.get("n_lexical", 0),
        "n_syntax": result.get("n_syntax", 0),
        "n_semantic": result.get("n_semantic", 0),
        "n_hard": result.get("n_hard", 0),
        "n_warn": result.get("n_warn", 0),
        "scope": result.get("source", ""),       # 'code' 单产物 / 'project' 合并口径
        "top": [f"[{d.get('path') or 'warn'}] {d.get('file')}:{d.get('line')}:{d.get('col')} "
                f"{str(d.get('msg'))[:_MSG_CAP]}" for d in errs[:top_n]],
        "error": result.get("error", ""),
        "at": result.get("at", ""),
    }


# 按路别给的一句话**修复方向**（不是修复方案——具体改法由 LLM 判断，本模块只判错不修复）
_HINTS = {
    "lexical": "词法：该位置的字符/名字不合法（标识符只能 ASCII；文件不得带 BOM）",
    "syntax": "语法：该处结构写法不被接受（对照 L0 卡的关键字与写法表）",
    "semantic": "语义：引用/类型族不成立（名字是否已声明、usage 与 def 是否用反）",
}
# 归一化消息：把引号内的具体 token 抹掉，用于「同一行同一类错」的二次合并
_QUOTED = re.compile(r"'[^']*'")


def _norm_msg(msg: str) -> str:
    """把 `at character '温'` 归一成 `at character '…'` —— 同类的字符级错才能合并。"""
    return _QUOTED.sub("'…'", msg or "")


def _fix_char_msg(msg: str, source: str, col: int) -> str:
    """把诊断里的**乱码字符**换回源码原文（校验器 stdout 非 UTF-8，中文会变成 `?` / `U+FFFD`）。

    实测依据：对 `package P { part def 温控单元; ... }`，校验器报 `:1:22: at character '??'`，
    而源码第 22 列**正是** `温`（`package P { part def ` 恰为 21 个字符）→ 列号是字符位置，
    因此 `source[col-1]` 就能精确取回那个字符。

    这一步不是美化：LLM 拿到 `at character '?'` 无法知道是哪个字符不合法，
    而 `at character '温'` 立刻指向"标识符不能写中文"这个修法。
    """
    if not msg or not source:
        return msg
    # ⚠️ 引号内可能是**多个**替换字符：`温` 的 GBK 是 2 字节，按 UTF-8 decode 后变成 2 个 U+FFFD。
    #    所以此处必须用 `[^']*` 而非 `.`，否则整个正则匹配不上、回填静默失效（实测踩过）。
    m = re.search(r"at character '([^']*)'", msg)
    if not m:
        return msg
    tok = m.group(1)
    if not tok or not all(ch in ("?", "\ufffd") for ch in tok):
        return msg
    idx = int(col or 0) - 1
    if 0 <= idx < len(source):
        return msg[:m.start(1)] + source[idx] + msg[m.end(1):]
    return msg


def diagnostics(result, *, max_items=40, max_msg=3, collapse_lines=True):
    """把诊断整理成**可直接回喂 LLM**的结构化清单：按位置聚合 + 附源码原文 + 路别。

    两层聚合，都是为了提高信噪比（实测依据写在下面）：

    ① **按位置**：同一 `(文件,行,列)` 的多条诊断合成一条 —— 实测 `satisfy R by A;`
       一处吐 4 条（`Couldn't resolve reference` + `Must reference a constraint.`
       + `Must reference a requirement.` + `Must be a valid feature`）；不合并会用重复噪音淹没错因。

    ② **按行同类**（`collapse_lines=True`）：同一 `(文件,行,路别)` 且**归一化消息相同**的诊断
       再合并成一条，列号聚成列表 —— 实测写一个中文标识符 `温控单元` 会吐 **9 条**
       `no viable alternative at character 'X'`（每个字符一条，只有列号不同）。
       不合并的话，LLM 要读 9 条才明白「中文不能当标识符」，而它本该 1 条就懂。
       归一化保留引号内差异 → `at input 'refines'` 与 `at input 'ReqX'` **不会**被并成一条（不同错因）。

    同时产出 `source`（**源码原文**，取自合并文本而非 stdout）：校验器 stdout 非 UTF-8，
    含中文时回显为乱码，而合并文本是正确解码的 —— 回喂必须用后者，否则 LLM 无法定位。

    返回 `[{path, file, line, cols[], col, source, messages[], hint}]`；`hint` 只给方向不给方案。
    """
    if not result or not result.get("ok"):
        return []
    items, by_pos, by_line = [], {}, {}
    for d in (result.get("errors") or []):
        f, ln, col, path = d.get("file"), d.get("line"), d.get("col"), d.get("path") or "semantic"
        # 乱码回填必须在归一化**之前**（归一化会把引号内容抹成 '…'，之后就取不回字符了）
        msg = _fix_char_msg(d.get("msg"), d.get("source") or "", col)
        key = (f, ln, col)
        if key in by_pos:                      # ① 同位置重复
            node = by_pos[key]
            if len(node["messages"]) < max_msg:
                node["messages"].append(msg)
            continue
        lkey = (f, ln, path, _norm_msg(msg))
        if collapse_lines and lkey in by_line:  # ② 同行同类
            node = by_line[lkey]
            node["cols"].append(col)
            if len(node["messages"]) < max_msg:
                node["messages"].append(msg)
            continue
        if len(items) >= max_items:
            break
        node = {
            "path": path, "file": f, "line": ln, "col": col, "cols": [col],
            "source": d.get("source") or "",
            "messages": [msg],
            "hint": _HINTS.get(path, ""),
        }
        by_pos[key] = node
        by_line[lkey] = node
        items.append(node)
    return items


def line_text(result):
    """一行日志文本（给 print / 后端日志用）。"""
    r = result or {}
    if not r.get("ok"):
        return f"[sysml-check] 未执行：{r.get('error', '')}"
    flag = {"pass": "OK", "report": "只有语义错(不阻断)", "block": "硬错(待人工)",
            "unavailable": "未执行"}.get(r.get("verdict"), r.get("verdict"))
    extra = " cached" if r.get("cached") else f" {r.get('elapsed', 0)}s"
    return (f"[sysml-check] {flag}: rc={r.get('rc')} ERROR={r.get('n_error')}"
            f"（词法 {r.get('n_lexical')} / 语法 {r.get('n_syntax')} / 语义 {r.get('n_semantic')}）"
            f"WARN={r.get('n_warn')}{extra}")


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
    _tag = {"lexical": "词法", "syntax": "语法", "semantic": "语义"}
    for d in (r.get("errors") or [])[:20]:
        print(f"  [{_tag.get(d.get('path'), d.get('path') or '?')}] "
              f"{d['file']}:{d['line']}:{d['col']} {d['msg']}")
    return 0 if r.get("verdict") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
