"""SysML v2 语法树解析（2026-09-20）——**改用 OMG 官方解析器，不再自己扫文本**。

背景
----
`sysml_importer.parse_text` 是手写的关键字扫描 + 括号配对（正则），不是语法解析器。
实测 conv 375：它**漏建「包级 part usage」节点**（`part evThermalSystem : EVThermalManagementSystem{...}`
声明了却不在节点集），导致引用它的 126/220 条关系被整条丢弃 —— 这是 BDD 投影 0 条边、
追溯关系 verify/trace/refine 全 0、以及「1 项悬空引用」的**共同根因**。

而工程里本来就有行业标准解析器：`checker.jar` = OMG SysML v2 官方参考实现
（Pilot Implementation）+ Eclipse Xtext 语法 + 121 文件标准库。
本模块只是**胶水**：调用它导出语法树 JSON，再把官方元类映射成本工程的 {nodes, edges}。
**语法正确性 100% 由 OMG 解析器负责**，这里不含任何 SysML 语法知识。

对外
----
- `available()`            Java/jar/源码是否齐备
- `parse_ast(code_text)`   原始语法树 {"nodes":[{"id","type","name","qualifiedName"}],"edges":[...]}
- `parse_text_ast(code)`   与旧 `parse_text` **同构**的 {nodes, edges}（None 语义，适合渐进迁移）
- `parse_strict(code)`     同上但失败**抛 RuntimeError**（唯一实现，无自造兜底）

用法：`from sysml_ast import parse_strict`（导入/知识链路）；`parse_text_ast` 保留给需要
"拿不到就跳过"的调用方。旧 `sysml_importer.parse_text` 已于 2026-09-20 删除。
失败一律返回 None（调用方回落到旧解析器），绝不抛异常打断主流程。
"""
import json
import os
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
JAVA = os.path.join(ROOT, "java-runtime", "bin", "java.exe")
JAVAC = os.path.join(ROOT, "java-runtime", "bin", "javac.exe")
JAR = os.path.join(ROOT, "checker.jar")
LIB = os.path.join(ROOT, "sysml.library")
SRC = os.path.join(ROOT, "tools", "sysml_ast", "SysMLAstExport.java")
OUTDIR = os.path.join(ROOT, "tmp", "sysml_ast")
CLASS = os.path.join(OUTDIR, "SysMLAstExport.class")

# OMG 元类 → (中文实体类型, kind)。只收这些进 nodes；其余（Membership/Documentation/表达式等）是语法骨架。
NODE_KINDS = {
    "PartUsage": ("部件", "part"), "PartDefinition": ("部件", "part"),
    "ItemUsage": ("项", "item"), "ItemDefinition": ("项", "item"),
    "RequirementUsage": ("需求", "requirement"), "RequirementDefinition": ("需求", "requirement"),
    "PortUsage": ("端口", "port"), "PortDefinition": ("端口", "port"),
    "InterfaceUsage": ("接口", "interface"), "InterfaceDefinition": ("接口", "interface"),
    "Package": ("包", "package"),
    "AttributeUsage": ("属性", "attribute"), "AttributeDefinition": ("属性", "attribute"),
    "ActionUsage": ("功能", "action"), "ActionDefinition": ("功能", "action"),
    "StateUsage": ("状态", "state"), "StateDefinition": ("状态", "state"),
    "UseCaseUsage": ("用例", "use_case"), "UseCaseDefinition": ("用例", "use_case"),
    "ConstraintUsage": ("约束", "constraint"), "ConstraintDefinition": ("约束", "constraint"),
    "ConnectionUsage": ("连接", "connection"), "ConnectionDefinition": ("连接", "connection"),
    "ViewUsage": ("视图", "view"), "ViewDefinition": ("视图", "view"),
    "OccurrenceUsage": ("发生", "occurrence"), "OccurrenceDefinition": ("发生", "occurrence"),
}

# 解析「满足方」时要穿过的语法骨架（表达式/链式引用等），不是语义节点
_REL_CONTAINMENT = "包含"
_REL_CONNECT = "连接"
_REL_SATISFY = "满足"
_REL_GENERAL = "泛化"
_REL_TYPE = "类型"


def available() -> bool:
    return all(os.path.isfile(p) for p in (JAVA, JAR, SRC)) and os.path.isdir(LIB)


