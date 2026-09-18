"""剥离文件里的路由处理函数（2026-09-18 S7）。

用途：`routers/studio_parts/shared.py` 本应是「共享依赖 + 辅助函数 + 唯一 router」，
但机械切分时把 prompts/skills 的路由处理函数也留了一份 → 与分片模块**重复注册**，
FastAPI 只命中先注册者，后注册的静默失效（症状：改了 prompts.py 却不生效）。

安全前提（脚本内置断言）：
  1) 被剥离的每个函数，必须在 **另一个给定文件里存在同名且逐字节相同** 的实现（用 ast 提取比对）；
     不满足则拒绝写盘 —— 避免把唯一实现删掉。
  2) 只删「顶层且带 @router.<method> 装饰器」的函数，辅助函数一律保留。

用法：
  python tools/verify/strip_router_routes.py --file routers/studio_parts/shared.py \
      --same-as routers/studio_parts/prompts.py,routers/studio_parts/skills.py [--apply]
"""
import argparse
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]


def top_fns(src):
    t = ast.parse(src)
    L = src.splitlines()
    out = {}
    for n in t.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = {
                "code": "\n".join(L[n.lineno - 1:n.end_lineno]),
                "start": n.lineno,             # 1-based, def 行
                "end": n.end_lineno,
                "decorators": [d.lineno for d in n.decorator_list],
                "deco_text": "\n".join(L[(n.decorator_list[0].lineno - 1):n.lineno - 1]) if n.decorator_list else "",
            }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--same-as", required=True)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    p = ROOT / a.file
    raw = p.read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw[:2000] else "\n"
    mine = top_fns(raw)
    others = {}
    for rel in a.same_as.split(","):
        rel = rel.strip()
        if not rel:
            continue
        others.update(top_fns((ROOT / rel).read_bytes().decode("utf-8")))

    targets = []
    for name, info in mine.items():
        if "@router." not in (info["deco_text"] or ""):
            continue
        other = others.get(name)
        if not other:
            print("!! %s() 在对照文件里不存在同名实现 —— 跳过（可能是该文件独有路由）" % name)
            continue
        if other["code"] != info["code"]:
            print("!! %s() 与对照实现**不同** —— 跳过（需人工判断保留哪份）" % name)
            continue
        targets.append((name, info))

    print("文件 %s：带 @router 的顶层函数 %d 个，其中「与对照文件逐字节相同」可安全剥离 %d 个"
          % (a.file, sum(1 for i in mine.values() if "@router." in (i["deco_text"] or "")), len(targets)))
    if not targets:
        print("无可剥离项")
        return 0
    for name, info in sorted(targets, key=lambda x: x[1]["start"]):
        print("    - %s()  L%d-L%d" % (name, info["start"], info["end"]))

    if not a.apply:
        print("（dry-run；加 --apply 生效）")
        return 0

    lines = raw.splitlines(keepends=True)
    spans = []
    for _, info in targets:
        s = min(info["decorators"]) - 1
        e = info["end"]
        while e < len(lines) and lines[e].strip() == "":   # 吃掉紧随空行
            e += 1
        spans.append((s, e))
    spans.sort(reverse=True)
    for s, e in spans:
        del lines[s:e]
    p.write_bytes("".join(lines).encode("utf-8"))
    left = len([n for n in top_fns(p.read_bytes().decode("utf-8")).values() if "@router." in (n["deco_text"] or "")])
    print("已剥离 %d 个函数；文件字节 %d → %d；剩余 @router 函数 %d 个"
          % (len(spans), len(raw.encode("utf-8")), len("".join(lines).encode("utf-8")), left))
    return 0


if __name__ == "__main__":
    sys.exit(main())
