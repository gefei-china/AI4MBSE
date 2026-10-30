# -*- coding: utf-8 -*-
"""按意图定位智源 MCP 工具（"我要做 X，用哪个工具？"）。

## 为什么需要这个脚本
`zhiyuan-dev-mcp` 有 71 个工具，不可能全塞进上下文。真正需要的是
**按意图定位到那 1~3 个工具**，再只看它们的契约。

## 两段式检索（这是本脚本可信的全部原因）

**第一段：确定映射（ALIAS）** —— 人工确认过的「说法 → 工具」。
命中就**直给**，并标注 `[确定]`。这些映射来自 `references/任务配方.md` 与
skill.md 的"按任务找工具"表，是**审过的**，不是猜的。

**第二段：模糊匹配** —— 没命中 ALIAS 时按名字/描述打分，结果标注 `[模糊]`，
并提示"请核对 top3"。**绝不假装模糊结果和确定结果一样可靠** ——
早先版本用纯打分给确定答案，实测 24 条常见说法里有 7 条 top1 是错的，
而输出看起来同样"言之凿凿"（最坏的失败形态：错得很自信）。

## 为什么中文查英文工具名必须要有桥接表
服务端描述未必含用户的说法：`import_sysml_text` 的描述是
"新增文本包或覆盖明确目标包"，**一个字都没有"导入"** ——
纯文本检索永远排不到它。`INTENT_LEX` 把"用户怎么说话"桥到"工具怎么命名"。

## 数据源
技能包**自包含**优先（`references/工具目录.md`，生成时落盘）；
缺失时回落活库 `mcp_servers.tool_meta`（需要能 import 仓库模块）。

## 用法
    python scripts/lookup.py 创建包
    python scripts/lookup.py 导入源码
    python scripts/lookup.py --list            # 只列全部工具名
    python scripts/lookup.py --selftest        # 检索质量自证（三段）
"""
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _resolve_pkg():
    """定位技能包目录（本脚本有两种运行位置，必须都支持）：

      ① 包内：`<pkg>/scripts/lookup.py`  ⇒ 上级目录就是包（`../references` 存在）
      ② 源头：`tools/zhiyuan_mcp_lookup.py`（入 git 的那份）
         ⇒ 包在 `<repo>/static/skill_packages/zhiyuan_mcp_tools`

    为什么不写死：这份脚本**同时**是包内可执行文件与仓库里的源码（gen 会复制过去）。
    写死任一侧，另一侧就跑不起来 —— 而"跑不起来"的报错是"读不到元数据"，
    很容易被误判成"包被删了"。
    """
    parent = os.path.dirname(_HERE)
    if os.path.isdir(os.path.join(parent, "references")):
        return parent
    root = _HERE
    for _ in range(5):
        cand = os.path.join(root, "static", "skill_packages", "zhiyuan_mcp_tools")
        if os.path.isdir(os.path.join(cand, "references")):
            return cand
        root = os.path.dirname(root)
    return parent          # 都没找到：返回上级，后续按"缺 references"报错


_PKG = _resolve_pkg()

