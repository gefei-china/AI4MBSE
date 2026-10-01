# -*- coding: utf-8 -*-
"""P1-4 会话槽位（DST slots）三层治理常驻验收。

  [K] L3 入参净化：澄清续答文本 → 抽取「原请求：」（含边界与端到端接线）
  [L] L1 归一化去重：空白/全半角归一 + 子串包含合并（含"A/B 开关有效"与"不误合并"）
  [M] L2 上限收敛 + 话题切换重置（含"本轮优先"、"未打标容错"、真实会话对照）
  [N] 变异自证：每个变异必须制造**新增失败**（证明断言是承重的）

跑法：<repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_slot_merge.py

设计要点（沿用本仓既有验收脚本的踩坑结论）：
  · **整库复制 + 官方覆盖通道**：启动即 `sqlite3.backup()` 复制生产库到临时目录并设
    `MBSE_DB_PATH`（**必须在 import core.config 之前**）→ 夹具可写、生产库零写入。
  · **话题判据走真实代码**：`_is_topic_switch` 是 HistoryMixin 的真方法，夹具会话建在副本库里，
    断言直接调真方法（不是手写复刻判据 —— 复刻版不随源码变异，会让断言全部空转）。
  · **变异用 exec 孪生体**：从源码文本注入 bug 再 exec，磁盘文件一字不动；
    锚点**缩进无关**（类方法经 `dedent` 会左移，写死缩进的锚点会静默不命中 —— 本仓踩过两次）。
"""
import inspect
import os
import sqlite3
import sys
import tempfile
import textwrap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

LIVE_DB = os.path.join(ROOT, "mbse.db")

# ── 隔离：整库复制 + 重定向（必须在 import core.config 之前）──
_TMP = tempfile.mkdtemp(prefix="wb_p14_")
COPY_DB = os.path.join(_TMP, "copy.db")
_src = sqlite3.connect("file:%s?mode=ro" % LIVE_DB, uri=True)
_dst = sqlite3.connect(COPY_DB)
_src.backup(_dst)
_dst.close()
_src.close()
os.environ["MBSE_DB_PATH"] = COPY_DB

import core.config as CFG                                        # noqa: E402
import llm as LLM                                                # noqa: E402
from agent.pipeline import AgentPipeline                         # noqa: E402
from agent.pipeline_parts.common import (                        # noqa: E402
    extract_original_request, merge_slot_items, norm_slot_item, is_topic_switch)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name, ("  ← " + str(detail)) if detail else ""))
    return bool(cond)


def new_conn():
    c = sqlite3.connect(COPY_DB)
    c.row_factory = sqlite3.Row
    return c


def src_of(fn):
    return textwrap.dedent(inspect.getsource(fn))


def _variant(fn, repl):
    """把 fn 的源码按 repl 替换后 exec 成孪生体（磁盘不动）；锚点必须命中，否则抛错。"""
    src = src_of(fn)
    for old, new in repl:
        if old not in src:
            raise AssertionError("变异锚点未命中: %r" % old)
        src = src.replace(old, new, 1)
    ns = dict(fn.__globals__)
    exec(compile(src, "<mutant:%s>" % fn.__name__, "exec"), ns)   # noqa: S102
    return ns[fn.__name__]


def _cfg_patch(vals):
    orig = CFG.get

    def fake(section, key=None, default=None):
        if (section, key) in vals:
            return vals[(section, key)]
        return orig(section, key, default)

    CFG.get = fake
    return lambda: setattr(CFG, "get", orig)


# ── 真实样本：conv=514 的**生产落库原文**（不是构造场景）──
CLARIFY_TEXT = ("【澄清补充】用户已补充以下建模信息（请据此继续，无需再确认）：\n"
                "问题「我不确定你想让我做哪件事 —— 请选一项，或直接在下方补充你的说法（选完我再执行）」"
                "→ 回答：设计/建模：理解用户意图，SysML v2建模与视图代码\n\n"
                "原请求：优化需求数据，需要支持参与者信息")
ORIGINAL = "优化需求数据，需要支持参与者信息"

# conv=514 的真实 last_slots（07 实体 / 11 约束，含同义副本）
REAL_SLOTS = {
    "goal": "优化需求数据支持参与者信息",
    "entities": ["需求数据", "参与者信息", "SysML v2 模型", "视图代码", "用户意图",
                 "SysML v2模型", "需求视图代码"],
    "constraints": ["采用 SysML v2 进行设计/建模", "生成对应视图代码", "需求数据需支持参与者信息",
                    "先理解用户意图", "使用SysMLv2建模与视图代码", "优化需求数据",
                    "支持参与者信息", "需先理解用户意图", "采用SysMLv2建模",
                    "输出相关视图代码", "保持需求模型一致性"],
}

