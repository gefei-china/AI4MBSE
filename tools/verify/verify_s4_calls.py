# -*- coding: utf-8 -*-
"""S4 离线验证：clarify 确定性前置规则 / 检索 top_k 对齐 / 汇总输出上限现状。

不改任何运行时状态：不连外网、不写业务表（_record_usage 被替换为 no-op）。
用「从真实源码 exec」的方式验证已落盘代码（S3 沿用做法）。

运行：.venv\Scripts\python.exe -X utf8 tools\verify\verify_s4_calls.py
"""
import io
import os
import re
import sys
import json
import types
import importlib

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_PATH = os.path.join(ROOT, "tools", "verify", "verify_s4_calls_report.txt")
_SKIP_DIRS = (".git", "__pycache__", "node_modules", ".venv", "docs", "static", "tmp")
# 2026-09-17 S4：把项目根加入 sys.path —— Python 只把**脚本所在目录**（tools/verify）放进 sys.path[0]，
# 不加这一行时 `import llm` / `import agent...` 会直接 ModuleNotFoundError（本脚本第一次独立跑就踩到）。
# 显式加入后，无论从哪个 cwd 调用都能跑通（与其余 verify_*.py 保持同一约定）。
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_RESULTS = []
_NOTES = []


def _log(line):
    print(line)


def chk(name, ok, detail=""):
    _RESULTS.append((bool(ok), name, detail))
    _log(("PASS " if ok else "FAIL ") + name + (("  | " + str(detail)) if detail else ""))
    return bool(ok)


def read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ===================================================================
# [1] 改动 1：_clarify_detect 确定性前置规则（命中即零 LLM 调用）
# ===================================================================
_log("== [1] clarify 前置规则（离线，注入假 llm 计数）==")

_CALLS = {"n": 0}


class _FakeLLMClient:
    def chat(self, *a, **k):
        _CALLS["n"] += 1
        return {"choices": [{"message": {"content": '{"need":false}'}}]}


_fake_llm = types.ModuleType("llm")
_fake_llm.llm_client = _FakeLLMClient()
sys.modules["llm"] = _fake_llm

_sess_src = read(os.path.join("agent", "pipeline_parts", "session.py"))
_src_lines = _sess_src.splitlines()
_src_lines = [("pass" if ln.strip() == "from .common import *" else ln) for ln in _src_lines]
_ns = {"json": json}
exec("\n".join(_src_lines), _ns)
SessionMixin = _ns["SessionMixin"]

chk("1.0 session.py 可整体 exec（相对导入替换为 pass）", "SessionMixin" in _ns)
_verbs = _ns.get("_CLARIFY_ACTION_VERBS", ())
chk("1.1 模块常量 _CLARIFY_ACTION_VERBS 含建模/动作动词",
    ("生成" in _verbs) and ("建模" in _verbs) and ("绘制" in _verbs))
chk("1.2 模块常量 _CLARIFY_SKIP_MIN_CHARS == 80", _ns.get("_CLARIFY_SKIP_MIN_CHARS") == 80,
    repr(_ns.get("_CLARIFY_SKIP_MIN_CHARS")))


class _StubRouter:
    INTENTS = {"design": ["设计", "建模"], "requirement_analysis": ["需求"], "chat": []}
    _db_intents = {}


def _make_obj():
    obj = SessionMixin.__new__(SessionMixin)
    obj.router = _StubRouter()
    obj._parse_json_block = lambda c: {}
    return obj


_LONG = "关于本次任务的背景说明：" + ("先看看相关资料再决定" * 8)
chk("1.3 长文本样本长度 >= 80", len(_LONG.strip()) >= 80, "len=" + str(len(_LONG.strip())))
chk("1.4 长文本样本不含动作动词/域关键词",
    not any(k in _LONG for k in ("生成", "分析", "检查", "统计", "梳理", "设计", "建模", "需求")))

_SAMPLES = [
    ("A 明确建模动词（短句）", "生成载荷分系统的用例图", "design", None, None, 0, "action_kw:"),
    ("B 长文本（>=80 字）", _LONG, "design", None, None, 0, "long_input"),
    ("C forced_intent 快捷指定", "帮我看看这个", "design", "design", None, 0, "quick_pick"),
    ("C2 skill_name 快捷指定", "帮我看看这个", "design", None, "sysml_modeling", 0, "quick_pick"),
    ("D 含糊短句（应仍走原 LLM 路径）", "帮我看看这个", "design", None, None, 1, ""),
    ("E 非建模意图（gate 直接返回）", "你好", "chat", None, None, 0, ""),
]

