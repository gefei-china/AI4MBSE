# -*- coding: utf-8 -*-
# CI-OPTIONAL: A+B 真库金标 + 运行中的 MCP 服务（2026-10-18 干净库实测：库中不存在 zhiyuan-dev-mcp、registry 取不到主 Agent、tools 0 个——三态都依赖生产库 MCP 注册与在线服务）
"""MCP 可用性链路验证（2026-10-16）。

背景（米爸报「MCP 为啥不可用，我在 WorkBuddy 绑定都可以使用」）
--------------------------------------------------------------
排查结论：**MCP 服务端一直是通的**，问题在 mbse_system 侧的三段链路：

① 后台健康巡检**从未成功跑过一次**。`main.py` 的 factory 传的是
   `lambda: get_db()`（sqlite3.Connection），而 `check_all()` 要调
   `repo.rows(...)` / `repo.update_mcp_status(...)` ⇒ 每轮 AttributeError，
   又被 `_loop` 的 `except Exception: pass` 吞掉 ⇒ 服务正常、日志无异常，
   但所有 MCP 的 `status`/`last_check` 永远停在旧值（实测停在 2026-10-14 08:25）。
② `zhiyuan-dev-mcp`(id=161) 实测能握手（online, 2551ms），库里却是**僵尸 offline**。
③ `AgentRepo.bound_tools_for()` 对 mcp 要求 `status=='online' and enabled`
   ⇒ 僵尸 offline 让绑定被丢弃 ⇒ 主 Agent 取不到 MCP ⇒「MCP 不可用」的假象。

另修一处隐患：`studio_repo.set_mcp_enabled` 末尾有悬空的 `self._sync(...)`
（缩进错位），会使该方法引用未定义的 `sid`。

上下线语义（回答「上线/下线怎么控制」）
--------------------------------------
  · `enabled=1/0`  —— **人工**开关（POST /api/studio/mcp-servers/{id}/enable|disable），
                      停用后不注入 Agent、不参与能力发现
  · `status`       —— **探测结果**（由巡检/测试写入），不是人工开关
  · 消费门禁      —— `enabled=1` **且** `status='online'`（两者都要满足）

判据
----
C1 巡检 factory 给的是 repo 对象（不是裸连接）      → 修掉根因
C2 check_all 用 repo 能跑通（本轮循环体不抛异常）   → 证明巡检真能工作
C3 全部 sse/http MCP 探测结果与库里 status 一致      → 无僵尸状态
C4 set_mcp_enabled 可正常调用（悬空语句已清）        → 上下线开关本身可用
C5 主 Agent 绑定的 MCP 能被取到                       → 消费链路通
C6 _loop 失败不再静默（有日志出口）                   → 故障不再隐身
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN_AGENT = "智源对接agent"
MAIN_AGENT_MCP = "zhiyuan-dev-mcp"

_fails: list[str] = []
_passes = 0


def chk(name: str, ok: bool, detail: str = "") -> None:
    global _passes
    if ok:
        _passes += 1
        print(f"  [PASS] {name}" + (f"  — {detail}" if detail else ""))
    else:
        _fails.append(name)
        print(f"  [FAIL] {name}  — {detail}")


def section(t: str) -> None:
    print(f"\n{'=' * 70}\n{t}\n{'=' * 70}")


def _tools_count(conn, sid: int) -> int:
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT tools FROM mcp_servers WHERE id=?", (sid,)).fetchone()
    try:
        return len(json.loads(r["tools"] or "[]")) if r else 0
    except Exception:
        return 0


def main() -> int:
    sys.path.insert(0, str(ROOT))
    # ⚠️ 必须用项目 .venv：mcp_client 依赖 httpx，系统 Python 没有
    #   （曾用错解释器，两个 MCP 都报 "No module named 'httpx'" 的假 offline）
    from database import get_db
    from mcp_health import check_all, check_server, _norm_transport
    from repositories.studio_repo import StudioRepo

    section("C1 · 巡检 factory 必须是 repo 对象（根因）")
    mp = (ROOT / "main.py").read_text(encoding="utf-8", errors="ignore")
    # 只看**可执行代码**行：注释里为了说明改前写法会含 `lambda: get_db()` 字面量，
    # 直接全字匹配会假红（第一版就栽在这）。
    code = "\n".join(re.sub(r"^\s*#.*$", "", ln) for ln in mp.splitlines())
    chk("main.py 传的是 StudioRepo(get_db()) 而非裸 get_db()",
        "StudioRepo(get_db())" in code.replace("_StudioRepo", "StudioRepo"),
        "factory 传裸连接 ⇒ check_all 每轮 AttributeError ⇒ 巡检从未成功")
    chk("main.py 巡检启动失败有日志出口（不再静默 pass）",
        "MCP 健康巡检启动失败" in mp)

    section("C1b · 巡检循环必须 commit（第二根因）")
    mh = (ROOT / "mcp_health.py").read_text(encoding="utf-8", errors="ignore")
    _loop_src = mh[mh.find("def _loop"): mh.find("t = threading.Thread")]
    chk("_loop 在 close 前显式 commit",
        "conn.commit()" in _loop_src,
        "不 commit ⇒ 写完就 close ⇒ 事务回滚 ⇒ 库里永不更新")
    chk("_loop 每轮关闭连接（防句柄泄漏）",
        "close()" in _loop_src)

    section("C2 · check_all 用 repo 能跑通（本轮循环体不抛异常）")
    # ⚠️ repo 的连接必须在**本段内提交并关闭**：C4 会在同一 repo 上写 enabled，
    #   不提交就关闭前一直持有 SQLite 写锁 ⇒ 后续 C7 巡检的 commit 被阻塞到超时
    #   （实测：16~20s 窗口内 last_check 毫无变化，且因 except 吞掉而无任何报错）。
    repo = StudioRepo(get_db())
    try:
        t0 = time.time()
        check_all(repo)                       # 真实执行一轮巡检
        chk("check_all 未抛异常（巡检真能工作）", True,
            f"耗时 {int((time.time()-t0)*1000)}ms")
    except Exception as e:
        chk("check_all 未抛异常（巡检真能工作）", False,
            f"{type(e).__name__}: {e}")

    section("C3 · 探测结果与库里 status 一致（无僵尸状态）")
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM mcp_servers").fetchall()
    for r in rows:
        srv = dict(r)
        if _norm_transport(srv.get("transport")) not in ("sse", "http"):
            continue
        ok, detail = check_server(srv, repo=None)     # 不落库，纯探测
        db_status = r["status"]
        chk(f"{r['name']}：实测 online ⇒ 库内也 online",
            (not ok) or db_status == "online",
            f"实测={'online' if ok else 'offline'} / 库内={db_status}"
            + ("" if ok or db_status != "online" else "（不一致）"))
        if not ok:
            print(f"         · 该服务器探测失败：{json.dumps(detail, ensure_ascii=False)[:160]}")
    conn.close()

    section("C4 · set_mcp_enabled 可正常调用（悬空语句已清）")
    # 用**独立的连接**并在本段内提交+关闭：与 C2 的 repo 隔离，
    # 避免未提交事务泄漏到 C7 阻塞巡检落库（实测踩过）。
    _c4 = get_db()
    _c4.row_factory = sqlite3.Row
    sid = _c4.execute("SELECT id FROM mcp_servers WHERE name=?", (MAIN_AGENT_MCP,)).fetchone()
    if sid:
        sid = sid["id"]
        try:
            StudioRepo(_c4).set_mcp_enabled(sid, 1)   # 幂等：本来就是 1
            _c4.commit()
            cur = _c4.execute("SELECT enabled FROM mcp_servers WHERE id=?", (sid,)).fetchone()
            chk("set_mcp_enabled 正常执行且 enabled 仍为 1",
                cur["enabled"] == 1, f"sid={sid}")
        except Exception as e:
            chk("set_mcp_enabled 正常执行且 enabled 仍为 1", False,
                f"{type(e).__name__}: {e}（悬空 self._sync 未清？）")
        finally:
            _c4.close()
    else:
        chk(f"找到 MCP {MAIN_AGENT_MCP}", False, "库中不存在")
        _c4.close()
    # C2 的 repo 连接到此为止全部用完：提交并关闭，把写锁交还给后续巡检
    try:
        if hasattr(repo, "conn") and repo.conn is not None:
            repo.conn.commit()
            repo.conn.close()
    except Exception:
        pass

    section("C5 · 主 Agent 绑定的 MCP 能被取到（消费链路）")
    from agent.pipeline import AgentPipeline
    p = AgentPipeline()
    p._load_db_agents()
    d = p.registry.get(MAIN_AGENT)
    chk("registry 取到主 Agent", d is not None and d.intent_name == MAIN_AGENT)
    bound = p.registry.get_bound_mcp(MAIN_AGENT)
    print(f"         · get_bound_mcp = {[(b.get('name'), b.get('type')) for b in bound]}")
    chk("主 Agent 绑定的 MCP 进入工具清单",
        any(b.get("name") == MAIN_AGENT_MCP for b in bound),
        "为空 ⇒ 检查该 MCP 的 enabled 与 status（需 enabled=1 且 online）")

    section("C6 · 巡检失败不再静默（有日志出口）")
    mh = (ROOT / "mcp_health.py").read_text(encoding="utf-8", errors="ignore")
    chk("_loop 不再裸 except: pass", "except Exception:\n                pass" not in mh)
    chk("失败有 print 出口（[mcp-health] 前缀）", "[mcp-health]" in mh)

    section("C7 · 巡检循环**持续**工作（不是只跑一次）")
    # 用 6s 间隔起一个独立循环观察 16s：last_check 必须自动前移。
    # 只验「能跑通」不够 —— 事故形态正是「跑通了却从不落库/从不更新」。
    from mcp_health import start_health_loop

    def _probe161() -> dict:
        _c = get_db()
        _c.row_factory = sqlite3.Row
        _r = _c.execute("SELECT status, last_check, latency_ms FROM mcp_servers WHERE id=?",
                        (161,)).fetchone()
        _c.close()
        return dict(_r) if _r else {}

    _b = _probe161()
    _t = start_health_loop(lambda: StudioRepo(get_db()), interval_sec=3.0)
    # ⚠️ 观察窗口必须 > 2×interval：只睡 1×interval 会与时序巧合相撞 ——
    # 服务进程自己的巡检（300s）可能刚好在 before 之前写过库，导致 1 轮内读不到变化
    # （实测栽过一次：16s/6s 间隔读到"无变化"的假红）。
    # 另外 latency 也在变，可作为第二判据（不依赖秒级时间戳精度）。
    _lat0 = _b.get("latency_ms")
    time.sleep(20)
    _a = _probe161()
    chk("巡检线程存活", _t.is_alive())
    chk("last_check 随时间自动前移（循环真的在转）",
        _b.get("last_check") != _a.get("last_check"),
        f"{_b.get('last_check')} → {_a.get('last_check')}")
    chk("库内 status 为 online（非僵尸 offline）",
        _a.get("status") == "online", f"status={_a.get('status')}")
    chk("库内 tools 非空（动态发现已落库）",
        _tools_count(get_db(), 161) > 0, f"{_tools_count(get_db(), 161)} 个工具")

    section("结果")
    print(f"  通过 {_passes} 项，失败 {len(_fails)} 项")
    if _fails:
        for f in _fails:
            print(f"    · {f}")
        return 1
    print("  全部通过 ✓  MCP 链路正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())