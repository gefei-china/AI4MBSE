# -*- coding: utf-8 -*-
"""把「智源MCP工具手册」技能包注册进 `skills` 表（幂等）。

## 为什么用脚本而不是手工 INSERT
技能包有 6 个字段必须与磁盘一致（`content` / `frontmatter` / `references` /
`scripts` / `allowed_tools` / `package_path`）。手工维护必然漂移，而漂移的表现是
"技能页显示的内容与包里不一致"——没人会主动去核对。

## 幂等口径
按 `name` 找**内置行**（builtin=1、source!='ai_improve'）：有则 UPDATE，无则 INSERT。
不碰 AI 草稿 / 用户自建的同名技能（与 `register_skills.py` 同一纪律）。

## 用法
    ./.venv/Scripts/python.exe -X utf8 tools/register_zhiyuan_mcp_skill.py
    ./.venv/Scripts/python.exe -X utf8 tools/register_zhiyuan_mcp_skill.py --dry
"""
import argparse
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PKG_NAME = "zhiyuan_mcp_tools"
SKILL_NAME = "智源MCP工具手册"

#: 技能命中时允许注入的工具（`skills.allowed_tools`）。
#: **刻意只列核心常用集**（不是 71 个全列）：
#: 该列表会进入工具候选（`_sk_wl`），全列等于给每轮塞 71 个工具契约 ⇒ 撑爆上下文。
#: 未列入的工具仍可通过 MCP 绑定 / 语义命中注入，只是不因本技能"保底"出现。
#: 选取口径：`references/任务配方.md` 里端到端链路真正用到的工具。
ALLOWED = [
    # 坐标与导航（几乎所有任务的前置）
    "query_projects", "query_branches", "query_model_versions", "query_data_tree",
    "search_model_elements", "get_model_elements", "get_modeling_capabilities",
    # 导出 / 导入
    "export_sysml_v2", "import_sysml_text",
    # 变更执行
    "preview_model_changes", "apply_model_changes", "get_model_operation",
    # 元素增删改（高频）
    "create_model_elements", "update_model_elements", "delete_model_elements",
    "move_model_elements", "update_model_properties", "set_model_expression",
    # 关系
    "create_model_relationships", "get_model_relationships",
    # 视图
    "get_model_diagram_guide", "create_model_view", "get_model_view_guidance",
    "present_model_elements", "layout_model_view",
    # 校验
    "validate_sysml_text", "validate_model", "get_model_validation_rules",
    # 删除影响（破坏性操作前置）
    "get_model_delete_impact",
]


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _parse_frontmatter(md):
    """取 `---\\n...\\n---` 段与正文；返回 (fm_dict, body, fm_raw)。"""
    m = re.match(r"^---\s*\n([\s\S]*?)\n---\s*\n", md)
    if not m:
        return {}, md, ""
    # ⚠️ `frontmatter` 字段要的是**完整块**（含首尾 `---`），不是块内文本：
    #   既有内置技能（如 id=95）的 frontmatter 以 `---` 开头、以 `---` 结尾。
    #   只取 `m.group(1)` 会让 `verify_skill_frontmatter` 的「与 content 的 YAML 头同源」
    #   判红（实测），且注入侧按"含分隔符"解析会读不到任何键。
    raw = md[:m.end()].rstrip("\n")
    body = md[m.end():]
    fm = {}
    for ln in raw.splitlines():
        if ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        fm[k.strip()] = v.strip()
    return fm, body, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印将写入什么")
    a = ap.parse_args()

    pkg_dir = os.path.join(ROOT, "static", "skill_packages", PKG_NAME)
    skill_md_p = os.path.join(pkg_dir, "skill.md")
    if not os.path.exists(skill_md_p):
        print("❌ 缺 %s —— 先跑 tools/gen_zhiyuan_mcp_skill.py" % skill_md_p)
        return 1

    md = _read(skill_md_p)
    fm, body, fm_raw = _parse_frontmatter(md)
    name = fm.get("name") or SKILL_NAME
    desc = fm.get("description") or ""
    version = fm.get("version") or "v1.0"
    category = fm.get("category") or "平台集成"

    # triggers 形如 `[a, b, c]`：按逗号拆（不 eval，避免执行任意内容）
    trig_raw = fm.get("triggers") or "[]"
    trig_raw = trig_raw.strip().lstrip("[").rstrip("]")
    triggers = [t.strip().strip("'\"") for t in trig_raw.split(",") if t.strip()]

    # references / scripts：按磁盘实际文件生成（不手写，防"文件没了 manifest 还写着"）
    refs = []
    rd = os.path.join(pkg_dir, "references")
    if os.path.isdir(rd):
        for fn in sorted(os.listdir(rd)):
            if fn.endswith((".md", ".txt")):
                refs.append({"title": os.path.splitext(fn)[0],
                             "path": "references/" + fn})
    scs = []
    sd = os.path.join(pkg_dir, "scripts")
    if os.path.isdir(sd):
        for fn in sorted(os.listdir(sd)):
            if fn.endswith(".py"):
                scs.append("scripts/" + fn)

    # 包内文件自检（生成器漏了就报错，不要注册一个"指向不存在文件"的技能）
    for rel in [r["path"] for r in refs] + scs:
        if not os.path.exists(os.path.join(pkg_dir, rel)):
            print("❌ 包内缺文件：%s" % rel)
            return 1

    print("技能名      : %s" % name)
    print("包目录      : %s" % os.path.relpath(pkg_dir, ROOT))
    print("skill.md    : %d 字（frontmatter %d 字 + 正文 %d 字）"
          % (len(md), len(fm_raw), len(body)))
    print("triggers    : %d 个" % len(triggers))
    print("references  : %d 个 —— %s" % (len(refs), ", ".join(r["path"] for r in refs)))
    print("scripts     : %d 个 —— %s" % (len(scs), ", ".join(scs)))
    print("allowed     : %d 个工具（核心常用集，非全部 71）" % len(ALLOWED))

    if a.dry:
        print("\n[--dry] 未写库")
        return 0

    from database import db_conn
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id FROM skills WHERE name=? AND COALESCE(builtin,0)=1 "
            "AND COALESCE(source,'')!='ai_improve' AND COALESCE(status,'')!='draft_revision' "
            "ORDER BY id LIMIT 1", (name,)).fetchone()
        # ⚠️ 字段口径（照既有内置技能，本轮踩过）：
        #   `content`     = **全文**（YAML 头 + 正文）—— 注入侧读它
        #   `frontmatter` = **仅 YAML 头**（几百字）—— 注入侧只取前 400 字做摘要
        # 第一版把两者传反（content=正文、frontmatter=全文）⇒ frontmatter 变成 3518 字
        # ⇒ `verify_skill_frontmatter` 的「≤ 注入上限」当场判红（门禁救了一次）。
        payload = (desc, "package", json.dumps(triggers, ensure_ascii=False), category,
                   md, fm_raw, version, json.dumps(ALLOWED, ensure_ascii=False),
                   json.dumps(refs, ensure_ascii=False), json.dumps(scs, ensure_ascii=False),
                   pkg_dir)
        if row:
            conn.execute(
                "UPDATE skills SET description=?, skill_type=?, triggers=?, category=?, "
                "content=?, frontmatter=?, version=?, status='published', enabled=1, "
                "source='manual', allowed_tools=?, `references`=?, scripts=?, package_path=?, "
                "builtin=1, scope='public', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                payload + (row["id"],))
            print("\n已更新：%s (id=%s)" % (name, row["id"]))
        else:
            any_row = conn.execute("SELECT id FROM skills WHERE name=? LIMIT 1",
                                   (name,)).fetchone()
            if any_row:
                print("\n⚠️ 已存在同名非内置技能 (id=%s)，**不覆盖**（避免动到用户/AI 技能）"
                      % any_row["id"])
                return 2
            conn.execute(
                "INSERT INTO skills (name, description, skill_type, triggers, category, "
                "content, frontmatter, version, status, enabled, source, allowed_tools, "
                "`references`, scripts, package_path, builtin, scope) "
                "VALUES (?,?,?,?,?,?,?,?,'published',1,'manual',?,?,?,?,1,'public')",
                # ⚠️ INSERT 的列序以 **name 开头**（12 个占位符），而 payload 是按
                #    UPDATE 的列序排的（11 项，不含 name）⇒ 必须前置补上 name。
                #    第一版直接把 payload 传进去 ⇒ "uses 12, supplied 11"（实测）。
                (name,) + payload)
            print("\n已创建：%s" % name)
        conn.commit()

        r2 = conn.execute("SELECT id,name,version,status,enabled,builtin,"
                          "length(content) cl,json_array_length(triggers) tw,"
                          "json_array_length(allowed_tools) aw FROM skills WHERE name=?",
                          (name,)).fetchone()
        print("库内现状：id=%s version=%s status=%s enabled=%s builtin=%s "
              "content=%s 字 / triggers=%s / allowed_tools=%s"
              % (r2["id"], r2["version"], r2["status"], r2["enabled"], r2["builtin"],
                 r2["cl"], r2["tw"], r2["aw"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