for nm, text, itn, fi, sk, want_calls, want_prefix in _SAMPLES:
    _CALLS["n"] = 0
    obj = _make_obj()
    try:
        obj._clarify_detect(text, itn, None, forced_intent=fi, skill_name=sk)
        err = ""
    except Exception as e:
        err = repr(e)[:160]
    got = _CALLS["n"]
    skip = getattr(obj, "_last_clarify_skip", "<缺属性>")
    chk("1.5 " + nm + " -> LLM 调用 " + str(got) + " 次", (got == want_calls) and (not err),
        "期望 " + str(want_calls) + " 次；_last_clarify_skip=" + repr(skip) + ((" err=" + err) if err else ""))
    ok_skip = (str(skip).startswith(want_prefix) if want_prefix else (skip == ""))
    chk("1.6 " + nm + " -> 跳过原因匹配", ok_skip,
        "_last_clarify_skip=" + repr(skip) + " 期望前缀=" + repr(want_prefix))

_CALLS["n"] = 0
_make_obj()._clarify_detect("【澄清补充】只要载荷部分", "design", None)
chk("1.7 续答消息（CLARIFY_RESUME_MARK）不发起 LLM 调用", _CALLS["n"] == 0, "calls=" + str(_CALLS["n"]))

_r_d = _make_obj()._clarify_detect("帮我看看这个", "design", None)
chk("1.8 含糊短句仍返回规则兜底澄清题（行为未变）",
    isinstance(_r_d, list) and len(_r_d) >= 1 and _r_d[0].get("id") == "q_scope", repr(_r_d)[:120])

_exe_src = read(os.path.join("agent", "pipeline_parts", "execute.py"))
_str_src = read(os.path.join("agent", "pipeline_parts", "stream.py"))
_pat = re.compile(r"_clarify_detect\(\s*user_input,\s*intent,\s*effective_provider,\s*forced_intent=forced_intent,\s*skill_name=skill_name\)")
chk("1.9 execute.py 调用点已补传 forced_intent/skill_name", bool(_pat.search(_exe_src)))
chk("1.10 stream.py 调用点已补传 forced_intent/skill_name", bool(_pat.search(_str_src)))
chk("1.11 _clarify_detect 签名含 forced_intent/skill_name 形参",
    "def _clarify_detect(self, user_input, intent, provider_id=None, forced_intent=None, skill_name=None)" in _sess_src)

del sys.modules["llm"]

# ===================================================================
# [2] 改动 2：hybrid_search 默认 top_k 与消费侧对齐（+ 调用点清单）
# ===================================================================
_log("")
_log("== [2] top_k 对齐（默认值 + 全部调用点）==")

_ke_src = read("knowledge_engine.py")
_rag_src = read(os.path.join("agent", "rag.py"))

chk("2.1 knowledge_engine.hybrid_search 默认 top_k == 4",
    "def hybrid_search(conn, query: str, top_k: int = 4," in _ke_src)
chk("2.2 hybrid_search 旧默认 top_k=5 已不存在",
    "def hybrid_search(conn, query: str, top_k: int = 5" not in _ke_src)
chk("2.2b 同文件其它检索函数（向量 _vector.search / BM25Engine.search）未被改动",
    ("def search(self, query: str, items: list, top_k: int = 5" in _ke_src)
    and ("def search(self, query: str, top_k: int = 5)" in _ke_src))
chk("2.3 agent/rag.py 消费侧实取 top_k=4（原 8）",
    ("_hybrid(conn, query, top_k=4, branches=None," in _rag_src) and ("top_k=8, branches=None" not in _rag_src))
chk("2.4 消费侧仍只消费 chunk_hits[:3]（对齐依据）",
    "chunk_hits[:3]" in read(os.path.join("agent", "pipeline_parts", "context.py")))

_SITES = []
for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
    for fn in filenames:
        if (not fn.endswith(".py")) or (".bak" in fn) or fn.startswith("_s4_"):
            continue
        p = os.path.join(dirpath, fn)
        rel = os.path.relpath(p, ROOT).replace("\\", "/")
        if rel.startswith("tools/verify/"):
            continue
        try:
            txt = io.open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        if "hybrid_search" not in txt:
            continue
        names = ["hybrid_search"] + re.findall(r"from knowledge_engine import hybrid_search as (\w+)", txt)
        for nm in names:
            for m in re.finditer(re.escape(nm) + r"\(([^)]{0,300})", txt):
                if "def " in txt[max(0, m.start() - 30):m.start()]:
                    continue
                _SITES.append((rel, txt[:m.start()].count("\n") + 1, m.group(1).strip()))

_log("  调用点共 " + str(len(_SITES)) + " 处：")
for rel, ln, arg in _SITES:
    _tk = re.search(r"top_k=([^,]+)", arg)
    _log("    " + rel + ":" + str(ln) + "  top_k=" + (_tk.group(1) if _tk else "<缺省,用默认值>"))

