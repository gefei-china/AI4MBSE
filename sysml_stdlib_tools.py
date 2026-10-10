# -*- coding: utf-8 -*-
"""`sysml_stdlib_meta` —— 查询 SysML 标准库（`sysml.library/`）的元类与成员。

为什么需要（实测依据）
--------------------
`sysml.library/` 有 **59 个 .sysml、1217 个定义**（`attribute def` 984 个最多），
但它们此前**只作为 `checker.jar` 的校验基准**（`sysml_v2_check.py:220` 传入 LIB 参数），
**内容一条都没进 prompt**。于是 LLM 写 `attribute x :> ISQ::mass;` 时：
  · 不知道 `ISQ::mass` 到底存不存在
  · 不知道它该`:>`（specializes）哪个基类
  · 只能猜 ⇒ 猜错时报 `An attribute must be typed by attribute definitions`

本工具把标准库变成**可查询的元类清单**，让 N2/N3 在生成前先查，而不是猜。

能提供什么
----------
① **包清单**：可导入的 standard library package（含定义数统计）
② **成员查询**：某包内有哪些成员（按类型分组，取前 N）
③ **元类查询**：某成员的类型（`attribute def` / `item def` / ...）与**继承链**（`:>` / `:>>`）
④ **反查**：按名称找它属于哪个包（跨包引用时必需）

不提供（避免误导）
----------------
· **不推断"必填/可选属性"** —— 标准库是 KerML 元模型，特性继承复杂，
  静态解析得出���结论不可靠 ⇒ 只给**声明事实**（有什么、在哪、继承谁），语义判断交给 LLM + 校验器。
· **不返回源码全文** —— 版权与 token 双重理由；只给行号定位。

实现要点
--------
*标准库文件路径含空格*（`sysml.library/Domain Libraries/...`）⇒ 所有路径操作走 `os.path`，
**不能用 shell glob 或字符串拼接**。
"""
import glob
import logging
import os
import re
from collections import Counter, defaultdict

logger = logging.getLogger(__name__)

LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sysml.library")

# `standard library package X {`（可选前缀）——实测标准库统一用这个形态
_PKG_RE = re.compile(r"^\s*(?:standard\s+library\s+)?package\s+(\w+)\s*\{", re.M)
# `xxx def Name[: Typed][: Specialization][:> Redefinition|Parent] {`
# ⚠️ `typed` 必须**非贪婪 + 不含 `>`**，否则会把 `:>` 里的 `>` 吃掉
#   （实测踩过：`attribute def X :> Base {` 的 typed 被抽成 ">"，parent 为空 ⇒ 继承链丢失）
# ⚠️ `sp` 分支是为 `part def X : Y : Z` 这类多重定型留的，但标准库里基本不出现，
#    保留以免误吞。
# ⚠️⚠️ 2026-10-09 修：`name` 原来只认 `\w+`，**漏掉 `<gv>` 这种尖括号标识符**。
#   标准库**确实**在用它，且是系统建模的核心写法：
#     `view def <gv> GeneralView`（StandardViewDefinitions）
#     `attribute <g> gram : MassUnit`（SI 单位）
#   ⇒ 实测 **7 个包声明了却索引到 0 个成员**，`sysml_stdlib_meta` 对它们一律回
#     「未找到包」—— 而模型据此以为自己写的包不存在，**去查标准库 /改代码**。
#   实测该包清单正是八视图耗时的大头（`sysml_stdlib_meta` 占工具调用 13/41 = 32%）。
#   放宽为「`<...>` 或 `\w+`」后：全库定义 1286 → **1308（+22）**，4 个文件从 0 变非 0。
#   判据：`tools/verify/verify_stdlib_index_coverage.py`（含变异自证）。
_DEF_RE = re.compile(
    r"^[ \t]*(?P<kind>(?:[a-z]\w*\s+)*?def)\s+(?P<name><[^>\n]+>|\w+)"
    r"(?:\s*:\s*(?P<typed>[^:>{\n]+?))?"
    r"(?:\s*:\s*(?P<sp>[^:>{\n]+?))?"
    r"(?:\s*:\s*>\s*(?P<parent>[\w:]+?)\s*(?=[{\s;]|$))?"
    r"(?:\s*:\s*>>\s*(?P<redf>[\w:]+?)\s*(?=[{\s;]|$))?"
    r"[^\n{;]*\{?", re.M)

