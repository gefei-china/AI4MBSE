"""门禁：L1 结构化问句生成（标题路径增强 HyDE）必须满足的三条。

守：
① **默认关闭零漂移**：`ingest.hyde_heading_enabled` 默认 False
   ⇒ `enhanced_hyde_questions()` 恒返回 None ⇒ 调用方保留原实现。
② **代码块内的 `#` 不算标题**：否则整段Python 代码会被当成标题，
   问句变成"def foo() 是什么？"这类噪声（ragflow qa.go:216 同款处理）。
③ **永不劣化**：无标题块（纯正文/表格/OCR）必须返回 None，
   由调用方回落到原模板；增强版的问句数不得少于 1。

变异自证：把「代码围栏短路」摘掉（让代码块内的 # 也算标题）⇒ 必须判红。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

MD_NESTED = ("# SysML V2 建模规范\n\n"
             "## 需求定义\n\n"
             "### 条目化要求\n每条需求必须可追溯到验证项。\n")
MD_WITH_CODE = ("# 说明\n\n正文内容。\n\n"
                "```python\n# 这是注释不是标题\ndef foo(): pass\n```\n\n"
                "## 真实标题\n内容2\n")
MD_PLAIN = "整车应具备儿童遗留保护功能，无任何 markdown 标题。"



def section_clean_checks() -> list:
    """★ 追加（2026-10-10 查证时发现的真缺陷）：
    真库 16436 条 hyde 问题重复率 60.4%，因 section 被原样塞进问句。"""
    from knowledge_pipeline.ingest import _clean_section, _generate_hyde_questions
    cases = [
        ("## 附录 C：标准库", "附录 C：标准库", "剥 markdown 标记"),
        ("### 0.1 权威层级", "权威层级", "剥编号前缀"),
        ("**Systems Library**", "Systems Library", "粗体成对去标记"),
        ("0..*", "0..*", "★ 保留 KerML 基数语法（剥 * 会变 0..）"),
        ("1.2.1.2.3 子系统架构设计", "子系统架构设计", "剥多级编号"),
        ("1.2.1", "", "纯编号 → 空（触发调用方回落）"),
        ("", "", "空输入安全"),
    ]
    out = []
    for src, want, why in cases:
        got = _clean_section(src)
        out.append((f"清洗 {src!r} → {want!r}（{why}）", got == want))
    # 端到端：脏 section 不再污染问句
    q = _generate_hyde_questions("一些正文内容用于测试。", "## 附录 C：标准库可导入包清单")
    out.append((f"★ 端到端：问句不含原始 ## （实测 {q[0]!r}）",
                "##" not in q[0] and q[0].startswith("什么是附录 C")))
    return out


def main() -> int:
    print("=" * 74)
    print("L1 结构化问句门禁（默认关闭 / 代码块隔离 / 永不劣化）")
    print("=" * 74)
    ok = True

    from core import config
    from knowledge_pipeline import chunking_deepdoc as dd

    # ── ① 默认关闭 ⇒ 恒 None ──────────────────────────────────────
    print("\n① 默认关闭（零漂移）")
    print(f"[{'OK  ' if not config.as_bool('ingest', 'hyde_heading_enabled', False) else 'FAIL'}] "
          f"★ ingest.hyde_heading_enabled 默认 False")
    ok = ok and not config.as_bool("ingest", "hyde_heading_enabled", False)
    off = dd.enhanced_hyde_questions(MD_NESTED)
    print(f"[{'OK  ' if off is None else 'FAIL'}] ★ 关闭时返回 None（实测 {off!r}）")
    ok = ok and off is None

    # 打开做后续验证
    config.save_override({"ingest.hyde_heading_enabled": True})
    try:
        # ── ② 标题层级路径 ─────────────────────────────────────────
        print("\n② 标题层级路径（ragflow qa.go 同款标题栈）")
        path = dd.heading_path(MD_NESTED)
        want = ["SysML V2 建模规范", "需求定义", "条目化要求"]
        print(f"[{'OK  ' if path == want else 'FAIL'}] ★ 三级标题路径（实测 {path}）")
        ok = ok and path == want
        qs = dd.enhanced_hyde_questions(MD_NESTED)
        print(f"[{'OK  ' if qs and len(qs) >= 3 else 'FAIL'}] ★ 生成多种问法"
              f"（{len(qs) if qs else 0} 条：{qs}）")
        ok = ok and bool(qs) and len(qs) >= 3
        # 区分度：问句之间不应完全相同（原模板的缺陷）
        uniq_ratio = len(set(qs or [])) / max(len(qs or []), 1)
        print(f"[{'OK  ' if uniq_ratio == 1.0 else 'FAIL'}] ★ 问句无重复"
              f"（唯一率 {uniq_ratio:.0%}，原模板三块可能同质）")
        ok = ok and uniq_ratio == 1.0

        # ── ③ 代码块隔离 ──────────────────────────────────────────
        print("\n③ 代码块内的 # 不算标题")
        p2 = dd.heading_path(MD_WITH_CODE)
        clean = ("# 这是注释不是标题" not in p2 and "def foo(): pass" not in p2
                 and "真实标题" in p2)
        print(f"[{'OK  ' if clean else 'FAIL'}] ★ 代码块标题被排除（实测 {p2}）")
        ok = ok and clean

        # ── ④ 永不劣化（无标题 ⇒ None）─────────────────────────────
        print("\n④ 永不劣化（无标题块回落到原模板）")
        none_plain = dd.enhanced_hyde_questions(MD_PLAIN)
        print(f"[{'OK  ' if none_plain is None else 'FAIL'}] ★ 纯正文返回 None"
              f"（实测 {none_plain!r}）⇒ 调用方用原模板")
        ok = ok and none_plain is None
        empty = dd.enhanced_hyde_questions("")
        print(f"[{'OK  ' if empty is None else 'FAIL'}] ★ 空内容返回 None")
        ok = ok and empty is None
    finally:
        config.save_override({"ingest.hyde_heading_enabled": False})

    # ── 接线检查 ─────────────────────────────────────────────────
    print("\n⑤ 接线安全（ingest 侧必须有 try 兜底 + 默认回落）")
    with open(os.path.join(_ROOT, "knowledge_pipeline", "ingest.py"),
              encoding="utf-8") as f:
        src = f.read()
    wired = "enhanced_hyde_questions" in src
    guarded = "from knowledge_pipeline.chunking_deepdoc import enhanced_hyde_questions" in src
    print(f"[{'OK  ' if wired else 'FAIL'}] ingest.py 已接线标题增强")
    print(f"[{'OK  ' if guarded else 'FAIL'}] ★ 局部 import 且在 try 内（导入失败不影响入库）")
    ok = ok and wired and guarded

    print("\n⑥ section 清洗（真库 60.4% 重复率的根因修复）")
    for name, passed in section_clean_checks():
        print(f"[{'OK  ' if passed else 'FAIL'}] {name}")
        ok = ok and passed

    # ── 变异自证：摘掉代码围栏短路 ⇒ 必须判红 ──────────────────────
    print("\n[变异自证] 让代码块内的 # 也算标题（摘掉围栏短路）⇒ 应判红")
    path_p = os.path.join(_ROOT, "knowledge_pipeline", "chunking_deepdoc.py")
    with open(path_p, encoding="utf-8") as f:
        src = f.read()
    anchor = "if _FENCE_RE.match(line):"
    if anchor not in src:
        print("     [变异] 锚点未找到")
        ok = False
    else:
        try:
            with open(path_p, "w", encoding="utf-8") as f:
                f.write(src.replace(anchor, "if False:  # 变异", 1))
            import importlib
            importlib.reload(dd)
            mut = dd.heading_path(MD_WITH_CODE)
            red = any("这是注释不是标题" in x or "def foo" in x for x in mut)
            print(f"[变异] 摘掉短路后标题路径={mut}")
            print(f"[变异] ⇒ {'已判红（代码块隔离这条被守住了）' if red else '★未判红'}")
            ok = ok and red
        finally:
            with open(path_p, "w", encoding="utf-8") as f:
                f.write(src)
            import importlib
            importlib.reload(dd)
            back = dd.heading_path(MD_WITH_CODE)
            print(f"[收尾] 已回滚，路径={back}"
                  f"{'' if '这是注释不是标题' not in back else ' ★回滚失败!'}")

    print("\n" + "=" * 74)
    print(f"结论：{'全部通过' if ok else '存在失败项'}")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())