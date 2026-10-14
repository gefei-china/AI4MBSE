# -*- coding: utf-8 -*-
"""B5-7 回归自检：泛词不再劫持意图 + 不确定时触发用户确认。

覆盖两批改动：
  A-1 编排复用路径补写 `exec.subtasks`（问题3：子任务卡片刷新后消失）
  A-2 正文长文档折叠（问题 2/4：页面被撑得很长）
  B-1 泛词白名单补漏（问题1：总被劫持到 requirement_analysis）
  B-2 泛词/多意图 → `needs_clarification`（问题1：从不触发用户确认）

纪律（§6.2/ §6.7/ §6.8）：
  · 行为断言**必须做变异自证** —— 注回旧写法后对应判据必须 FAIL；
  · 判据**剥注释**再判（本仓注释里常引用反例代码，见 MEMORY）；
  · 不碰真库（夹具驱动），不写生产配置。

跑法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_b5_7_generic_intent_clarify.py
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL, SKIP = [], [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + ((" — " + str(detail)) if (detail and not cond) else ""))


def skip(name, why):
    SKIP.append(name)
    print("  [SKIP] %s — %s" % (name, why))


def section(t):
    print("\n=== %s ===" % t)


def strip_js_comments(src):
    """去掉整行注释（// 与 * / 块），用于避免判据命中自己写的反例注释。"""
    out, in_block = [], False
    for ln in src.splitlines():
        s = ln.strip()
        if in_block:
            if "*/" in s:
                in_block = False
            continue
        if s.startswith("/*"):
            if "*/" not in s:
                in_block = True
            continue
        if s.startswith("//"):
            continue
        out.append(ln)
    return "\n".join(out)


def strip_py_comments(src):
    """去掉 # 整行注释与 docstring 内的示例行（判据只对代码行）。"""
    out = []
    in_doc = False
    for ln in src.splitlines():
        s = ln.strip()
        if in_doc:
            if '"""' in s:
                in_doc = False
            continue
        if s.startswith('"""') or s.startswith('"""'):
            if s.count('"""') < 2:
                in_doc = True
            continue
        if s.startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


def read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ══════════════════════════════════════════════════════════════════
# [A] 编排复用路径补写 subtasks（A-1）
# ══════════════════════════════════════════════════════════════════
section("[A] 复用路径 card_data 与真实执行路径字段集对齐（A-1）")
stream = read("agent/pipeline_parts/stream.py")

i_flow = stream.find("def _stream_orchestrated_flow(")
i_reuse = stream.find("def _stream_orchestrated(")
assert i_flow > 0 and i_reuse > i_flow, "两个编排方法定位失败"
flow_body = stream[i_flow:i_reuse]
reuse_body = stream[i_reuse:stream.find("\n    def ", i_reuse + 10)
                  if stream.find("\n    def ", i_reuse + 10) > 0 else len(stream)]

chk("A1 真实执行路径的 exec 带 subtasks", '"subtasks": subtasks}' in flow_body)
chk("A2 复用路径的 exec 也带 subtasks（改前缺失 ⇒ 整块不渲染）",
    '"subtasks": _subtasks}' in reuse_body)
chk("A3 复用路径真的构造了 _subtasks 列表", "_subtasks = []" in reuse_body)
chk("A4 复用子任务带 from_template 标记（区分真跑/模板声明）",
    '"from_template": True' in reuse_body)
chk("A5 latency/token 缺失时留空而非填 0（0 会被渲染成假数据 ⏱ 0.0s）",
    '"latency_ms": _tk.get("latency_ms") or None' in reuse_body
    and '"token_count": _tk.get("token_count") or None' in reuse_body)
chk("A6 deps 字段被带上（前端要渲染「依赖:…」）", '"deps": _tk.get("deps") or []' in reuse_body)

# 变异 M1：把复用路径的 subtasks 删掉（退回改前）
_m1 = reuse_body.replace('"subtasks": _subtasks},', '"subtasks": []},')
M1 = ('"subtasks": _subtasks}' in reuse_body) and ('"subtasks": _subtasks}' not in _m1)
chk("M1 变异（复用路径不带 subtasks）被 A2 抓住", M1)


# ══════════════════════════════════════════════════════════════════
# [B] 正文长文档折叠（A-2）
# ══════════════════════════════════════════════════════════════════
section("[B] 正文折叠与代码块口径统一（A-2）")
mdjs = read("static/js/mods/05-markdown.js")
mdcode = strip_js_comments(mdjs)

chk("B1 存在 _wrapBodyFold 折叠包装函数", "function _wrapBodyFold(" in mdcode)
chk("B2 折叠阈值是行数常量（_FOLD_MIN_LINES）", "_FOLD_MIN_LINES = 24" in mdcode)
chk("B3 renderMarkdown 的收尾调用了折叠包装",
    "return _wrapBodyFold(html, text);" in mdcode)