_index = None          # 懒加载索引


def _kind_of(kw: str) -> str:
    """把 `attribute def` / `part def` / `def` 归一成类型名。"""
    kw = (kw or "").strip()
    if kw.endswith(" def"):
        return kw[:-4].strip()
    return kw or "def"


def build_index():
    """扫描标准库，建立 package → 成员 索引（懒加载，进程内缓存）。"""
    global _index
    if _index is not None:
        return _index
    files = sorted(glob.glob(os.path.join(LIB_DIR, "**", "*.sysml"), recursive=True))
    pkgs = {}
    member_owner = {}          # name -> [pkg, ...]（同名可能多个包）
    total = 0
    for path in files:
        rel = os.path.relpath(path, LIB_DIR).replace(os.sep, "/")
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        lines = src.split("\n")
        # 逐 package 块归属（标准库一般一文件一包，但不强假设）
        marks = [(m.start(), m.group(1)) for m in _PKG_RE.finditer(src)]
        if not marks:
            continue
        for i, (pos, pname) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(src)
            body = src[pos:end]
            members = []
            for m in _DEF_RE.finditer(body):
                kind = _kind_of(m.group("kind"))
                if not m.group("name"):
                    continue
                line_no = body.count("\n", 0, m.start()) + src.count("\n", 0, pos) + 1
                members.append({
                    "name": m.group("name"),
                    "kind": kind,
                    "parent": (m.group("parent") or m.group("redf") or "").strip(),
                    "typed": (m.group("typed") or "").strip(),
                    "line": line_no,
                })
            # ★ 2026-10-09 修：原为 `if members:` —— **"没有直接成员"的包整个不存在**。
            #   实测 `DS_Views`（Domain Libraries/DSViewsAlias.sysml）只是
            #   `package DS_Views { package SymbolicViews { … } }` 的**外层壳**，
            #   自身 0 个成员 ⇒ 被整包丢弃 ⇒ 工具回「未找到包 DS_Views」。
            #   ⇒ 模型据此以为这个包不存在。
            #   现在**无条件登记包**（成员为 0 也登记），只有 `file` 与空成员列表。
            #   ⚠️ 不能改成"登记所有 package"：`Symbols` 之类嵌套包会把
            #   `member_owner` 的归属算错，保持现有归属逻辑不变。
            pkgs.setdefault(pname, {"file": rel, "members": [], "kinds": Counter()})
            if members:
                pkgs[pname]["members"].extend(members)
                for m in members:
                    pkgs[pname]["kinds"][m["kind"]] += 1
                    member_owner.setdefault(m["name"], []).append(pname)
            total += len(members)
    for p in pkgs.values():
        p["kinds"] = dict(p["kinds"])
    _index = {"packages": pkgs, "owner": member_owner, "files": len(files), "members": total}
    return _index


def _fmt_pkgs(idx, limit=60):
    out = [f"【SysML 标准库包清单】共 {len(idx['packages'])} 个包 / "
           f"{idx['members']} 个定义（来自 {idx['files']} 个 .sysml）", ""]
    rows = []
    for name, p in sorted(idx["packages"].items()):
        kinds = "、".join(f"{k}×{v}" for k, v in
                          sorted(p["kinds"].items(), key=lambda x: -x[1])[:3])
        rows.append((name, len(p["members"]), kinds, p["file"]))
    rows.sort(key=lambda r: -r[1])
    for name, n, kinds, f in rows[:limit]:
        out.append(f"  · {name:32} {n:4} 成员  [{kinds}]  ← {f}")
    if len(rows) > limit:
        out.append(f"  …… 另有 {len(rows) - limit} 个包，可用 package 参数指定")
    return "\n".join(out)


def _fmt_members(idx, pname):
    p = idx["packages"].get(pname)
    if not p:
        avail = sorted(idx["packages"])[:20]
        return (f"未找到包「{pname}」。可用包（部分）：{avail}\n"
                f"提示：包名区分大小写，标准库用 standard library package <名> 声明。")
    by_kind = defaultdict(list)
    for m in p["members"]:
        by_kind[m["kind"]].append(m)
    out = [f"【标准库包 {pname}】{len(p['members'])} 个成员  ← {p['file']}", ""]
    for kind, ms in sorted(by_kind.items(), key=lambda x: -len(x[1])):
        out.append(f"  ▸ {kind}（{len(ms)} 个）")
        for m in ms[:25]:
            ext = f" :> {m['parent']}" if m["parent"] else ""
            t = f" : {m['typed']}" if m["typed"] else ""
            out.append(f"      {m['name']}{t}{ext}   (行 {m['line']})")
        if len(ms) > 25:
            out.append(f"      …… 另有 {len(ms) - 25} 个（可用 name 参数精确查）")
    return "\n".join(out)


