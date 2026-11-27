# -*- coding: utf-8 -*-
"""降级直行路径「能收尾」门禁 —— 2026-11-27（P0，防 UnboundLocalError 复发）。

## 它防的是什么

`_stream_orchestrated_flow` 里有一类局部名，**只在「非降级」分支赋值**：

    降级直行（计划为空 / 计划校验失败 / 编排异常兜底）
      → 这些名字从未绑定
      → 函数尾部组装 card_obj / 统计时读它们
      → UnboundLocalError
      → 整轮以 error 帧收尾，**已经生成好的正文一个字也送不到用户**

本仓已踩到两次，形态完全一致：
  * 2026-11-04 `_gate_gaps`（会话 10028：用户看到「编排执行失败」，其实只是没有交付物可汇总）
  * 2026-11-27 `_fill_verdicts/_fill_filled/_fill_patch`
             `_orch_tool_counts/_orch_probe_caps/_orch_val_stalls`
    （mock + 智源对接agent：前三阶段全 done，646 字正文已生成，「写入会话」抛错整轮 error）

两次的修法也一致：尾部 `try: <name> / except NameError: <name> = <空值>` 兜底。
**兜底是行为护栏，不是根治** —— 所以本门禁要同时锁两件事：

  D1 **行为**：真的跑一轮降级直行会话，必须拿到 done 帧 + 正文非空。
     这是唯一能证明"降级路径没炸"的证据；静态判据只能猜。
  D2 **结构**：那 6 个名字 + `_gate_gaps` 的兜底不许被删。
     删掉后行为门禁会红，但红在"整轮 error"，定位要重新查一遍；
     结构断言能直接指出是哪个名字的兜底没了。

## 为什么不用「全仓 AST 扫描」做这件事（2026-11-27 实测）

曾用「某名字的所有 Store 都落在条件块内、却在块外被 Load」做全仓通用判据，
在 372 个文件上命中 **701 条**，绝大多数是安全写法：

    if x:    v = 1
    else:    v = 2
    print(v)        # 分支全覆盖，安全，但命中

要压到可用，得做完整的 CFG 支配分析（判断"每条到达 Load 的路径上都有 Store"），
等价于自己写半个 pylint；而本仓无 pylint/mypy 依赖，装一个整仓跑一遍还要维护基线。
⇒ **判据收窄到"已知会炸的真实路径"上**：行为复现 + 定点结构断言，零误报。

## 用法

    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_degraded_direct_flow.py

服务需在 `MBSE_BASE`（默认 http://127.0.0.1:8000）可访问。
脚本会**临时开启 `llm.force_mock`** 并在 finally 里逐字节还原配置文件；
会新建一个标题为 `__degraded_direct_gate__` 的会话用于取证（不复用、不改历史）。
"""
import ast
import json
import os
import sys

import httpx

BASE = os.environ.get("MBSE_BASE", "http://127.0.0.1:8000").rstrip("/")
H = {"X-User-Id": os.environ.get("MBSE_UID", "3")}
CFG = os.path.expanduser("~/.workbuddy/mbse_config.json")
DB = os.environ.get("MBSE_DB_PATH", "mbse.db")
STREAM = "agent/pipeline_parts/stream.py"
FUNC = "_stream_orchestrated_flow"

# 只在「非降级」分支赋值的名字（两次事故的并集）
GUARDED = ["_gate_gaps", "_fill_verdicts", "_fill_filled", "_fill_patch",
           "_orch_tool_counts", "_orch_probe_caps", "_orch_val_stalls"]

results = []


def chk(tag, ok, detail=""):
    results.append((tag, bool(ok), detail))
    print(("  PASS " if ok else "  FAIL ") + tag + ("  | " + detail if detail else ""))
    return ok


# ══════════════════════════════════════════════════════════════════
# D2 结构：兜底赋值必须在（不依赖服务，先跑）

def _block_ranges(fn):
    """函数内所有条件块的行区间（用于判断某次 Store 是否"被条件包住"）。

    ★ `else:` 分支必须算进来（2026-11-27 踩到）：本次缺陷的形态恰恰是
    「只在 `else:`（非降级）分支赋值」。若只采 `body`，`else` 里的赋值会被
    误判成"无条件赋值"⇒ 判据对真正的缺陷形态反而失明。
    """
    blocks = (ast.If, ast.Try, ast.For, ast.AsyncFor, ast.While,
              ast.With, ast.AsyncWith, ast.ExceptHandler)
    out = []
    for n in ast.walk(fn):
        if not isinstance(n, blocks):
            continue
        for group in (getattr(n, "body", None), getattr(n, "orelse", None)):
            if not group:
                continue
            lo = min(s.lineno for s in group)
            hi = max(getattr(s, "end_lineno", s.lineno) for s in group)
            out.append((lo, hi))
    return out


def _nameerror_guards(fn):
    """识别 `try: <Name> ... except NameError: <Name> = ...` 兜底，返回被保护的名字。"""
    ok = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Try) or not n.handlers:
            continue
        for st in n.body:
            if isinstance(st, ast.Expr) and isinstance(st.value, ast.Name):
                nm = st.value.id
                for h in n.handlers:
                    is_ne = h.type is None or (isinstance(h.type, ast.Name) and h.type.id == "NameError")
                    if not is_ne:
                        continue
                    if any(isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store) and x.id == nm
                           for x in ast.walk(h)):
                        ok.add(nm)
    return ok