def _ensure_class():
    """首次使用时用自带 javac 编译胶水类（源码入版本库，产物落 tmp/）。"""
    if os.path.isfile(CLASS) and os.path.getmtime(CLASS) >= os.path.getmtime(SRC):
        return True
    if not os.path.isfile(JAVAC):
        return False
    os.makedirs(OUTDIR, exist_ok=True)
    p = subprocess.run([JAVAC, "-encoding", "UTF-8", "-cp", JAR, "-d", OUTDIR, SRC],
                       capture_output=True)
    return p.returncode == 0 and os.path.isfile(CLASS)


def parse_ast(code_text: str, timeout: int = 180):
    """用 OMG 解析器解析代码 → 语法树 JSON。失败返回 None。"""
    if not (code_text or "").strip() or not available() or not _ensure_class():
        return None
    fd, path = tempfile.mkstemp(suffix=".sysml")
    try:
        os.close(fd)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(code_text)
        p = subprocess.run([JAVA, "-cp", "%s;%s" % (JAR, OUTDIR), "SysMLAstExport", path, LIB],
                           cwd=ROOT, capture_output=True, timeout=timeout)
    except Exception:
        return None
    finally:
        try:
            os.remove(path)
        except Exception:
            pass
    out = (p.stdout or b"").decode("utf-8", "replace")
    # ⚠️ 官方实现会在 stdout 前面打 100+ 行 `Reading <库文件>...`，必须先过滤再取 JSON
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                j = json.loads(s)
            except Exception:
                continue
            if j.get("ok"):
                return j
    return None


# 解析引用目标时要穿过的语法骨架（表达式/链式引用/Membership…），这些不是语义节点
_TRAVERSE_REFS = (
    "owned", "ownedMember", "ownedElement", "ownedRelatedElement", "relatedElement",
    "featureTarget", "chainingFeature", "feature", "member", "referenceType", "target",
)


def _resolve_leaf(ast, nid, out, skip_kinds=()):
    """把一个引用目标解析成**语义节点**。

    为什么需要：`satisfy X by evThermalSystem.batteryThermal.pump` / `connect a.p1 to b.p2`
    直接指向的往往是 ReferenceUsage / FeatureReferenceExpression 这类**语法骨架**，
    真正要的是引用链末端的语义元素。这些骨架之间是**引用**关系（FeatureChaining），
    不是包含关系 —— 只走包含边会一无所获（实测：全部解析成 None）。

    :param skip_kinds: 要跳过的 kind（如端口 —— connect 的两端要解析到**部件**而非端口）
    :return: 语义节点 id，或 None
    """
    nmap = ast["_nmap"]
    stack, seen, found = [nid], {nid}, []
    while stack:
        cur = stack.pop()
        n = nmap.get(cur)
        if not n:
            continue
        k = NODE_KINDS.get(n["type"])
        if k and n["name"]:
            if k[1] not in skip_kinds:
                found.append(cur)
            continue          # 语义节点：记录，不再往下钻
        if len(seen) > 400:   # 兜底：防止在语法骨架里打转
            break
        for r, t in out.get(cur, ()):
            if r in _TRAVERSE_REFS and t not in seen:
                seen.add(t)
                stack.append(t)
    if not found:
        return None
    # 路径末端 = 限定名最长者（`a.b.c` 取 `c`），比栈序确定
    return max(found, key=lambda i: len(nmap[i].get("qualifiedName") or ""))


def parse_strict(code_text: str, timeout: int = 180) -> dict:
    """解析失败**直接抛 RuntimeError** —— 供把 OMG 解析器当唯一权威的调用方使用。

    为什么要有它：`parse_text_ast` 返回 None 的"静默回落"语义适合渐进迁移，
    但一旦旧解析器（`sysml_importer.parse_text`）退役，"解析失败"必须**可见**，
    不能再退回一个会静默丢节点/丢关系的实现（那正是本次要消灭的问题形态）。
    失败只可能来自：Java/checker.jar/标准库缺失、超时、或模型本身语法错。
    """
    r = parse_text_ast(code_text, timeout=timeout)
    if r is None:
        raise RuntimeError(
            "OMG SysML v2 解析器不可用（缺少 java-runtime / checker.jar / sysml.library，或解析超时）。"
            "本工程已改用 OMG 官方参考实现做代码→图谱抽取，不再提供自造解析器兜底。")
    return r