def _fmt_one(idx, name):
    owners = idx["owner"].get(name)
    if not owners:
        near = [n for n in idx["owner"] if name.lower() in n.lower()][:8]
        hint = f"\n相近名称：{near}" if near else ""
        return f"标准库中未找到「{name}」。{hint}\n⚠️ 不要臆造标准库成员 —— 请改用已存在的元类或自定义。"
    out = [f"【标准库成员 {name}】"]
    for pname in owners:
        for m in idx["packages"][pname]["members"]:
            if m["name"] != name:
                continue
            t = f" : {m['typed']}" if m["typed"] else ""
            ext = f" :> {m['parent']}" if m["parent"] else ""
            out.append(f"  · 所属包：{pname}{t}{ext}")
            out.append(f"    位置：{idx['packages'][pname]['file']} 第 {m['line']} 行")
            if m["parent"]:
                # 继承链（深度 3， 防环）
                chain, cur, seen = [m["parent"]], m["parent"], {name}
                for _ in range(3):
                    base = cur.split("::")[-1]
                    owners2 = idx["owner"].get(base)
                    if not owners2:
                        chain.append(f"{base}（未在标准库中找到定义）")
                        break
                    nxt = None
                    for p2 in owners2:
                        for mm in idx["packages"][p2]["members"]:
                            if mm["name"] == base:
                                nxt = mm
                                break
                        if nxt:
                            break
                    if not nxt or base in seen:
                        break
                    seen.add(base)
                    chain.append(nxt["parent"] or "（根）")
                    cur = nxt["parent"] or ""
                    if not cur:
                        break
                out.append(f"    继承链：{' → '.join(chain)}")
    out.append("")
    out.append("注意：以上是**声明事实**（有什么、在哪、继承谁）。"
               "必填/可选特性与具体用法请以官方规范 + sysml_v2_validate 校验结果为准。")
    return "\n".join(out)


#: 2026-10-09 实测：模型把包名传成**自造参数名**（`package_name` / `__packages__`），
#: 而契约里的参数叫 `package`。`exec_tool` 不校验未知参数 ⇒ 直接落到 `name` 分支
#: ⇒ 回「标准库中未找到「StandardViewDefinitions」」。
#:
#: ★这个假阴性**极贵**：v1 里 `sysml_stdlib_meta` 调了 **4 次**，其中 3 次
#:   都是这个原因（`name='StandardViewDefinitions'` / `name='Requirements'` /
#:   `name='__packages__'`）。模型据此以为标准库没有这些包
#:   ⇒ 去猜元类名 ⇒ 产出里出现 `import StandardViewDefinitions::*`。
#:
#: ⚠️ 实测基线（八视图首轮）：**同参数重复 0 次**，`probe_dup` 短路**一次都没触发**；
#:   12 次「同工具重复」全是**不同参数**（属合理的多步查询，不是浪费）。
#:   ⇒ 问题**不在重复调用**，而在**每次都查错**。
#: 兼容别名：把常见自造名映射到契约参数，而不是让模型白跑一轮。
_ARG_ALIASES = {
    "package_name": "package",     # 模型最常自造的名字
    "pkg": "package",
    "packagename": "package",
    "packages": "package",
    "member_name": "name",
    "member": "name",
}

#: 模型想「列全部包」时用的**自造成员名**（实测 v1 #2400：`name='__packages__'`）。
#: 它不是契约参数，也查不到 ⇒ 现在显式当成「列包清单」的意图，**不当作成员名去查**。
_PSEUDO_MEMBER_LIST = {"__packages__", "__package_list__", "packages", "all"}