#: **确定映射**：人工确认的「说法 → 工具」。一条说法可给多个工具（顺序 = 建议调用顺序）。
#: 维护原则：只放**已经能说清"为什么是它"**的映射；拿不准的别放，让第二段去模糊匹配。
ALIAS = {
    # ── 坐标与导航 ──
    "工程列表": ["query_projects"],
    "查工程": ["query_projects"],
    "找工程": ["query_projects"],
    "projectId": ["query_projects"],
    "工程坐标": ["query_projects", "query_branches", "query_model_versions"],
    "三元组": ["query_projects", "query_branches", "query_model_versions"],
    "分支": ["query_branches"],
    "版本": ["query_model_versions"],
    "模型版本": ["query_model_versions"],
    "草稿版本": ["query_model_versions"],
    "包树": ["query_data_tree"],
    "数据树": ["query_data_tree"],
    "包结构": ["query_data_tree"],
    "看包": ["query_data_tree"],
    "找元素": ["search_model_elements"],
    "按名字找": ["search_model_elements"],
    "搜索元素": ["search_model_elements"],
    "元素详情": ["get_model_elements"],
    "长文本": ["get_model_text"],
    "关联": ["get_model_associations"],
    "关系查询": ["get_model_relationships"],
    "候选": ["query_reference_candidates"],
    "可建什么": ["get_modeling_capabilities"],
    "编辑资格": ["get_modeling_context"],

    # ── 导出 / 导入 ──
    "导出": ["export_sysml_v2"],
    "导出源码": ["export_sysml_v2"],
    "拉源码": ["export_sysml_v2"],
    "导入": ["import_sysml_text"],
    "导入源码": ["import_sysml_text"],
    "写回": ["import_sysml_text"],
    "写回源码": ["import_sysml_text"],
    "写入平台": ["import_sysml_text"],
    "覆盖包": ["import_sysml_text"],
    "命名空间导入": ["manage_model_imports"],

    # ── 变更执行 ──
    "预览变更": ["preview_model_changes"],
    "改模型": ["preview_model_changes", "apply_model_changes"],
    "提交变更": ["apply_model_changes"],
    "操作状态": ["get_model_operation"],
    "撤销": ["undo_model_operation"],
    "回滚": ["undo_model_operation"],

    # ── 元素增删改 ──
    "创建包": ["create_model_elements"],
    "新建包": ["create_model_elements"],
    "建包": ["create_model_elements"],
    "新增元素": ["create_model_elements"],
    "新建元素": ["create_model_elements"],
    "创建元素": ["create_model_elements"],
    "改名": ["update_model_elements"],
    "改说明": ["update_model_elements"],
    "删除元素": ["delete_model_elements"],
    "删除包": ["get_model_delete_impact", "delete_model_elements"],
    "级联删除": ["get_model_delete_impact", "delete_model_elements"],
    "移动": ["move_model_elements"],
    "移动包": ["move_model_elements"],
    "复制包": ["clone_model_elements"],
    "克隆": ["clone_model_elements"],
    "可见性": ["update_model_properties"],
    "多重性": ["update_model_properties"],
    "改属性": ["update_model_properties"],
    "自定义属性": ["get_model_custom_properties", "update_model_custom_properties"],
    "属性值": ["set_feature_value"],
    "表达式": ["set_model_expression", "get_model_expression"],
    "设置表达式": ["set_model_expression"],
    "泛化": ["set_specializations"],
    "继承": ["set_specializations"],
    "数据类型": ["set_model_attribute_declaration"],
    "单位": ["set_model_attribute_declaration"],
    "类型关系": ["set_model_type_relations"],

    # ── 关系 ──
    "创建关系": ["create_model_relationships"],
    "建连线": ["create_model_relationships"],
    "连线": ["create_model_relationships"],
    "删除关系": ["delete_model_relationships"],
    "关系强度": ["update_model_relationship_properties"],

    # ── 视图 ──
    "建视图": ["get_model_diagram_guide", "create_model_view"],
    "创建视图": ["create_model_view"],
    "画图指引": ["get_model_diagram_guide"],
    "图种": ["get_model_diagram_guide"],
    "视图工具": ["get_model_view_guidance"],
    "放元素进视图": ["present_model_elements"],
    "呈现": ["present_model_elements"],
    "画关系线": ["present_model_relationship"],
    "加图元": ["create_model_view_element"],
    "布局": ["layout_model_view"],
    "自动排版": ["layout_model_view"],
    "样式": ["update_view_styles"],
    "改坐标": ["update_view_presentations"],
    "移除图元": ["remove_view_presentations"],
    "表格": ["query_model_table"],
    "矩阵": ["query_model_table"],
    "泳道": ["place_model_activity_nodes"],

    # ── 状态机 ──
    "状态机": ["get_state_machine_semantics"],
    "转移": ["configure_state_transition"],
    "trigger": ["configure_state_transition"],
    "guard": ["configure_state_transition"],
    "状态动作": ["configure_state_actions"],

    # ── 校验 ──
    "语法检查": ["validate_sysml_text"],
    "语法校验": ["validate_sysml_text"],
    "校验语法": ["validate_sysml_text"],
    "工程内校验": ["validate_model"],
    "语义校验": ["validate_model"],
    "校验模型": ["validate_model"],
    "校验规则": ["get_model_validation_rules"],
    "规则清单": ["get_model_validation_rules"],

    # ── 注释 ──
    "加注释": ["annotate_model_elements"],
    "加评论": ["annotate_model_elements"],
    "改注释": ["update_model_annotation"],
    "看注释": ["get_model_annotations"],

    # ── 自动化 / 变体 ──
    "自动化": ["get_model_automation_options", "run_modeling_automation"],
    "自动化配置": ["get_model_automation_options"],
    "跑自动化": ["run_modeling_automation"],
    "变体": ["manage_model_variants"],
    "删除影响": ["get_model_delete_impact"],
}

