# -*- coding: utf-8 -*-
"""编排链路端到端验证（通用、可复跑、断言规则化）。

**为什么需要它**：编排是「多子任务 → 汇总 → 反思 → 聚合状态」的长链路，
此前只靠人工看页面/看库，失败表现为「HTTP 200 但零产物」，且**不可复现地依赖 LLM 判定**
（`_needs_orchestration` 末段是 LLM 复杂度判定）—— 旧脚本曾在**没进编排**的情况下
报「编排字段缺失」，把「测错路径」误读成「功能坏了」。

本脚本的三条设计原则：
  ① **判据不硬编码业务内容**：所有断言都是结构性/契约性的（字段存在性、三态取值域、
     信号与状态的蕴含关系），不校验任何具体模型、具体子任务标题。
  ② **测错路径必须响亮失败**：进入编排是**前提**，不是期望。未进编排 → 直接 FAIL
     并给出原因（LLM 判定为 False 或消息未命中规则信号），不继续跑后面的断言。
  ③ **离线可复跑**：`--check-only <conv_id>` 只读接口复核已落库卡片，不调 LLM、不写库。

读结果时务必分清「回归」与「既知未结项 / 历史数据」
--------------------------------------------------
  · **F1 禁用词失败 ≠ 编排代码回归**。F1 检的是**上下文污染**（子 Agent 被喂了别领域素材而
    拒绝产出）。实测 2026-09-19 根因在**配置与库内容**，不在本链路代码：
      ① `design` agent 的 `kb_scope.branches=["release"]`，而该分支装的是 54 条**演示实体**
         （巡飞弹体系）→ 与本任务领域无关的素材恒被命中；
      ② 默认项目宪法（`settings.default_project_id`）无条件注入，其内容是另一领域。
    这两个是**待产品拍板**的开放项（改范围 or 补库内容），不是能靠改代码"改通"的东西。
    因此 F1 默认**关闭**（`--forbid` 留空）；显式传入时才检，失败即如实报出并指向上述根因。
  · **C2 在历史会话上失败属正常**：本脚本断言的是**当前契约**。`quality_gate_gaps` 是
    P0-3 新增键，晚于它生成的旧会话卡片必然缺该键 → 用 `--check-only` 复核旧会话会 FAIL。
    这**不代表**现网回归；要判回归请用**改动之后**新建的会话（或用 --forbid 之外的全项）。

用法
----
    # 端到端（真实调用，耗时数分钟）
    <repo>\\.venv\\Scripts\\python.exe -X utf8 tools/verify/verify_orchestration_e2e.py

    # 指定消息 / 超时 / 禁用词（上下文隔离回归）
    ... --message "先分析……再生成……并校验" --timeout 1500 --forbid 巡飞弹,动力分系统

    # 只复核已落库的会话（零 LLM 开销，改完代码可反复跑）
    ... --check-only 358

    # 毫秒级静态哨兵（G0：模块别名遮蔽；零 LLM。改完编排代码先跑这个，别直接等 9 分钟）
    ... --static-only

每次 live / check-only 运行都会**先**跑 G0；G0 不过则**不进入** live（避免分钟级空跑）。

退出码：全绿 0 / 有失败 1 / 无法建立前置条件 2。
"""
import argparse
import ast
import glob
import json
import os
import re
import sys
import time

import httpx

# 默认消息：**故意用规则信号**（「先…再…」）而非让 LLM 判复杂度 —— 保证确定性进入编排。
# 见 agent/pipeline_parts/orchestration.py::_needs_orchestration 的规则分支。
DEFAULT_MESSAGE = "先分析电动汽车热管理系统需求，再生成 SysML V2 模型代码并进行校验"

VALID_STATUS = ("full", "partial", "failed")


class Ctx:
    def __init__(self):
        self.oks = []
        self.fails = []
        self.notes = []

    def ck(self, name, ok, detail=""):
        (self.oks if ok else self.fails).append(name)
        print(("  PASS  " if ok else "  FAIL  ") + name + (f"  | {detail}" if detail else ""))
        return ok

    def note(self, txt):
        self.notes.append(txt)
        print("  · " + txt)


