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
import ast
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
# 2026-10-19 改判：7bd3199（澄清门控回归修复）起，两处调用点改传
#   `forced_intent=_clarify_gate_intent`——**用户入口原值**的早期捕获
#   （team/R1 覆写 forced_intent 之前取的值）。覆写后的 forced_intent
#   恒非空 ⇒ quick_pick 判据恒命中 ⇒ 内容级澄清在 UI 路径恒被跳过
#   （2026-10-16 R1 引入的回归，由 7bd3199 修复）。旧模式锚的是修复前
#   的字面形态 ⇒ 改断言新形态；并补一条**不变式**：别名捕获必须早于
#   任何 forced_intent 覆写、且早于调用点——有人把捕获挪到覆写之后
#   （或改回传覆写后的值），行号序断言照样转红（比纯字面量更防回归）。
_pat = re.compile(
    r"_clarify_detect\(\s*user_input,\s*intent,\s*effective_provider,\s*"
    r"forced_intent=_clarify_gate_intent,\s*skill_name=skill_name\)")
_OW = re.compile(r"^[ \t]*forced_intent[ \t]*=[ \t]*(?:None|team_intent|intent)[ \t]*$", re.M)
_ALIAS = re.compile(r"^[ \t]*_clarify_gate_intent[ \t]*=[ \t]*forced_intent[ \t]*$", re.M)


def _gate_intent_captured_early(src):
    """别名定义行 < 调用点行，且 < 首个 forced_intent 覆写行（防晚捕获回归）。"""
    m_alias, m_call = _ALIAS.search(src), _pat.search(src)
    if not (m_alias and m_call):
        return False
    ln_alias = src[:m_alias.start()].count("\n") + 1
    ln_call = src[:m_call.start()].count("\n") + 1
    ow = [src[:m.start()].count("\n") + 1 for m in _OW.finditer(src)]
    return ln_alias < ln_call and (not ow or ln_alias < min(ow))


for _nm, _src in (("1.9 execute.py", _exe_src), ("1.10 stream.py", _str_src)):
    chk(_nm + " 调用点补传 forced_intent/skill_name（入口原值别名形态）", bool(_pat.search(_src)))
    chk(_nm + "b 入口原值捕获早于 team/R1 覆写（澄清门控不变式）", _gate_intent_captured_early(_src))
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
# 2026-10-04 改判：2.3 / 2.4 / 2.6 原本断言**源码字面量**
#   （`top_k=4, branches=None,`、`chunk_hits[:3]`、别名 `_hybrid`），
#   但检索链已重构为**四层独立旋钮**（config.py 中明写约束：
#   `recall_k ≥ top_k ≥ rerank_max_candidates ≥ inject_k`，
#   理由是"top_k 卡死在 4 会让重排无选择空间，结构倒挂"）。
#   ⇒ **门禁断言的是重构前的旧形态**，三次失败的根因都在这里。
#   改为断言**分层约束本身**——这才是真正的不变式，且比字面量更能抓错：
#   若有人把 inject_k 设得比 top_k 大（结构倒挂），照样会被抓。
try:
    from core import config as _ragcfg
    _rk = int(_ragcfg.get("rag", "recall_k", 0) or 0)
    _tk = int(_ragcfg.get("rag", "top_k", 0) or 0)
    _rmc = int(_ragcfg.get("rag", "rerank_max_candidates", 0) or 0)
    _ik = int(_ragcfg.get("rag", "inject_k", 0) or 0)
    _fb = int(_ragcfg.get("rag", "fallback_top_k", 0) or 0)
except Exception as _e:
    _rk = _tk = _rmc = _ik = _fb = -1
chk("2.3 四层旋钮齐备且分层约束成立：recall_k >= top_k >= rerank_max_candidates >= inject_k",
    min(_rk, _tk, _rmc, _ik) > 0 and _rk >= _tk >= _rmc >= _ik,
    "recall_k=%s top_k=%s rerank_max_candidates=%s inject_k=%s" % (_rk, _tk, _rmc, _ik))
chk("2.3b 消费侧走配置读取（不硬编码 top_k）",
    ('_cfg.get("rag", "top_k"' in _rag_src or '_cfg.get("rag","top_k"' in _rag_src)
    and "top_k=8, branches=None" not in _rag_src)
chk("2.4 最终注入条数由 inject_k 控制（消费侧 chunk_hits[:inject_k]）",
    "chunk_hits[:_inject_k]" in read(os.path.join("agent", "pipeline_parts", "context.py")))
chk("2.4b inject_k 兜底默认仍为 3（配置缺失时不放大注入量）",
    'int(_cfg.get("rag", "inject_k", 3)) or 3' in read(
        os.path.join("agent", "pipeline_parts", "context.py")))
chk("2.4c fallback_top_k 为正（混合检索异常时的兜底条数）", _fb > 0, "fallback_top_k=%s" % _fb)

# 2026-10-19 改判：原实现用正则文本扫全部 .py——**注释/docstring 里的
#   `hybrid_search(top_k=4)`（rag.py:530 corrective-retry 的实测记录）被当成
#   调用点**；且同一文件两次 `from knowledge_engine import hybrid_search as
#   _hybrid` 时别名列表重复 ⇒ 每个真实调用点被数两遍（rag.py 2 个真实调用点
#   报出 5 个，2.6 因 docstring 里的 top_k=4 误红）。文本级判据在「注释描述
#   旧写法」面前必然误报 ⇒ 改为 **ast 级**提取：只认真 Call 节点（注释/字符串
#   免疫、无重复计数），并可分辨 top_k 是关键字实参还是第 3 位置实参、
#   实参值是常量（硬编码）还是变量/表达式。
_HY_ALIAS = re.compile(r"from\s+knowledge_engine\s+import\s+hybrid_search\s+as\s+(\w+)")


