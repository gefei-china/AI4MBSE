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
_DEF_RE = re.compile(
    r"^[ \t]*(?P<kind>(?:[a-z]\w*\s+)*?def)\s+(?P<name>\w+)"
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
            if members:
                pkgs.setdefault(pname, {"file": rel, "members": [], "kinds": Counter()})
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


def _stdlib_meta(args):
    try:
        idx = build_index()
    except Exception as exc:                                        # noqa: BLE001
        return {"ok": False,
                "result": f"标准库索引构建失败：{type(exc).__name__}: {exc}。"
                          f"⚠️ 这不代表标准库不可用 —— 请如实说明『未能查询标准库』，不要臆造元类。"}

    name = str(args.get("name") or "").strip()
    pkg = str(args.get("package") or "").strip()

    if name:
        text = _fmt_one(idx, name)
    elif pkg:
        text = _fmt_members(idx, pkg)
    else:
        text = _fmt_pkgs(idx)

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