#: 中文意图词 → (英文工具名片段, **是否泛词**)。
#: 泛词（出现在 6+ 个工具名/描述里的"工程/元素/版本/视图"）权重减半 ——
#: 它们是**噪声源**：查"工程里有什么不符合规则的"时，"工程"会把 `query_projects`
#: 顶到第一，而真实意图是 `validate_model`（实测）。
INTENT_LEX = {
    # 校验族（含口语：不符合/违规/报错）
    "校验": ("validate", 0), "验证": ("validate", 0), "检查": ("validate", 0),
    "不符合": ("validate", 0), "违规": ("validate", 0), "报错": ("validate", 0),
    "规则": ("validation_rules", 0),
    # 查询族
    "查询": ("query", "get"), "查": ("query", "get"), "找": ("search", "query"),
    "搜索": ("search",), "检索": ("search",), "列表": ("query", "list"),
    "都有什么": ("data_tree", "get_model_elements"), "有哪些": ("data_tree", "query"),
    "包里": ("data_tree",), "这层": ("data_tree",),
    # 结构性对象（泛词权重减半）
    "工程": ("project", 1), "分支": ("branch", 0), "版本": ("version", 1),
    "包树": ("data_tree",), "数据树": ("data_tree",),
    # 写族（含口语：塞进/写进）
    "创建": ("create",), "新建": ("create",), "新增": ("create",),
    "删除": ("delete",), "移除": ("remove", "delete"),
    "修改": ("update",), "更新": ("update",), "设置": ("set",), "配置": ("configure",),
    "导入": ("import",), "写回": ("import",), "导出": ("export",),
    "塞进": ("import",), "写进": ("import",), "上载": ("import",),
    # 视图族
    "视图": ("view", 1), "图元": ("present", "view_element"), "布局": ("layout",),
    "重排": ("layout",), "排布": ("layout",), "画布": ("view", 0),
    # 关系 / 移动
    "关系": ("relationship",), "连线": ("relationship",),
    "移动": ("move",), "挪": ("move",), "搬": ("move",),
    "复制": ("clone",), "克隆": ("clone",),
    # 其它
    "注释": ("annotation", "annotate"), "评论": ("annotation", "annotate"),
    "自动化": ("automation",), "变体": ("variant",),
    "属性": ("propert", 1), "特征": ("feature",), "表达式": ("expression",),
    "状态": ("state", 1), "预览": ("preview",), "撤销": ("undo",),
}

#: 泛化测试：这些说法**故意不写进 ALIAS**，用来验证第二段在"没背过"的措辞上
#: 仍能把正确工具排进 top3。要求 top3 而不是 top1 —— 模糊匹配能给到候选即可；
#: 强行要求 top1 只会诱使继续调权重（本文件历史教训：越调越乱且看不出来）。
GENERALIZE = [
    ("把源码塞进平台", "import_sysml_text"),
    ("看看这个工程下面有哪些包", "query_data_tree"),
    ("我要把这个包挪到别的地方", "move_model_elements"),
    ("检查一下这段代码语法", "validate_sysml_text"),
    ("工程里有什么不符合规则的地方", "validate_model"),
    ("重排一下画布的节点位置", "layout_model_view"),
    ("给我这个元素的评论内容", "get_model_annotations"),
    ("这个包里都有什么元素", "query_data_tree"),
]


def _norm(s):
    """归一化：去空白/下划线/连字符、转小写。让"导入源码"与"导入 源码"等价。"""
    return re.sub(r"[\s_\-]+", "", (s or "").lower())