CONV_SAME = 909101      # 同话题段 + 尾部未打标（复刻 conv=514 的真实形态）
CONV_EMPTY = 909102     # 消息全未打标 → 判据应保守返回 False
TOPIC_TAG = "整车需求数据导入"


def _seed():
    c = new_conn()
    for cid, title in ((CONV_SAME, "p14-same"), (CONV_EMPTY, "p14-empty")):
        c.execute("INSERT OR REPLACE INTO conversations (id,title,intent,current_intent,last_slots) "
                  "VALUES (?,?,'design','design','{}')", (cid, title))

    def add(cid, role, content, topic):
        c.execute("INSERT INTO messages (conversation_id,role,content,msg_type,topic,created_at) "
                  "VALUES (?,?,?,'text',?,CURRENT_TIMESTAMP)", (cid, role, content, topic))

    add(CONV_SAME, "user", "【建模目的】将客户提供的整车需求数据导入模型，形成整车需求分析初始输入", TOPIC_TAG)
    add(CONV_SAME, "assistant", "已完成整车需求图（v0.1）", TOPIC_TAG)
    add(CONV_SAME, "user", "优化需求数据，需要支持参与者信息", TOPIC_TAG)
    add(CONV_SAME, "assistant", "已更新需求模型（v0.2）", TOPIC_TAG)
    add(CONV_SAME, "user", "再补充一下涉众与参与者信息", "")   # 尾部未打标（真实形态）
    add(CONV_EMPTY, "user", "随便聊聊", "")
    add(CONV_EMPTY, "assistant", "好的", "")
    c.commit()
    c.close()


_seed()
PIPE = AgentPipeline()

# ══════════════════════════════════════════════════════════════════════════
print("[K] L3 入参净化：澄清续答文本 → 抽取「原请求：」")
# ══════════════════════════════════════════════════════════════════════════
check("K1 真实 514 澄清文本 → 抽出原请求", extract_original_request(CLARIFY_TEXT) == ORIGINAL,
      repr(extract_original_request(CLARIFY_TEXT))[:60])
check("K2 元对话壳已不在结果里（不再出现「问题」/「回答：」）",
      "问题「" not in extract_original_request(CLARIFY_TEXT)
      and "回答：" not in extract_original_request(CLARIFY_TEXT))
check("K3 无标记输入原样返回", extract_original_request("优化需求数据") == "优化需求数据")
check("K4 标记后为空 → 回退原文（绝不把输入清空）",
      extract_original_request("【澄清补充】xxx 原请求：") == "【澄清补充】xxx 原请求：")
check("K5 嵌套标记 → 取最后一次出现",
      extract_original_request("原请求：A 原请求：B") == "B")
check("K6 非字符串 / 空值不抛异常",
      extract_original_request(None) is None and extract_original_request("") == "")

# 端到端：真 task_decompose 收到的 user 内容必须已净化
_cap = []


class _StubLLM:
    def chat(self, msgs, **kw):
        _cap.append(msgs)
        return {"choices": [{"message": {"content":
                '{"goal":"g","entities":["e1"],"constraints":["c1"],"scope":{}}'}}]}


_orig_llm = LLM.llm_client
LLM.llm_client = _StubLLM()
try:
    PIPE.task_decompose(CLARIFY_TEXT, "design")
finally:
    LLM.llm_client = _orig_llm
_sent = (_cap[0][1]["content"] if _cap and len(_cap[0]) > 1 else "")
check("K7 端到端：task_decompose 下发的 user 内容 = 原请求（元对话壳未进拆解器）",
      _sent == ORIGINAL, "sent=%r" % _sent[:70])

# 反向：开关关闭时必须回到原文（防"断言恒真"）
_cap.clear()
_restore = _cfg_patch({("slots", "sanitize_clarify_input"): False})
LLM.llm_client = _StubLLM()
try:
    PIPE.task_decompose(CLARIFY_TEXT, "design")
finally:
    LLM.llm_client = _orig_llm
    _restore()
_sent2 = (_cap[0][1]["content"] if _cap and len(_cap[0]) > 1 else "")
check("K8 开关置 False → 走原文（证明 K7 不是恒真）",
      _sent2.startswith("【澄清补充】"), "sent=%r" % _sent2[:50])

# ══════════════════════════════════════════════════════════════════════════
print()
print("[L] L1 归一化去重 + 子串合并")
# ══════════════════════════════════════════════════════════════════════════
check("L0 归一化键：空格差异同键", norm_slot_item("SysML v2 模型") == norm_slot_item("SysML v2模型"),
      "%s / %s" % (norm_slot_item("SysML v2 模型"), norm_slot_item("SysML v2模型")))