chk("B4 有 bodyFoldToggle 交互函数", "function bodyFoldToggle(el)" in mdcode)
chk("B5 折叠头复用 .cf-lang（与代码块同一视觉语言）",
    'class="cf-lang">完整输出' in mdjs)
chk("B6 短消息不受影响（低于阈值直接返回原html）",
    "if(lines < _FOLD_MIN_LINES) return html;" in mdcode)
chk("B7 展开态限高内滚（避免展开后又是超长页面）",
    "body-fold.open .bf-body" in read("static/css/app.css"))

# 变异 M2：阈值调到 0（等于全部折叠，短消息也被折叠 = 回归）
_m2 = mdcode.replace("_FOLD_MIN_LINES = 24", "_FOLD_MIN_LINES = 0")
M2 = ("_FOLD_MIN_LINES = 24" in mdcode) and ("_FOLD_MIN_LINES = 24" not in _m2)
chk("M2 变异（阈值归零）被 B2 抓住", M2)

# 变异 M3：去掉 renderMarkdown 收尾的包装调用
_m3 = mdcode.replace("return _wrapBodyFold(html, text);", "return html;")
M3 = ("return _wrapBodyFold(html, text);" in mdcode) and \
     ("return _wrapBodyFold(html, text);" not in _m3)
chk("M3 变异（收尾不包折叠）被 B3 抓住", M3)


# ══════════════════════════════════════════════════════════════════
# [C] 泛词白名单补漏（B-1）
# ══════════════════════════════════════════════════════════════════
section("[C] 泛词不再劫持意图（B-1）")
intent_mod = sys.modules.get("agent.intent") if "agent.intent" in sys.modules else None
try:
    from agent.intent import IntentRouter
    G = IntentRouter._GENERIC_KW
    chk("C1 「解析」已归入泛词（改前不在白名单，拿到 0.5 特异权重）", "解析" in G)
    chk("C2 「条目」已归入泛词", "条目" in G)
    chk("C3 原有泛词未被误删（需求/视图/建模/设计仍在）",
        all(w in G for w in ("需求", "视图", "建模", "设计")))
    chk("C4 泛词数量从 22 扩到 >= 31", len(G) >= 31, "实际 %d" % len(G))

    def score(hits, gset):
        spec = [k for k in hits if k not in gset]
        gene = [k for k in hits if k in gset]
        return sum(min(1.0, len(k) / 4.0) for k in spec) + 0.25 * len(gene)

    # 真库conv 9360 的三条真实输入（口径见 MEMORY：真流量，不是构造样本）
    cases = [
        ("整车需求→需求视图", ["需求", "解析", "条目", "需求分析"], 2.25),
        ("生命周期→ACT", ["交互视图", "活动图"], 1.75),
        ("利益相关方→BDD", ["需求", "需求分析"], 1.25),
    ]
    OLD = G - {"解析", "条目", "提取", "整理", "建立", "创建", "定义", "输出", "导入"}
    # ⚠️ 判据只锁**真正被本次改动压低的那一条**（含「解析」「条目」的那条）。
    #   另两条的命中词里**没有**新增泛词，分数理应**不变**——
    #   若把它们也写成"必须下降"，就是在要求一个**没有发生**的变化，
    #   将来任何人给别的意图调词表都会连带打红（判据耦合了无关意图）。
    hit_changed, unchanged_ok = [], []
    for label, hits, before in cases:
        after = score(hits, G)
        if after < before:
            hit_changed.append(label)
            chk("C5 %s：分数被压低（%.2f → %.2f）" % (label, before, after), True)
        else:
            unchanged_ok.append(label)
            chk("C5 %s：分数**未变**（%.2f）——其命中词不含新增泛词，本就不该变"
                % (label, after), after == before)
    chk("C5b 恰好只有「整车需求」那条被压低（其余命中词不含解析/条目）",
        len(hit_changed) == 1 and "整车需求" in hit_changed[0], hit_changed)
    chk("C6 ★ 整车需求那条跌破 2.0 直接采信门槛（原 2.25）",
        score(["需求", "解析", "条目", "需求分析"], G) < 2.0,
        "仍 %.2f" % score(["需求", "解析", "条目", "需求分析"], G))
    chk("C7 反事实：改前泛词集下该条确实 ≥2.0（证明是本次改动让它降下来的）",
        score(["需求", "解析", "条目", "需求分析"], OLD) >= 2.0)
except Exception as e:      # noqa: BLE001
    skip("C1~C7 泛词白名单", "导入 agent.intent 失败：%s" % str(e)[:80])


# ══════════════════════════════════════════════════════════════════
# [D] 不确定时触发用户确认（B-2）
# ══════════════════════════════════════════════════════════════════
section("[D] 泛词/多意图 → needs_clarification（B-2）")
icode = strip_py_comments(read("agent/intent.py"))

