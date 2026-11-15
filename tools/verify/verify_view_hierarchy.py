# -*- coding: utf-8 -*-
"""视图层级（包嵌套）验证（2026-11-09）。

被验对象：`view_generator._attach_package_hierarchy` + `_finalize` + BDD 节点集

背景（实测）：全库 332 个视图、84 个 BDD/PKG，节点 `parent` 字段**一个都没有**。
渲染层的包框样式（`node:parent`）因此从未生效，层级视图一律画成扁平方块。
根因是投影层把「包包含」当成普通组合连线输出，没提升为父子关系。

判据（哪条最容易做错）：
  · **只有 package 能当父** —— part/block 之间的组合是连线语义，画成嵌套会改变模型含义；
  · **防环** —— A 包 B、B 包 A 的脏数据不能让渲染层无限递归；
  · **可回滚** —— 开关关闭时必须逐字节回到"无 parent"。

运行：<venv>/python.exe -X utf8 tools/verify/verify_view_hierarchy.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.config as cfg  # noqa: E402
from view_generator import _attach_package_hierarchy, generate_views_from_sysml  # noqa: E402

# ★ 2026-11-15：**无依赖 SKIP**（此前 CI 上直接抛 RuntimeError 判红）。
#
# 本门禁 8 个段落全部依赖 `generate_views_from_sysml` → `sysml_ast.parse_strict`
# → OMG 官方参考实现（需 java-runtime + checker.jar + sysml.library）。
# CI runner 没有这些资产 ⇒ 第一次调用就抛
#   `RuntimeError: OMG SysML v2 解析器不可用（缺少 java-runtime / checker.jar / ...）`
# ⇒ 整个门禁红，而红的原因是**环境缺资产**，不是视图层级逻辑有问题。
#
# 用 `sysml_ast.available()`（产品自带的可用性判定，检查 JAVA/JAR/SRC/LIB 四个路径）
# 显式探测，缺就 SKIP 并打印缺什么 —— 不用 try/except 吞异常：
#   吞异常会把「解析器真的坏了」和「没装 java」压成同一种表现（均静默变绿）。
_ast_ok = False
_ast_why = ""
try:
    import sysml_ast as _sa
    _ast_ok = bool(_sa.available())
    if not _ast_ok:
        _ast_why = "缺少 java-runtime / checker.jar / sysml.library（sysml_ast.available()=False）"
except Exception as _e:
    _ast_why = "%s: %s" % (type(_e).__name__, _e)

if not _ast_ok:
    print("SKIP 视图层级门禁 —— OMG SysML v2 解析器不可用")
    print("     原因：%s" % _ast_why)
    print("     影响：本门禁全部 8 段都需真实解析视图；该维度在本次运行中**未验证**"
          "（SKIP ≠ 通过）。")
    print("     恢复：安装 java-runtime 并提供 checker.jar 与 sysml.library 后重跑。")
    sys.exit(0)


fails, checks = [], [0]


def chk(name, cond, detail=""):
    checks[0] += 1
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  | " + str(detail)) if detail else ""))
    if not cond:
        fails.append(name)


def node(i, kind):
    return {"id": i, "name": i, "kind": kind}


print("[1] 纯函数：包 → 成员")
nodes = [node("P", "package"), node("A", "block"), node("B", "part")]
edges = [{"source": "P", "target": "A", "kind": "composition", "type": "包含"}]
_attach_package_hierarchy(nodes, edges)
chk("A.parent == P", next(n for n in nodes if n["id"] == "A").get("parent") == "P")
chk("B 无父（无包含边）", "parent" not in next(n for n in nodes if n["id"] == "B"))

print("\n[2] 包嵌包")
nodes = [node("P", "package"), node("Q", "package"), node("C", "block")]
edges = [{"source": "P", "target": "Q", "kind": "composition", "type": "包含"},
         {"source": "Q", "target": "C", "kind": "composition", "type": "包含"}]
_attach_package_hierarchy(nodes, edges)
m = {n["id"]: n for n in nodes}
chk("Q.parent == P", m["Q"].get("parent") == "P")
chk("C.parent == Q", m["C"].get("parent") == "Q")

print("\n[3] 组合关系不得变成嵌套（part 含 part 是连线语义）")
nodes = [node("Engine", "block"), node("Piston", "part")]
edges = [{"source": "Engine", "target": "Piston", "kind": "composition", "type": "组合"}]
_attach_package_hierarchy(nodes, edges)
chk("Piston 无父（父不是 package）",
    "parent" not in next(n for n in nodes if n["id"] == "Piston"))

print("\n[4] 防环：A 包 B、B 包 A")
nodes = [node("A", "package"), node("B", "package")]
edges = [{"source": "A", "target": "B", "kind": "composition", "type": "包含"},
         {"source": "B", "target": "A", "kind": "composition", "type": "包含"}]
_attach_package_hierarchy(nodes, edges)
m = {n["id"]: n for n in nodes}
_cyc = (m.get("A", {}).get("parent") == "B" and m.get("B", {}).get("parent") == "A")
chk("不成环（至少断开一条）", not _cyc, {k: v.get("parent") for k, v in m.items()})

print("\n[5] 真实解析链路：package + part def → BDD 节点带 parent")
CODE = """
package DemoPkg {
    part def Vehicle {
        part engine : Engine;
    }
    part def Engine {
        attribute power = 100;
    }
}
"""
res = generate_views_from_sysml(CODE, view_types=["BDD", "PKG"], intent="design")
bdd = (res.get("views") or {}).get("BDD") or {}
pkg = (res.get("views") or {}).get("PKG") or {}
chk("BDD 已生成", bool(bdd.get("nodes")), f"节点 {len(bdd.get('nodes') or [])}")
chk("PKG 已生成", bool(pkg.get("nodes")), f"节点 {len(pkg.get('nodes') or [])}")
_has_pkg_node = any(n.get("kind") == "package" for n in (bdd.get("nodes") or []))
chk("BDD 节点集含 package（层级要靠它承载）", _has_pkg_node,
    [n.get("kind") for n in (bdd.get("nodes") or [])])
_children = [n for n in (bdd.get("nodes") or []) if n.get("parent")]
chk("BDD 至少一个节点带 parent", bool(_children),
    [(n.get("name"), n.get("parent")) for n in (bdd.get("nodes") or [])][:6])

print("\n[6] 回滚：开关关闭 → 无 parent")
# 判据：开关必须真的能关。**不写系统配置文件**（会污染用户环境），
# 改为对本进程内的 cfg.as_bool 打补丁 —— view_generator 是"函数内 import"，
# 打到 core.config 模块上即可生效。
_orig_as_bool = cfg.as_bool


def _patched_as_bool(section, key, default=None):
    if (section, key) == ("sysml", "view_package_nesting"):
        return False
    return _orig_as_bool(section, key, default)


try:
    cfg.as_bool = _patched_as_bool
    res2 = generate_views_from_sysml(CODE, view_types=["BDD"], intent="design")
    _any = [n for n in ((res2.get("views") or {}).get("BDD") or {}).get("nodes") or [] if n.get("parent")]
    chk("关闭后无 parent", not _any, _any[:3])
    chk("关闭后 BDD 仍有节点（不是靠清空节点蒙过判据）",
        bool(((res2.get("views") or {}).get("BDD") or {}).get("nodes")))
finally:
    cfg.as_bool = _orig_as_bool
    # 自证补丁已撤销
    chk("开关已复位（不影响同进程后续生成）",
        cfg.as_bool("sysml", "view_package_nesting", True) in (True, False)
        and cfg.as_bool is _orig_as_bool)

print("\n[7] 中文标识符不得被编码毁掉（2026-11-09 实测事故）")
# 症状：`package '通信子系统时序图'` 导出的节点名是 `ͨ����ϵͳʱ��ͼ`。
# 根因：JVM 在 Windows 按系统代码页（GBK）写 stdout，Python 按 utf-8 解码；
# GBK 的 CD A8 恰好是**合法的** utf-8 两字节序列（U+0368），
# 所以 `decode(utf-8, replace)` 连替换符都不产生 —— 乱码静默流进视图标签和产物名，
# 而且让 name→id 匹配全部失效（实测同一份代码 parent 字段从 47 掉到 0）。
from sysml_ast import parse_strict  # noqa: E402

_cjk = parse_strict("package '通信子系统时序图' {\n part def '接入网关';\n}")
_names = [n.get("name") for n in (_cjk or {}).get("nodes") or []]
chk("CJK 包名解析正确", "通信子系统时序图" in _names, _names)
chk("CJK 元素名解析正确", "接入网关" in _names, _names)
chk("无替换符 U+FFFD", not any("\ufffd" in str(x) for x in _names), _names)

print("\n[8] 视图节点按 id 去重（重复项会叠成 N 层方块）")
# 实测：conv 9736 的 634 行代码里有 5 份一模一样的包裹，
# 解析出 90 个节点、每名 5 份 ⇒ 视图 45 个节点里只有 9 个唯一。
DUP = "package P {\n part def A;\n part def B;\n}\n" * 3
_r = generate_views_from_sysml(DUP, view_types=["BDD"], intent="design")
_ns = ((_r.get("views") or {}).get("BDD") or {}).get("nodes") or []
_ids = [n["id"] for n in _ns]
chk("重复代码 → 视图节点 id 唯一", len(_ids) == len(set(_ids)), f"{len(_ids)} 个 / 唯一 {len(set(_ids))}")
chk("去重后仍保留元素（不是清空蒙判据）", {"A", "B"}.issubset(set(_ids)) or len(_ids) >= 2, _ids)

print("\n" + "=" * 62)
if fails:
    print(f"FAILED {len(fails)}/{checks[0]} 项：{fails}")
    sys.exit(1)
print(f"ALL PASS（{checks[0]} 项断言）")