check("L0b 归一化键：全角半角同键", norm_slot_item("ＡＢＣ") == norm_slot_item("abc"))
check("L0c 归一化键：不同语义不同键", norm_slot_item("需求数据") != norm_slot_item("参与者信息"))

_r, _s = merge_slot_items([], ["SysML v2 模型", "SysML v2模型"], max_items=0)
check("L1 空格差异被合并（2 → 1）", _r == ["SysML v2 模型"], "%s stats=%s" % (_r, _s))

_r2, _s2 = merge_slot_items([], ["先理解用户意图", "需先理解用户意图"], max_items=0)
check("L2 子串包含 → 保留信息更全者", _r2 == ["需先理解用户意图"], "%s stats=%s" % (_r2, _s2))

_r3, _ = merge_slot_items([], ["需求数据", "参与者信息", "视图代码"], max_items=0)
check("L3 不同语义不误合并（3 条都在）", _r3 == ["需求数据", "参与者信息", "视图代码"], "%s" % _r3)

_r4, _ = merge_slot_items([], ["SysML v2 模型", "SysML v2模型"],
                          max_items=0, normalize=False, substring=False)
check("L4 关掉归一化+子串 → 退回精确去重（2 条，A/B 开关有效）", len(_r4) == 2, "%s" % _r4)

_r5, _ = merge_slot_items([], ["先理解用户意图", "需先理解用户意图"], max_items=0, substring=False)
check("L5 只关子串 → 两条都在（证明 L2 是子串规则承重）", len(_r5) == 2, "%s" % _r5)

_r6, _s6 = merge_slot_items([], ["a b", "ab", "ab "], max_items=0)
check("L6 三条归一后同键 → 只留先到者原文", _r6 == ["a b"] and _s6["dedup"] == 2,
      "%s stats=%s" % (_r6, _s6))

# 真实数据：conv=514 的 11 条 constraints 过新逻辑
# 注：输入全部来自 prev=[]，即"全是本轮" → **按 M2 语义不触发上限**（本轮优先），
#     所以这里的期望是"同义副本被合并"，不是"压到 8 条以内"。
_rc, _sc = merge_slot_items([], REAL_SLOTS["constraints"], max_items=8)
check("L7 真实 514 constraints：同义副本被合并（11 条 → 减少）",
      len(_rc) < 11 and "先理解用户意图" not in _rc and "需先理解用户意图" in _rc,
      "→ %d 条 stats=%s" % (len(_rc), _sc))
_re, _se = merge_slot_items([], REAL_SLOTS["entities"], max_items=10)
check("L8 真实 514 entities：`SysML v2 模型` 与 `SysML v2模型` 合并",
      "SysML v2模型" not in _re, "→ %s" % _re)

# ══════════════════════════════════════════════════════════════════════════
print()
print("[M] L2 上限收敛（本轮优先）+ 话题切换重置")
# ══════════════════════════════════════════════════════════════════════════
_m1, _st1 = merge_slot_items(["h1", "h2", "h3", "h4"], ["n1"], max_items=3)
check("M1 超上限 → 丢最旧历史，**本轮保留**", _m1 == ["h3", "h4", "n1"] and _st1["dropped_cap"] == 2,
      "%s stats=%s" % (_m1, _st1))
_m2, _st2 = merge_slot_items([], ["n1", "n2", "n3", "n4"], max_items=2)
check("M2 本轮自身超上限 → 一条不丢（不牺牲本轮信息）", len(_m2) == 4 and _st2["dropped_cap"] == 0,
      "%s" % _m2)
_m3, _st3 = merge_slot_items(["h1", "h2"], ["n1", "n2", "n3"], max_items=5)
check("M3 未超上限 → 不动（历史+本轮全在，共 5 条）",
      _m3 == ["h1", "h2", "n1", "n2", "n3"] and _st3["dropped_cap"] == 0,
      "%s" % _m3)
_m4, _ = merge_slot_items(["h1"], ["n1"], max_items=0)
check("M4 max_items=0 → 不启用上限（保持旧语义）", _m4 == ["h1", "n1"], "%s" % _m4)

# 话题判据：走真方法 + 副本库夹具
check("M5 异话题输入 → 判换话题（重置）",
      PIPE._is_topic_switch(CONV_SAME, "帮我写一份第三季度的市场分析报告，重点看华东区") is True)
check("M6 同话题输入 → 不重置",
      PIPE._is_topic_switch(CONV_SAME, "优化需求数据，需要支持参与者信息") is False)
check("M7 承接词 → 不重置", PIPE._is_topic_switch(CONV_SAME, "继续") is False)
check("M8 整段未打标 → 保守不重置（False）",
      PIPE._is_topic_switch(CONV_EMPTY, "帮我写一份第三季度的市场分析报告") is False)
