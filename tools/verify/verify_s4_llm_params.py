"""S4 验证：LLM 采样参数链路（参数重复 → 静默 Mock 的修复 + 显式参数优先级）。

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
check("planner.py 汇总调用带 max_tokens=3000", "max_tokens=3000" in SRC["workflows/planner.py"])
check("refine.py 修订调用带 max_tokens=3000", "max_tokens=3000" in SRC["workflows/refine.py"])
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
