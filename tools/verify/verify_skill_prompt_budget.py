"""S3 门禁：技能提示词**总量**预算必须生效，且降级不改变命中语义（2026-10-28）。

## 要防的三件事

1. **线性膨胀回归**：真库实测（@75eca26，`context_audit.block_name='skill_prompt'`，n=124）
   p50 3,669 tok / p90 7,244 / p99 30,009 / **峰值 34,761 tok（48,813 字符）**，7% 超 8K。
   根因是只有单条正文 cap（600 字符）**没有总量上限**：`bound_first` 让候选＝绑定＋全局池，
   命中几条就注几条正文。若不设总量闸，多技能输入会重新涨回去。
2. **降级变成"丢能力"**：预算裁掉正文后，模型必须仍能取回全文（走既有
   `skill_body_fetch`）。若只截断不给取回通道，等于**静默降能**。
3. **裁剪改变了命中语义**：预算只该影响"注入多少"，**不该**影响"命中哪些"。
   `_last_skill_hits` 在有/无预算两次调用中必须一致 —— 否则 agent 行为会随预算漂移。

## 判据写法

- 断言**关系**而非定值：`设预算后量 < 不设预算时量`、`注流量 ≤ 预算 × 容差`。
  不写 `== 12000`（预估式闸门允许最后一条略微越界，钉死数字会假红）。
- 用**真实库技能**（不造假数据），但用 stub pipeline 隔离 LLM/registry 依赖。
- 只读库；不调 LLM；不写业务表（offload 走 `tool_result_offloads`，属观测表）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

_PASS, _FAIL, _SKIP = [], [], []


def check(name, cond, detail=""):
    (_PASS if cond else _FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


def skip(name, why):
    _SKIP.append(name)
    print("  [SKIP] %s — %s" % (name, why), flush=True)


_DB = os.environ.get("MBSE_DB_PATH") or os.path.join(_ROOT, "mbse.db")
con = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row

print("[库] %s" % os.path.basename(_DB))
_rows = list(con.execute(
    "SELECT * FROM skills WHERE status='published' ORDER BY length(content) DESC LIMIT 6"))
print("  已发布技能样本 %d 个，正文合计 %d 字符"
      % (len(_rows), sum(len(r["content"] or "") for r in _rows)))

if len(_rows) < 2:
    skip("全部行为级断言", "库内已发布技能不足 2 个，无法构造多技能场景（样本缺失，非缺陷）")


def _j(v):
    try:
        return json.loads(v) if isinstance(v, str) else (v or [])
    except Exception:
        return []


def _mk_skill(r):
    return {
        "name": r["name"],
        "desc": (r["description"] if "description" in r.keys() else "") or "",
        "content": r["content"] or "",
        "frontmatter": (r["frontmatter"] if "frontmatter" in r.keys() else "") or "",
        "triggers": _j(r["triggers"]),
        "allowed_tools": _j(r["allowed_tools"]),
        "allowed_roles": [],
        "references": [], "examples": [], "scripts": [],
    }


_SKILLS = [_mk_skill(r) for r in _rows]


class _Reg:
    def get_bound_skills(self, intent):
        return []


def build(max_chars):
    """构造 stub pipeline，让**全部**样本技能都命中，返回 (注入文本, 实例)。"""
    from agent.pipeline_parts.skills import SkillMixin

    class _P(SkillMixin):
        def __init__(self):
            self.registry = _Reg()
            self._tool_conv_ctx = 0
            self._skill_allowed_tools = None
            self._skill_forced = False
            self._skill_prompt_cache = None

        def _global_skill_pool(self, user=None):
            return list(_SKILLS)

        def _trigger_hit(self, trg, low):
            return True                      # 全部命中：构造"多技能同时命中"的最坏场景

        def _semantic_skill_match(self, ui, rest):
            return []

        def _llm_pick_skills(self, ui, rest):
            return []

        def _semantic_candidates(self, ui, rest):
            return []

    p = _P()
    # 用实例属性覆盖配置读取：直接打桩 config 更脆弱，改为覆写读配置的那段行为
    import agent.pipeline_parts.skills as _sk
    _orig = _sk.SkillMixin._build_skill_prompt_uncached

    # 通过环境式打桩：临时替换 core.config.get 对目标键的返回
    from core import config as _cfg
    _real_get = _cfg.get

    def _patched(section, key=None, default=None):
        if section == "agent" and key == "skill_prompt_max_chars":
            return max_chars
        if section == "agent" and key == "skill_pool_scope":
            return "bound_first"
        return _real_get(section, key, default)

    _cfg.get = _patched
    try:
        txt = p._build_skill_prompt_uncached("knowledge_qa", "活动图 时序图 需求 变更 校验", None)
    finally:
        _cfg.get = _real_get
    return txt, p


print()
print("== A 预算开启：注入量受控 ==")
BUDGET = 12000
txt_on, p_on = build(BUDGET)
txt_off, p_off = build(0)          # 0 = 不限
len_on, len_off = len(txt_on), len(txt_off)
print("  预算=%d → 注入 %d 字符；预算=0(不限) → 注入 %d 字符" % (BUDGET, len_on, len_off))
check("A1 设预算后注入量 < 不限时注入量", len_on < len_off, "%d < %d" % (len_on, len_off))
check("A2 设预算后注入量 <= 预算×1.3（预估式闸门容差）",
      len_on <= BUDGET * 1.3, "%d <= %d" % (len_on, BUDGET * 1.3))
check("A3 不限预算时确实会超预算（说明闸门有意义）", len_off > BUDGET, "%d > %d" % (len_off, BUDGET))

print()
print("== B 降级不丢能力：被裁技能仍可经 offload 取回 ==")
check("B1 预算触发时记录了被裁技能名单",
      bool(getattr(p_on, "_skill_budget_trimmed", [])),
      getattr(p_on, "_skill_budget_trimmed", []))
check("B2 被裁技能提供了 fetch 通道 id（_skill_body_offloads 非空）",
      bool(getattr(p_on, "_skill_body_offloads", [])),
      getattr(p_on, "_skill_body_offloads", []))
_ref_pat = re.compile(r"技能正文已 offload #\d+")
check("B3 注入文本中出现「技能正文已 offload #id」取回指引",
      bool(_ref_pat.search(txt_on)))
check("B4 未设预算时不被裁（名单为空）",
      not getattr(p_off, "_skill_budget_trimmed", []),
      getattr(p_off, "_skill_budget_trimmed", []))

print()
print("== C 裁剪不改变命中语义（最关键，防行为随预算漂移）==")
hits_on = sorted(getattr(p_on, "_last_skill_hits", []))
hits_off = sorted(getattr(p_off, "_last_skill_hits", []))
check("C1 有/无预算时 _last_skill_hits 完全一致", hits_on == hits_off,
      "on=%d off=%d 项" % (len(hits_on), len(hits_off)))
check("C2 命中数 > 1（确认真的构造出了多技能场景，否则 A 组无意义）",
      len(hits_on) > 1, "%d 项" % len(hits_on))

print()
print("== D 首个技能名仍在注入中（未被整体丢弃）==")
# ⚠️ 断言"技能名出现"而不是"正文开头 60 字符出现"：
#   首个技能的正文若 >600 字符会被单条 cap offload 成引用块**带 head 摘要**，
#   而 head 由 build_head 按段落优先级装配，**不保证包含正文前 60 字符**
#   ⇒ 首版按"正文前 60 字符"断言是**假红**（实测首个技能为「变更影响分析」时误判）。
first_ok = bool(_SKILLS) and (_SKILLS[0]["name"] in txt_on)
check("D1 首个技能名出现在注入文本中（未被整体丢弃）", first_ok,
      (_SKILLS[0]["name"] if _SKILLS else "-"))
# 补一条正向：首个技能要么全量注入、要么给了取回指引，二者必居其一
_has_head = ("头部摘要" in txt_on) or ("正文按需加载" in txt_on) or ("已按需加载" in txt_on)
check("D2 提供摘要或取回指引其一", _has_head)

print()
print("== E 配置项已注册（可经设置页/API 调整，不是隐藏魔法数）==")
_cfg_src = open(os.path.join(_ROOT, "core", "config.py"), encoding="utf-8").read()
check("E1 CONFIG_SCHEMA 含 skill_prompt_max_chars", '"skill_prompt_max_chars"' in _cfg_src)
_sk_src = open(os.path.join(_ROOT, "agent", "pipeline_parts", "skills.py"), encoding="utf-8").read()
check("E2 注入实现读该配置", '"skill_prompt_max_chars"' in _sk_src)
check("E3 实现未把 bound_only 强改为默认（不改 bound_first 语义）",
      'skill_pool_scope", "bound_first"' in _sk_src)

print()
print("[RESULT] PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
if _FAIL:
    print("[FAILED]", _FAIL)
    sys.exit(1)
print("[ALL GREEN]")
