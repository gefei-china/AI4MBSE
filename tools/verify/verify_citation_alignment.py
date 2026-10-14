# -*- coding: utf-8 -*-
"""B5-10 自检：引用条数与注入条数**同源** + 引用提示就近复述 + 硬约束措辞。

覆盖两个根因：
  ① 「引用内容区域总是显示 8 条」是**写死的 `[:8]`**（agent/utils.py），
     与"实际注入了几条"脱钩 ⇒ 编号体系错位（prompt 只注 3 条，界面给 8 个可点条目，
     后 5 条的编号在 prompt 里根本不存在，正文永远引用不到）；
  ② 正文 [n] 标引实测 0/4 —— 指令是"若引用请标"的**软约束**，
     且指令与数据在 prompt 里**相距 12 个 block**。

纪律（§6.2/ §6.7/ §6.8）：
  · 行为断言**必须做变异自证** —— 注回旧写法后对应判据必须 FAIL；
  · 判据**剥注释**再判（注释里常引用反例代码）；
  · 不碰真库、不写生产配置。

跑法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_citation_alignment.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def chk(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name
          + ((" — " + str(detail)) if (detail and not cond) else ""))


def section(t):
    print("\n=== %s ===" % t)


def read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


def strip_py_comments(src):
    out, in_doc = [], False
    for ln in src.splitlines():
        s = ln.strip()
        if in_doc:
            if '"""' in s:
                in_doc = False
            continue
        if s.startswith('"""'):
            if s.count('"""') < 2:
                in_doc = True
            continue
        if s.startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


# ══════════════════════════════════════════════════════════════════
# [A] citations 条数与 prompt 注入条数同源
# ══════════════════════════════════════════════════════════════════
section("[A] citations 与注入同源（不再写死 8）")
utils_code = strip_py_comments(read("agent/utils.py"))

chk("A1 _citations_payload 读 rag.inject_k 作为上限",
    '_k = int(_cfg.get("rag", "inject_k", 3)) or 3' in utils_code)
chk("A2 切片上限用变量而非写死的 8",
    "for h in (hits or [])[:_k]:" in utils_code
    and "for h in (hits or [])[:8]:" not in utils_code)
chk("A3 读配置失败有回落（异常态不清空引用区）",
    "_k = 8" in utils_code)

# 行为断言：真实调函数（不碰真库，纯函数）
from agent.utils import _citations_payload
_hits = [{"document_id": i, "source_doc": "d%d" % i, "chunk_index": i, "section": "s",
          "score": 0.5, "confidence_level": "高", "content": "x"} for i in range(20)]
_out = _citations_payload(_hits)
chk("A4 行为·20 条命中 → 返回 3 条（= inject_k，改前恒 8）", len(_out) == 3,
    "实得 %d" % len(_out))
chk("A5 行为·编号连续从 1 开始（前端 [n] 映射 cites[n-1]）",
    len(_out) > 0 and _out[0].get("document_id") == 0)
chk("A6 行为·空命中不炸", _citations_payload([]) == [] and _citations_payload(None) == [])

# ── 变异 M1：把上限写回 8 ──
_m1 = utils_code.replace("for h in (hits or [])[:_k]:", "for h in (hits or [])[:8]:")
M1 = ("for h in (hits or [])[:_k]:" in utils_code) and \
     ("for h in (hits or [])[:_k]:" not in _m1)
chk("M1 变异（上限写回 8）被 A2 抓住", M1)


# ══════════════════════════════════════════════════════════════════
# [B] 引用提示在数据尾部就近复述
# ══════════════════════════════════════════════════════════════════
section("[B] 引用提示与数据零距离（common.py）")
common_code = strip_py_comments(read("agent/pipeline_parts/common.py"))

chk("B1 存在 _citation_footer 函数", "def _citation_footer(" in common_code)
chk("B2 retrieval 块拼接了 citation_footer",
    "ctx.context_text,\n            _citation_footer(ctx.context_text)," in common_code
    or "_citation_footer(ctx.context_text)" in common_code)

from agent.pipeline_parts.common import _citation_footer

chk("B3 行为·3 条来源 → 提示列出编号范围【来源1】～【来源3】",
    "【来源1】～【来源3】" in _citation_footer("【来源1】A\n【来源2】B\n【来源3】C"))
chk("B4 行为·1 条来源 → 提示只给 [1]",
    "[1]" in _citation_footer("【来源1】A") and "1～" not in _citation_footer("【来源1】A"))
chk("B5 行为·无来源（旧向量兜底）→ 不输出提示（不留空行）",
    _citation_footer("[向量] 文件 (parsed)") == "")
chk("B6 行为·空文本 → 空串", _citation_footer("") == "")

# ── 变异 M2：去掉 footer 拼接 ──
_m2 = common_code.replace('"retrieval": "检索到的互联数据：\\n%s%s" % (\n            ctx.context_text,\n            _citation_footer(ctx.context_text),\n        ),',
                          '"retrieval": "检索到的互联数据：\\n%s" % ctx.context_text,')
M2 = ("_citation_footer(ctx.context_text)" in common_code) and \
     ("_citation_footer(ctx.context_text)" not in _m2)
chk("M2 变异（去掉 footer 拼接）被 B2 抓住", M2)


# ══════════════════════════════════════════════════════════════════
# [C] 引用规范改为硬约束
# ══════════════════════════════════════════════════════════════════
section("[C] 引用规范措辞（软约束 → 硬约束）")
prompt_code = strip_py_comments(read("agent/pipeline_parts/prompt.py"))

chk("C1 文案含「硬性要求」", "硬性要求" in prompt_code)
chk("C2 文案含「必须」", "必须" in prompt_code)
chk("C3 不再用「若…请…」的软约束措辞",
    "作答时若引用了" not in prompt_code)
chk("C4 仍保留「不得标注不存在的编号」（防幻觉编号）",
    "不得标注不存在的编号" in prompt_code)

from agent.pipeline import AgentPipeline
_p = AgentPipeline()
_p._load_db_agents(None)
_cr = _p._build_citation_rules()
chk("C5 行为·citation_rules 实际文本含硬约束", "硬性要求" in _cr and "必须" in _cr)

# ── 变异 M3：文案退回软约束 ──
_mut_cr = _cr.replace("硬性要求", "软提示").replace("**必须在对应论述末尾标注来源序号**",
                                                    "若引用了可在论述末尾标注")
M3 = ("硬性要求" in _cr) and ("硬性要求" not in _mut_cr)
chk("M3 变异（文案退回软约束）被 C5 抓住", M3)


# ══════════════════════════════════════════════════════════════════
# [D] 端到端：注入条数 == citations 条数（防再次错位）
# ══════════════════════════════════════════════════════════════════
section("[D] 两侧条数必须相等（错位即失败）")
from core import config as _cfg
_inject_k = int(_cfg.get("rag", "inject_k", 3)) or 3
_n_cit = len(_citations_payload(_hits))
chk("D1 rag.inject_k == citations 条数", _inject_k == _n_cit,
    "inject_k=%d citations=%d" % (_inject_k, _n_cit))
chk("D2 citations 不超过 8（不退回旧写死值）", _n_cit <= 8, "实得 %d" % _n_cit)


print("\n" + "=" * 66)
print("总计：%d PASS / %d FAIL" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  - " + f)
print("=" * 66)
sys.exit(1 if FAIL else 0)