def _own_scope_nodes(scope):
    """产出 scope **自身作用域**内的节点，不下钻到独立作用域（嵌套函数/类/推导式）。

    作用域精确性是必须的：`for x in ...` 在推导式里是**推导式局部**，不算遮蔽；
    同名绑定出现在嵌套函数里，也只让它在**那个**函数内变局部。
    """
    stack = list(scope.body)
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef,
                          ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _imported_names(scope):
    """scope 自身作用域内 `import` 绑定的名字（含 as 别名；`import a.b` 绑定 `a`）。"""
    out = set()
    for n in _own_scope_nodes(scope):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name != "*":
                    out.add(a.asname or a.name)
    return out


def _scope_bindings(scope):
    """scope 自身作用域内的**按源码顺序**的绑定：`[(行号, 名字, 是否 import 绑定)]`。

    包含 import 绑定（`Import` / `ImportFrom`）与赋值绑定（assign / ann / aug / for /
    with / except / 元组解包）。刻意**不含**函数形参：形参在入口即绑定，遮蔽模块名不会引发
    UnboundLocalError，属常见且合法写法（`def f(time):`），计入会变噪声。
    """
    out = []
    for n in _own_scope_nodes(scope):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.append((n.lineno, a.asname or a.name.split(".")[0], True))
            continue
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name != "*":
                    out.append((n.lineno, a.asname or a.name, True))
            continue
        if isinstance(n, ast.Assign):
            tgts = n.targets
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            tgts = [n.target]
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            tgts = [n.target]
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            tgts = [it.optional_vars for it in n.items if it.optional_vars]
        elif isinstance(n, ast.ExceptHandler):
            if n.name:
                out.append((n.lineno, n.name, False))
            continue
        else:
            continue
        for t in tgts:
            if t is None:
                continue
            if isinstance(t, ast.Starred):
                t = t.value
            if isinstance(t, ast.Name):
                out.append((n.lineno, t.id, False))
            elif isinstance(t, (ast.Tuple, ast.List)):
                for e in ast.walk(t):
                    if isinstance(e, ast.Name):
                        out.append((n.lineno, e.id, False))
    return out


def _scope_attr_uses(scope):
    """scope 自身作用域内 `name.attr` 的 (行号, name) —— 即「把某个名字当命名空间用」。"""
    out = []
    for n in _own_scope_nodes(scope):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            out.append((n.lineno, n.value.id))
    return out


def _shadow_sites(scope, imported_anywhere):
    """scope 内的「别名遮蔽」命中点（顺序敏感）。

    判据：对每个 `N.attr`，取**同作用域内源码顺序上最近的、行号 ≤ 该处的 N 绑定**；
    若该绑定是 import → 正常（例：`from core import config as _cfg` 紧接 `_cfg.get(...)`）；
    若是非 import 赋值 → 命中（例：`for _t in orch_tasks` 覆盖了顶部 `import time as _t`，
    之后 `_t.time()` 必抛 AttributeError）。若 N 在**本作用域**内无先行绑定，而它由
    模块级 import 提供 → 该处访问正常，不算遮蔽。

    「顺序敏感」这一维不可省：早期版本只看「有绑定 + 当模块用」，把
    `workflows/planner.py` 里 `_cfg` 的两处用法误判为缺陷（那处 L226 刚重新 import）。
    """
    binds = _scope_bindings(scope)
    hits = []
    for ln, nm in _scope_attr_uses(scope):
        if nm not in imported_anywhere:
            continue
        prev = [b for b in binds if b[1] == nm and b[0] <= ln]
        if prev and not max(prev, key=lambda b: b[0])[2]:      # 最近一次绑定是赋值 → 命中
            hits.append((ln, nm))
    return hits