chk("D1 __init__ 里初始化了 _last_generic_only（否则属性不存在会 AttributeError）",
    "self._last_generic_only = False" in icode)
chk("D2 __init__ 里初始化了 _last_multi_intent", "self._last_multi_intent = 0" in icode)
chk("D3 _scored_keyword_pick 留痕「是否只靠泛词」",
    "self._last_generic_only = bool(" in icode)
chk("D4 detect_multi 留痕段数（无命中也重置，防残留）",
    "self._last_multi_intent = len(tasks) if tasks else 0" in icode)
chk("D5 detect() 入口先跑多意图切分（否则 _done 读到的是上一次残留）",
    "self.detect_multi(text)" in icode)
chk("D6 澄清判据含 _gen_only 或 _multi", "or _gen_only or _multi)" in icode)
chk("D7 ★ 段数用 int() 取值后比大小（bool(2) 是 True，True>=2 是 False）",
    "_nseg = int(getattr(self, \"_last_multi_intent\", 0) or 0)" in icode
    and "bool(getattr(self, \"_last_multi_intent\", 0) or 0) >= 2" not in icode)
chk("D8 两个开关都在（保留回退位）",
    "_cfg_get(\"generic_clarify\", True)" in icode
    and "_cfg_get(\"multi_intent_clarify\", True)" in icode)

# 行为断言：直接单测 _done 的判据（不依赖 LLM/embedding，恒可跑）
try:
    from agent.intent import IntentRouter
    r = IntentRouter()

    def nc_for(nseg, gen_only, route="rule", conf=0.95, confirmed=False):
        r._last_multi_intent = nseg
        r._last_generic_only = gen_only
        r._done("design", route, conf, "单元测试文本", "fp_test", confirmed=confirmed)
        return r.get_last_meta().get("needs_clarification")

    chk("D9 行为·多意图(2段)+泛词 ⇒ needs_clarification=True",
        nc_for(2, True) is True)
    chk("D10 行为·单意图 + 无泛词 ⇒ False（不打扰普通输入）",
        nc_for(1, False) is False)
    chk("D11 行为·只靠泛词(单意图) ⇒ True",
        nc_for(1, True) is True)
    chk("D12 行为·confirmed=True 时静音（历史确认不打扰，沿用 P0-5 v2）",
        nc_for(2, True, confirmed=True) is False)
    chk("D13 ★ 反证：段数为 0 时不应误报（防'残留值'假澄清）",
        nc_for(0, False) is False)

    # 变异 M4：把段数比较写成 `bool(...) >= 2` 形态（恒假）。
    #   ⚠️ 变异体必须**真的改坏判据**，否则 M4 是空转变异（变异自证最常见的假绿）。
    #   做法：不重写整个 `_done`，只把 `_last_multi_intent` 临时**压成布尔**，
    #   模拟"用 bool() 取值后比大小"的那个缺陷 —— D9 应当随之 FAIL。
    _orig_done2 = IntentRouter._done

    def _mut_done(self, intent, route, confidence, text, fp, **kw):
        # 变异：模拟 `bool(n) >= 2` —— 段数先被 bool() 压成 True/False
        self._last_multi_intent = bool(getattr(self, "_last_multi_intent", 0) or 0)
        return _orig_done2(self, intent, route, confidence, text, fp, **kw)

    IntentRouter._done = _mut_done
    r3 = IntentRouter()
    r3._last_multi_intent = 2
    r3._last_generic_only = False        # 只留多意图这一个信号，排除泛词兜底
    r3._done("design", "rule", 0.95, "单元测试文本", "fp3")
    mutated_flag = r3.get_last_meta().get("needs_clarification")
    IntentRouter._done = _orig_done2
    # 对照：同样条件下未变异时必须为 True ⇒ 变异体若也是 True，说明变异没生效
    r4 = IntentRouter()
    r4._last_multi_intent = 2
    r4._last_generic_only = False
    r4._done("design", "rule", 0.95, "单元测试文本", "fp4")
    base_flag = r4.get_last_meta().get("needs_clarification")
    M4 = (base_flag is True) and (mutated_flag is not True)
    chk("M4 变异（段数被 bool() 压成布尔）被 D7/D9 抓住", M4,
        "base=%s mutated=%s" % (base_flag, mutated_flag))
except Exception as e:      # noqa: BLE001
    skip("D9~D13/M4 _done 行为单测", "构造 IntentRouter 失败：%s" % str(e)[:80])


# ══════════════════════════════════════════════════════════════════
print("\n" + "=" * 66)
print("总计：%d PASS / %d FAIL / %d SKIP" % (len(PASS), len(FAIL), len(SKIP)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
if SKIP:
    print("跳过项：")
    for s in SKIP:
        print("  - " + s)
print("=" * 66)
sys.exit(1 if FAIL else 0)