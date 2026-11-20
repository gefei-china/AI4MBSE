# -*- coding: utf-8 -*-
"""门禁：技能状态与操作项闭环（SKILL_STATE_ACTIONS_V1）

背景（2026-11-20 用户实测投诉）：
  新建的技能在能力中心显示「已停用」，操作区却只有一个「停用」按钮，没有任何
  「启用 / 发布」入口 —— 用户被锁死在死胡同里。根因三层，本门禁逐条钉住：

  A. 同步桥把「未发布(draft)」二元折叠成「已停用(disabled)」
     —— 两者对用户是完全不同的下一步（发布 vs 恢复），不能同词。
  B. 新建能力 author_id=0 ⇒ 创建者本人也不是「作者」(is_mine=False)
     ⇒ can_edit / can_publish_self 全灭，卡片只剩「卸载」。
  C. 卡片把「发布态」与「个人安装态」用同一个词并列（已停用 … 已启用），
     动作按钮却只看后者；技能卡还错调 MCP 的 CRUD 函数。

判据分组：
  S 静态契约（源码级，防倒退）
  P 纯函数（derive_status / _resolve_author）
  R 真库副本端到端（新建 draft → 同步 → 断言状态与归属）
  F 前端契约（限射程：loadSkillCombined 函数体内不得出现 MCP 函数调用）
  M 变异自证（把 derive_status 改回二元折叠 → S/P 必须翻红）
"""
# CI-OPTIONAL: 需真库 mbse.db 副本（R 组 sqlite3.backup）且 M 组临时改写源码做变异自证；干净克隆无真库，故只在 local 跑，已登记 gates_manifest.json 的 local 层
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "mbse.db")
PASS, FAIL = 0, 0
FAILS = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  FAIL  %s  << %s" % (name, detail))


def section(t):
    print("\n=== %s ===" % t)


def read(p):
    with open(os.path.join(ROOT, p), encoding="utf-8", errors="replace") as f:
        return f.read()


def code_only(src):
    """剥掉注释与文档串，只留可执行代码。

    ★ 为什么必须剥：本门禁断言「不再二元折叠」「死代码已删」，而源码里**注释与
    docstring 恰好引用了被删掉的旧写法**（用来解释为什么删）。不剥就会把"解释"
    当成"残留"，门禁变成误报器 —— 判断必须打在代码上，不是打在文字上。
    """
    s = re.sub(r'"""[\s\S]*?"""', "", src)
    s = re.sub(r"'''[\s\S]*?'''", "", s)
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in s.splitlines())


def _label(status):
    """发布态 → 用户可见文案（与 store/base.py 的 STATUS_LABELS 同源）。"""
    from plugin_system.store.base import STATUS_LABELS
    return STATUS_LABELS.get(status, status)


def func_body(src, name, until=None):
    """取函数体文本（用于限定源码断言的射程）。

    ★ 必须同时认 JS 的 `function` 与 Python 的 `def`：本门禁跨两类文件做比对，
      只认 `def` 时对 .js 一律取到空串，F 组就会以"命中=[]"的形式集体假红 ——
      那是断言射程取错，不是代码有问题（首轮实测即如此）。
    """
    esc = re.escape(name)
    m = re.search(r"\n(?:async )?function %s\b[\s\S]*?\n\}" % esc, src)
    if m:
        return m.group(0)
    m = re.search(r"\ndef %s\b[\s\S]*?(?=%s)" % (esc, until or r"\ndef |\nclass "), src)
    return m.group(0) if m else ""


# ────────────────────────── S 静态契约 ──────────────────────────
def sec_static():
    section("S 静态契约（源码级）")
    ls = read("plugin_system/legacy_sync.py")
    ls_code = code_only(ls)
    check("S1 同步桥不再二元折叠状态（代码里无 published/disabled 二选一写法）",
          "if available else \"disabled\"" not in ls_code
          and "if available else 'disabled'" not in ls_code)
    check("S2 derive_status 存在且被 sync_from_legacy 调用",
          "def derive_status(" in ls and "status = derive_status(" in ls)
    check("S3 derive_status 区分草稿族（draft / draft_revision）",
          '"draft", "draft_revision"' in ls)
    check("S4 新建/更新插件时解析归属（_resolve_author 被调用）",
          "def _resolve_author(" in ls and ls.count("_resolve_author(conn, d)") >= 2)

    dp = read("plugin_system/store/queries.py")
    check("S5 plugin_dto 给出单一可消费判据 consumable",
          'd["consumable"]' in dp and 'd.get("status") == "published"' in dp)

    sr = read("repositories/studio_repo.py")
    check("S6 create_skill 落 owner（skills 归属不再丢失）",
          "owner: str" in sr and "owner)" in sr and "INSERT INTO skills" in sr)

    rt = read("routers/studio_parts/skills.py")
    check("S7 编辑技能时对无归属的私有技能做归属自愈",
          "UPDATE skills SET owner=?" in rt and "'personal','private'" in rt)
    check("S8 已删除引用了未定义变量 s 的版本递增死代码（代码里已无该调用/定义）",
          "_bump_version(" not in code_only(rt))


