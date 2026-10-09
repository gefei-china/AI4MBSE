"""verify_frontend_modeling_e2e — **前端 AI 建模入口**的完整链路验证。
# CI-OPTIONAL: 需**真实 HTTP 服务在跑**（uvicorn 起在 8012）且真调 LLM；CI 里没有常驻服务 ⇒ 属本地端到端口径。与 verify_chat_contract_e2e 同类（那批也因需服务而豁免）。


## 为什么需要这个（2026-10-09 实测）

前端「AI 建模」页（`static/index.html` 的`go('ai')`，**默认首页**）的入口是
「＋ 新建任务」，**没有独立的建模入口按钮** ——
2026-09-18 用户确认时移除了「AI 建模」导航项，统一由「＋ 新建任务」承担。

⇒ 后果：**用户从前端建模，唯一路径就是普通对话流 →意图识别**。
   这与所有历史验证都不同——那些脚本用 `forced_intent=` 直连，
   **跳过了意图识别与编排**，等于"我自己在用，用户从 UI 走不到"。

本脚本**不传 `forced_intent`**，走真实 HTTP：
```
POST /api/conversations            建会话
POST /api/conversations/{id}/chat  发消息（只传message，意图交给路由）
```
⇒ 任何一次 `intent != 预期节点`，都会被本门禁判红。

## 前置

需先起服务：`uvicorn main:app --port 8012`（或项目既有启动方式）。
用 `BASE=` 指定其它地址。

## 判据（缺一即 FAIL）

① **服务可达**：能建会话
② **意图路由**：每条建模指令都命中预期的流水线节点
   （**不许forced_intent** —— 这是本脚本存在的全部意义）
③ **产出真代码块**：响应里含 ```sysml 代码块
   （不是"我这就生成…"这类叙述）
④ **代码过官方校验器**：`sysml_v2_check.check_code` 的 `n_hard == 0`
⑤ **产出不雷同**：多视图的 md5 不得相同（照抄骨架的失效形态）

用法：
```
# 1) 起服务
./.venv/Scripts/python.exe -m uvicorn main:app --port 8012 &
# 2) 跑验证
./.venv/Scripts/python.exe -X utf8 tools/verify/verify_frontend_modeling_e2e.py
```
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

#: 建模指令 → 期望命中的流水线节点（**只走意图识别，不许 forced_intent**）
FLOW = [
    ("为纯电动汽车热管理系统生成SysML v2建模需求条目", "requirement_structuring"),
    ("生成架构骨架", "architecture_skeleton"),
    ("生成活动图", "view_expansion"),
    ("生成状态机视图", "view_expansion"),
]

#: 只验证意图路由（不跑模型）的快速档——省 token
ROUTE_ONLY = "--route-only" in sys.argv


def _post(url: str, payload: dict, timeout: int = 600) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_service(base: str, tries: int = 20) -> bool:
    for i in range(tries):
        try:
            urllib.request.urlopen(base + "/api/conversations", timeout=5)
            return True
        except urllib.error.HTTPError:
            return True          # 有响应即算可达（4xx 也说明服务在）
        except Exception:        # noqa: BLE001
            time.sleep(1)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BASE", "http://127.0.0.1:8012"))
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print("=" * 72)
    print("前端 AI 建模入口 · 完整链路验证（真实 HTTP，不传 forced_intent）")
    print("=" * 72)
    print(f"  服务地址：{base}")

    # ── ① 服务可达 ──
    if not wait_service(base):
        print(f"[FAIL] 服务不可达：{base}")
        print("先起服务：./.venv/Scripts/python.exe -m uvicorn main:app --port 8012")
        return 1
    print("  [OK  ] 服务可达")

    import hashlib
    from sysml_v2_check import check_code

    results = {}
    problems = []

    # ── 建一个会话，全程复用（模拟"用户在建好的任务里连续下指令"）──
    try:
        conv = _post(base + "/api/conversations", {"title": "前端建模E2E"})
        cid = conv.get("id") or conv.get("conversation_id")
    except Exception as exc:                # noqa: BLE001
        print(f"[FAIL] 建会话失败：{type(exc).__name__}: {exc}")
        return 1
    if not cid:
        print(f"[FAIL] 建会话未返回 id：{conv}")
        return 1
    print(f"  [OK  ] 会话已建 conv={cid}")

    md5s = {}
    for i, (msg, expect) in enumerate(FLOW, 1):
        print("\n" + "-" * 72)
        print(f"第 {i}/{len(FLOW)} 步：{msg!r}")
        print(f"  期望意图：{expect}")
        try:
            resp = _post(base + f"/api/conversations/{cid}/chat",
                         {"message": msg, "branch": "dev"})
        except Exception as exc:            # noqa: BLE001
            print(f"  [FAIL] 调用异常 {type(exc).__name__}: {exc}")
            problems.append(f"{msg[:16]} 调用异常")
            continue

        # ── ② 意图路由（**本脚本的核心判据**）──
        got = (resp.get("_meta") or {}).get("intent") \
            or resp.get("intent") or resp.get("agent") or "?"
        ok_intent = got == expect
        print(f"  [{'OK  ' if ok_intent else 'FAIL'}] 意图路由 = {got}"
              f"（期望 {expect}）")
        if not ok_intent:
            problems.append(f"{msg[:16]}：意图 {got} ≠ {expect}")
            # 意图错了就不用继续看产出了
            continue

        if ROUTE_ONLY:
            print("  [--route-only] 跳过产出校验")
            continue

        # ── ③ 产出真代码块 ──
        text = ""
        for k in ("response", "content", "reply", "message"):
            v = resp.get(k)
            if isinstance(v, str) and v:
                text = v
                break
        blocks = re.findall(r"```(?:sysml|sysmlv2)?\s*\n(.*?)```", text, re.S | re.I)
        if not blocks:
            print(f"  [FAIL] 无代码块（响应 {len(text)} 字符）")
            print(f"         模型实际说：{text[:120]!r}")
            problems.append(f"{msg[:16]}：无代码块")
            continue
        code = blocks[-1]
        print(f"  [OK  ] 代码块 {len(code)} 字符")

        # ── ④ 官方校验器 ──
        r = check_code(code)
        nh = r.get("n_hard")
        print(f"  [{'OK  ' if nh == 0 else 'FAIL'}] 校验 verdict={r.get('verdict')} "
              f"n_hard={nh}")
        if nh:
            for e in (r.get("errors") or [])[:3]:
                print(f"         L{e.get('line')} {str(e.get('msg'))[:56]}")
            problems.append(f"{msg[:16]}：{nh} 个硬错")

        md5 = hashlib.md5(code.encode()).hexdigest()[:8]
        md5s.setdefault(md5, []).append(expect)
        results[expect] = {"len": len(code), "n_hard": nh, "md5": md5}

    # ── ⑤ 产出雷同 ──
    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    if not ROUTE_ONLY:
        dups = {k: v for k, v in md5s.items() if len(v) > 1}
        if dups:
            print(f"  ⚠️ 产出雷同：{list(dups.values())}")
            problems.append("多步骤产出了完全相同的内容（模型在照抄）")
        elif md5s:
            print(f"  [OK  ] 无产出雷同（{len(md5s)} 份各不相同）")
    if problems:
        print(f"  问题 {len(problems)} 项：")
        for p in problems:
            print("   -" + p)
        return 1
    print("✅ 前端入口完整链路通过：意图命中流水线节点 + 产出合格代码")
    return 0


if __name__ == "__main__":
    sys.exit(main())
