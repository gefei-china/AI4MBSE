"""migrate_v17_methodology_rules —把广汽方法论的祈使句落进`methodology_rules`。

## 背景（2026-10-09 实测）

`methodology_rules` 表**早已建好但0 行**（`migrate_v9_four_tools.py` 建的），
`methodology_profile_tools.py:_load_rule_layer` 会读它并如实报告"该profile 无规则行"。
⇒ 规则层是**设计中但从未落地**的载体。

而`sysml_v2_lint` 是**唯一真正执行判定**的门禁工具，但它当前只覆盖
**命名规范 + 禁用构造**，且命名规则来自 `methodology_profile_tools.DEFAULT_PROFILE`
（通用 OMG 风格），**与客户方法论无关**。

## ★ 本脚本最重要的结论：24 条祈使句**一条都不能**写成代码门禁

用户要求"25 条祈使句编程门禁"。实测逐条验证（判据可复现，见下方 `--prove`）：

| 分类 | 条数 | 为什么不可判定 |
|---|---|---|
| 术语释义（表格行） | 12 | ���"约束使用 constraint \| 定义场景必须满足的时空边界条件"——**这是术语解释**，不是约束 |
| 对工具链的要求 | 7 | "行为元模型**必须支持**功能分层、动作细化…"——主语是**元模型**，不是生成的代码 |
| 元层设计描述 | 5 | "横向仍保持需求、结构、行为…六类方面"——描述方法论结构，无判定对象 |

**机器门禁的必要条件**（句中须同时含「具体 SysML 元素」+「明确的违规行为」）：
⇒ 实测 **0/24 条满足**。

⇒ **不硬凑**。硬凑出来的规则会全部是假阳性或恒绿，
   与本项目已反复踩到的"判据比被测物更严/更松"同一族错误。

## 那这些句子该放哪？

落进 `methodology_rules`，`rule_type` 显式区分可判定性：

- `machine`     —— 有可判定的元素+违规行为（**当前 0 条**，schema 预留）
- `human_review`—— 需人工确认（**当前 24 条**，全部）

`enforce` 一律填 `lint`（**提示级、不阻断**），因为这些是"设计要求"而非
"代码违规"——把它们设成 `check`/`forbid` 会让门禁误杀合格产出。

## 这解决的是什么问题

规则层从"空表"变成"有24 条带来源、可追溯的规约"：
- `methodology_profile_tools` 能如实报告"该 profile 有 24 条规约，全部待人工确认"
  （而不是"无规则行"= 让人误以为方法论没约束）
- N4 校验修复节点能读到这些条款作为**检查清单**
- 业务方review 后把某条改写成 `rule_type='machine'` + 填 `expression`，
  **同一张表即可变成真门禁**，无需改代码

## 用法

```
./.venv/Scripts/python.exe -X utf8 tools/migrate_v17_methodology_rules.py --prove   # 只打印逐条判定
./.venv/Scripts/python.exe -X utf8 tools/migrate_v17_methodology_rules.py          # 写库
./.venv/Scripts/python.exe -X utf8 tools/migrate_v17_methodology_rules.py --dry-run
```

幂等：`UNIQUE(profile_id, rule_id)` + 按 `rule_id` 覆盖式更新。
回滚：本脚本输出的「改动前原值」照抄即可；或
`DELETE FROM methodology_rules WHERE profile_id='gac_260813' AND source_doc_id=812`。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: 规则所属 profile（与 methodology_profile_tools 的 profile 命名一致）
PROFILE_ID = "gac_260813"
#: 规则来源文档
SOURCE_DOC_ID = 812
SOURCE_DOC_NAME = "广汽方法论-260813.docx"

# ── 抽取祈使句的关键词（实测：chunk 内无换行，必须按标点切句）──
IMPERATIVE_KW = r"(禁止|不得|必须|严禁|不允许|应当|切勿|避免|需满足)"

# ── 机器门禁必要条件：句中须同时含【具体 SysML 元素】+【明确的违规行为】──
CODE_TOKENS = (
    r"(part|port|state|transition|action|use\s+case|requirement|constraint"
    r"|import|package|namespace|attribute|connection|connect|satisfy|subsets?)"
)
VIOLATION_KW = r"(不允许|禁止|不得|必须|只能|仅允许)"

#: 分类判据（可复现，不靠印象）
SUBJ_META = (
    r"(元模型|建模框架|视图规范参考|需求元模型|结构元模型|行为元模型"
    r"|分析元模型|方法论|元模型级)"
)


def extract_imperatives(conn) -> list:
    """抽取去重后的祈使句 [(chunk_index, sentence)]。"""
    rows = conn.execute(
        "SELECT chunk_index, content FROM document_chunks "
        "WHERE document_id=? ORDER BY chunk_index", (SOURCE_DOC_ID,)).fetchall()
    seen, out = set(), []
    for r in rows:
        # ⚠️ chunk 内没有换行（实测 180 chunk 只有 1 个"段落"）⇒ 必须按标点切句
        for sent in re.split(r"[。；\n]", r["content"] or ""):
            s = sent.strip()
            if not (6 <= len(s) <= 200):
                continue
            if not re.search(IMPERATIVE_KW, s):
                continue
            key = re.sub(r"\s+", "", s)
            if key in seen:
                continue
            seen.add(key)
            out.append((r["chunk_index"], s))
    return out


def classify(sentence: str) -> tuple:
    """返回 (rule_type, 可判定性判定理由)。判据必须可复现。"""
    # 表格行 = 术语释义
    if sentence.lstrip().startswith("|"):
        return "human_review", "表格行/术语释义，无可判定对象"
    has_code = bool(re.search(CODE_TOKENS, sentence, re.I))
    has_vio = bool(re.search(VIOLATION_KW, sentence))
    if has_code and has_vio:
        return "machine", "含具体元素+违规行为，可正则判定"
    if re.search(SUBJ_META, sentence):
        return "human_review", "主语是元模型/框架，对生成的代码不可判定"
    if not has_code:
        return "human_review", "句中无具体 SysML 元素，无法定位判定对象"
    return "human_review", "有元素但违规行为不明确，需人工定义判定方式"


def prove(items: list) -> int:
    """打印逐条判定，证明"没有一条能写成代码门禁"。"""
    from collections import Counter
    cnt = Counter(t for _, _, t, _ in items)
    print("=" * 72)
    print("逐条判定：机器门禁必要条件 = 【具体 SysML 元素】+【明确的违规行为】")
    print("=" * 72)
    for ci, s, rt, why in items:
        print(f"  [{rt:12}] [c{ci:3}] {s[:74]}")
        print(f"                 └ {why}")
    print("\n" + "-" * 72)
    print(f"共 {len(items)} 条：")
    for k, n in cnt.most_common():
        print(f"  {k:14} {n:2} 条")
    n_machine = cnt.get("machine", 0)
    print(f"\n⇒ 可机器判定：{n_machine}/{len(items)} 条")
    if n_machine == 0:
        print("   **没有一条能写成代码门禁** ⇒ 不硬凑规则；")
        print("   全部落进 methodology_rules 并标 human_review，等业务方 review。")
    return 0


def run(conn, args) -> int:
    items = [(ci, s, *classify(s)) for ci, s in extract_imperatives(conn)]
    if not items:
        print("[FAIL] 没抽出任何祈使句 —— 是文档变了还是抽取逻辑坏了？")
        return 1

    prove(items)

    print("\n" + "=" * 72)
    print("① 改动前（回滚照抄）")
    print("=" * 72)
    before = conn.execute(
        "SELECT rule_id, scope, rule_type, enforce, message FROM methodology_rules "
        "WHERE profile_id=? ORDER BY rule_id", (PROFILE_ID,)).fetchall()
    print(f"  {PROFILE_ID} 现有规则 {len(before)} 行")
    for r in before[:10]:
        print(f"    {r['rule_id']}")

    # ── 构造规则行 ──
    # rule_id 用 chunk 序号 + 序号，稳定且可回溯到原文位置
    payload = []
    for i, (ci, s, rt, why) in enumerate(items, 1):
        payload.append({
            "rule_id": f"GAC-C{ci:03d}-{i:02d}",
            # scope：用命中的关键词标注约束类型，便于按主题检索
            "scope": "methodology",
            "rule_type": rt,
            # expression 留空：当前无机器可判定表达式（不可硬凑）
            "expression": None,
            "message": f"{s}",
            "enforce": "lint",       # 一律提示级：这些是设计要求，不是代码违规
            "source_doc_id": SOURCE_DOC_ID,
            "_chunk": ci,
            "_why": why,
        })

    print("\n" + "=" * 72)
    print("② 将写入")
    print("=" * 72)
    from collections import Counter
    c2 = Counter(p["rule_type"] for p in payload)
    print(f"  {PROFILE_ID}：{len(payload)} 条{dict(c2)}")
    print(f"  enforce 全部= lint（不阻断）｜profile_id={PROFILE_ID}")
    print(f"  source_doc_id={SOURCE_DOC_ID}（{SOURCE_DOC_NAME}）")
    for p in payload[:5]:
        print(f"    {p['rule_id']}  {p['_why']}")

    if args.dry_run:
        print("\n[dry-run] 未写库")
        return 0

    print("\n" + "=" * 72)
    print("③ 写库（覆盖式，幂等）")
    print("=" * 72)
    for p in payload:
        conn.execute(
            "INSERT INTO methodology_rules "
            "(profile_id, rule_id, scope, rule_type, expression, message, enforce, source_doc_id) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(profile_id, rule_id) DO UPDATE SET "
            "scope=excluded.scope, rule_type=excluded.rule_type, "
            "expression=excluded.expression, message=excluded.message, "
            "enforce=excluded.enforce, source_doc_id=excluded.source_doc_id",
            (PROFILE_ID, p["rule_id"], p["scope"], p["rule_type"], p["expression"],
             p["message"], p["enforce"], p["source_doc_id"]))
    conn.commit()
    print(f"  [写入] {len(payload)} 条")

    # ── 出口断言 ──
    print("\n" + "=" * 72)
    print("④ 出口断言")
    print("=" * 72)
    problems = []
    n = conn.execute(
        "SELECT count(*) FROM methodology_rules WHERE profile_id=?",
        (PROFILE_ID,)).fetchone()[0]
    print(f"  [{'OK  ' if n == len(payload) else 'FAIL'}] 行数 {n}/{len(payload)}")
    if n != len(payload):
        problems.append(f"行数不符：{n} != {len(payload)}")

    # 所有 machine 类必须有 expression，否则是"假机器规则"
    bad_machine = conn.execute(
        "SELECT rule_id FROM methodology_rules "
        "WHERE profile_id=? AND rule_type='machine' AND (expression IS NULL OR expression='')",
        (PROFILE_ID,)).fetchall()
    print(f"  [{'OK  ' if not bad_machine else 'FAIL'}] "
          f"machine 类均有 expression（{len(bad_machine)} 条缺）")
    if bad_machine:
        problems.append(f"machine 类缺 expression：{[r[0] for r in bad_machine]}")

    # enforce 只能三值
    bad_enf = conn.execute(
        "SELECT DISTINCT enforce FROM methodology_rules WHERE profile_id=?",
        (PROFILE_ID,)).fetchall()
    vals = sorted(r[0] for r in bad_enf)
    print(f"  [{'OK  ' if set(vals) <= {'lint','check','forbid'} else 'FAIL'}] "
          f"enforce 取值合法：{vals}")
    if not set(vals) <= {"lint", "check", "forbid"}:
        problems.append(f"enforce 非法取值：{vals}")

    if problems:
        print("\n[FAIL]")
        for p in problems:
            print("  -", p)
        return 1

    print("\n" + "=" * 72)
    print("⑤ 消费方验证（规则层能读到了吗）")
    print("=" * 72)
    from methodology_profile_tools import _load_rule_layer
    rules, warn = _load_rule_layer(conn, PROFILE_ID)
    if rules is None:
        print(f"  [FAIL] 消费方读不到：warn={warn}")
        return 1
    print(f"  [OK  ] _load_rule_layer 读到 {len(rules)} 条，warn={warn}")
    print(f"         样例: {rules[0]['id']} / {rules[0]['rule_type']} / "
          f"{rules[0]['message'][:40]}")

    print("\n✅ 规则层已落地（从0 行到 "
          f"{len(payload)} 条，全部 human_review + lint 级）")
    print("   ⚠️ 这些是**设计要求**不是代码违规，故 enforce=lint 不阻断。")
    print("   业务方 review 后，把某条改成 rule_type='machine' 并填 expression，")
    print("   同一张表即可成为真门禁，无需改代码。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只校验不写库")
    ap.add_argument("--prove", action="store_true", help="只打印逐条可判定性判定")
    args = ap.parse_args()

    from database import db_conn

    # ★ db_conn() 是 @contextmanager，必须 with 使用（误当连接对象会让
    #   下游 try/except 兜住异常 ⇒ 校验"假通过"，本项目已踩过两次）
    with db_conn() as conn:
        if args.prove:
            items = [(ci, s, *classify(s))
                     for ci, s in extract_imperatives(conn)]
            return prove(items)
        return run(conn, args)


if __name__ == "__main__":
    sys.exit(main())