def parse_text_ast(code_text: str, timeout: int = 180):
    """与 `sysml_importer.parse_text` 同构的 {nodes, edges}；失败返回 None。"""
    ast = parse_ast(code_text, timeout=timeout)
    if not ast:
        return None
    nmap = {n["id"]: n for n in ast["nodes"]}
    ast["_nmap"] = nmap
    out = {}
    for e in ast["edges"]:
        out.setdefault(e["source"], []).append((e["ref"], e["target"]))

    # ── 1) 节点：只收语义元素，且必须有名字（匿名元素只是语法骨架）─────────────────
    #    端口**不独立成节点**（规范 7.12：端口是部件边界特征），只挂到所属部件
    #    properties.ports —— 与旧解析器及 view_generator 的约定保持一致。
    nodes, id_of = [], {}
    for n in ast["nodes"]:
        if n["type"] == "PortUsage":
            continue
        k = NODE_KINDS.get(n["type"])
        if not k or not n["name"]:
            continue
        entity_type, kind = k
        props = {"kind": kind}
        # 类型（usage → definition）：part X : Y → part_type=Y
        for r, t in out.get(n["id"], ()):
            if r == "definition" and t in nmap and nmap[t]["name"]:
                props["part_type"] = nmap[t]["name"]
                break
        # 端口：本元素拥有的 PortUsage。
        # ⚠️ 不能只看包含边 `owned`：SysML v2 的 EMF 包含要经过 **Membership 中间对象**
        #    （PartDefinition → OwnedMembership → PortUsage），所以端口是**引用**
        #    （`feature`/`ownedMember`）而非直接包含 —— 只看 `owned` 会一个都收不到。
        ports = []
        for r, t in out.get(n["id"], ()):
            if r in ("feature", "ownedMember", "owned") and t in nmap \
                    and nmap[t]["type"] in ("PortUsage", "PortDefinition") and nmap[t]["name"]:
                ports.append(nmap[t]["name"])
        if ports:
            props["ports"] = ports
        nid = n["name"]
        id_of[n["id"]] = nid
        nodes.append({"name": nid, "entity_type": entity_type,
                      "properties": props, "def_id": nid})

    # ── 2) 边 ────────────────────────────────────────────────────────────────────
    def nm(i):
        return id_of.get(i) or (nmap.get(i) or {}).get("name") or None

    edges = []

    def add(src, tgt, rel, props=None):
        if src and tgt:
            edges.append({"source_name": src, "target_name": tgt,
                          "relation_type": rel, "props": props or {}})

    # 只有落到"结构级"元素的包含才建边（属性/端口/表达式等一律不建，否则噪声淹没 BDD）
    _CONTAIN_KINDS = ("part", "item", "requirement", "package", "interface", "occurrence")
    for n in ast["nodes"]:
        for r, t in out.get(n["id"], ()):
            if r in ("feature", "ownedMember", "owned"):
                tk = NODE_KINDS.get((nmap.get(t) or {}).get("type", ""))
                if tk and tk[1] in _CONTAIN_KINDS and (nmap.get(t) or {}).get("name"):
                    add(nm(n["id"]), nm(t), _REL_CONTAINMENT, {"sysml": r})
            elif r == "subclassification" or r == "superclassifier":
                add(nm(n["id"]), nm(t), _REL_GENERAL, {"sysml": r})
            elif r == "definition":
                add(nm(n["id"]), nm(t), _REL_TYPE, {"sysml": r})

        # 连接：connect a.p1 to b.p2 → 两端解析到**部件**（跳过端口）
        if n["type"] == "ConnectionUsage":
            ends = [t for r, t in out.get(n["id"], ()) if r in ("connectorEnd", "endFeature")]
            if len(ends) >= 2:
                a = _resolve_leaf(ast, ends[0], out, skip_kinds=("port",))
                b = _resolve_leaf(ast, ends[1], out, skip_kinds=("port",))
                add(nm(a), nm(b), _REL_CONNECT, {"sysml": "connect"})
        # 满足：satisfy <需求> by <部件>
        elif n["type"] == "SatisfyRequirementUsage":
            req = None
            for r, t in out.get(n["id"], ()):
                if r in ("assertedConstraint", "constraintDefinition", "definition"):
                    req = t
                    break
            who = None
            for r, t in out.get(n["id"], ()):
                if r in ("directedFeature", "directedUsage", "subjectParameter"):
                    who = _resolve_leaf(ast, t, out)
                    break
            add(nm(who), nm(req), _REL_SATISFY, {"sysml": "satisfy"})

    # 同一对 (source, target, relation) 只留一条：EMF 的 `feature` / `ownedMember` / `owned`
    # 会指向同一个成员（Membership 中间对象导致），不去重会让每个组合重复 2~3 次。
    seen_e = {}
    for e in edges:
        k = (e["source_name"], e["target_name"], e["relation_type"])
        if k not in seen_e:
            seen_e[k] = e
    return {"nodes": nodes, "edges": list(seen_e.values())}
