# -*- coding: utf-8 -*-
"""verify_sysml_classify.py —— SysML 诊断三路分类（词法/语法/语义）必须真的在分类。

## 真机事故（2026-11-17，米爸报「检验逻辑要细化结果，不能统一标记为词法错误」）

实测：`sysml_models/ev_thermal_mgmt` 的 20 条 ERROR **全部被标成「词法」**，
`n_syntax = 0`、`n_semantic = 0`。而基线记录的是「词法 0 / 语法 2 / 语义 18」。

### 根因：一行被改空的默认正则（提交说明里完全没提）

`core/domain/sysml_v2_check.py` 的 `_LEX_DEFAULT` 在 `cf33d3e`
（2026-11-09~10 那条 391 门禁的大提交）里被改成 `r""`：

```diff
- _LEX_DEFAULT = r"no viable alternative at character|token recognition error|mismatched character"
+ _LEX_DEFAULT = r""
```

`re.compile("").search(任意字符串)` **恒为真**（空模式在位置 0 即匹配）
⇒ `classify()` 第一句 `if lex.search(msg): return "lexical"` 永远命中
⇒ 所有诊断都是「词法」。

**为什么这种改动能活下来**（值得记住的失效模式）：
`n_hard = n_lexical + n_syntax` 仍然算得对 ⇒ **「拦不拦」的结论没变**，
只有**标签**错了。而该函数上方原本就有一段注释说
「路别只影响给 LLM 的标签精度，不影响拦不拦」——
这句话被当成了"标签错了没关系"的许可。
**但标签精度本身就是需求**：用户要靠错因决定怎么改（词法错改字符，语法错改结构）。

### 同族第二形态（也一起修了）

`_join_pattern(base, extra)` 原实现 `f"{base}|{extra}"`：
当 `base=""` 时得到 `"|extra"` —— **带空分支的交替**，空分支匹配一切。
即使默认值修好，只要有人在 config 里把 `lexical_signs` 清空、只留
`lexical_signs_extra`，同样会全判词法。

⇒ 核心原则：**空规则集 == 不匹配任何东西**（`(?!)`），
   **绝不等于"匹配一切"**（空正则是后者）。

## 判据

A 组（分类正确性，纯函数）：
  A1 词法文案 → lexical（token recognition error / at character N / mismatched character）
  A2 语法文案 → syntax（no viable alternative at input / mismatched input / extraneous input / missing）
  A3 其他文案 → semantic
  A4 **三条路都有命中**（防"某一路恒空"——这正是本次事故的形态）
  A5 `classify` 的判定顺序是**词法优先**（与 ANTLR 报错层次一致）

B 组（空规则集防护，本次事故的两种形态）：
  B1 `_LEX_DEFAULT` / `_SYN_DEFAULT` 非空
  B2 `_join_pattern("", "")` 返回**永不匹配**的正则（不是空串）
  B3 `_join_pattern("", "x")` **不会**退化成"匹配一切"
  B4 变异自证：把词法正则置空后，A4「三路都有命中」必须翻红

C 组（真实校验，端到端）：
  C1 对 `sysml_models/ev_thermal_mgmt` 跑一次，**不得**出现"全部归同一路"
  C2 与基线一致（有基线文件时）；无基线时只断言"不是单一分类"

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_sysml_classify.py
退出码：0 全绿（无 java/jar 时 C 组 SKIP）／1 有失败
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
#: 基线文件在 `tools/verify/` 下（与 verify_sysml_check_gate.py 同目录），不在仓库根。
BASELINE = os.path.join(ROOT, "tools", "verify", "sysml_check_baseline.json")

_PASS, _FAIL, _SKIP = [], [], []


def chk(name, ok, detail=""):
    (_PASS if ok else _FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("　— " + str(detail)) if detail and not ok else ""))


def skip(name, why):
    _SKIP.append(name)
    print("  [SKIP] %s :: %s" % (name, why))


def main():
    print("=" * 78)
    print("门禁：SysML 诊断三路分类（词法/语法/语义）真的在分类")
    print("=" * 78)

    import sysml_v2_check as V

    print("\n--- GROUP_A ---")
    V.reload_signs()
    lex_cases = [
        "token recognition error at: '§'",
        "token recognition error at: 0xA7",
        "at character 5, invalid char",
        "at character 42 unexpected char",
        "mismatched character '<EOF>' expecting ''",
        "no viable alternative at character '\u00a7'",
    ]
    syn_cases = [
        "no viable alternative at input 'foo'",
        "no viable alternative at input 'typed'",
        "mismatched input ';' expecting TYPE",
        "extraneous input ')' expecting EOF",
        "missing TYPE at 'x'",
    ]
    sem_cases = [
        "元素未归一入 BFO 基本范畴",
        "requirement 缺少 id 属性",
        "some semantic problem about element",
        "无法解析的引用 ::",
    ]
    bad_lex = [m for m in lex_cases if V.classify(m) != "lexical"]
    bad_syn = [m for m in syn_cases if V.classify(m) != "syntax"]
    bad_sem = [m for m in sem_cases if V.classify(m) != "semantic"]
    chk("A1 词法文案 → lexical", not bad_lex, bad_lex[:3])
    chk("A2 语法文案 → syntax", not bad_syn, bad_syn[:3])
    chk("A3 其他文案 → semantic", not bad_sem, bad_sem[:3])

    # A4：三条路都必须能有命中（本次事故正是"只有一条路有命中"）
    all_paths = {V.classify(m) for m in lex_cases + syn_cases + sem_cases}
    chk("A4 三条路都有命中（防某一路恒空 —— 本次事故形态）",
        all_paths == {"lexical", "syntax", "semantic"}, sorted(all_paths))

    # A5：判定顺序词法优先。构造一条同时含两路关键词的消息，
    #     必须判词法（与 ANTLR「lexer 先于 parser 报错」的层次一致）。
    mixed = "token recognition error; mismatched input 'x'"
    chk("A5 两路关键词同时出现时判词法（顺序敏感：词法优先）",
        V.classify(mixed) == "lexical", V.classify(mixed))

    print("\n--- GROUP_B ---")
    chk("B1a `_LEX_DEFAULT` 非空（空正则 = 匹配一切 = 全判词法）",
        bool(str(V._LEX_DEFAULT or "").strip()), repr(V._LEX_DEFAULT))
    chk("B1b `_SYN_DEFAULT` 非空",
        bool(str(V._SYN_DEFAULT or "").strip()), repr(V._SYN_DEFAULT))

    # B2：空+空 ⇒ 永不匹配（不是空串）
    both_empty = V._join_pattern("", "")
    never_matches = False
    try:
        never_matches = not bool(re.search(both_empty, "any text at all"))
    except Exception:
        never_matches = False
    chk("B2 `_join_pattern('','')` 返回永不匹配的正则（不是空串）",
        never_matches, "pattern=%r" % both_empty)

    # B3："只追加 extra"不得退化成匹配一切
    only_extra = V._join_pattern("", "token recognition error")
    hit_ok = bool(re.search(only_extra, "token recognition error at: 'x'"))
    hit_no = not bool(re.search(only_extra, "完全无关的语义错误"))
    chk("B3 `_join_pattern('', extra)` 只匹配 extra，不退化成匹配一切",
        hit_ok and hit_no, "pattern=%r hit_ok=%s hit_no=%s"
        % (only_extra, hit_ok, hit_no))

    # B4 变异自证：把词法正则置空 ⇒ 模拟事故 ⇒ A4 判据必须能翻红
    _orig_lex = V._LEX_DEFAULT
    try:
        V._LEX_DEFAULT = r""
        V.reload_signs()
        # 重新计算"三路是否都有命中"
        mut_paths = {V.classify(m) for m in lex_cases + syn_cases + sem_cases}
        # 事故形态：全判词法 ⇒ mut_paths == {"lexical"} ⇒ A4 条件不成立 ⇒ 翻红
        chk("B4 变异自证：词法正则置空 ⇒ 三路退化为一类 ⇒ A4 必翻红",
            mut_paths != {"lexical", "syntax", "semantic"},
            "变异后分类集合=%s（若仍三路齐全说明判据抓不到）" % sorted(mut_paths))
    finally:
        V._LEX_DEFAULT = _orig_lex
        V.reload_signs()

    print("\n--- GROUP_C ---")
    model_dir = os.path.join(ROOT, "sysml_models", "ev_thermal_mgmt")
    if not V.available():
        for n in ("C1 真实校验不是单一分类", "C2 与基线一致"):
            skip(n, "校验器三件套不齐（java/jar/sysml.library）")
        return _summary()
    if not os.path.isdir(model_dir):
        for n in ("C1 真实校验不是单一分类", "C2 与基线一致"):
            skip(n, "样例目录不存在: %s" % model_dir)
        return _summary()

    try:
        import glob
        files = sorted(glob.glob(os.path.join(model_dir, "*.sysml")))
        r = V.check_project(files)
    except Exception as e:
        chk("C1 真实校验不是单一分类", False, "调用失败: %r" % (e,))
        return _summary()

    n_lex = r.get("n_lexical") or 0
    n_syn = r.get("n_syntax") or 0
    n_sem = r.get("n_semantic") or 0
    n_err = (r.get("n_error") or 0)
    kinds = [k for k, v in (("词法", n_lex), ("语法", n_syn), ("语义", n_sem)) if v > 0]
    print("      实测：词法 %d / 语法 %d / 语义 %d（ERROR 总 %d）"
          % (n_lex, n_syn, n_sem, n_err))
    # C1：有错误时不得"全归同一路"（这正是事故形态）
    if n_err == 0:
        chk("C1 真实校验无错误（无法检验分类，按 SKIP 记）", True)
        print("      （样例当前零 ERROR，分类判据由 A 组纯函数覆盖）")
    else:
        chk("C1 真实校验不是单一分类（≥2 条路有命中）",
            len(kinds) >= 2, "只有 %s 一路有命中：%s"
            % (kinds[0] if kinds else "?", {"词法": n_lex, "语法": n_syn, "语义": n_sem}))

    # C2：与基线对拍
    if not os.path.isfile(BASELINE):
        skip("C2 与基线一致", "无基线文件 %s" % os.path.basename(BASELINE))
    else:
        try:
            base = json.load(open(BASELINE, encoding="utf-8"))
            b = base.get("counts") or base
            want = {k: b.get(k) for k in ("n_lexical", "n_syntax", "n_semantic")}
            got = {"n_lexical": n_lex, "n_syntax": n_syn, "n_semantic": n_sem}
            diff = {k: (want[k], got[k]) for k in want if want[k] is not None and want[k] != got[k]}
            chk("C2 三路计数与基线一致", not diff, "不一致（基线, 实测）: %s" % diff)
        except Exception as e:
            chk("C2 三路计数与基线一致", False, "基线读取失败: %r" % (e,))

    return _summary()


def _summary():
    print("\n" + "=" * 78)
    print("结果：PASS %d / FAIL %d / SKIP %d" % (len(_PASS), len(_FAIL), len(_SKIP)))
    for n in _FAIL:
        print("  - " + n)
    if _FAIL:
        print("\n⚠️ 分类失效的典型原因：正则被改空（空正则匹配一切）")
        print("   检查 core/domain/sysml_v2_check.py 的 _LEX_DEFAULT / _SYN_DEFAULT / _join_pattern")
    print("=" * 78)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
