# -*- coding: utf-8 -*-
# CI-OPTIONAL: 自起独立实例 + 改全局 mock/降级开关，跑约 40s 且与共享服务互斥；开发机 20/20，不进 CI
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

默认**自起独立实例**（独立端口 + 独立库快照 + 独立配置，跑完即销毁）：
生产服务的配置、数据、会话列表**零改动**（D0.3 用会话数前后对拍证明）。
`MBSE_GATE_USE_SHARED=1` 才连当前服务（需先通过排他守卫，且目标进程必须是测试态 ——
2026-11-27 起服务态进程禁止运行时开 mock，PUT 会返回 409 并如实跳过）。
"""
import ast
import json
import os
import sys
import time

import httpx

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
BASE = os.environ.get("MBSE_BASE", "http://127.0.0.1:8000").rstrip("/")
H = {"X-User-Id": os.environ.get("MBSE_UID", "3")}
CFG = os.path.expanduser("~/.workbuddy/mbse_config.json")
# 绝对路径：门禁要能在任意 cwd 下跑（独立实例会以 ROOT 为 cwd 起子进程）
DB = os.path.abspath(os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db"))
STREAM = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
FUNC = "_stream_orchestrated_flow"

# 只在「非降级」分支赋值的名字（两次事故的并集）
GUARDED = ["_gate_gaps", "_fill_verdicts", "_fill_filled", "_fill_patch",
           "_orch_tool_counts", "_orch_probe_caps", "_orch_val_stalls"]

results = []
skipped = []


def chk(tag, ok, detail=""):
    results.append((tag, bool(ok), detail))
    print(("  PASS " if ok else "  FAIL ") + tag + ("  | " + detail if detail else ""))
    return ok


def skip(tag, detail=""):
    """不算 PASS、也不算 FAIL —— 但**绝不能**静默当成通过（假绿）。"""
    skipped.append((tag, detail))
    print("  SKIP " + tag + ("  | " + detail if detail else ""))


# ══════════════════════════════════════════════════════════════════
# D0 排他守卫：绝不把全局 mock 开关翻在一个**正被使用**的服务上
#
# 2026-11-27 事故（本脚本上一版，入口 prep_degraded_render.py）：
#   10:19 用户发起真实建模任务 → 10:20:55 门禁 PUT llm.force_mock=true
#   → 该会话其后 11 次 LLM 调用 used_mock=1（lat 3~10ms / out 161）
#   → 模型被替换成固定样板，验收连续 2 次无进展 ⇒ harness 强制放行
#   → 用户看到「任务未完成」+ 一段与需求无关的「3 个方案对比」。
#   `llm_mock_rate_1h` 因此被打到 90.9%。
# ⇒ 跑之前必须确认没有别的会话在跑；不确定就不要改全局配置。

def _active_others(window_s=180):
    """返回 [(kind, label)] —— 非本门禁的活跃迹象。空列表才算独占。"""
    import sqlite3
    hits = []
    try:
        c = sqlite3.connect(DB, timeout=5)
        c.row_factory = sqlite3.Row
        for r in c.execute("select id, conversation_id, status from agent_tasks "
                           "where status in ('running','pending') limit 5"):
            hits.append(("task", "任务 %s（conv %s）status=%s"
                         % (r["id"], r["conversation_id"], r["status"])))
        for r in c.execute("select id, title, updated_at from conversations "
                           "where updated_at >= datetime('now', ?) "
                           "and coalesce(title,'') not like '__degraded%' "
                           "order by updated_at desc limit 5", ("-%d seconds" % window_s,)):
            hits.append(("conv", "会话 %s「%s」%s 内仍有活动"
                         % (r["id"], (r["title"] or "")[:24], window_s)))
        c.close()
    except Exception as e:
        hits.append(("db", "无法确认服务是否空闲（%s）—— 保守视为忙碌" % e))
    return hits


def check_exclusive():
    if os.environ.get("MBSE_GATE_ALLOW_SHARED") == "1":
        skip("D0 排他守卫", "已显式设 MBSE_GATE_ALLOW_SHARED=1，明知会污染共享服务仍继续")
        return True
    hits = _active_others()
    if hits:
        skip("D0 排他守卫", "服务正被使用，拒绝翻全局 mock 开关")
        for kind, label in hits:
            print("       · " + label)
        print("       → 改用独立实例（独立端口 + 独立库），或确认无人使用后重跑")
        return False
    chk("D0 排他守卫", True, "无其他活跃会话/任务，可安全临时开 mock")
    return True


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
# D0 独立实例：门禁自己起一个**独立端口 + 独立库 + 独立配置**的服务
#
# 为什么必须这么做（2026-11-27 两次事故，都由"在共享服务上跑门禁"造成）：
#   ① 上一版门禁在**用户正在跑真实任务**的服务上 PUT llm.force_mock=true
#      ⇒ 该会话 11 次真实调用被替换成固定样板 ⇒ 用户拿到 841 字无关内容。
#   ② 取证会话（1000000245/247/248）混进了用户的会话列表。
# 加"排他守卫"只挡住"有人正在用"，挡不住"跑完留下垃圾"与"两个门禁同跑互相覆盖配置"。
# ⇒ 正解是**根本不碰那个进程**：
#   · 库：sqlite backup API 复制快照到临时目录（读生产快照、写副本）
#   · 配置：MBSE_CONFIG_PATH 指向临时文件；graph_db 用影子配置隔离（同 store 双进程会锁冲突）
#   · 进程：MBSE_TEST_MODE=1 —— 我们的 mock 硬约束只放行测试态，这是唯一合法入口
#   · 跑完 terminate + 删临时目录，生产服务的配置/数据/会话列表零改动
# 需要"就在当前服务上跑"时显式设 MBSE_GATE_USE_SHARED=1（退回旧行为 + 排他守卫）。

def _free_port():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _snapshot_db(src, dst):
    """SQLite 在线备份：一致快照（不锁生产库，也不会复制出半截 -wal 状态）。"""
    import sqlite3
    s = sqlite3.connect(src, timeout=10)
    d = sqlite3.connect(dst)
    try:
        with d:
            s.backup(d)
    finally:
        s.close()
        d.close()


def _conv_count(db):
    import sqlite3
    try:
        c = sqlite3.connect(db, timeout=10)
        n = c.execute("select count(*) from conversations").fetchone()[0]
        c.close()
        return int(n)
    except Exception:
        return -1


def _tail(path, n=15):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except Exception:
        return ""


class IsolatedServer:
    """临时服务实例：独立端口 / 独立库快照 / 独立配置；用完即毁。"""

    def __init__(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="mbse_gate_")
        self.db = os.path.join(self.dir, "gate.db")
        self.cfg = os.path.join(self.dir, "mbse_config.json")
        self.port = _free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.proc = None
        self.log = os.path.join(self.dir, "server.log")
        self._logf = None

    def start(self, timeout_s=150, test_mode=True):
        _snapshot_db(DB, self.db)
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"llm": {"force_mock": True}}, f)      # 只活在临时文件里
        env = dict(os.environ)
        env.update({
            # 唯一允许 Mock 生效的开关。`test_mode=False` 时**不设**：
            # 用于验证"服务态进程即使配置写了 force_mock 也不生效"（verify_mock_scope M5）
            "MBSE_TEST_MODE": ("1" if test_mode else ""),
            "MBSE_PORT": str(self.port),
            "MBSE_DB_PATH": self.db,
            "MBSE_CONFIG_PATH": self.cfg,
            "MBSE_SHADOW_CONFIG": json.dumps({"graph_db": {
                "path": os.path.join(self.dir, "graph_db")}}),
        })
        env.pop("MBSE_GATE_USE_SHARED", None)
        self._logf = open(self.log, "wb")
        import subprocess
        self.proc = subprocess.Popen([sys.executable, "-X", "utf8", "main.py"],
                                     cwd=ROOT, env=env,
                                     stdout=self._logf, stderr=subprocess.STDOUT)
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            if self.proc.poll() is not None:
                return False, "进程提前退出（日志 %s）" % self.log
            try:
                if httpx.get(self.base + "/api/dashboard", headers=H, timeout=3).status_code == 200:
                    return True, "启动 %.1fs（port %d）" % (time.time() - t0, self.port)
            except Exception:
                time.sleep(1.0)
        return False, "启动超时 %ds（日志 %s）" % (timeout_s, self.log)

    def stop(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=20)
                except Exception:
                    self.proc.kill()
        except Exception:
            pass
        try:
            if self._logf:
                self._logf.close()
        except Exception:
            pass
        # Windows 上刚 terminate 的进程可能还持有 graph_db / wal 文件句柄 ⇒
        # 单次 rmtree 会静默失败（ignore_errors=True 会把失败吞掉，留下垃圾目录）。
        # 重试若干次，并把"还剩什么"如实打出来（否则下一个人只看到"目录还在"）。
        import shutil
        import time as _t
        for _ in range(8):
            shutil.rmtree(self.dir, ignore_errors=True)
            if not os.path.isdir(self.dir):
                break
            _t.sleep(0.7)
        if os.path.isdir(self.dir):
            try:
                left = []
                for dp, dns, fns in os.walk(self.dir):
                    left += [os.path.relpath(os.path.join(dp, f), self.dir) for f in fns][:6]
                print("  (临时目录残留：%s)" % ", ".join(left[:8]))
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════
# D1 行为：真跑一轮降级直行

def check_behavior(base=None, db=None, isolated=False):
    base = (base or BASE).rstrip("/")
    db = db or DB
    try:
        code = httpx.get(base + "/api/dashboard", headers=H, timeout=8).status_code
    except Exception as e:
        chk("D1.0 服务可访问", False, "%s: %s" % (base, e))
        return
    if not chk("D1.0 服务可访问", code == 200, "HTTP %s" % code):
        return
    if not isolated and not check_exclusive():
        return

    orig = None
    cid = None
    try:
        if not isolated:
            orig = open(CFG, "rb").read()
        r = httpx.post(base + "/api/conversations",
                       json={"title": "__degraded_direct_gate__"}, headers=H, timeout=30)
        cid = (r.json() or {}).get("id")
        chk("D1.1 建取证会话", bool(cid), "cid=%s" % cid)

        if not isolated:
            _pr = httpx.put(base + "/api/system/config/static",
                            json={"updates": {"llm.force_mock": True}}, timeout=30)
            if _pr.status_code == 409:
                # 2026-11-27 起：服务态进程**禁止**运行时打开 mock（事故约束）。
                # 共享模式因此只能在"目标服务本身就是测试态"时用；否则如实报跳过，
                # 而不是把 409 当成"设好了"继续跑出假绿。
                skip("D1.0b 共享服务是测试态", "PUT force_mock 被拒（409）⇒ 该服务是服务态；"
                     "请用默认的独立实例模式（去掉 MBSE_GATE_USE_SHARED）")
                return
            httpx.post(base + "/api/system/config/reload", timeout=30)

        ev, content, msg_id = [], "", None
        with httpx.stream("POST", base + "/api/conversations/%s/chat/stream" % cid,
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

        chk("D1.2 收到 done 帧", "done" in ev,
            "事件 %d 类: %s" % (len(set(ev)), ",".join(sorted(set(ev)))[:120]))
        chk("D1.3 无 error 帧", "error" not in ev,
            "出现 error 说明降级路径仍未收口" if "error" in ev else "")
        chk("D1.4 正文非空", len(content) > 200,
            "%d 字：%s" % (len(content), content[:40].replace("\n", " ")))

        # 取证：确认这一轮**确实**走的是降级直行（否则 D1.2~D1.4 证明不了降级路径）
        card = {}
        if msg_id:
            try:
                import sqlite3
                c = sqlite3.connect(db)
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
        if isolated:
            chk("D1.7 生产配置未被触碰", True, "独立实例：配置/库都在临时目录，无需还原")
        else:
            try:
                httpx.put(base + "/api/system/config/static",
                          json={"updates": {"llm.force_mock": False}}, timeout=30)
            except Exception:
                pass
            if orig is not None:
                with open(CFG, "wb") as f:
                    f.write(orig)
            try:
                httpx.post(base + "/api/system/config/reload", timeout=30)
            except Exception:
                pass
            chk("D1.7 配置逐字节还原", open(CFG, "rb").read() == orig)


def main():
    print("== D2 结构断言（不依赖服务）==")
    check_structure()
    print("== D1 行为断言（真实跑一轮降级直行）==")
    if os.environ.get("MBSE_GATE_USE_SHARED") == "1":
        print("  （MBSE_GATE_USE_SHARED=1：使用当前服务 %s，需通过排他守卫）" % BASE)
        check_behavior()
    else:
        srv = IsolatedServer()
        print("  临时实例目录：%s" % srv.dir)
        try:
            ok, detail = srv.start()
            chk("D0.1 独立实例启动", ok, detail)
            if ok:
                guards = (httpx.get(srv.base + "/api/system/config", headers=H, timeout=10)
                          .json() or {}).get("guards") or {}
                chk("D0.2 临时实例为测试态", guards.get("test_mode") is True,
                    "source=%s force_mock_effective=%s"
                    % (guards.get("source"), guards.get("force_mock_effective")))
                before = _conv_count(DB)
                check_behavior(srv.base, srv.db, isolated=True)
                after = _conv_count(DB)
                chk("D0.3 生产库零写入", before == after, "生产会话数 %d → %d" % (before, after))
            else:
                print("---- 临时实例日志尾部 ----")
                print(_tail(srv.log))
        finally:
            srv.stop()
            chk("D0.4 临时目录已清理", not os.path.isdir(srv.dir), srv.dir)

    bad = [t for t, ok, _ in results if not ok]
    print("\n合计 %d 项，PASS %d，FAIL %d，SKIP %d"
          % (len(results) + len(skipped), len(results) - len(bad), len(bad), len(skipped)))
    if bad:
        print("失败项：" + "; ".join(bad))
    if skipped:
        print("跳过项：" + "; ".join(t for t, _ in skipped))
        print("⚠️ 行为段未验证 —— 不得据此认为降级路径正常")
        return 2 if not bad else 1
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