def _normalize_args(args: dict) -> dict:
    """把自造参数名映射到契约参数，并回传可读的提示（不静默丢弃）。"""
    out, remapped = dict(args), []
    for k, v in list(out.items()):
        kl = str(k).strip().lower()
        tgt = _ARG_ALIASES.get(kl)
        if tgt and not out.get(tgt):
            out[tgt] = v
            remapped.append(f"{k}→{tgt}")
    # 伪成员名（要包清单）⇒ 清掉 name，让下面走"列包清单"分支
    if str(out.get("name") or "").strip().lower() in _PSEUDO_MEMBER_LIST:
        out.pop("name", None)
        out["_want_pkg_list"] = True
    return out, remapped


def _stdlib_meta(args):
    try:
        idx = build_index()
    except Exception as exc:                                        # noqa: BLE001
        return {"ok": False,
                "result": f"标准库索引构建失败：{type(exc).__name__}: {exc}。"
                          f"⚠️ 这不代表标准库不可用 —— 请如实说明『未能查询标准库』，不要臆造元类。"}

    _raw = dict(args or {})
    args, _remapped = _normalize_args(_raw)
    # 未知参数：显式点名，别让模型白跑一轮
    _known = {"name", "package", "query"}
    _unknown = [k for k in _raw if str(k).strip().lower() not in _known
                and str(k).strip().lower() not in _ARG_ALIASES]

    name = str(args.get("name") or "").strip()
    pkg = str(args.get("package") or "").strip()
    if args.pop("_want_pkg_list", None):
        name, pkg = "", ""          # 显式要包清单（伪成员名转来的）

    if name:
        text = _fmt_one(idx, name)
    elif pkg:
        text = _fmt_members(idx, pkg)
    else:
        text = _fmt_pkgs(idx)

    # 拼上诊断尾巴：告诉模型「你传的参数名不对/这个包真的不存在」，
    # 而不是让它把「工具说没有」当成事实。
    if _remapped or _unknown:
        tips = []
        if _remapped:
            tips.append(f"已按契约自动重映射：{', '.join(_remapped)}"
                        f"（契约参数只有 name / package / query）")
        if _unknown:
            tips.append(f"以下参数不在契约里、已忽略：{', '.join(_unknown)}；"
                        f"可用参数只有 name / package / query")
        text = text + "\n\n⚠️ 参数提示：" + "；".join(tips)

    #包名疑似被当成成员名来查：直接给出它到底是不是包
    if name and not pkg and name not in idx["owner"]:
        if name in idx["packages"]:
            text += (f"\n\n💡 「{name}」是一个**包**（{len(idx['packages'][name]['members'])} 个成员），"
                     f"不是成员名。要列它的成员请用 `package=\"{name}\"`。")

    if not idx["packages"]:
        return {"ok": False, "result": "标准库为空或路径不存在：" + LIB_DIR}
    return {
        "ok": True,
        "result": text,
        "n_packages": len(idx["packages"]),
        "n_members": idx["members"],
        "n_files": idx["files"],
    }


def exec_tool(name: str, arguments: dict | None = None) -> dict:
    args = arguments or {}
    if name == "sysml_stdlib_meta":
        return _stdlib_meta(args)
    return {"ok": False, "result": f"未知标准库工具: {name}"}


if __name__ == "__main__":
    import json as _json
    # 简易自检：继承链抽取是否有效（实测踩过 typed 贪婪吃掉 `:>` 导致 parent 空）
    _cases = [
        ("    attribute def X :> Base {", "Base"),
        ("  item def Y :> QuantityValue {", "QuantityValue"),
        ("  part def Vehicle {", None),
    ]
    _bad = 0
    for _s, _want in _cases:
        _m = _DEF_RE.search(_s)
        _got = _m.group("parent") if _m else "<no match>"
        _ok = (_got == _want)
        if not _ok:
            _bad += 1
        print(f"  [{'OK  ' if _ok else 'FAIL'}] {_s.strip()[:40]:42} parent={_got} (期望 {_want})")
    if _bad:
        print(f"❌ 继承链抽取有 {_bad} 处失效")
    else:
        print("✅ 继承链抽取正常")

    for kw in ({}, {"package": "ISQ"}, {"name": "ElectricChargeValue"},
               {"name": "Mass"}, {"name": "NotExistThing"}):
        r = _stdlib_meta(kw)
        print(f"\n{'='*70}\n>>> 查询参数：{kw}\n{'='*70}")
        print(r["result"][:1100])
