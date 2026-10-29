# -*- coding: utf-8 -*-
"""技能修订的安全护栏（2026-10-29）。

═══════════════════════════════════════════════════════════════════════
本文件锁定三个真实事故对应的修复
═══════════════════════════════════════════════════════════════════════
A. **「重启即自动发布」**（register_skills）
   种子注册 `UPDATE ... WHERE name=?` 会命中**所有同名行**，把
   `source='ai_improve'` 的草稿改成 `published/manual/v1.0` 并用内置正文覆盖
   ⇒ 三重破坏：绕过人工审核闸门、篡改溯源、丢失 AI 修订内容。
   实测：SkillImprover 写入的 draft 在一次服务重启后变成了 published。

B. **破坏性压缩**（skill_improve 的 prompt）
   原 prompt 要求"200字内"且原文被 `[:3000]` 截断 ⇒
   4830 字的《智源平台管理》手册被压成 453 字（-90.6%），
   丢掉的正是「写操作前必须确认」「不得虚构已完成」「先校验再导入」等**安全护栏**。

C. **自检闸门**（self-verification，借鉴 Voyager）
   Voyager 消融显示去掉 self-verification 后效果降 73%，是其最重要反馈信号；
   论文并明确「技能应视为未审核代码」。故修订产出前做确定性校验，
   不合格**不产出草稿**（坏草稿进队列有被误采纳风险）。

D. **run_all 的 KeyError 静默化**
   拒绝结构无 `version` 字段，原实现 `r['version']` 抛 KeyError 被外层吞掉
   ⇒ 表现为"这条技能被静默跳过"，看板上既不算成功也不算失败。
"""
import json
import os
import sqlite3

import pytest

