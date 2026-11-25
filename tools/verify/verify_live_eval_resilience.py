# -*- coding: utf-8 -*-
# CI-OPTIONAL: [local] tier=local（见 tools/gates_manifest.json）：live 跑批抗 429：Retry-After 退避复用会话 + 单例异常隔离 + partial 增量落盘。实现是纯内存夹具（0.5s），但脚本名含 live_ 触发清单 G8 的「pr 层不得依赖服务」启发式，且 pr 层还须同步改 ci.yml（G6a）⇒ 登记 local，避免为一条快门禁改 CI 面
"""live 跑批抗 429 / 抗单例崩溃门禁（2026-11-17 真机事故驱动）。

## 事故

`run_arch_ab.py run --mode live --repeat 3` 跑到第 15 个会话时被服务返回
`HTTP Error 429: Too Many Requests`（服务 chat 桶默认 5 次/分钟，eval 客户端
**匿名**与真人聊天共用 `anon` 桶）。原实现让 HTTPError 一路冒出 `main`：
  · **3 小时进度全丢**（15 个真实会话的评分、conv_map、延迟 token 全不见）；
  · 产物只在**全部**用例跑完后写一次 ⇒ 没有任何可恢复的进度快照；
  · 一个用例失败就炸掉整批 ⇒ 12 例 × 3 重复中任何一例抖动都前功尽弃。

## 修复（三处）

  1. `_live_case`：429 按服务端 `Retry-After` 退避重试（上限 3 次），
     **复用同一个会话** —— 重建会污染 conv_map「每例一个新会话」判据；
  2. `run_batch`：live 单例异常写成同形错误行（`invalid_reason='live_error'`、
     score=0/passed=False、latency/token=None 不编数字），继续跑后面的用例；
  3. 每跑完一例增量落 `<out>.partial`（可恢复进度快照，**不是**正式报告）。

## 断言分组

  A. 代码级：重试上限 / Retry-After / per-case try / partial 接线
  B. 行为级：假 opener 先 429 后成功 ⇒ 重试成功且复用会话；一直 429 ⇒ 按上限抛
  C. 行为级：单例异常不炸全批，剩余用例照跑且错误行同形、partial 落盘

用法：``.venv/Scripts/python.exe tools/verify/verify_live_eval_resilience.py``
退出码：0=全绿 / 1=有失败
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import urllib.error

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools", "eval"))

import run_arch_ab as ra  # noqa: E402

FAILS: list = []
N = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global N
    N += 1
    if cond:
        print("  [OK]   %s" % name)
    else:
        print("  [FAIL] %s%s" % (name, ("  —— " + detail) if detail else ""))
        FAILS.append(name)


CASE = {"id": "G01", "intent": "design", "input_preview": "生成活动视图代码",
        "input_source": {"message_id": 1}, "checks": []}
DATASET = {"cases": [CASE, dict(CASE, id="G02"), dict(CASE, id="G03")],
           "dataset_id": "0123456789abcdef", "version": "v0"}


class _FakeConn:
    def execute(self, sql, args=()):
        class _R:
            def fetchone(self_inner):
                return {0: 0, "content": "生成活动视图代码：含异常分支"}
        return _R()

    def close(self):
        pass


class _Resp:
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return iter(self._lines)

    def __exit__(self, *a):
        return False


class _Opener:
    """第 1 次抛 429（带 Retry-After），之后返回 done 帧。"""

    def __init__(self, *, fail_times=1):
        self.calls = 0
        self.fail_times = fail_times
        self.urls = []
        self.headers = []

    def open(self, req, timeout=None):
        self.calls += 1
        self.urls.append(req.full_url)
        self.headers.append(dict(req.headers))
        if self.calls <= self.fail_times:
            raise urllib.error.HTTPError(
                req.full_url, 429, "Too Many Requests",
                {"Retry-After": "1"}, io.BytesIO(b"{}"))
        return _Resp([b'data: {"event":"done"}\n\n'])


class _Ctx:
    """可回滚的全局桩：退出时一定恢复，避免污染同进程的其它门禁。"""

    def __init__(self):
        self.saved = {}

    def set(self, obj, name, val):
        self.saved[(obj, name)] = getattr(obj, name)
        setattr(obj, name, val)

    def close(self):
        for (obj, name), val in reversed(list(self.saved.items())):
            setattr(obj, name, val)
        self.saved.clear()


def _stub_all(ctx):
    ctx.set(ra, "_ro_conn", lambda: _FakeConn())
    ctx.set(ra, "_http_json",
            lambda opener, url, payload, timeout=60: {"id": 999999900 + len(CASES_SEEN)})
    ctx.set(ra.G, "score_case",
            lambda c, case, cid=None: {"case_id": case["id"], "score": 1.0,
                                       "passed": True, "checks": []})
    ctx.set(time, "sleep", lambda s: None)      # 退避不真睡（判据是次数，不是时长）
    CASES_SEEN.clear()


CASES_SEEN = []


def case_code() -> None:
    print("\nA. 代码级")
    src = io.open(os.path.join(REPO, "tools", "eval", "run_arch_ab.py"),
                  encoding="utf-8").read()
    check("A1 429 重试上限存在且 >0", "MAX_429_RETRY" in src
          and "MAX_429_RETRY = 3" in src, "未找到")
    check("A2 尊重服务端 Retry-After", 'headers.get("Retry-After")' in src,
          "未读 Retry-After")
    check("A3 429 退避重试复用同一会话（不在 except 里重建 req）",
          src.find("except urllib.error.HTTPError") < src.find("res = G.score_case"),
          "重试位置异常")
    check("A4 live 单例异常隔离（invalid_reason='live_error'）",
          '"invalid_reason": "live_error"' in src, "未隔离")
    check("A5 每例增量落 partial", "_write_partial(" in src
          and src.count("_write_partial(") >= 3, "partial 未接线")
    check("A6 CLI 把 out_path 传进 run_batch", "out_path=args.out" in src, "未传")
    check("A7 错误行不编 latency/token",
          '"latency_ms": None, "tokens": None' in src, "编了数字")
    check("A8 ★ eval 有独立限流身份（不再与真人共用 anon 桶）",
          "def _eval_headers()" in src and '"X-User-Id": "0"' in src
          and "_headers.update(_eval_headers())" in src, "未分桶")
    check("A9 强制登录开启时不伪造身份（认证优先于分桶）",
          '_c.as_bool("auth", "enforce_login", False)' in src, "无保护")


def case_retry() -> None:
    print("\nB. 行为级：429 退避重试")
    ctx = _Ctx()
    _stub_all(ctx)
    try:
        op = _Opener(fail_times=1)
        r = ra._live_case(op, "http://127.0.0.1:8000", CASE, 30)
        check("B1 先 429 后成功 ⇒ 最终 sse_done=True", r.get("sse_done") is True,
              str(r.get("sse_done")))
        check("B2 open 恰好调 2 次（1 次 429 + 1 次成功）", op.calls == 2,
              "calls=%d" % op.calls)
        check("B3 ★ 重试复用同一会话 URL（不新建会话污染 conv_map）",
              len(set(op.urls)) == 1 and "/chat/stream" in op.urls[0], str(op.urls))
        check("B4 重试成功后本例正常通过（不因曾 429 误判失败）", r.get("passed") is True,
              str(r.get("passed")))
        check("B6 ★ 建会话与 chat 两个请求都带 eval 分桶头",
              len(op.headers) == 2 and all(h.get("X-user-id") == "0" for h in op.headers),
              str(op.headers))  # retry 成功后本例正常通过（桩 score_case=1.0）

        op2 = _Opener(fail_times=99)
        try:
            ra._live_case(op2, "http://127.0.0.1:8000", CASE, 30)
            check("B5 一直 429 ⇒ 最终抛出（不无限重试）", False, "竟然成功了")
        except urllib.error.HTTPError:
            check("B5 一直 429 ⇒ 最终抛出（不无限重试）",
                  op2.calls == ra.MAX_429_RETRY + 1,
                  "calls=%d 上限=%d" % (op2.calls, ra.MAX_429_RETRY))
    finally:
        ctx.close()


def case_batch() -> None:
    print("\nC. 行为级：单例失败不炸全批 + partial 增量落盘")
    ctx = _Ctx()
    _stub_all(ctx)
    tmpdir = tempfile.mkdtemp(prefix="ab_resilience_")
    out = os.path.join(tmpdir, "ab.json")
    try:
        _orig = ra._live_case
        state = {"n": 0}

        def _flaky(opener, base_url, case, timeout_s):
            state["n"] += 1
            if case["id"] == "G02":
                raise RuntimeError("夹具：模拟单例 502")
            return {"case_id": case["id"], "score": 1.0, "passed": True,
                    "conv_id": 999999800 + state["n"], "checks": [],
                    "sse_done": True, "latency_ms": 1, "tokens": 1}

        ra._live_case = _flaky
        rep = ra.run_batch(DATASET, "A", "夹具", 1, "live", {}, None,
                           "http://127.0.0.1:8000", 30, out_path=out)
        cases = rep["runs"][0]["cases"]
        check("C1 单例异常未炸整批（3 例都跑完）", len(cases) == 3, str(len(cases)))
        bad = [x for x in cases if x["case_id"] == "G02"][0]
        check("C2 错误行同形且如实（score=0/passed=False）",
              bad["score"] == 0.0 and bad["passed"] is False, str(bad))
        check("C3 错误行带可归因 invalid_reason",
              bad.get("invalid_reason") == "live_error", str(bad.get("invalid_reason")))
        check("C4 错误行不编 latency/token",
              bad.get("latency_ms") is None and bad.get("tokens") is None, str(bad))
        check("C5 其它用例照常通过",
              sum(1 for x in cases if x["passed"]) == 2, str([x["passed"] for x in cases]))
        _pf = out + ".partial"
        check("C6 ★ partial 已落盘（3 小时进度不再全丢）", os.path.exists(_pf), _pf)
        if os.path.exists(_pf):
            p = json.load(io.open(_pf, encoding="utf-8"))
            check("C7 partial 记了完成数与 conv_map（可追溯跑到哪）",
                  p.get("completed_cases") == 3 and isinstance(p.get("conv_map"), dict)
                  and p.get("phase") == "running", str({k: p.get(k) for k in
                                                        ("phase", "completed_cases", "conv_map")}))
    finally:
        ra._live_case = _orig if "_orig" in dir() else ra._live_case
        ctx.close()



def case_resume() -> None:
    print("\nD. 行为级：从 .partial 续跑（不重烧已完成用例）")
    ctx = _Ctx()
    _stub_all(ctx)
    tmpdir = tempfile.mkdtemp(prefix="ab_resume_")
    out = os.path.join(tmpdir, "ab.json")
    try:
        state = {"dataset_id": DATASET["dataset_id"], "mode": "live",
                 "runs": [{"repeat_index": 1, "cases": [
                     {"case_id": "G01", "repeat_index": 1, "score": 1.0,
                      "passed": True, "sse_done": True, "conv_id": 8888001,
                      "checks": [], "latency_ms": 100, "tokens": 10},
                     {"case_id": "G02", "repeat_index": 1, "score": 0.0,
                      "passed": False, "sse_done": False,
                      "invalid_reason": "sse_done_missing", "conv_id": 8888002,
                      "checks": [], "latency_ms": None, "tokens": None},
                 ]}]}
        calls = []

        def _counted(opener, base_url, case, timeout_s):
            calls.append(case["id"])
            return {"case_id": case["id"], "score": 1.0, "passed": True,
                    "conv_id": 8888000 + len(calls), "checks": [],
                    "sse_done": True, "latency_ms": 1, "tokens": 1}

        ra._live_case = _counted
        rep = ra.run_batch(DATASET, "A", "夹具续跑", 1, "live", {}, None,
                           "http://127.0.0.1:8000", 30, out_path=out,
                           resume_state=state)
        check("D1 ★ 已完成用例不重跑（只补 2 例）",
              sorted(calls) == ["G02", "G03"], str(calls))
        cases = rep["runs"][0]["cases"]
        check("D2 最终报告仍是完整 3 例（可交 golden_e2e）",
              len(cases) == 3 and {c["case_id"] for c in cases} == {"G01", "G02", "G03"},
              str([c["case_id"] for c in cases]))
        check("D3 作废样本被重跑并换成有效结果",
              [c for c in cases if c["case_id"] == "G02"][0]["sse_done"] is True,
              str([c for c in cases if c["case_id"] == "G02"][0]))
        check("D4 conv_map 包含续用的旧会话",
              rep["conv_map"].get("G01") == 8888001, str(rep["conv_map"]))

        # 指纹漂移必须拒绝（防止拿别的评测集合并成基线）
        bad = dict(state, dataset_id="ffffffffffffffff")
        try:
            ra.run_batch(DATASET, "A", "夹具", 1, "live", {}, None,
                         "http://127.0.0.1:8000", 30, resume_state=bad)
            check("D5 指纹不一致 ⇒ 拒绝续跑", False, "竟然接受了")
        except ValueError as e:
            check("D5 指纹不一致 ⇒ 拒绝续跑", "dataset_id" in str(e), str(e))

        # D6：中途快照的重复 run 段必须被去重（否则下一次 resume 会重复计分）
        dup_path = os.path.join(tmpdir, "dup.json")
        c1 = {"case_id": "G01", "repeat_index": 3, "sse_done": True,
              "passed": True, "conv_id": 1, "score": 1.0, "checks": [],
              "latency_ms": 1, "tokens": 1}
        runs = [{"repeat_index": 3, "cases": [c1]},
                {"repeat_index": 3, "cases": [c1]}]     # 模拟中途快照拼接
        ra._write_partial(dup_path, runs, {}, variant="A", label="x",
                          mode="live", dataset=DATASET, total_cases=12)
        d = json.load(io.open(dup_path + ".partial", encoding="utf-8"))
        keys = [(c.get("case_id"), c.get("repeat_index"))
                for r in d.get("runs", []) for c in r.get("cases", [])]
        check("D6 ★ 快照按 repeat 去重（防止下次续跑重复计分）",
              len(keys) == len(set(keys)) == 1 and len(d.get("runs") or []) == 1,
              str(d.get("runs")))

        # D7：从含重复段的旧快照续跑，最终报告也必须只有一份
        def _done(case_id, rep, cid):
            return {"case_id": case_id, "repeat_index": rep, "sse_done": True,
                    "passed": True, "conv_id": cid, "score": 1.0, "checks": [],
                    "latency_ms": 1, "tokens": 1}
        d7 = {"dataset_id": DATASET["dataset_id"], "mode": "live",
              "runs": [{"repeat_index": 1, "cases": [_done("G01", 1, 1),
                                                     _done("G02", 1, 2), _done("G03", 1, 3)]},
                       {"repeat_index": 2, "cases": [_done("G01", 2, 4),
                                                     _done("G02", 2, 5), _done("G03", 2, 6)]},
                       {"repeat_index": 3, "cases": [c1]},
                       {"repeat_index": 3, "cases": [c1]}]}   # 旧 runner 的重复段
        calls2 = []
        def _counted2(opener, base_url, case, timeout_s):
            calls2.append((case["id"], 3))
            return {"case_id": case["id"], "score": 1.0, "passed": True,
                    "conv_id": 9000000 + len(calls2), "checks": [],
                    "sse_done": True, "latency_ms": 1, "tokens": 1}
        ra._live_case = _counted2
        rep2 = ra.run_batch(DATASET, "A", "夹具", 3, "live", {}, None,
                            "http://127.0.0.1:8000", 30, resume_state=d7)
        k2 = [(c.get("case_id"), c.get("repeat_index"))
              for r in rep2["runs"] for c in r["cases"]]
        check("D7 ★ 从重复段快照续跑，最终 9 例各一份且只补 2 例",
              len(k2) == len(set(k2)) == 9 and sorted(calls2) == [("G02", 3), ("G03", 3)],
              str((len(k2), len(set(k2)), calls2)))
    finally:
        ctx.close()


def main() -> int:
    print("live 跑批抗 429 / 抗单例崩溃门禁")
    print("=" * 68)
    case_code()
    case_retry()
    case_batch()
    case_resume()
    print("\n" + "=" * 68)
    if FAILS:
        print("结论：%d/%d 通过，%d 条失败：" % (N - len(FAILS), N, len(FAILS)))
        for f in FAILS:
            print("  - %s" % f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