chk("2.5 全部调用点均显式传 top_k（默认值变更对现有调用零影响）",
    all("top_k" in a for _, _, a in _SITES),
    "缺省调用点数=" + str(sum(1 for _, _, a in _SITES if "top_k" not in a)))
_rag_sites = [x for x in _SITES if x[0] == "agent/rag.py"]
chk("2.6 消费链路改动点仅 agent/rag.py（别名 _hybrid，8 -> 4）",
    (len(_rag_sites) == 1) and ("top_k=4" in _rag_sites[0][2]), str(_rag_sites))

# ===================================================================
# [3] 改动 3：plan_summary / plan_refine 输出上限 —— 现状与阻断项
# ===================================================================
_log("")
_log("== [3] 汇总输出上限：落盘状态 + 链路修复回归 ==")

_planner_src = read(os.path.join("workflows", "planner.py"))
_refine_src = read(os.path.join("workflows", "refine.py"))
_oac_src = read(os.path.join("llm", "providers", "openai_compat.py"))
_llm_src = read(os.path.join("llm", "__init__.py"))

# 2026-09-20：原判据是字面串 `provider_id=provider_id, max_tokens=3000, _intent="plan_summary")`。
# 汇总上限改为**可配置**（`delegation.summary_max_tokens`，默认仍 3000 = 不改行为）后，
# 该判据**换写法即失效**（技能 §6.1 的「文本级判据」陷阱）→ 改为两段式**行为/配置级**判据：
#   ① 调用点仍在（`_intent="plan_summary"`，语义锚点，不绑变量名）
#   ② 配置默认值 == 3000（锁住"用户可见报告不砍半"这条**语义**，而非字面量）
try:
    from core import config as _cfg_pm
    _pm_max_tokens = _cfg_pm.get("delegation", "summary_max_tokens", None)
    _rf_max_tokens = _cfg_pm.get("refine", "max_tokens", None)
except Exception:
    _pm_max_tokens = _rf_max_tokens = None

chk("3.1 planner.py:_summarize_plan 汇总输出上限在位（默认 8000；2026-09-20 起配置化 delegation.summary_max_tokens）",
    ('_intent="plan_summary"' in _planner_src) and (_pm_max_tokens == 8000),
    "配置值=%s" % _pm_max_tokens)
chk("3.2 refine.py:_refine 修订输出上限在位（默认 8000；2026-09-20 起配置化 refine.max_tokens）",
    ('_intent="plan_refine"' in _refine_src) and (_rf_max_tokens == 8000),
    "配置值=%s" % _rf_max_tokens)

chk("3.3 旧阻断 A 形态已不存在（llm/__init__.py 不再显式传 + **kwargs 重复）",
    'max_tokens=kwargs.get("max_tokens")' not in _llm_src)
chk("3.3b 修法在位：三键先 pop 再走具名参数",
    ('_impl_kw = dict(kwargs)' in _llm_src)
    and ('max_tokens=_impl_kw.pop("max_tokens", None)' in _llm_src)
    and ('**_impl_kw)' in _llm_src))
chk("3.3c 降级留痕已补（except 内 WARNING，不改降级行为）", 'LLM 真实调用失败' in _llm_src)

chk("3.4 旧阻断 B 形态已不存在（openai_compat 不再 kwargs.get 优先）",
    'mt = kwargs.get("max_tokens", cfg.get("max_tokens", max_tokens))' not in _oac_src)
chk("3.4b 新优先级在位：调用方显式 > DB > 内置默认",
    'mt = max_tokens if max_tokens is not None else cfg.get("max_tokens", 4096)' in _oac_src)
chk("3.4c 三参数默认值改为 None（否则无法区分未传与显式传默认值）",
    'def chat(self, messages, model=None, temperature=None, max_tokens=None, stream=False,' in _oac_src)
chk("3.5 cfg 上限仍被 context_window 二次截断（原行为保留）",
    ('if mt > cw:' in _oac_src) and ('cw = cfg.get("context_window")' in _oac_src))

_llm_mod = importlib.import_module("llm")
_llm_client = _llm_mod.llm_client
from llm.registry import ProviderRegistry

_SEEN = {}