# ────────────────────────── P 纯函数 ──────────────────────────
def sec_pure():
    section("P 纯函数（状态派生 / 归属解析）")
    from plugin_system import legacy_sync as ls

    cases = [
        ("skills", {"status": "draft", "enabled": 1}, "draft"),
        ("skills", {"status": "draft_revision", "enabled": 1}, "draft"),
        ("skills", {"status": "published", "enabled": 1}, "published"),
        ("skills", {"status": "published", "enabled": 0}, "disabled"),
        ("skills", {"status": "rejected", "enabled": 1}, "rejected"),
        ("tools", {"status": "draft"}, "draft"),
        ("tools", {"status": "active"}, "published"),
        ("mcp_servers", {"enabled": 0, "status": "online"}, "disabled"),
    ]
    for tbl, row, want in cases:
        got = ls.derive_status(tbl, row)
        check("P1 %s %s → %s" % (tbl, json.dumps(row, ensure_ascii=False), want),
              got == want, "got=%s" % got)

    # 归属解析（真库只读副本上的 users 表）
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    uid, nm = ls._resolve_author(conn, {"owner": "wang"})
    check("P2 owner=wang 解析到真实用户 id（不再恒 0）", uid > 0 and nm, "id=%s nm=%s" % (uid, nm))
    uid2, nm2 = ls._resolve_author(conn, {"owner": ""})
    check("P3 无 owner 时保持 0/「本地创建」，不编造归属",
          uid2 == 0 and nm2 == ls.AUTHOR_NAME, "id=%s nm=%s" % (uid2, nm2))
    uid3, _ = ls._resolve_author(conn, {"owner": "不存在的用户-xyz"})
    check("P4 解析不到时回落 0（不挂到别人名下）", uid3 == 0, "id=%s" % uid3)
    conn.close()


# ────────────────────────── R 真库副本端到端 ──────────────────────────
def sec_e2e():
    section("R 真库副本端到端：新建技能 → 同步 → 状态与归属")
    tmpdb = os.path.join(tempfile.gettempdir(), "_gate_skill_state_%d.db" % os.getpid())
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(tmpdb)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()

    conn = sqlite3.connect(tmpdb)
    conn.row_factory = sqlite3.Row
    try:
        from plugin_system import legacy_sync as ls
        name = "门禁用例技能-STATE-%d" % os.getpid()
        cur = conn.execute(
            "INSERT INTO skills (name, description, skill_type, triggers, category, content, "
            "frontmatter, package_path, status, version, owner, scope, enabled) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, "门禁用例", "text2json", "[]", "AI建模", "正文", "{}", "",
             "draft", "v1.0", "wang", "private", 1))
        rid = cur.lastrowid
        conn.commit()

        ok, msg = ls.sync_from_legacy(conn, "skills", rid)
        # 用官方反查函数，而不是 sqlite json_extract 直比字符串：
        # manifest 里 legacy_id 存的是**数字**，json_extract 返回 int，与 str(rid) 永不相等
        # —— 首轮门禁就栽在这里（自证：插件其实已建成，只是断言取错了行）。
        pid = ls.find_plugin_by_legacy(conn, "skills", rid)
        row = conn.execute("SELECT * FROM plugins WHERE plugin_id=?", (pid,)).fetchone()
        check("R1 新建草稿技能同步后 plugin.status=draft（不是 disabled）",
              bool(row) and row["status"] == "draft",
              "ok=%s msg=%s pid=%s status=%s" % (ok, msg, pid, row["status"] if row else None))
        check("R2 归属落到真实用户（author_id>0，不再恒 0）",
              bool(row) and int(row["author_id"] or 0) > 0,
              "author_id=%s" % (row["author_id"] if row else None))

        # 发布 → published；关 enabled → disabled（两态必须可区分）
        conn.execute("UPDATE skills SET status='published' WHERE id=?", (rid,))
        conn.commit()
        ls.sync_from_legacy(conn, "skills", rid)
        st1 = conn.execute("SELECT status FROM plugins WHERE plugin_id=?", (pid,)).fetchone()[0]
        conn.execute("UPDATE skills SET enabled=0 WHERE id=?", (rid,))
        conn.commit()
        ls.sync_from_legacy(conn, "skills", rid)
        st2 = conn.execute("SELECT status FROM plugins WHERE plugin_id=?", (pid,)).fetchone()[0]
        check("R3 已发布 → published；关掉 enabled → disabled（两态不混）",
              st1 == "published" and st2 == "disabled", "st1=%s st2=%s" % (st1, st2))

        # 草稿族的用户可见文案必须与「已停用」分开（用户报的正是"显示已停用"）
        check("R3b 草稿与已停用是**两个不同的用户文案**（草稿 ≠ 已停用）",
              _label("draft") == "草稿" and _label("disabled") == "已停用",
              "draft=%s disabled=%s" % (_label("draft"), _label("disabled")))

        # consumable 单一判据
        from plugin_system.store.queries import plugin_dto
        d = plugin_dto(dict(conn.execute("SELECT * FROM plugins WHERE plugin_id=?",
                                         (pid,)).fetchone()),
                       {"id": 1, "role_name": "admin"})
        check("R4 disabled 时 consumable=False（AI 不会消费它）", d["consumable"] is False)
        check("R5 disabled 行 status_label=已停用，与 draft 的草稿文案不撞词",
              d["status_label"] == "已停用", "label=%s" % d.get("status_label"))
    finally:
        conn.close()
        try:
            os.remove(tmpdb)
        except Exception:
            pass


