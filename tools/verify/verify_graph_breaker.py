# -*- coding: utf-8 -*-
"""verify_graph_breaker.py —— 守卫：图谱（Fuseki）不可用时必须短熔断，不得逐次白等。

## 背景（2026-11-11 真实多轮验证实测）

真实 LLM 多轮验证（4 轮，DeepSeek-V3）实测：
    首token 延迟 = **61.5s / 44.8s / 53.8s**（模型本身直连只要 **0.4s**）
服务日志同期刷屏：
    graph_db.fuseki.query_sparql 失败: Fuseki HTTP 502: upstream connect failed:
    由于目标计算机积极拒绝，无法连接。 (os error 10061)   ← **一条对话里出现数十次**

根因：`FusekiClient.query_sparql` 的 `timeout=15` 且**失败只记日志、照常返回 []**，
没有任何熔断 ⇒ 一次对话的几十次图谱查询**每次都白等满 15s** 叠加。

⇒ 与 embedding 的 `_UNAVAIL_BLOCK_*`（P1-34）同款处置。同一类问题在两个依赖上
各犯一次，说明**缺少统一的「外部依赖不可用」处置规范**（记入方案待办）。

## 不变式

  I1 基础设施类错误（连接被拒/超时/502/503/504）⇒ 触发短熔断
  I2 熔断期内 `query_sparql` **直接返回 []**，一次网络往返都不打
  I3 熔断期返回 [] 而非抛异常 ⇒ 调用方走向量/词面兜底，**流程不中断**
     （米爸口径：不能因外部依赖故障让产品流程走不通）
  I4 查询成功后**立刻解除**熔断（不等窗口过期，服务恢复即可用）
  I5 **SPARQL 语法错（HTTP 400）不熔断** —— 那是查询本身的问题，
     熔断它会把"一条查询写错"放大成"整个图谱 60 秒不可用"

## 变异自证

  M1 去掉熔断（还原旧写法）⇒ I1/I2 必须判红
  M2 熔断期抛异常而非返回 [] ⇒ I3 必须判红
  M3 把 400 也算作不可用 ⇒ I5 必须判红

用法： ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_graph_breaker.py
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_results = []


def check(name, ok, detail=""):
    _results.append(bool(ok))
    print("[%s] %s%s" % ("OK  " if ok else "FAIL", name, ("　— " + detail) if detail and not ok else ""))


CONN_REFUSED = ("Fuseki HTTP 502: upstream connect failed: "
                "由于目标计算机积极拒绝，无法连接。 (os error 10061)")
TIMEOUT_ERR = "Fuseki HTTP 502: upstream connect failed: context timed out"


def make_client():
    #类名是 `FusekiWriter`（`graph_db.py:416`，继承 `GraphDBWriter`），
    # 熔断逻辑挂在它的 `query_sparql`（:539）上——日志里那句
    # `graph_db.fuseki.query_sparql 失败` 就是它打的。
    from graph_db import FusekiWriter
    return FusekiWriter(endpoint="http://127.0.0.1:3030/ns")


def main():
    print("=== 图谱不可用短熔断守卫 ===")
    cli = make_client()
    cls = type(cli)

    # I5 前置：语法错**不得**触发熔断
    cls._UNAVAIL_BLOCK_UNTIL = 0.0
    cli._note_unavail(RuntimeError("Fuseki HTTP 400: Parse error in query"))
    check("[I5] SPARQL 语法错（HTTP 400）不触发熔断",
          not cli.unavail_blocked(), "语法错被当成基础设施故障了")

    # I1：基础设施类错误触发熔断
    cls._UNAVAIL_BLOCK_UNTIL = 0.0
    cli._note_unavail(RuntimeError(CONN_REFUSED))
    check("[I1] 连接被拒（os error 10061）触发短熔断", cli.unavail_blocked())
    cls._UNAVAIL_BLOCK_UNTIL = 0.0
    cli._note_unavail(RuntimeError(TIMEOUT_ERR))
    check("[I1b] 超时触发短熔断", cli.unavail_blocked())
    cls._UNAVAIL_BLOCK_UNTIL = 0.0
    cli._note_unavail(RuntimeError("Fuseki HTTP 503: Service Unavailable"))
    check("[I1c] HTTP 503 触发短熔断", cli.unavail_blocked())

    # I2 + I3：熔断期内**不逐次白等**，且返回 [] 而不抛
    #   ⚠️ 判据口径修正（首版写成"零网络往返"，把**探测口**也判成违规）：
    #   熔断期必须留一个探测口（每`_PROBE_INTERVAL` 打一次真实请求探活），
    #   否则"服务已恢复"永远无法被感知 ⇒ 要等满 60s 窗口才能恢复。
    #   真正的契约是：**N 次查询最多打 1 次网络**（而非 N 次各白等 15s）。
    cls._UNAVAIL_BLOCK_UNTIL = 0.0
    cli._note_unavail(RuntimeError(CONN_REFUSED))
    calls = {"n": 0}

    def _boom2(q, limit=50):
        calls["n"] += 1
        raise RuntimeError(CONN_REFUSED)
    cli._query = _boom2
    t0 = time.time()
    raised = None
    rows = None
    try:
        for _ in range(10):                      # 连打 10 次，模拟一次对话里的几十次图谱查询
            rows = cli.query_sparql("SELECT ?s WHERE { ?s ?p ?o }")
    except Exception as e:                      # noqa: BLE001
        raised = e
    dt = time.time() - t0
    check("[I2] 10 次查询最多打 1 次网络（不逐次白等 15s）", calls["n"] <= 1,
          "打了 %d 次（每次 15s ⇒ 用户等 %ds）" % (calls["n"], calls["n"] * 15))
    check("[I3] 熔断期返回 [] 而不抛异常 ⇒ 调用方可兜底、流程不中断",
          raised is None and rows == [], "raised=%r rows=%r" % (raised, rows))
    check("[I3b] 熔断期返回是瞬时的（<1s，非 15s 超时）", dt < 1.0, "耗时 %.2fs" % dt)

    # I4：探测成功 ⇒ 立刻解除（不必等窗口过期）
    cls._UNAVAIL_BLOCK_UNTIL = time.time() + 60
    cls._last_probe_at = 0.0                      # 允许本轮就探
    cli._query = lambda q, limit=50: [{"s": "http://x"}]      # 服务已恢复
    got = cli.query_sparql("SELECT ?s WHERE { ?s ?p ?o }")
    check("[I4] 探测成功后立刻解除熔断（不等窗口过期）",
          bool(got) and not cli.unavail_blocked(),
          "got=%d blocked=%s" % (len(got or []), cli.unavail_blocked()))
    # 探测口有频率限制：紧接着再打一次不该走网络
    calls3 = {"n": 0}

    def _count3(q, limit=50):
        calls3["n"] += 1
        raise RuntimeError(CONN_REFUSED)
    cli._query = _count3
    cli._note_unavail(RuntimeError(CONN_REFUSED))
    cli._last_probe_at = time.time()             # 刚探过
    for _ in range(5):
        cli.query_sparql("SELECT ?s WHERE { ?s ?p ?o }")
    check("[I4c] 探测口有频率限制（5 次查询 0 次网络）", calls3["n"] == 0,
          "打了 %d 次" % calls3["n"])

    # 窗口长度合理性
    cls._UNAVAIL_BLOCK_UNTIL = 0.0
    cli._note_unavail(RuntimeError(CONN_REFUSED))
    remain = cls._UNAVAIL_BLOCK_UNTIL - time.time()
    check("[I4b] 熔断窗口 30~90s（短窗口：服务恢复后不至于久废）",
          30 <= remain <= 90, "窗口 %.0fs" % remain)
    cls._UNAVAIL_BLOCK_UNTIL = 0.0

    # ── 变异自证 ──────────────────────────────────────────────
    print("\n=== 变异自证 ===")
    import types
    orig_query = type(cli).query_sparql

    # M1：还原旧写法（无熔断）
    def _no_breaker(self, query, limit=50):
        try:
            return self._query(query, limit)
        except Exception as e:
            return []
    type(cli).query_sparql = _no_breaker
    cls._UNAVAIL_BLOCK_UNTIL = 0.0
    calls2 = {"n": 0}

    def _boom(q, limit=50):
        calls2["n"] += 1
        raise RuntimeError(CONN_REFUSED)
    cli._query = _boom
    for _ in range(5):
        cli.query_sparql("SELECT ?s WHERE { ?s ?p ?o }")
    check("[M1] 去掉熔断 ⇒ I2 判据必须翻红（5 次查询应打 5 次网络）",
          calls2["n"] == 5, "守卫空转（实际打了 %d 次）" % calls2["n"])
    type(cli).query_sparql = orig_query

    # M2：熔断期抛异常
    def _raise_in_breaker(self, query, limit=50):
        if self.unavail_blocked():
            raise RuntimeError("图谱不可用")
        return self._query(query, limit)
    type(cli).query_sparql = _raise_in_breaker
    cls._UNAVAIL_BLOCK_UNTIL = time.time() + 60
    raised2 = False
    try:
        cli.query_sparql("SELECT ?s WHERE { ?s ?p ?o }")
    except Exception:
        raised2 = True
    check("[M2] 熔断期抛异常 ⇒ I3 判据必须翻红", raised2, "守卫空转")
    type(cli).query_sparql = orig_query

    # M3：把 400 也当不可用
    cls._UNAVAIL_BLOCK_UNTIL = 0.0
    orig_note = type(cli)._note_unavail

    def _bad_note(self, exc):
        try:
            type(self)._UNAVAIL_BLOCK_UNTIL = time.time() + 60
        except Exception:
            pass
    type(cli)._note_unavail = _bad_note
    cli._query = lambda q, limit=50: (_ for _ in ()).throw(RuntimeError("Fuseki HTTP 400: Parse error"))
    cli.query_sparql("SELECT ?s WHERE { ?s ?p ?o }")
    check("[M3] 把 400 当不可用 ⇒ I5 判据必须翻红", cli.unavail_blocked(), "守卫空转")
    type(cli)._note_unavail = orig_note
    cls._UNAVAIL_BLOCK_UNTIL = 0.0

    n_fail = sum(1 for r in _results if not r)
    print("\n%d pass / %d fail" % (len(_results) - n_fail, n_fail))
    return 1 if n_fail else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())