def _hybrid_call_sites(rel):
    """[(rel, 行号, top_k 实参源码, 是否硬编码常量)]；读取/解析失败返回 []。"""
    try:
        txt = io.open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace").read()
    except Exception:
        return []
    if "hybrid_search" not in txt:
        return []
    try:
        tree = ast.parse(txt)
    except SyntaxError:
        return []
    names = {"hybrid_search"} | set(_HY_ALIAS.findall(txt))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        nm = fn.id if isinstance(fn, ast.Name) else (
            fn.attr if isinstance(fn, ast.Attribute) else None)
        if nm not in names:
            continue
        kw = next((k.value for k in node.keywords if k.arg == "top_k"), None)
        pos = node.args[2] if len(node.args) >= 3 else None
        expr = kw if kw is not None else pos
        if expr is None:
            out.append((rel, node.lineno, "<缺省,用默认值>", False))
            continue
        seg = ast.get_source_segment(txt, expr)
        hard = isinstance(expr, ast.Constant) and isinstance(expr.value, int)
        out.append((rel, node.lineno, (seg or "<表达式>").strip(), hard))
    return out


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
        _SITES.extend(_hybrid_call_sites(rel))

_log("  调用点共 " + str(len(_SITES)) + " 处：")
for rel, ln, expr, hard in _SITES:
    _log("    " + rel + ":" + str(ln) + "  top_k=" + expr + ("（硬编码常量）" if hard else ""))

chk("2.5 全部调用点均显式传 top_k（关键字或第 3 位置实参；默认值变更对现有调用零影响）",
    all("<缺省" not in e for _, _, e, _ in _SITES),
    "缺省调用点数=" + str(sum(1 for _, _, e, _ in _SITES if "<缺省" in e)))
_rag_sites = [x for x in _SITES if x[0] == "agent/rag.py"]
# 2026-10-04 改判：原断言是"消费链路只允许有 **1 个**调用点，且其参数是字面量 top_k=4"。
#   这类"白名单 + 字面量"断言在四层旋钮重构后必然失效（现在实参是 `top_k=_rag_top_k`）。
#   改为断言**真正要守的东西**：混合检索的调用点存在、且实参是配置变量（不是硬编码）。
# 2026-10-19 再改判：判据从「文本无 top_k=数字」升级为「ast 实参非常量整数」——
#   `top_k=max(POOL, 50)` 这类表达式实参合法（非硬编码），docstring 里的旧写法
#   记录不再误报（提取改判说明见上方 _hybrid_call_sites 注释）。
chk("2.6 混合检索在 agent/rag.py 有调用点且实参走配置变量（ast 级：无硬编码常量实参）",
    len(_rag_sites) >= 1 and all(not h for _, _, _, h in _rag_sites),
    "rag 调用点=%d，top_k 实参=%s" % (len(_rag_sites), [e for _, _, e, _ in _rag_sites]))

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

# 2026-10-04改判：3.1 / 3.2 原本断言 `_pm_max_tokens == 8000`，但 P1-27 已把
#   summary_max_tokens / max_tokens 有意调到 16384（config.py 留有注释"实测已触顶被截断"）
#   ⇒ **门禁期望过时**。且这类"固定数值"断言本身就是坏判据：任何人调整预算都会被它拦住，
#   而它并不能保证"报告尾部不被裁掉"（那才是真条件，由 verify_orch_summary_budget 的
#   I4c/I4d 关系不变式守）。
#   改为：① 必须走具名 _intent（区分调用点）② 上限必须为正整数（真条件）。
chk("3.1 planner.py:_summarize_plan 传 _intent=plan_summary 且上限为正整数",
    ('_intent="plan_summary"' in _planner_src) and (isinstance(_pm_max_tokens, int)
                                                   and _pm_max_tokens > 0),
    "配置值=%s" % _pm_max_tokens)
chk("3.2 refine.py:_refine 传 _intent=plan_refine 且上限为正整数",
    ('_intent="plan_refine"' in _refine_src) and (isinstance(_rf_max_tokens, int)
                                                 and _rf_max_tokens > 0),
    "配置值=%s" % _rf_max_tokens)

chk("3.3 旧阻断 A 形态已不存在（llm/__init__.py 不再显式传 + **kwargs 重复）",
    'max_tokens=kwargs.get("max_tokens")' not in _llm_src)
chk("3.3b 修法在位：三键走具名参数、且**不重复透传**（避免 TypeError）",
    # 实际实现是 `impl_kw.get(...)` + `**{k: v for k,v in impl_kw.items() if k not in (...)}`
    #（2026-10-04 改判：原断言要求 `pop`，而实现是"get + 过滤"—— 两者**行为等价**，
    #   都保证 model/temperature/max_tokens 不在 **kwargs 里重复出现。
    #   `kwargs` 重复传同名参数会 TypeError，而这类异常会被 except 吞掉 → 静默回落 Mock，
    #   正是本条要防的故障。）
    ('_impl_kw = dict(kwargs)' in _llm_src)
    and ('max_tokens=impl_kw.get("max_tokens")' in _llm_src
         or 'max_tokens=_impl_kw.get("max_tokens")' in _llm_src)
    and ('if k not in ("model", "temperature", "max_tokens")' in _llm_src
         or 'if k not in ("model", "temperature", "max_tokens")' in _llm_src.replace("'", '"')))
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
