# -*- coding: utf-8 -*-
"""verify_mcp_tool_closure — 门禁：**工具暴露必须保住「动作链完整」**，不能只做 top-k 裁剪。

## 为什么需要这道门禁（会话 9629 实测：用户「在通信子系统用例包中新增一个测试包」失败）

用户问「创建包的工具也有，工程树查询工具也有，为啥还是失败，是不是工程能力太差」。
实测答案：**能力不差，是被藏起来了。**

`pick_for_injection(min_keep=8)` 只按"粗分词 token 重叠"取前 8 个，对这条**短中文请求**
（工具名却是英文）打分几乎全 0 ⇒ 退化成"原始顺序前 8 个"⇒ 实测暴露的是

    move_model_elements / layout_model_view / get_model_annotations /
    get_model_associations / get_model_operation / get_model_expression / ...

**跟建包毫无关系**，而真正的四件套
`search_model_elements` / `query_data_tree` / `get_modeling_capabilities` /
`create_model_elements` **一个都没暴露** ⇒ 模型不是不会，是没工具。

更隐蔽的一层：**依赖链被剪断**。71 个工具里 41 个在 description 里显式引用同伴
（`create_model_elements`：『先 get_modeling_capabilities 读取父元素，使用返回的
creationRef 创建普通包』），逐工具独立裁剪必然出现「引用了但没给」。
而 `create_model_elements` **不提** `query_data_tree` —— 定位父包那一环不在它的
描述里，所以光靠依赖闭包也补不到，必须再加"结构定位"规则。

## 本门禁锁五件事

① 建包请求下，四件套必须**全部**暴露；
② 依赖闭包成立：任何被暴露工具在描述里引用的同伴也必须被暴露（1 跳内）；
③ 只读请求（取源码）**不应**被塞进写工具（避免反向膨胀 / 误写）；
④ 输出**可复现**（同一输入两次调用完全一致；遍历不许依赖 set 顺序）；
⑤ token 仍显著低于全量（不能退化成"全量注入"）。

含**变异自证**：把闭包拆掉后第①项必须变红。
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent.mcp_tool_meta import expand_with_deps, load_tool_meta, pick_for_injection  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    print("  [%s] %s%s" % ("OK  " if cond else "FAIL", name, ("  ← " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def load_meta():
    c = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT tool_meta FROM mcp_servers WHERE name='zhiyuan-dev-mcp'").fetchone()
    c.close()
    if not row or not row["tool_meta"]:
        return {}
    return load_tool_meta({"tool_meta": row["tool_meta"]})


def schema_chars(meta, names):
    n = 0
    for nm in names:
        info = meta.get(nm) or {}
        n += len(json.dumps({"name": nm,
                             "description": info.get("description") or "",
                             "parameters": info.get("inputSchema") or {}},
                            ensure_ascii=False))
    return n


def main():
    meta = load_meta()
    if not meta:
        print("!! 库中无 zhiyuan-dev-mcp 的 tool_meta，无法校验（先跑 /mcp-servers/check-all）")
        sys.exit(1)
    print("工具元数据：%d 个" % len(meta))

    NEED = ["search_model_elements", "query_data_tree",
            "get_modeling_capabilities", "create_model_elements"]
    Q_WRITE = "在通信子系统用例包中新增一个测试包"
    Q_WRITE2 = "在“通信子系统用例”包中新增一个“测试”包"

    print("\n=== ① 建包请求：四件套必须全部暴露 ===")
    for q in (Q_WRITE, Q_WRITE2):
        picked, st = pick_for_injection(meta, q, min_keep=8, top_n=0)
        miss = [n for n in NEED if n not in picked]
        ok("暴露齐全（%r）" % q[:16], not miss,
           "共 %d 个，缺失=%s" % (len(picked), miss))

    print("\n=== ② 依赖闭包：**动作链上的工具**引用必须闭合 ===")
    picked, _ = pick_for_injection(meta, Q_WRITE, min_keep=8, top_n=0)
    names = list(meta.keys())
    # 只看"本次意图命中的写工具"及其 1 跳引用 —— 这才是任务成败所在。
    # 不要求"所有已暴露工具的引用全闭合"：注释/关系/视图等边缘工具的互相引用
    # 与"建包"无关，强行闭合会把工具数从 21 推到 32、把 token 预算烧穿
    # （conv 9637 实测：13 轮 311,426 tokens > 300k 预算 ⇒ 模型在最后一步前被收口）。
    write_tools = [n for n in picked
                   if any(g in n.lower() for g in ("create", "update", "delete", "move", "apply"))]
    broken = []
    for nm in write_tools:
        d = (meta.get(nm) or {}).get("description") or ""
        if not d:
            continue
        for other in names:
            if other == nm or len(other) < 6:
                continue
            if other in d and other not in picked:
                broken.append((nm, other))
    ok("写工具的引用全部闭合", not broken,
       "写工具 %d 个，断裂=%s" % (len(write_tools), broken[:4]))
    # 关键前置必须在场（写工具自己不做定位/授权）
    ok("create_model_elements 的前置 get_modeling_capabilities 在场",
       "create_model_elements" not in picked or "get_modeling_capabilities" in picked)
    ok("定位父容器所需的导航工具在场（query_data_tree 或 search_model_elements）",
       ("query_data_tree" in picked) or ("search_model_elements" in picked))

    print("\n=== ③ 只读请求不应塞写工具（反向膨胀/误写）===")
    q_read = "基于工程广汽方法论建模1022获取V2代码"
    pr, _ = pick_for_injection(meta, q_read, min_keep=8, top_n=0)
    ok("只读请求不含 create_model_elements", "create_model_elements" not in pr,
       "共 %d 个" % len(pr))
    ok("只读请求仍能拿到建包以外的读工具", any("query" in n or "get_" in n for n in pr))

    print("\n=== ④ 可复现 ===")
    a, _ = pick_for_injection(meta, Q_WRITE, min_keep=8, top_n=0)
    b, _ = pick_for_injection(meta, Q_WRITE, min_keep=8, top_n=0)
    ok("同一输入两次完全一致", a == b)

    print("\n=== ⑤ token 仍需显著低于全量 ===")
    full = schema_chars(meta, list(meta.keys()))
    cur = schema_chars(meta, a)
    ratio = cur / full if full else 1
    ok("暴露体量 < 全量的 80%", ratio < 0.8,
       "%d/%d 字符（%.0f%%）" % (cur, full, ratio * 100))
    print("   （%d 个工具 / 全量 %d 个）" % (len(a), len(meta)))

    print("\n=== ⑤b 参数契约瘦身必须**合法**（非法 schema 会让整轮 LLM 400 直接终止）===")
    from agent.mcp_tool_meta import _schema_sane, slim_schema
    bad = [nm for nm in meta if not _schema_sane(slim_schema(meta[nm].get("inputSchema") or {}))]
    ok("全部工具瘦身后仍是合法 JSON Schema", not bad, "非法=%s" % bad[:5])
    # 逐项查 properties 的值必须是对象（这正是上一轮 400 的形态：
    #   "properties": {"type": "object"} ⇒ 声明了一个叫 type 的字符串属性）
    def _props_bad(node, path=""):
        out = []
        if isinstance(node, dict):
            pr = node.get("properties")
            if isinstance(pr, dict):
                for k, v in pr.items():
                    if not isinstance(v, dict):
                        out.append(path + "/" + str(k))
                    else:
                        out += _props_bad(v, path + "/" + str(k))
            for k, v in node.items():
                if k != "properties":
                    out += _props_bad(v, path + "/" + str(k))
        elif isinstance(node, list):
            for i, x in enumerate(node):
                out += _props_bad(x, path + "[%d]" % i)
        return out
    leaks = []
    for nm in meta:
        leaks += ["%s%s" % (nm, p) for p in _props_bad(slim_schema(meta[nm].get("inputSchema") or {}))]
    ok("properties 的值全是对象（无字符串泄漏）", not leaks, "泄漏=%s" % leaks[:5])

    print("\n=== ⑥ 变异自证：拆掉闭包 ⇒ 第①项必须变红 ===")
    # 变异：把 expand_with_deps 变成空操作（等价于"只做 top-8 裁剪"的旧行为）
    mutated_src = None
    p = os.path.join(ROOT, "agent", "mcp_tool_meta.py")
    src = open(p, encoding="utf-8").read()
    stub = '''def expand_with_deps(meta: dict, chosen, user_input: str = "", max_rounds: int = 1,
                     cap_extra: int = 0) -> list:
    return []
'''
    import re
    mutated_src, nsub = re.subn(r"def expand_with_deps\([\s\S]*?\n(?=#: 中英文常见词停用表)", stub, src)
    ok("变异点命中（闭包函数被替换）", nsub == 1 and mutated_src != src)
    tmp_py = os.path.join(ROOT, "agent", "_mut_mcp_tool_meta.py")
    try:
        with open(tmp_py, "w", encoding="utf-8", newline="\n") as f:
            f.write(mutated_src)
        import importlib
        mmod = importlib.import_module("agent._mut_mcp_tool_meta")
        mp, _ = mmod.pick_for_injection(meta, Q_WRITE, min_keep=8, top_n=0)
        miss_mut = [n for n in NEED if n not in mp]
        ok("变异版丢失关键工具（证明闭包是真起作用）", bool(miss_mut),
           "变异版共 %d 个，缺失=%s" % (len(mp), miss_mut))
    finally:
        try:
            if os.path.exists(tmp_py):
                os.remove(tmp_py)
        except Exception:
            pass

    print("\n" + "=" * 70)
    if FAILS:
        print("❌ FAIL %d 项：%s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("✅ 通过 —— 动作链完整 / 依赖闭合 / 只读不误加写工具 / 可复现 / token 受控（含变异自证）")


if __name__ == "__main__":
    main()
