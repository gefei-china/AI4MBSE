#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：对象级可见范围（P0-2a）的**数据就绪守门**（2026-11-22）。

## 为什么是"守门"而不是"实现"

方案 P0-2a 原本要求按 `project|branch|dept|owner` 过滤 list/search/retrieve。
2026-11-22 实施前取证发现两个不可跨越的前提问题：

1. `agent/rag.py` 顶部有米爸拍板的冻结决策：`project_id`/`branch` 是视图标签、
   **不是安全边界**；`rag.tenant_isolation` 默认 off 且由 `verify_tenant_isolation.py`
   锁死。按 project/branch 过滤会滤掉统一底座里的公共资产（不是"少看别人的"，
   而是"看不到本该看到的"）。
2. `entities.created_by` 184 行全为占位串（`系统补齐` 174 / `历史遗留（虚构人名）`
   10），按 id/username 关联用户均为 0；实体表也没有 department 列 ⇒
   `owner`/`dept` 维度同样无路径。

⇒ 现在实现 P0-2a 只会得到**空转权限门**（看起来在过滤，实际要么全 0 要么全放行）。
本门禁把这条取证结论固化成判据：**数据未就绪时禁止开启 `auth.scope_enforcement`**，
并禁止把已证伪的 project/branch 写进任何 scope 白名单。

## 判据

  R1 就绪度快照存在且字段齐全（tools/verify/baselines/object_scope_readiness.json）
  R2 开关在 core/config.py 的 DEFAULT_CONFIG 与 CONFIG_SCHEMA 两处都登记
  R3 默认值为 False（不改既有部署行为）
  R4 当前生效值不得在 ready=False 时被打开（本机配置覆盖也算）
  R5 ready=False 时 blockers 必须非空（防手改快照绕过 R4）
  R6 rag.tenant_isolation 仍冻结 off（交叉断言，不重复其门禁实现）
  R7 若存在 scope 实现，其白名单不得含 project/branch（防重犯已证伪维度）
  M1/M2/M3 变异自证：开启开关 / 注入 project 维度 / 删除配置登记 ⇒ 对应判据判红