def _static_alias_shadow(repo_root, roots):
    """G0 静态哨兵：`import X as a` / `from X import a` 绑定的名字，若在**同一作用域**内被
    赋值/循环绑定**覆盖**，且覆盖之后仍把它当命名空间用（`a.attr`）→ 命中。

    为什么值得单列一条（2026-09-19 实测代价很大）：`agent/pipeline_parts/stream.py` 的
    `_stream_orchestrated_flow` 顶部 `import time as _t`，而聚合段写了 `for _t in orch_tasks`
    → **把 time 模块重绑成任务字典**，函数尾 `_t.time()` 抛 AttributeError，被外层兜底
    静默回落单 Agent。表现是「编排跑了 543s、子任务全跑完，落库却是单 Agent 卡片」，
    端到端要 9 分钟才复现一次 —— 这类「别名遮蔽 → 整链静默失效」用静态检查秒级拦住。

    判据（结构性、与业务无关，见 `_shadow_sites`）：顺序敏感的「最近一次绑定是否为赋值」。
    作用域精确：不下钻嵌套函数/类/推导式（各自独立作用域，同名不算遮蔽）。
    实测 2026-09-19：本判据对 47 个文件 0 误报；放宽到「只看有绑定+当模块用」时误报 5 处
    （本地字典重名、可选依赖 `except ImportError: mod = None` 惯用写法、以及顺序正确的
    `from core import config as _cfg` 紧接 `_cfg.get(...)`），故保留顺序这一维。
    """
    hits, scanned = [], 0
    for root in roots:
        for path in sorted(glob.glob(os.path.join(repo_root, root, "**", "*.py"), recursive=True)):
            scanned += 1
            rel = os.path.relpath(path, repo_root)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError as e:            # 语法都不过 → 单独报，别混进遮蔽结论
                hits.append((rel, 0, "<SYNTAX>", str(e)))
                continue
            fns = [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            # 危险名集合：模块级 import ∪ 文件内任一处 import 绑定名。
            # 不区分二者是刻意的 —— 两种写法下「再绑定 + 当模块用」都是 bug：
            #   · 模块级别名被函数内绑定 → 该名在函数内变局部 → 之前的 `_t.time()` 直接 UnboundLocalError；
            #   · 函数自身 import 的别名被同函数内绑定 → 后段 `_t.time()` 抛 AttributeError（本次实测形态）。
            danger = _imported_names(tree)
            for fn in fns:
                danger |= _imported_names(fn)
            for scope, label in [(tree, "<模块>")] + [(f, f.name) for f in fns]:
                for ln, nm in _shadow_sites(scope, danger):
                    hits.append((rel, ln, label, nm))
    return hits, scanned


def _assert_static(cx, repo_root, roots):
    hits, scanned = _static_alias_shadow(repo_root, roots)
    cx.ck(f"G0 无「模块别名被遮蔽」（扫描 {scanned} 个文件）", not hits,
          "; ".join(f"{p}:{ln} {fn} 遮蔽 {nm}" for p, ln, fn, nm in hits[:5]))
    return not hits


def _stream(cx, base, message, user_id, timeout):
    """建立会话并消费 SSE；返回 (conv_id, card, stats)。"""
    r = httpx.post(f"{base}/api/conversations",
                   json={"title": "E2E-编排验证"}, timeout=30)
    r.raise_for_status()
    cid = r.json()["id"]
    print(f"conversation_id = {cid}")
    cx.note(f"消息：{message}")

    card, errors = None, []
    env_text = ""      # done 信封里的答复正文 —— F1 禁用词必须扫正文（污染先出现在正文里）
    ev_counts, last_ev, max_gap, last_t = {}, None, 0.0, time.time()
    done = False
    t0 = time.time()
    try:
        with httpx.stream("POST", f"{base}/api/conversations/{cid}/chat/stream",
                          json={"message": message},
                          headers={"X-User-Id": str(user_id)},
                          timeout=httpx.Timeout(30.0, read=float(timeout))) as resp:
            cx.ck("A1 SSE 建立（200 + text/event-stream）",
                  resp.status_code == 200 and "text/event-stream" in (resp.headers.get("content-type") or ""),
                  f"{resp.status_code} {resp.headers.get('content-type')}")
            et = None
            for line in resp.iter_lines():
                now = time.time()
                gap = now - last_t
                if gap > max_gap:
                    max_gap = gap
                last_t = now
                if line.startswith("event:"):
                    et = line[6:].strip()
                    last_ev = et
                    ev_counts[et] = ev_counts.get(et, 0) + 1
                elif line.startswith("data:") and et:
                    try:
                        d = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    if et == "error":
                        errors.append(d)
                    elif et == "done":
                        # ⚠️ done 事件的 `data` 是**外层信封**（message_id/content/card/plan/...），
                        # 卡片本体在 `data.card`。旧版直接取 `data` → 声称「orchestrated_status 缺失」
                        # 的假 FAIL（实测 2026-09-19：库里卡片明明有该键）。取不到 card 才退回信封。
                        _env = d.get("data") or {}
                        _c = _env.get("card")
                        card = _c if isinstance(_c, dict) else _env
                        env_text = str(_env.get("content") or "")
                        done = True
    except Exception as e:
        cx.note(f"流式中断：{type(e).__name__}: {e}（最后事件={last_ev}）")

    elapsed = time.time() - t0
    return cid, card, {"errors": errors, "counts": ev_counts,
                       "elapsed": elapsed, "max_gap": max_gap,
                       "last_ev": last_ev, "done": done, "text": env_text}


def _assert_card(cx, card, ev):
    """卡片契约与质量信号蕴含关系（结构性断言，与业务内容无关）。"""
    print("\n---- 编排卡字段 ----")
    print(json.dumps({k: card.get(k) for k in
                      ("intent", "agent", "orchestrated", "orchestrated_status",
                       "quality_gate_gaps", "degraded")}, ensure_ascii=False)[:400])

    # B：必须真的进了编排（测错路径要响亮失败）
    orch = card.get("orchestrated") is True
    cx.ck("B1 进入编排路径（orchestrated=True）", orch,
          "未进编排 → 本脚本的编排判据未被检验；请改用必然命中规则信号的消息"
          if not orch else str(card.get("orchestrated")))
    if not orch:
        return False
    plan = card.get("plan") or []
    cx.ck("B2 plan 非空且每个子任务带 status",
          bool(plan) and all(t.get("status") for t in plan), f"n={len(plan)}")

    # C：卡片契约（同一功能两条路径必须同键）
    st = card.get("orchestrated_status")
    cx.ck("C1 orchestrated_status ∈ {full,partial,failed}", st in VALID_STATUS, str(st))
    cx.ck("C2 quality_gate_gaps 键存在（与 orchestration.py 同键）",
          "quality_gate_gaps" in card and isinstance(card.get("quality_gate_gaps"), (list, type(None))),
          repr(card.get("quality_gate_gaps"))[:120])
    cx.ck("C3 reflection 键存在（dict 或 None）",
          "reflection" in card and isinstance(card.get("reflection"), (dict, type(None))),
          type(card.get("reflection")).__name__)

    # C4/C5：**状态必须与子任务成败自洽**（结构性不变式，与业务内容无关）。
    # 这条不变式是修 P0-3 时用端到端实测暴露出来的：当时 4 个子任务里 t2 failed、t3/t4 blocked，
    # 而卡片仍报 full —— 失败在 UI 上完全不可见。断言写在这里可防回归。
    _pst = [str(t.get("status") or "") for t in plan]
    _unfinished = [s for s in _pst if s and s != "done"]
    cx.ck("C4 存在未完成子任务 → 状态不得为 full（失败可见）",
          (not _unfinished) or st != "full",
          f"子任务状态={_pst} → status={st}")
    cx.ck("C5 子任务全 done → 状态不得为 failed（不过度降级）",
          bool(_unfinished) or st != "failed", f"{_pst} → {st}")

    # D：质量信号不得被吞（P0-3 核心：失败必须可见）
    ref = card.get("reflection") if isinstance(card.get("reflection"), dict) else None
    if ref is not None and ref.get("passed") is False:
        cx.ck("D1 反思未通过 → 状态降级（≠full，失败可见）", st != "full",
              f"passed=False, score={ref.get('score')}, status={st}")
    else:
        cx.note(f"D1 跳过：反思未判未通过（passed={ref.get('passed') if ref else None}）")
    if ref is not None and ref.get("passed") is True and plan and all(t.get("status") == "done" for t in plan):
        cx.ck("D2 反思通过 + 子任务全 done → 状态不被过度降级（=full）", st == "full", f"status={st}")
    else:
        cx.note("D2 跳过：前置不满足（反思未通过或子任务未全 done）")

    # A2/A3：链路完整性与无错误事件
    cx.ck("A2 收到 done 事件（无静默挂起）", bool(ev["done"]),
          f"最后事件={ev['last_ev']} 用时={ev['elapsed']:.1f}s 最大间隔={ev['max_gap']:.1f}s")
    cx.ck("A3 全链无 error 事件", not ev["errors"], json.dumps(ev["errors"], ensure_ascii=False)[:200])
    return True


def _assert_persist(cx, base, cid, card):
    """落库与流式卡片同源（写入路径不能丢字段）。"""
    try:
        d = httpx.get(f"{base}/api/conversations/{cid}/messages", timeout=30).json()
    except Exception as e:
        cx.ck("E1 落库可读", False, str(e))
        return
    msgs = d.get("messages") if isinstance(d, dict) else (d or [])
    roles = [m.get("role") for m in msgs]
    cx.ck("E1 落库 1 问 1 答", roles.count("user") >= 1 and roles.count("assistant") >= 1, str(roles))
    asst = [m for m in msgs if m.get("role") == "assistant"]
    if not asst:
        return
    cd = asst[-1].get("card_data")
    if isinstance(cd, str):
        try:
            cd = json.loads(cd)
        except Exception:
            cd = None
    if not isinstance(cd, dict):
        cx.ck("E2 落库卡片可解析", False, type(cd).__name__)
        return
    same = (cd.get("orchestrated_status") == card.get("orchestrated_status")
            and ("quality_gate_gaps" in cd))
    cx.ck("E2 落库卡片与流式卡片同源（状态+门禁键一致）", same,
          f"db={cd.get('orchestrated_status')} sse={card.get('orchestrated_status')} "
          f"gaps_key={'quality_gate_gaps' in cd}")


def _check_only(base, cid, forbid, cx=None):
    cx = cx or Ctx()
    d = httpx.get(f"{base}/api/conversations/{cid}/messages", timeout=30).json()
    msgs = d.get("messages") if isinstance(d, dict) else (d or [])
    asst = [m for m in msgs if m.get("role") == "assistant"]
    if not asst:
        print(f"会话 {cid} 无助手消息（零产物）→ 失败")
        return 2
    cd = asst[-1].get("card_data")
    if isinstance(cd, str):
        cd = json.loads(cd)
    ok = _assert_card(cx, cd or {}, {"errors": [], "done": True, "elapsed": 0,
                                     "max_gap": 0, "last_ev": "-", "counts": {}})
    cx.ck("E1 落库 1 问 1 答",
          [m.get("role") for m in msgs].count("user") >= 1, str([m.get("role") for m in msgs]))
    if forbid:
        txt = json.dumps(cd, ensure_ascii=False) + asst[-1].get("content", "")
        hits = [w for w in forbid if w and w in txt]
        cx.ck("F1 上下文隔离：禁用词未出现", not hits, str(hits))
    if cx.fails and forbid:
        cx.note("F1 命中≠编排代码回归：根因在 kb_scope.branches / 默认项目宪法（见脚本头部说明）")
    return 0 if not cx.fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--message", default=DEFAULT_MESSAGE)
    ap.add_argument("--user-id", default="1")
    ap.add_argument("--timeout", type=float, default=1500.0, help="SSE 单次读取超时（秒）")
    ap.add_argument("--forbid", default="", help="逗号分隔的禁用词（上下文隔离回归，可选）")
    ap.add_argument("--check-only", type=int, default=0, help="只复核已落库会话 id（零 LLM）")
    ap.add_argument("--scan-roots", default="agent,services,workflows",
                    help="G0 静态哨兵扫描根（逗号分隔，相对仓库根）")
    ap.add_argument("--static-only", action="store_true", help="只跑 G0 静态哨兵（毫秒级，零 LLM）")
    args = ap.parse_args()
    forbid = [w.strip() for w in (args.forbid or "").split(",") if w.strip()]
    roots = [r.strip() for r in (args.scan_roots or "").split(",") if r.strip()]
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    print("=" * 90)
    print("编排链路端到端验证")
    print(f"  base_url={args.base_url}  模式="
          f"{'static-only' if args.static_only else ('check-only' if args.check_only else 'live')}")

    cx = Ctx()
    _assert_static(cx, repo_root, roots)
    if args.static_only:
        print("\n" + "=" * 90)
        print(f"断言汇总：{len(cx.oks)}/{len(cx.oks) + len(cx.fails)} 通过")
        for f in cx.fails:
            print("  失败：" + f)
        print("=" * 90)
        return 0 if not cx.fails else 1
    if cx.fails:
        # 静态哨兵不过 → 编排**必然**在运行期失败（别名被遮蔽会把整链兜底掉）。
        # 直接返回，别白等 9 分钟；修完再跑。
        print("\n静态哨兵未通过 → 不进入 live（省一次分钟级空跑）。先修静态问题。")
        print("=" * 90)
        return 1

    if args.check_only:
        rc = _check_only(args.base_url, args.check_only, forbid, cx)
        print("\n" + "=" * 90)
        print(f"断言汇总：{len(cx.oks)}/{len(cx.oks) + len(cx.fails)} 通过")
        for f in cx.fails:
            print("  失败：" + f)
        print("=" * 90)
        return rc

    cid, card, ev = _stream(cx, args.base_url, args.message, args.user_id, args.timeout)
    print(f"\n事件分布：{json.dumps(ev['counts'], ensure_ascii=False)}")
    print(f"总耗时 {ev['elapsed']:.1f}s  最大事件间隔 {ev['max_gap']:.1f}s")
    if card is None:
        cx.ck("A0 拿到最终卡片", False, f"未收到 done；最后事件={ev['last_ev']}")
        print("=" * 90)
        return 1

    ok = _assert_card(cx, card, ev)
    if ok:
        _assert_persist(cx, args.base_url, cid, card)

    if forbid:
        # 扫「卡片 + 答复正文」：污染既可能出现在子任务轨迹里，也可能出现在最终答复里
        # （会话 351 实测：正文写「工作记忆里的活跃实体是动力分系统、巡飞弹等，与电动汽车热管理无关」）。
        txt = json.dumps(card, ensure_ascii=False) + "\n" + ev.get("text", "")
        hits = [w for w in forbid if w in txt]
        cx.ck("F1 上下文隔离：禁用词未出现", not hits, str(hits))
        if hits:
            cx.note("F1 命中≠编排代码回归：根因在 kb_scope.branches / 默认项目宪法（见脚本头部说明）")

    print("\n" + "=" * 90)
    print(f"断言汇总：{len(cx.oks)}/{len(cx.oks) + len(cx.fails)} 通过"
          f"（会话 id={cid}，可用 --check-only {cid} 离线复跑）")
    for f in cx.fails:
        print("  失败：" + f)
    print("=" * 90)
    return 0 if not cx.fails else 1


if __name__ == "__main__":
    sys.exit(main())