def _lex_spec(spec):
    """把 `INTENT_LEX` 的值规范成 `(frags_tuple, is_generic)`。

    两种写法都允许：
      `("validate",)` / `("query", "get")`        —— 纯片段
      `("project", 1)` / `("view", 1)`            —— 末位 int = 是否泛词
    第一版直接把 `spec` 当片段元组用 ⇒ `("validate", 0)` 里的 `0` 也被当成片段
    去做 `if f in name_l` ⇒ `TypeError: 'in <string>' requires string`（实测）。
    规范化只做一次，打分循环里不再做类型判断。
    """
    if isinstance(spec, str):
        return (spec,), False
    items = list(spec or ())
    generic = False
    if items and isinstance(items[-1], int):
        generic = bool(items.pop())
    return tuple(x for x in items if isinstance(x, str)), generic


def _from_markdown():
    p = os.path.join(_PKG, "references", "工具目录.md")
    try:
        text = open(p, encoding="utf-8").read()
    except Exception:
        return None
    out = {}
    blocks = re.split(r"\n### `([a-z0-9_]+)`\n", text)
    for i in range(1, len(blocks) - 1, 2):
        name, body = blocks[i], blocks[i + 1].strip()
        out[name] = {"name": name, "description": body.split("\n\n")[0].strip(),
                     "_md": body}
    return out or None