class _ProbeProvider:
    """离线探针：签名与 OpenAICompatProvider 一致，不联网；记录实际收到的采样参数。"""

    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def chat(self, messages, model=None, temperature=None, max_tokens=None,
             stream=False, tools=None, thinking=False, **kwargs):
        _SEEN.update({"model": model, "temperature": temperature, "max_tokens": max_tokens,
                      "kwargs_keys": sorted(kwargs.keys())})
        return {"choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


ProviderRegistry.register("probe_s4", _ProbeProvider)
_PROBE_PROVIDER = {"id": 999001, "name": "probe-s4", "provider_type": "probe_s4",
                   "model_name": "probe-model", "api_key": "sk-probe",
                   "context_window": 65536, "max_tokens": 16384}

_orig_get = _llm_client.get_provider
_orig_rec = _llm_client._record_usage
_llm_client.get_provider = lambda pid: dict(_PROBE_PROVIDER)
_llm_client._record_usage = lambda *a, **k: None
try:
    _r_plain = _llm_client.chat([{"role": "user", "content": "hi"}],
                                provider_id=999001, _intent="probe_plain")
    _plain_seen = dict(_SEEN)
    _r_mt = _llm_client.chat([{"role": "user", "content": "hi"}],
                             provider_id=999001, max_tokens=1500, _intent="plan_summary")
    _mt_seen = dict(_SEEN)
    _r_model = _llm_client.chat([{"role": "user", "content": "hi"}], provider_id=999001,
                                model="x", temperature=0.1, _intent="probe_model")
    _model_seen = dict(_SEEN)
finally:
    _llm_client.get_provider = _orig_get
    _llm_client._record_usage = _orig_rec


def _meta(r):
    return r.get("_meta", {}) or {}


chk("3.6 基线：不传采样参数 -> 真实 provider，三键均 None（回落 DB 配置，行为不变）",
    (_meta(_r_plain).get("used_mock") is False)
    and (_plain_seen.get("max_tokens") is None) and (_plain_seen.get("model") is None)
    and (_plain_seen.get("temperature") is None),
    "seen=" + str(_plain_seen))
chk("3.7 回归：传 max_tokens=1500 -> 真实 provider（不再静默 Mock）且 provider 实收 1500",
    (_meta(_r_mt).get("used_mock") is False) and (_mt_seen.get("max_tokens") == 1500),
    "used_mock=" + str(_meta(_r_mt).get("used_mock")) + " seen=" + str(_mt_seen))
chk("3.8 回归：传 model/temperature -> 真实 provider 且 provider 实收传值",
    (_meta(_r_model).get("used_mock") is False)
    and (_model_seen.get("model") == "x") and (_model_seen.get("temperature") == 0.1),
    "used_mock=" + str(_meta(_r_model).get("used_mock")) + " seen=" + str(_model_seen))
chk("3.8b 其余 kwargs（_intent 等）仍原样透传，未被 pop 误伤",
    "_intent" in (_model_seen.get("kwargs_keys") or []),
    "kwargs_keys=" + str(_model_seen.get("kwargs_keys")))

_norm_src = read("norm_apply.py")
_wf_src = read(os.path.join("workflows", "engine.py"))
chk("3.9 既有调用点仍传采样参数：norm_apply.py（temperature/max_tokens）——现已真正生效",
    ('max_tokens=8192' in _norm_src) and ('temperature=0.1' in _norm_src))
chk("3.10 既有调用点仍传采样参数：workflows/engine.py（model=）——现已真正生效",
    "llm_client.chat(messages, provider_id=provider_id, model=model)" in _wf_src)

chk("3.11 证据：refine.py 按轮次循环调用（首评 + 最多 max_rounds 次修订）",
    "for round_i in range(1, cls._max_rounds() + 2):" in _refine_src)
chk("3.12 证据：_max_rounds 默认 2 / _pass_score 默认 70（core.config 可覆盖）",
    ('return int(_cfg.get("refine", "max_rounds", 2))' in _refine_src)
    and ('return int(_cfg.get("refine", "pass_score", 70))' in _refine_src))
chk("3.13 证据：planner.py:_summarize_plan 有 <2 子任务短路（非循环调用）",
    "if len(done_items) < 2:" in _planner_src)

chk("3.14 降级日志的 tools 计数已修正（具名参数 tools 而非 kwargs）",
    ("len(tools or [])" in _llm_src) and ('len(kwargs.get("tools") or [])' not in _llm_src))

_ok = sum(1 for r in _RESULTS if r[0])
_bad = [r for r in _RESULTS if not r[0]]
_out = ["S4 离线验证结果：" + str(_ok) + "/" + str(len(_RESULTS)) + " PASS"]
for ok, name, detail in _RESULTS:
    _out.append(("PASS " if ok else "FAIL ") + name + (("  | " + str(detail)) if detail else ""))
if _bad:
    _out.append("")
    _out.append("失败项：")
    for _, name, detail in _bad:
        _out.append("  - " + name + (("  | " + str(detail)) if detail else ""))
io.open(OUT_PATH, "w", encoding="utf-8", newline="\n").write("\n".join(_out) + "\n")
print("SUMMARY " + str(_ok) + "/" + str(len(_RESULTS)) + " PASS  log=" + OUT_PATH)
sys.exit(0 if not _bad else 1)
