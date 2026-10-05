"""S4 验证：LLM 采样参数链路（参数重复 → 静默 Mock 的修复 + 显式参数优先级）。

# ── CI 豁免（2026-10-05 标注，理由已实测）──────────────────
# CI-OPTIONAL: B 需真实 LLM provider 的 api_key（CI 无 secret；实测本地 rc=1）
#   分类：A=需服务在跑/ B=需密钥或写真库/ C=实测就红需先修。
#   依据见 docs/遗留优化项-第二轮盘点-20261005.md；
#   由 tools/verify/verify_gate_wiring.py 强制要求（要么接线，要么写理由）。

背景（rt-cost 实测定位，非推测）：
  A) llm/__init__.py 旧代码把 model/temperature/max_tokens 既当**具名参数**传、又用 **kwargs 透传 →
     调用方一旦带这三个参数就抛 `TypeError: got multiple values for keyword argument`，
     被 except 吞掉后**静默回落 Mock**。现网正在这样调用的有两处：
       norm_apply.py:281（temperature/max_tokens）、workflows/engine.py:569（model）。
  B) llm/providers/openai_compat.py 旧优先级是「DB 配置 > 调用方显式传参」→ 传了也不生效。

跑法：.venv\\Scripts\\python.exe -X utf8 tools\\verify\\verify_s4_llm_params.py
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s%s" % (name, ("  | " + str(detail)) if detail else ""))
    else:
        FAIL += 1
        print("  FAIL  %s%s" % (name, ("  | " + str(detail)) if detail else ""))


PROBE = "PROBE_HTTP_REACHED"

print("\n[A] broker 层：带 model/max_tokens/temperature 调用不得再抛「参数重复」而静默降级")
import httpx  # noqa: E402

_orig_post = httpx.post


def _probe_post(*a, **kw):
    raise RuntimeError(PROBE)


httpx.post = _probe_post          # 拦截真实 HTTP；只有真正走到 HTTP 才会命中该标记
try:
    from llm import llm_client
    r = llm_client.chat([{"role": "user", "content": "ping"}], provider_id=1,
                        model="probe-model", max_tokens=1500, temperature=0.1, stream=False)
    reason = str((r.get("_meta") or {}).get("fallback_reason") or "")
    check("调用未抛异常直达 provider（旧代码此处必抛 multiple values）",
          PROBE in reason, "fallback_reason=%r" % reason[:120])
    check("降级行为本身未被改变（无真实网络 → 仍回落 Mock）",
          (r.get("_meta") or {}).get("used_mock") is True)
    # 反向确认：不带这三个参数时同样能走到 HTTP（说明是"参数重复"而非别的原因）
    r2 = llm_client.chat([{"role": "user", "content": "ping"}], provider_id=1, stream=False)
    reason2 = str((r2.get("_meta") or {}).get("fallback_reason") or "")
    check("不带采样参数时也走到 HTTP（对照组）", PROBE in reason2, "fallback_reason=%r" % reason2[:120])
    check("两种情况下 fallback_reason 一致（差别只在参数传递，不在分支）", reason == reason2)
finally:
    httpx.post = _orig_post

print("\n[B] provider 层：显式传参 > DB 配置 > 内置默认")
from llm.providers.openai_compat import OpenAICompatProvider  # noqa: E402

captured = {}


class _FakeResp:
    status_code = 200

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}}


def _cap_post(url, json=None, headers=None, timeout=None):
    captured.clear()
    captured.update(json or {})
    return _FakeResp()


CFG = {"name": "probe", "base_url": "http://probe.local/v1", "api_key": "k",
       "model_name": "cfg-model", "max_tokens": 16384, "context_window": 65536, "temperature": 0.7}

httpx.post = _cap_post
try:
    p = OpenAICompatProvider(CFG)
    p.chat([{"role": "user", "content": "x"}])
    check("不传参数 → 沿用 DB 配置（行为不变）",
          captured.get("max_tokens") == 16384 and captured.get("model") == "cfg-model"
          and abs(float(captured.get("temperature")) - 0.7) < 1e-9,
          "max_tokens=%s model=%s temperature=%s" % (captured.get("max_tokens"), captured.get("model"), captured.get("temperature")))

    p.chat([{"role": "user", "content": "x"}], max_tokens=1500)
    check("显式 max_tokens=1500 覆盖 DB 的 16384（修复点）", captured.get("max_tokens") == 1500,
          "实际=%s" % captured.get("max_tokens"))

    p.chat([{"role": "user", "content": "x"}], model="override-model")
    check("显式 model 覆盖 DB model_name", captured.get("model") == "override-model", captured.get("model"))

    p.chat([{"role": "user", "content": "x"}], temperature=0.1)
    check("显式 temperature 覆盖 DB 温度", abs(float(captured.get("temperature")) - 0.1) < 1e-9,
          captured.get("temperature"))

    p.chat([{"role": "user", "content": "x"}], max_tokens=999999)
    check("仍受 context_window 上限约束（超窗截到 cw）", captured.get("max_tokens") == 65536,
          "实际=%s" % captured.get("max_tokens"))
finally:
    httpx.post = _orig_post

print("\n[C] 调用点：汇总环节已带输出上限；既有两处参数重复调用点已被修复覆盖")
SRC = {p: (ROOT / p).read_text(encoding="utf-8") for p in (
    "workflows/planner.py", "workflows/refine.py", "norm_apply.py", "workflows/engine.py",
    "llm/__init__.py", "llm/providers/openai_compat.py")}
# 2026-09-20：汇总 / 修订两个环节的输出上限都由硬编码 3000 改为**配置驱动**
# （`delegation.summary_max_tokens` / `refine.max_tokens`，默认均 8000）。
# 原判据是字面串 `max_tokens=3000` → **换写法即失效**（技能 §6.1「文本级判据」陷阱）。
# 改为两段式**行为级**判据：① 配置默认值正确（锁住"用户可见报告不砍半"这条**语义**）；
# ② AST 断言调用点的 `max_tokens` 实参**不是字面常量**（配置驱动）—— 既不绑死变量名，
#    也不因重排/改名误报，且能抓住"有人把它写回硬编码"。


def _cfg_get(sec, key, default=None):
    """独立读取配置（**不要复用别处的局部 `_cfg`** —— 本脚本实测踩过：把新断言插在
    导入语句之前，变量未定义 → 被 `except` 吞成 None，断言报假失败）。"""
    try:
        from core import config as _c
        return _c.get(sec, key, default)
    except Exception:                                   # noqa: BLE001
        return default


def _mt_arg_is_dynamic(src, func_name):
    """AST：`func_name` 内 `.chat(...)` 的 `max_tokens` 实参是否**非字面常量**。

    返回 True=动态（`_mt` 名字 / `RefineGate._max_tokens()` 调用等）、False=写死常量、
    None=没找到该调用或其 max_tokens 实参。
    ⚠️ 判「非 `ast.Constant`」而不是「是 `ast.Name`」：修订侧的写法是
    `max_tokens=RefineGate._max_tokens()`（`ast.Call`），只认 `Name` 会误报（实测踩过）。
    """
    import ast as _ast
    for n in _ast.walk(_ast.parse(src)):
        if isinstance(n, _ast.FunctionDef) and n.name == func_name:
            for c in _ast.walk(n):
                if (isinstance(c, _ast.Call) and isinstance(c.func, _ast.Attribute)
                        and c.func.attr == "chat"):
                    for kw in c.keywords:
                        if kw.arg == "max_tokens":
                            return not isinstance(kw.value, _ast.Constant)
    return None


_sum_mt = _cfg_get("delegation", "summary_max_tokens", None)
check("汇总输出上限默认 8000（配置 delegation.summary_max_tokens = 不砍半）",
      _sum_mt == 8000, "实际=%s" % _sum_mt)
_dyn = _mt_arg_is_dynamic(SRC["workflows/planner.py"], "_summarize_plan")
check("planner.py 汇总调用的 max_tokens 是配置驱动（AST：实参非字面常量）",
      _dyn is True, "AST 判据=%s（None=没找到该调用）" % _dyn)

_rf_mt = _cfg_get("refine", "max_tokens", None)
check("修订输出上限默认 8000（配置 refine.max_tokens）", _rf_mt == 8000, "实际=%s" % _rf_mt)
_dyn_rf = _mt_arg_is_dynamic(SRC["workflows/refine.py"], "_refine")
check("refine.py 修订调用的 max_tokens 是配置驱动（AST：实参非字面常量）",
      _dyn_rf is True, "AST 判据=%s（None=没找到该调用）" % _dyn_rf)
check("汇总上限未再回落到过激的 1500（用户可见报告不宜砍半）",
      "max_tokens=1500" not in SRC["workflows/planner.py"] and "max_tokens=1500" not in SRC["workflows/refine.py"])
check("broker 不再「具名 + **kwargs」重复传参",
      "_impl_kw.pop(\"max_tokens\", None)" in SRC["llm/__init__.py"] and "**_impl_kw)" in SRC["llm/__init__.py"])
check("openai_compat 优先级已改为显式参数优先",
      "max_tokens if max_tokens is not None else cfg.get" in SRC["llm/providers/openai_compat.py"])
check("norm_apply.py:281 的 temperature/max_tokens 现在能真正生效（既有调用点）",
      "max_tokens=8192" in SRC["norm_apply.py"])
check("workflows/engine.py:569 的 model 现在能真正生效（既有调用点）",
      "model=model" in SRC["workflows/engine.py"])

print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
sys.exit(0 if FAIL == 0 else 1)