def _from_db():
    root = _PKG
    for _ in range(6):
        root = os.path.dirname(root)
        if os.path.exists(os.path.join(root, "database.py")):
            break
    else:
        return None
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from database import db_conn
        with db_conn() as conn:
            row = conn.execute(
                "SELECT tool_meta FROM mcp_servers WHERE name='zhiyuan-dev-mcp' "
                "ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        meta = json.loads(row["tool_meta"] or "[]")
        d = {x["name"]: x for x in meta if isinstance(x, dict) and x.get("name")}
        return d or None
    except Exception:
        return None


def match_alias(q, tools):
    """第一段：确定映射。**最长说法优先**（"导出源码" 应压过 "导出"）。"""
    qn = _norm(q)
    hits = []
    for phrase, names in ALIAS.items():
        pn = _norm(phrase)
        if pn and pn in qn:
            hits.append((len(pn), phrase, names))
    if not hits:
        return []
    hits.sort(key=lambda x: -x[0])
    _, phrase, names = hits[0]      # 只取最长那条，不叠加更短同义词
    return [(n, phrase) for n in names if n in tools]


def fuzzy(q, tools, topn=6):
    """第二段：模糊匹配（只给候选，调用方必须标注"模糊"）。"""
    ql = q.lower()
    big = set(re.findall(r"[a-z0-9_]{2,}", ql))
    for seg in re.findall(r"[\u4e00-\u9fff]+", ql):
        for i in range(len(seg) - 1):
            big.add(seg[i:i + 2])
        if len(seg) <= 2:
            big.add(seg)
    uni = set(re.findall(r"[\u4e00-\u9fff]", ql))
    res = []
    for nm, x in tools.items():
        name_l = nm.lower()
        name_sp = name_l.replace("_", " ")
        desc = (x.get("description") or "").lower()
        props = " ".join(((x.get("inputSchema") or {}).get("properties") or {}).keys())
        w = 0
        for kw, spec in sorted(INTENT_LEX.items(), key=lambda kv: -len(kv[0])):
            if kw not in q:
                continue
            frags, generic = _lex_spec(spec)
            for f in frags:
                if f in name_l:
                    w += (6 if generic else 12) * len(kw)
                    break
        for t in big:
            if t in name_sp:
                w += 10
            elif t in desc:
                w += 5
        w += min(sum(1 for t in uni if t in desc + props), 4)
        if w:
            res.append((w, nm, x))
    # 平局打破：名字更短优先（短名通常是该概念的主工具）
    res.sort(key=lambda r: (-r[0], len(r[1]), r[1]))
    return res[:topn]


def show(nm, x):
    print("─" * 74)
    print("★ %s" % nm)
    sch = x.get("inputSchema")
    if isinstance(sch, dict):
        print("  %s" % ((x.get("description") or "").replace("\n", " ")))
        props = sch.get("properties") or {}
        req = list(sch.get("required") or [])
        print("  必填：%s" % (", ".join(req) if req else "无"))
        for k in sorted(props, key=lambda z: (z not in req, z)):
            p = props[k] or {}
            t = p.get("type") or "?"
            if p.get("enum"):
                t += "：" + " / ".join(str(e) for e in p["enum"])
            if t == "object" and (p.get("properties") or {}):
                t = "object{%s}" % ", ".join(sorted(p["properties"].keys()))
            d = (p.get("description") or "").replace("\n", " ")
            print("    %s %-22s %-30s %s"
                  % ("✓" if k in req else " ", "`%s`" % k, t[:30], d[:64]))
        return
    _md = x.get("_md") or ""
    parts = _md.split("\n\n")
    if parts and not parts[0].lstrip().startswith(("**必填**", "|")):
        print("  %s" % parts[0].replace("\n", " ").strip())
        _md = "\n\n".join(parts[1:])
    for ln in _md.splitlines():
        if ln.startswith("### "):
            continue
        print("  " + ln if ln.strip() else "")


def selftest(tools):
    """三段自证：ALIAS 自洽 / 泛化 top3 / 反例不误命中。"""
    print("=== 一、确定映射（ALIAS 自洽：每条 key 作为查询应命中自己的工具）===")
    bad = []
    for phrase, names in sorted(ALIAS.items()):
        want = names[0]
        hits = match_alias(phrase, tools)
        got = hits[0][0] if hits else "(未命中)"
        if got != want:
            bad.append((phrase, want, got))
    for phrase, want, got in bad:
        print("  [FAIL] %-16s 期望 %-32s 实得 %s" % (phrase, want, got))
    print("  %d/%d 通过" % (len(ALIAS) - len(bad), len(ALIAS)))
    print("  ⚠️ 这一组只证明表**自洽**（表里写的→查得到），不证明泛化能力。")

    print("\n=== 二、泛化（说法不在 ALIAS 里，要求正确工具进 top3）===")
    bad2 = []
    for q, want in GENERALIZE:
        res = fuzzy(q, tools, topn=3)
        names3 = [n for _, n, _ in res]
        ok = want in names3
        print("  [%s] %-26s top3=%s" % ("PASS" if ok else "FAIL", q,
                                        ", ".join(names3) or "(无命中)"))
        if not ok:
            bad2.append((q, want, names3))

    print("\n=== 三、反例（无意义输入不得假装命中确定映射）===")
    neg = match_alias("随便聊聊天气", tools)
    neg_ok = not neg
    print("  [%s] '随便聊聊天气' → ALIAS 命中 %d 条（应为 0）"
          % ("PASS" if neg_ok else "FAIL", len(neg)))

    fail = len(bad) + len(bad2) + (0 if neg_ok else 1)
    print("\n合计 %d 项失败" % fail)
    return 1 if fail else 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    tools = _from_markdown() or _from_db()
    if not tools:
        print("❌ 读不到工具元数据。先跑 scripts/dump_tools.py 生成 references。")
        return 1

    arg = sys.argv[1]
    if arg == "--selftest":
        return selftest(tools)
    if arg == "--list":
        print("共 %d 个工具：" % len(tools))
        for n in sorted(tools):
            print("  %-34s %s" % (n, (tools[n].get("description") or "")[:50]))
        return 0

    q = " ".join(sys.argv[1:])
    print("查询：%r   （工具总数 %d）" % (q, len(tools)))

    hits = match_alias(q, tools)
    if hits:
        print("\n【确定】命中说法：%r" % hits[0][1])
        for nm, _ph in hits:
            print()
            show(nm, tools[nm])
        return 0

    res = fuzzy(q, tools, topn=6)
    if not res:
        print("\n没有命中。看 `--list` 或 references/任务配方.md。")
        return 0
    print("\n【模糊】未命中确定映射 —— **下面是按名字/描述猜的候选，请核对再调用**：")
    for w, nm, x in res[:4]:
        print("\n[分 %3d]" % w)
        show(nm, x)
    if len(res) > 4:
        print("\n… 另 %d 个候选：%s" % (len(res) - 4, ", ".join(n for _, n, _ in res[4:])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