# ────────────────────────── F 前端契约 ──────────────────────────
def sec_front():
    section("F 前端契约（射程限定函数体）")
    s = read("static/js/mods/28-studio.js")
    body = func_body(s, "loadSkillCombined")
    check("F1 技能卡的编辑/启停/删除不再调用 MCP 函数（会打到 mcp_servers）",
          body and all(k not in body for k in ("editMCP(", "toggleMCPStatus(", "deleteMCP(")),
          "命中=%s" % [k for k in ("editMCP(", "toggleMCPStatus(", "deleteMCP(") if k in body])
    check("F2 技能卡改走插件轨（capEdit / setSkillPluginEnabled / deleteSkillPlugin）",
          body and "capEdit(" in body and "setSkillPluginEnabled(" in body,
          "")
    check("F3 技能卡的个人启停受发布态约束（草稿不再挂「停用」）",
          "const _pubd = (it.status === 'published');" in body and "${_pubd ?" in body, "")

    c = read("static/js/mods/36-capability.js")
    card = func_body(c, "capCard")
    check("F4 能力中心个人启停只在已发布时渲染",
          "const published = (x.status === 'published');" in card
          and "if (published) {" in card, "")
    check("F5 发布态与个人态措辞分离（不再都用「已启用/已停用」）",
          "_capStatusText" in c and "个人已启用" in card and "个人已停用" in card, "")
    check("F6 卡片给出综合结论 consumable（AI 可用/不可用）",
          "x.consumable ? 'AI 可用'" in card, "")

    a = read("static/js/mods/30-agents.js")
    of = func_body(a, "openSkillForm")
    check("F7 openSkillForm 支持带 id 打开编辑（草稿箱「查看」不再变新建）",
          "function openSkillForm(id)" in a and "editSkill(Number(id))" in of, "")


# ────────────────────────── M 变异自证 ──────────────────────────
def sec_mutation():
    section("M 变异自证：把 derive_status 改回二元折叠，判据必须翻红")
    path = os.path.join(ROOT, "plugin_system/legacy_sync.py")
    orig = open(path, "rb").read()
    mutated = orig.decode("utf-8").replace(
        'if _st in ("draft", "draft_revision"):',
        'if False:  # MUTANT').encode("utf-8")
    if mutated == orig:
        check("M0 变异注入点存在", False, "锚点未命中，变异无效")
        return
    try:
        open(path, "wb").write(mutated)
        import importlib
        from plugin_system import legacy_sync as ls
        importlib.reload(ls)
        got = ls.derive_status("skills", {"status": "draft", "enabled": 1})
        check("M1 折叠回 disabled 后，P1 判据会翻红（说明断言不是空转）",
              got == "disabled", "got=%s（若仍为 draft，说明 derive_status 没被改到）" % got)
    finally:
        open(path, "wb").write(orig)
        import importlib
        from plugin_system import legacy_sync as ls
        importlib.reload(ls)
        back = ls.derive_status("skills", {"status": "draft", "enabled": 1})
        check("M2 还原后行为复位（逐字节写回原文件）", back == "draft",
              "got=%s" % back)


def main():
    print("门禁：技能状态与操作项闭环（SKILL_STATE_ACTIONS_V1）")
    sec_static()
    sec_pure()
    sec_e2e()
    sec_front()
    sec_mutation()
    print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：" + " / ".join(FAILS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
