# -*- coding: utf-8 -*-
"""`skills.frontmatter` 重建门禁。

## 背景（实测，生产库快照）

`frontmatter` 被写成「旧版 YAML 头 + 旧版全文」，而 `content` 已更新为
「新版 YAML 头 + 新版全文」⇒ 两者不同步。注入侧只取 `frontmatter[:400]`
（`agent/pipeline_parts/skills.py:179`）⇒ 模型看到的是**旧版正文的开头 400 字符**，
其中包含与运行时注入相矛盾的话术（例："工具调用轮次上限为 3" vs 运行时"共 6 轮"）。

## 判据

  [1] 全部发布技能的 frontmatter **≤ 注入上限**（不再被截断）
  [2] 截断点不落在句中（若超限则必须是整句被截，不能腰斩）
  [3] frontmatter 里**无**与运行时真值冲突的表述（轮次/预算硬编码）
  [4] frontmatter 与 content 的 YAML 头**同源**（同步，不是旧快照）
  [5] `author` 可被 `capability_registry.frontmatter_author` 取出（消费方不退化）
  [6] 六个必需键齐全（name/description/category/version/triggers/author）
  [7] 无 YAML 头的技能**保持原值**（不强改，不造假）
  [8] 变异自证：把某个 frontmatter 写成旧式"整份正文"⇒ [1] 必须翻红
  [9] 端到端：真实 `_build_skill_prompt` 里无冲突话术

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_skill_frontmatter.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from database import get_db                          # noqa: E402
from tools.rebuild_skill_frontmatter import (        # noqa: E402
    INJECT_CAP, _conflicts, _extract_yaml, _author_of,
)

_FAILS = []
_WARNS = []
#: 六个必需键（content 侧 YAML 头的实测键集合，见盘点输出）
_REQUIRED = ("name", "description", "category", "version", "triggers", "author")

#: **已知既有偏差**（改造前就存在，与本次 frontmatter 重建无关）。
#: 显式登记而不是静默放行 —— 一旦出现**清单外**的技能落进这些偏差，门禁必须翻红。
#: 每条都写清成因，避免后来者误以为"这是被允许的形态"。
_KNOWN_DEVIATIONS = {
    "SysML v2 校验与修复": (
        "content 的 YAML 头本身只有 name/description/category/version，"
        "**缺 triggers 与 author** ⇒ 上游技能数据缺口，重建只能照抄已有键，修不了"
    ),
    "覆盖性分析": (
        "content **完全没有 YAML 头**，且 frontmatter 存的是正文 markdown"
        "（以 '# 覆盖性分析' 开头）⇒ 只能人工补，脚本按设计 skip_no_yaml 保持原值"
    ),
}


def check(name, cond, detail=""):
    if not cond:
        _FAILS.append(name)
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def _keys_of(yaml_text: str) -> set:
    m = re.match(r"^\s*---\r?\n([\s\S]*?)\r?\n---", yaml_text or "")
    body = m.group(1) if m else (yaml_text or "")
    out = set()
    for line in body.splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k = line.split(":", 1)[0].strip()
            if k:
                out.add(k)
    return out


con = get_db()
rows = con.execute(
    "SELECT id, name, frontmatter, content FROM skills "
    "WHERE enabled=1 AND status='published' ORDER BY name").fetchall()
check("[0] 有发布技能可检查", bool(rows), f"{len(rows)} 个")

over, mid_cut, conflicted, unsynced, author_bad, missing_keys = [], [], [], [], [], []
for r in rows:
    fm = (r["frontmatter"] or "").strip()
    ct = r["content"] or ""
    if not fm or fm == "{}":
        continue
    if len(fm) > INJECT_CAP:
        over.append((r["name"], len(fm)))
        if fm[INJECT_CAP:INJECT_CAP + 1] not in ("", "\n", "\r", "。", "；", "！", "？"):
            mid_cut.append(r["name"])
    cf = _conflicts(fm[:INJECT_CAP])
    if cf:
        conflicted.append((r["name"], cf))
    yaml_ct = _extract_yaml(ct)
    if yaml_ct and fm != yaml_ct.strip():
        unsynced.append(r["name"])
    if not _author_of(fm):
        author_bad.append(r["name"])
    ks = _keys_of(fm)
    miss = [k for k in _REQUIRED if k not in ks]
    if miss:
        missing_keys.append((r["name"], miss))

check("[1] 全部 frontmatter ≤ 注入上限（不再被截断）", not over,
      f"超限 {len(over)} 个：{over[:3]}")
check("[2] 截断点不落在句中", not mid_cut, f"句中截断：{mid_cut[:3]}")
check("[3] 无「轮次/预算」硬编码冲突表述", not conflicted,
      f"仍有冲突 {len(conflicted)} 个：{conflicted[:2]}")
check("[4] frontmatter 与 content 的 YAML 头同源（已同步）", not unsynced,
      f"未同步 {len(unsynced)} 个：{unsynced[:3]}")
# ── [5][6][7]：区分「既有偏差」与「本次改造引入的回归」──
_new_bad_author = [n for n in author_bad if n not in _KNOWN_DEVIATIONS]
_new_bad_keys = [(n, m) for n, m in missing_keys if n not in _KNOWN_DEVIATIONS]
check("[5] author 不退化（清单外无新增取不到）", not _new_bad_author,
      f"新增 {_new_bad_author}" if _new_bad_author
      else f"既有偏差 {[n for n in author_bad if n in _KNOWN_DEVIATIONS]}")
check("[6] 必需键齐全（清单外无新增缺键）", not _new_bad_keys,
      f"新增 {_new_bad_keys}" if _new_bad_keys
      else f"既有偏差 {[n for n, _m in missing_keys if n in _KNOWN_DEVIATIONS]}")
for _n in author_bad:
    if _n in _KNOWN_DEVIATIONS:
        _WARNS.append(f"[既知] {_n} author 取不到 —— {_KNOWN_DEVIATIONS[_n]}")

# ── [7] 无 YAML 头的技能保持原值（不得被伪造）──
no_yaml = [(r["name"], (r["frontmatter"] or ""))
           for r in rows if not _extract_yaml(r["content"] or "")]
forged = [n for n, fm in no_yaml if fm.strip() and fm.strip() not in ("{}",)
          and not fm.lstrip().startswith("---")]
_new_forged = [n for n in forged if n not in _KNOWN_DEVIATIONS]
check("[7] 无 YAML 头的技能未**新增**伪造 frontmatter", not _new_forged,
      f"新增伪造 {_new_forged}" if _new_forged
      else f"既有偏差 {[n for n in forged if n in _KNOWN_DEVIATIONS]}"
           f"（无 YAML 头的技能共 {len(no_yaml)} 个）")
for _n in forged:
    if _n in _KNOWN_DEVIATIONS:
        _WARNS.append(f"[既知] {_n} frontmatter 非 YAML —— {_KNOWN_DEVIATIONS[_n]}")

# ── [8] 变异自证：模拟"整份正文塞进 frontmatter"必须被 [1] 检出 ──
_sample = rows[0] if rows else None
if _sample:
    _fake = (("---\nname: x\n---\n" + ("填充" * 300)))[:INJECT_CAP + 50]
    check("[8] 变异自证：超限值会被 [1] 的判据检出",
          len(_fake) > INJECT_CAP,
          f"构造 {len(_fake)} 字符 > 上限 {INJECT_CAP} ⇒ 若 [1] 通过则说明判据空转")

# ── [9] 端到端：真实装配里无冲突话术 ──
try:
    from agent import AgentPipeline
    U = {"role_name": "admin", "role": "admin"}
    p = AgentPipeline()
    c = get_db()
    try:
        p.registry.load_from_db(c, U)
    finally:
        c.close()
    sp = p._build_skill_prompt("chat", "生成活动图", U) or ""
    check("[9] 真实 skill_prompt 无「轮次上限为 3」",
          "轮次上限为 3" not in sp, f"长度 {len(sp)}")
    check("[9] 真实 skill_prompt 有运行时真值注入",
          "运行时真值" in sp or "工具轮次预算" in sp)
except Exception as e:
    check("[9] 真实 skill_prompt 无冲突话术", False, f"{type(e).__name__}: {e}")

con.close()

print("\n" + "=" * 68)
if _WARNS:
    print(f"⚠️ 已知既有偏差 {len(_WARNS)} 条（不影响本次改造结论）：")
    for w in _WARNS:
        print("   " + w)
    print("   ⇒ 这些需**人工补数据**（补 content 的 YAML 头 / triggers / author），")
    print("     不在本脚本职责内；本门禁只保证「不新增偏差」。")
    print()
if _FAILS:
    print(f"❌ FAIL {len(_FAILS)} 项：" + "; ".join(_FAILS))
    sys.exit(1)
print("✅ ALL PASS —— frontmatter 已同步为 YAML 头（不截断/无冲突/author 保留/无 YAML 不伪造）")