check("M8b 空输入 / 非法会话 → False",
      PIPE._is_topic_switch(CONV_SAME, "") is False
      and PIPE._is_topic_switch(0, "任意") is False)

# 重置的真实效果：换话题时历史 entities/constraints 被丢弃
_merged_switch = PIPE._merge_slots(REAL_SLOTS, {"goal": "写市场分析报告", "entities": ["华东区"]},
                                   conversation_id=CONV_SAME,
                                   user_input="帮我写一份第三季度的市场分析报告，重点看华东区")
check("M9 换话题 → 历史 constraints 被清（不再拖着一堆旧对象）",
      len(_merged_switch.get("constraints") or []) < len(REAL_SLOTS["constraints"]),
      "constraints=%s" % (_merged_switch.get("constraints") or []))
check("M9b 换话题 → goal 仍按新值覆盖",
      _merged_switch.get("goal") == "写市场分析报告", "goal=%s" % _merged_switch.get("goal"))

_merged_keep = PIPE._merge_slots(REAL_SLOTS, {"goal": "优化需求数据支持参与者信息"},
                                 conversation_id=CONV_SAME, user_input="优化需求数据，需要支持参与者信息")
check("M10 同话题 → 保留历史槽位（与 M9 对照，防恒真）",
      len(_merged_keep.get("constraints") or []) >= 5,
      "constraints=%d" % len(_merged_keep.get("constraints") or []))

_restore2 = _cfg_patch({("slots", "reset_on_topic_switch"): False})
_m_off = PIPE._merge_slots(REAL_SLOTS, {"goal": "写市场分析报告"}, conversation_id=CONV_SAME,
                           user_input="帮我写一份第三季度的市场分析报告")
_restore2()
check("M11 reset 开关置 False → 不重置（A/B 可回滚）",
      len(_m_off.get("constraints") or []) >= 5,
      "constraints=%d" % len(_m_off.get("constraints") or []))

# 真实会话对照：conv=514（生产数据）
_c = new_conn()
_row = _c.execute("SELECT last_slots FROM conversations WHERE id=514").fetchone()
_c.close()
if _row and _row["last_slots"]:
    import json
    _real = json.loads(_row["last_slots"])
    _before_c = len(_real.get("constraints") or [])
    _after, _ = merge_slot_items([], _real.get("constraints") or [], max_items=8)
    check("M12 真实 conv=514 对照：constraints 由 %d 条收敛" % _before_c,
          len(_after) < _before_c, "→ %d 条" % len(_after))
else:
    check("M12 真实 conv=514 对照（副本库无该会话 → 跳过视为失败）", False, "未读到 last_slots")

# ══════════════════════════════════════════════════════════════════════════
print()
print("[N] 变异自证：每个变异必须制造**新增失败**")
# ══════════════════════════════════════════════════════════════════════════
_n1 = _variant(merge_slot_items, [("return norm_slot_item(x) if normalize else x", "return x")])
_rn1, _ = _n1([], ["SysML v2 模型", "SysML v2模型"], max_items=0)
check("N1 归一化失效 → L1 翻（两条不再合并）", len(_rn1) == 2, "变异后 len=%d" % len(_rn1))

_n2 = _variant(merge_slot_items, [("if k == ek or (substring and k in ek):", "if k == ek:"),
                                  ("if substring and ek in k:", "if False:")])
_rn2, _ = _n2([], ["先理解用户意图", "需先理解用户意图"], max_items=0)
check("N2 子串合并失效 → L2 翻（两条不再合并）", len(_rn2) == 2, "变异后 %s" % _rn2)

_n3 = _variant(merge_slot_items, [("if max_n > 0 and len(out) > max_n:", "if False:")])
_rn3, _ = _n3(["h1", "h2", "h3", "h4"], ["n1"], max_items=3)
check("N3 上限失效 → M1 翻（历史不再被丢）", _rn3 == ["h1", "h2", "h3", "h4", "n1"], "变异后 %s" % _rn3)

_n4 = _variant(extract_original_request,
               [("if not isinstance(text, str) or not text or SLOT_ORIGINAL_MARK not in text:",
                 "if True:")])
check("N4 净化失效 → K1 翻（元对话壳原样留下）",
      _n4(CLARIFY_TEXT) == CLARIFY_TEXT, "变异后 %r" % _n4(CLARIFY_TEXT)[:40])

# 判据纯函数的 use_shared 开关必须真的改变行为（否则 N 组本身空转）
check("N5 use_shared=False 时 shared 不参与判定（开关有效）",
      is_topic_switch(0.01, True, False, 0.08, use_shared=False) is True
      and is_topic_switch(0.01, True, False, 0.08, use_shared=True) is False)

print()
print("=== P1-4 槽位治理验收：%d PASS / %d FAIL ===" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