def check_structure():
    src = open(STREAM, encoding="utf-8").read()
    tree = ast.parse(src)
    fn = None
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == FUNC:
            fn = n
            break
    if not chk("D2.0 找到 %s" % FUNC, fn is not None):
        return
    ranges = _block_ranges(fn)
    guards = _nameerror_guards(fn)
    for name in GUARDED:
        stores = [x for x in ast.walk(fn)
                  if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store) and x.id == name]
        if not stores:
            chk("D2 %s 有赋值" % name, False, "函数内完全没有该名字（改名了？同步本门禁）")
            continue
        # 通过条件：存在一次赋值既不在条件块内（顶层/尾部），或落在 NameError 兜底里
        uncond = [s for s in stores if not any(lo <= s.lineno <= hi for (lo, hi) in ranges)]
        if uncond:
            chk("D2 %s 兜底赋值仍在" % name, True, "顶层 Store @%d" % uncond[0].lineno)
        elif name in guards:
            chk("D2 %s 兜底赋值仍在" % name, True, "靠 except NameError 兜底")
        else:
            chk("D2 %s 兜底赋值仍在" % name, False,
                "既无顶层赋值也无 NameError 兜底 ⇒ 降级直行必然 UnboundLocalError")


# ══════════════════════════════════════════════════════════════════
# D1 行为：真跑一轮降级直行

def check_behavior():
    try:
        code = httpx.get(BASE + "/api/dashboard", headers=H, timeout=8).status_code
    except Exception as e:
        chk("D1.0 服务可访问", False, "%s: %s" % (BASE, e))
        return
    if not chk("D1.0 服务可访问", code == 200, "HTTP %s" % code):
        return

    orig = open(CFG, "rb").read()
    cid = None
    try:
        r = httpx.post(BASE + "/api/conversations",
                       json={"title": "__degraded_direct_gate__"}, headers=H, timeout=30)
        cid = (r.json() or {}).get("id")
        chk("D1.1 建取证会话", bool(cid), "cid=%s" % cid)

        httpx.put(BASE + "/api/system/config/static",
                  json={"updates": {"llm.force_mock": True}}, timeout=30)
        httpx.post(BASE + "/api/system/config/reload", timeout=30)

        ev, content, msg_id = [], "", None
        with httpx.stream("POST", BASE + "/api/conversations/%s/chat/stream" % cid,
                          json={"message": "请分析需求条目并生成设计模型，最后校验建模结果",
                                "team": "智源对接agent"},
                          headers=H, timeout=300) as resp:
            cur = None
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    cur = line.split(":", 1)[1].strip()
                    ev.append(cur)
                elif line.startswith("data:") and cur == "done":
                    d = json.loads(line[5:].strip()).get("data") or {}
                    content = d.get("content") or ""
                    msg_id = d.get("message_id")
                    break

        chk("D1.2 收到 done 帧", "done" in ev, "事件 %d 类: %s" % (len(set(ev)), ",".join(sorted(set(ev)))[:120]))
        chk("D1.3 无 error 帧", "error" not in ev,
            "出现 error 说明降级路径仍未收口" if "error" in ev else "")
        chk("D1.4 正文非空", len(content) > 200, "%d 字：%s" % (len(content), content[:40].replace("\n", " ")))

        # 取证：确认这一轮**确实**走的是降级直行（否则 D1.2~D1.4 证明不了降级路径）
        card = {}
        if msg_id:
            try:
                import sqlite3
                c = sqlite3.connect(DB)
                row = c.execute("select card_data from messages where id=?", (msg_id,)).fetchone()
                c.close()
                if row and row[0]:
                    card = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
            except Exception as e:
                print("  (取证读库失败，跳过降级标记校验: %s)" % e)
        if card:
            chk("D1.5 确为降级直行", card.get("degraded") is True and not (card.get("plan") or []),
                "degraded=%r plan=%d 条" % (card.get("degraded"), len(card.get("plan") or [])))
            chk("D1.6 补齐字段可组装", card.get("fill_causes") == [] and card.get("fill_filled") == [],
                "fill_causes=%r" % (card.get("fill_causes"),))
    finally:
        try:
            httpx.put(BASE + "/api/system/config/static",
                      json={"updates": {"llm.force_mock": False}}, timeout=30)
        except Exception:
            pass
        with open(CFG, "wb") as f:
            f.write(orig)
        try:
            httpx.post(BASE + "/api/system/config/reload", timeout=30)
        except Exception:
            pass
        chk("D1.7 配置逐字节还原", open(CFG, "rb").read() == orig)


def main():
    print("== D2 结构断言（不依赖服务）==")
    check_structure()
    print("== D1 行为断言（真实跑一轮降级直行）==")
    check_behavior()
    bad = [t for t, ok, _ in results if not ok]
    print("\n合计 %d 项，PASS %d，FAIL %d" % (len(results), len(results) - len(bad), len(bad)))
    if bad:
        print("失败项：" + "; ".join(bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