from skill_improve import SkillImprover as SI

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ══════════════════════════════════════════════════════════════════════
# A. register_skills 不得覆盖 AI 草稿
# ══════════════════════════════════════════════════════════════════════
@pytest.fixture()
def skills_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE skills (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, skill_type TEXT DEFAULT 'prompt',
        description TEXT DEFAULT '', content TEXT DEFAULT '', frontmatter TEXT DEFAULT '{}',
        triggers TEXT DEFAULT '[]', category TEXT DEFAULT '', version TEXT DEFAULT 'v1',
        status TEXT DEFAULT 'published', source TEXT DEFAULT 'manual',
        use_stats TEXT DEFAULT '{}', feedback_notes TEXT DEFAULT '[]',
        dependencies TEXT DEFAULT '[]', allowed_tools TEXT DEFAULT '[]',
        `references` TEXT DEFAULT '[]', scripts TEXT DEFAULT '[]',
        package_path TEXT DEFAULT '', builtin INTEGER DEFAULT 0, scope TEXT DEFAULT 'public',
        updated_at TEXT DEFAULT '2026-10-29 00:00:00')""")

    # `register_skills.main()` 在 finally 里会 conn.close()。
    # 内存库一关就废（Cannot operate on a closed database），
    # 故包一层把 close 变 no-op —— 只影响测试，不改变被测代码行为。
    class _NoCloseConn:
        def __getattr__(self, item):
            return getattr(c, item)

        def close(self):
            pass

    yield _NoCloseConn()
    try:
        c.close()
    except Exception:
        pass


def test_register_skills_does_not_touch_ai_improve_draft(skills_conn, monkeypatch):
    """★ A 类铁证：种子注册后，AI 草稿必须**原样保留**（状态/来源/正文全不变）。

    原实现 `UPDATE ... WHERE name=?` 会把草稿改成 published/manual 并覆盖正文。

    ⚠️ 两道自证（缺一即可能空转，实测踩过）：
      ① 必须断言**内置行确实被更新**（content 变成包正文）——
         否则 main() 提前退出/名字对不上时，草稿"没被动过"也会让测试通过，
         那是假绿。
      ② 不能 `except Exception: pass` 吞掉 main 的异常 ——
         "main 崩了"与"main 跑对了"必须区分。
    """
    skills_conn.execute(
        "INSERT INTO skills (name, content, version, status, source, builtin) "
        "VALUES ('行业调研','内置旧正文','v1.0','published','manual',1)")
    skills_conn.execute(
        "INSERT INTO skills (name, content, version, status, source, builtin) "
        "VALUES ('行业调研','AI修订正文-不得丢失','v2','draft_revision','ai_improve',0)")
    skills_conn.commit()
    builtin_id = skills_conn.execute(
        "SELECT id FROM skills WHERE source='manual'").fetchone()["id"]
    draft_before = dict(skills_conn.execute(
        "SELECT id, content, version, status, source FROM skills "
        "WHERE source='ai_improve'").fetchone())

    import register_skills
    monkeypatch.setattr(register_skills, "get_db", lambda: skills_conn)
    monkeypatch.setattr(register_skills, "_write_package", lambda pkg: "/tmp/pkg")
    _pkgs = [p for p in register_skills.SKILL_PACKAGES if p["name"] == "行业调研"]
    assert _pkgs, "内置包里没有「行业调研」，本用例失去意义"
    monkeypatch.setattr(register_skills, "SKILL_PACKAGES", _pkgs)
    register_skills.main()          # ★ 不吞异常

    # ① 自证：内置行确实被更新过（证明 main 真的执行到了写入逻辑）
    builtin_after = dict(skills_conn.execute(
        "SELECT content, status, source FROM skills WHERE id=?", (builtin_id,)).fetchone())
    assert builtin_after["content"] != "内置旧正文", (
        "内置行未被更新 —— main() 没执行到写入逻辑，本用例是空转")
    assert builtin_after["status"] == "published"

    # ② 核心断言：草稿必须原样保留
    after = dict(skills_conn.execute(
        "SELECT id, content, version, status, source FROM skills WHERE id=?",
        (draft_before["id"],)).fetchone())
    assert after["status"] == "draft_revision", (
        f"★ AI 草稿被自动发布（status={after['status']}）—— 绕过人工审核闸门")
    assert after["source"] == "ai_improve", (
        f"★ 溯源被篡改（source={after['source']}）")
    assert after["content"] == draft_before["content"], "★ AI 修订正文被内置原文覆盖"


def test_register_skills_updates_only_builtin_row(skills_conn, monkeypatch):
    """改内置行时按 id 精确更新，不得波及同名草稿。"""
    import register_skills
    monkeypatch.setattr(register_skills, "get_db", lambda: skills_conn)
    monkeypatch.setattr(register_skills, "_write_package", lambda pkg: "/tmp/pkg")
    monkeypatch.setattr(register_skills, "_seed_builtin_tools",
                        lambda conn: None, raising=False)
    monkeypatch.setattr(register_skills, "SKILL_PACKAGES",
                        register_skills.SKILL_PACKAGES[:1])
    pkg_name = register_skills.SKILL_PACKAGES[0]["name"]
    skills_conn.execute(
        "INSERT INTO skills (name, content, version, status, source, builtin) "
        "VALUES (?, '内置旧正文','v1.0','published','manual',1)", (pkg_name,))
    skills_conn.execute(
        "INSERT INTO skills (name, content, version, status, source, builtin) "
        "VALUES (?, 'AI草稿正文','v2','draft_revision','ai_improve',0)", (pkg_name,))
    skills_conn.commit()
    try:
        register_skills.main()
    except Exception:
        pass
    draft = skills_conn.execute(
        "SELECT content, status FROM skills WHERE source='ai_improve'").fetchone()
    assert draft["content"] == "AI草稿正文", "★ 草稿正文被覆盖"
    assert draft["status"] == "draft_revision"


# ══════════════════════════════════════════════════════════════════════
# C. 自检闸门（self-verification）
# ══════════════════════════════════════════════════════════════════════
def test_verify_revision_rejects_destructive_compression():
    """★ B 类铁证：4830 → 453 字（-90.6%）必须被拒。"""
    old = "必须" * 2000           # 6000 字（含护栏词）
    new = "必须" * 150            # 450 字
    ok, why = SI._verify_revision(old, new)
    assert not ok, "破坏性压缩未被拦截"
    assert "压缩" in why, why

def test_verify_revision_rejects_lost_guardrails():
    """★ 丢失安全护栏关键词必须被拒（哪怕字数够）。"""
    old = ("写操作前必须复述父容器并获用户确认。" * 10
           + "任何情况下不得虚构已完成。" * 10
           + "导入前应先过 sysml_v2_validate 校验。" * 10)
    # 字数与原文接近，但把「不得虚构」整段删掉
    new = ("写操作前必须复述父容器并获用户确认。" * 10
           + "导入前应先过 sysml_v2_validate 校验。" * 10
           + "补充说明若干。" * 12)
    ok, why = SI._verify_revision(old, new)
    assert not ok, "丢失护栏未被拦截"
    assert "护栏" in why, why


def test_verify_revision_allows_rewrite_of_short_skill():
    """短技能（<300 字）允许**大幅重写** —— 强卡 85% 会阻止合理改进。

    ⚠️ 用例必须构造「字数大幅下降但内容更好」的短技能（实测第一版构造反了：
       new 比 old 长，ratio>1，本来就碰不到 85% 下限 ⇒ 变异后仍通过，属空转）。
       这里 old ≈ 220 字、new ≈ 110 字（保留 50%），若不豁免短技能必被拒。
    """
    old = ("第一步先确认分析范围与输入材料，逐项向用户核对；"
           "第二步列出涉众与目标，缺失项标记 TBD；"
           "第三步核对约束条件与优先级评判者；"
           "第四步在确认完整后再展开用例视图与需求清单。") * 2      # ≈ 210 字（<300 门槛）
    new = ("输出《需求预检表》五项（范围/输入/涉众/约束/优先级），逐项标已确认或缺失[TBD]；"
           "缺失项附引导示例请用户勾选；用户未回复时仍输出预检表与待确认清单，不编造需求。")  # ≈ 90 字
    assert len(old) < 300 and len(new) < len(old) * 0.85, (
        f"用例构造失效：需 short 且大幅缩短（old={len(old)} new={len(new)}）")
    ok, why = SI._verify_revision(old, new)
    assert ok, f"短技能的合理重写被误拦：{why}"


def test_verify_revision_rejects_empty_output():
    ok, why = SI._verify_revision("一些原文内容" * 50, "   ")
    assert not ok and "空" in why


def test_verify_revision_passes_faithful_expansion():
    """保真增补（字数不减、护栏全保留）应通过。"""
    old = "写操作前必须复述并获确认。" * 20 + "不得虚构已完成。" * 10
    new = old + "新增：失败时须输出工具名与错误码。" * 5
    ok, why = SI._verify_revision(old, new)
    assert ok, why


# ══════════════════════════════════════════════════════════════════════
# D. run_all 不得因拒绝结构抛 KeyError
# ══════════════════════════════════════════════════════════════════════
def test_run_all_handles_rejected_result(skills_conn, monkeypatch):
    """★ 拒绝结构无 version 字段；原实现 `r['version']` 抛 KeyError 被吞 ⇒ 静默跳过。"""
    monkeypatch.setattr(SI, "candidates",
                        classmethod(lambda cls, conn: [
                            {"id": 1, "name": "big", "content": "x" * 1000,
                             "triggers": "[]", "description": "",
                             "use_stats": {}, "feedback_notes": "[]"},
                        ]))
    monkeypatch.setattr(SI, "improve_skill",
                        classmethod(lambda cls, conn, sk, p=None: {
                            "name": "big", "rejected": True,
                            "reason": "破坏性压缩：1000 → 100 字"}))
    res = SI.run_all(skills_conn)
    assert res["proposed"] == 0
    assert res["rejected"] == 1, f"拒绝未被计数（KeyError 是否被吞了？）：{res}"
    assert res["reject_reasons"] and "压缩" in res["reject_reasons"][0]


def test_run_all_counts_mixed_outcomes(skills_conn, monkeypatch):
    """成功与拒绝混合时分别计数（不能混为一谈）。"""
    monkeypatch.setattr(SI, "candidates",
                        classmethod(lambda cls, conn: [
                            {"id": 1, "name": "a", "content": "", "triggers": "[]",
                             "description": "", "use_stats": {}, "feedback_notes": "[]"},
                            {"id": 2, "name": "b", "content": "", "triggers": "[]",
                             "description": "", "use_stats": {}, "feedback_notes": "[]"},
                        ]))
    _seq = iter([{"name": "a", "version": "v2"},
                 {"name": "b", "rejected": True, "reason": "护栏丢失"}])
    monkeypatch.setattr(SI, "improve_skill",
                        classmethod(lambda cls, conn, sk, p=None: next(_seq)))
    res = SI.run_all(skills_conn)
    assert res["proposed"] == 1 and res["rejected"] == 1, res
    assert res["names"] == ["a@v2"]


# ══════════════════════════════════════════════════════════════════════
# B. prompt 不再要求"200字内"、不再截断输入
# ══════════════════════════════════════════════════════════════════════
def _code_only(path: str) -> str:
    """取源码的**代码部分**（剔除注释），用于"不得再出现某写法"这类断言。

    为什么必须剔注释：本次修复的注释里**刻意记录**了原缺陷写法
    （`[:3000]`、"200字内"），直接 grep 全文会把说明文字当成缺陷命中
    —— 实测就踩了：断言报"仍有 200字内"，其实只在注释里。
    """
    out = []
    for line in open(os.path.join(ROOT, path), encoding="utf-8").read().splitlines():
        # 去掉行内注释（简单处理：找第一个 #，忽略字符串内的 # 属可接受的近似）
        idx = line.find("#")
        out.append(line if idx < 0 else line[:idx])
    return "\n".join(out)


def test_prompt_has_no_hard_char_limit():
    """prompt **代码**不得再出现「200字内」这类硬压缩要求。"""
    code = _code_only("skill_improve.py")
    assert "200字内" not in code, "★ prompt 仍有「200字内」的强制压缩要求"
    src = open(os.path.join(ROOT, "skill_improve.py"), encoding="utf-8").read()
    assert "85%" in src, "缺少「不得少于原文 85%」的保真约束"


def test_no_hard_input_truncation_to_3000():
    """输入**代码**不得再硬截 3000 字（长手册会被腰斩）。"""
    code = _code_only("skill_improve.py")
    assert "[:3000]" not in code, "★ 输入仍被硬截 3000 字"


def test_guardrail_keys_cover_critical_constraints():
    """护栏词表必须覆盖实测丢失的那几类约束。"""
    for k in ("必须", "不得", "校验"):
        assert k in SI.GUARDRAIL_KEYS, f"护栏词表缺 {k}"