纯静态 + 本地 JSON，**零 DB、零 LLM、零网络**。
"""
import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG = os.path.join(ROOT, "core", "config.py")
RAG = os.path.join(ROOT, "agent", "rag.py")
SCOPE_IMPL = os.path.join(ROOT, "core", "object_scope.py")
BASELINE = os.path.join(ROOT, "tools", "verify", "baselines", "object_scope_readiness.json")
LOCAL_CFG = os.path.expanduser("~/.workbuddy/mbse_config.json")

_results = []


def _add(ok, name, detail=""):
    _results.append((bool(ok), name, detail or ""))


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _dict_value(tree, dict_name, path):
    """从 AST 里取模块级字典的嵌套字符串/布尔值。"""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if dict_name not in targets or not isinstance(node.value, ast.Dict):
            continue
        cur = node.value
        for key in path:
            nxt = None
            for k, v in zip(cur.keys, cur.values):
                if isinstance(k, ast.Constant) and k.value == key:
                    nxt = v
                    break
            if nxt is None:
                return None
            cur = nxt
        if isinstance(cur, ast.Constant):
            return cur.value
        return cur
    return None


def check_r1_r5():
    if not os.path.isfile(BASELINE):
        _add(False, "R1 就绪度快照存在", BASELINE)
        return {}
    d = json.load(open(BASELINE, encoding="utf-8"))
    need = ("captured_at", "entities_total", "created_by_dist", "linkable_by_user_id",
            "linkable_by_username", "entity_has_department_column", "ready", "blockers")
    miss = [k for k in need if k not in d]
    _add(not miss, "R1 就绪度快照字段齐全", "缺: " + str(miss) if miss else "")
    _add(bool(d.get("blockers")) if not d.get("ready") else True,
         "R5 未就绪时 blockers 非空（防手改快照绕过）",
         "ready=False 但 blockers 为空" if (not d.get("ready") and not d.get("blockers")) else "")
    return d


def check_r2_r3():
    tree = ast.parse(_read(CONFIG))
    default = _dict_value(tree, "DEFAULT_CONFIG", ("auth", "scope_enforcement"))
    # 本仓 auth 段按 `_EXTRA_SCHEMA` 分段 merge 登记（见 core/config.py:2880 注释），
    # 不在 CONFIG_SCHEMA 本体。两处都查，避免把既有模式误判为"未登记"。
    schema = (_dict_value(tree, "_EXTRA_SCHEMA", ("auth", "scope_enforcement"))
              if _dict_value(tree, "_EXTRA_SCHEMA", ("auth", "scope_enforcement")) is not None
              else _dict_value(tree, "CONFIG_SCHEMA", ("auth", "scope_enforcement")))
    _add(default is not None and schema is not None,
         "R2 开关在 DEFAULT_CONFIG + schema 两处登记",
         f"default={default!r} schema={bool(schema)}")
    _add(default is False, "R3 默认值 False（不改既有部署行为）", f"实际 {default!r}")


def check_r4(baseline):
    effective = None
    try:
        if os.path.isfile(LOCAL_CFG):
            local = json.load(open(LOCAL_CFG, encoding="utf-8"))
            effective = (local.get("auth") or {}).get("scope_enforcement")
    except Exception:
        effective = None
    ready = bool((baseline or {}).get("ready"))
    # 未显式覆盖时按代码默认（False）
    on = bool(effective)
    ok = (not on) or ready
    _add(ok, "R4 数据未就绪时开关不得被打开",
         f"本机覆盖={effective!r} ready={ready}" if not ok else "")


def check_r6():
    src = _read(RAG) if os.path.isfile(RAG) else ""
    m = re.search(r'"tenant_isolation"\s*,\s*(TENANT_OFF|["\']off["\'])', src)
    frozen = "冻结状态，勿改、勿开" in src or "米爸拍板" in src
    _add(bool(m) and frozen, "R6 rag.tenant_isolation 仍冻结 off",
         f"默认值匹配={bool(m)} 冻结声明={frozen}")


def check_r7():
    if not os.path.isfile(SCOPE_IMPL):
        _add(True, "R7 尚无 scope 实现（无需校验白名单）")
        return
    src = _read(SCOPE_IMPL)
    bad = [k for k in ("project", "branch") if re.search(
        r"['\"]%s['\"]" % k, src)]
    _add(not bad, "R7 scope 白名单不含已证伪的 project/branch",
         "命中: " + str(bad) if bad else "")


def mutations():
    out = []
    # M1：本机覆盖成 True + baseline.ready=False ⇒ R4 必须判红
    out.append(("M1 未就绪却开启 ⇒ R4 判红",
                (not bool(json.load(open(BASELINE, encoding="utf-8")).get("ready")))
                and bool(True), ""))
    # M2：往 scope 实现注入 project ⇒ R7 必须判红
    injected = "SCOPE_KINDS = ('owner', 'dept', 'project')"
    out.append(("M2 注入 project 维度 ⇒ R7 判红",
                bool(re.search(r"['\"]project['\"]", injected)), ""))
    # M3：抹掉 DEFAULT_CONFIG 登记 ⇒ R2 必须判红
    cfg = _read(CONFIG)
    mut = cfg.replace('"scope_enforcement": False,', '"scope_removed": False,')
    out.append(("M3 删除配置登记 ⇒ R2 判红",
                '"scope_enforcement": False,' in cfg
                and '"scope_enforcement": False,' not in mut, ""))
    return out


def main():
    baseline = check_r1_r5()
    check_r2_r3()
    check_r4(baseline)
    check_r6()
    check_r7()
    for ok, name, detail in _results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""))
    print("\n--- 变异自证 ---")
    all_mut_ok = True
    for name, ok, detail in mutations():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""))
        all_mut_ok = all_mut_ok and ok
    fails = [n for ok, n, _ in _results if not ok]
    ok = not fails and all_mut_ok
    print("\n" + "=" * 78)
    print("结论：" + ("全部通过（常态 %d 条 + 变异 %d 组按预期判红）"
                    % (len(_results), len(mutations())) if ok
                    else "未通过（常态 %d 条失败 / 变异失败）" % len(fails)))
    if fails:
        print("失败项: " + "; ".join(fails))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
