"""P0-2：架构变更级 A/B 跑批器（对标 Anthropic「一次只改一个变量 + 逐任务回归清单」）。

为什么不是 run_eval.py 加个参数：run_eval.py 的 --cfg 是**单参数、检索层**的进程内 A/B；
架构变更（合并 Subagent / 换提示词分层 / 改编排拓扑）不是一个 cfg 能表达的，
它发生在「两次运行之间」。本脚本负责把这两次运行**固定其余条件**并逐例对比。

四个子命令：
  run            跑一遍（A 或 B），每例重复 --repeat 次（默认 3），产出 run JSON
  compare        对比两份 run JSON → 平均分 + **逐任务变化清单** + 延迟 + token + 每例通过率
  self-test      确定性自证（含变异），不需要服务/LLM/网络
  live-preflight 用桩端到端验证 live 路径的代码（含变异），不需要服务/LLM/网络

⚠️ live-preflight 验证的是「live 路径代码本身」，不是「真实 LLM 跑出了基线」。
   后者仍需起服务跑 --mode live 并落 eval_reports；两者不可互相替代（方案 §13）。

★ 机器化的三条纪律：
  1. 单变量：compare 校验 dataset_id / mode / repeat 一致，且 A、B 的 cfg_overrides
     差异**恰好 1 项**（或 0 项 + 由 label 声明的变量）；不一致直接拒绝比较。
  2. 重跑：repeat < 3 拒绝比较（文章客服案例的漏问交付日期只在 2 次里出现 1 次）。
  3. 不只看平均分：只要存在「A 某次通过 → B 同次失败」的用例就 verdict=blocked
     且 exit 2；这类回归在平均分上常常被其它用例的波动抵消。

⚠️ 反污染：offline 模式不调用 LLM、不写生产统计，latency/token 记为 None（**不编数字**）；
   live 模式才真跑，且统计按 llm_usage_stats 的 **id 基线**（不是时间窗口，见 §P1-22）。
⚠️ 架构变量由调用方在两次 run 之间切换；脚本本身不改 DB、不改代码、不改 override。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/eval/run_arch_ab.py run --variant A --label "合并前" --repeat 3 --out tmp/ab_A.json
  # ... 在这里只切换一个变量 ...
  ./.venv/Scripts/python.exe -X utf8 tools/eval/run_arch_ab.py run --variant B --label "合并后" --repeat 3 --out tmp/ab_B.json
  ./.venv/Scripts/python.exe -X utf8 tools/eval/run_arch_ab.py compare --a tmp/ab_A.json --b tmp/ab_B.json --out tmp/ab_compare.json
  ./.venv/Scripts/python.exe -X utf8 tools/eval/run_arch_ab.py self-test
"""
import argparse
import copy
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, _HERE)

import golden_e2e as G  # noqa: E402

RUN_SCHEMA = "p0/arch-ab-run@1"
CMP_SCHEMA = "p0/arch-ab-compare@1"

#: live 跑批遇到 HTTP 429 的重试上限（2026-11-17 真机事故驱动）。
#: 服务 chat 桶默认 5 次/分钟（core/rate_limit.py），而 eval 客户端**匿名**请求
#: 与真人聊天共用 `anon` 桶 —— 一次长跑批中途撞上别人的聊天就 429。
#: 原实现让 HTTPError 直接冒出 main ⇒ **3 小时进度、全部用例一起丢**（实测
#: 15 个会话后全灭，且产物只在最后一次性写）。尊重 Retry-After 退避重试。
MAX_429_RETRY = 3

DEFAULT_SET = G.DEFAULT_SET
DB = os.path.join(ROOT, "mbse.db")
MIN_REPEAT = 3


def _utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ro_conn():
    c = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    return c


def _cfg_key(case_id, rep):
    return "%s#%d" % (case_id, rep)


# ── run ─────────────────────────────────────────────────────────────────────
def _case_input(case):
    """live 重放：按 message_id 从真库取**完整原文**（数据集只存预览）。"""
    c = _ro_conn()
    try:
        r = c.execute("SELECT content FROM messages WHERE id=?",
                      (case["input_source"]["message_id"],)).fetchone()
        return str(r[0]) if r else case["input_preview"]
    finally:
        c.close()


def _no_proxy_opener():
    """本机 httpx/urllib 会走代理连 127.0.0.1（§2 坑 17）→ 显式禁代理。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _eval_headers() -> dict:
    """给 eval 请求一个**独立限流桶**，避免与真人聊天抢 5 次/分钟的 anon 桶。

    2026-11-17 真机事故：长跑批第 15 例撞 429 的根因就是 `core.rate_limit._identity`
    对无凭据请求一律归 `anon`，而 eval 客户端从不带身份头 —— 真人正在聊天时
    eval 就被挤爆。这里用 `X-User-Id: 0` 只做**分桶**：
      · `rate_limit._identity` 取 uid ⇒ 落到 `u:0`，与 `anon`/真实用户彻底分开；
      · `deps.current_user` 查 uid=0 查不到任何用户 ⇒ **仍是匿名**，
        当前 `auth.enforce_login=False` 下与不带头的行为逐字节一致（不提权）。
    ⚠️ 若将来开启强制登录，则**不带头**并明示告警 —— 那时 eval 必须走正式
       登录凭据，而不是继续用一个不存在的 uid 冒充（严格优先级：认证 > 分桶）。
    """
    try:
        from core import config as _c
        if _c.as_bool("auth", "enforce_login", False):
            print("!! auth.enforce_login=True：eval 不使用匿名分桶头；"
                  "请先配置正式凭据，否则本次跑批可能与真人共用限流桶")
            return {}
    except Exception:
        return {}
    return {"X-User-Id": "0"}


def _http_json(opener, url, payload, timeout=60):
    _headers = {"Content-Type": "application/json"}
    _headers.update(_eval_headers())
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=_headers, method="POST")
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _live_case(opener, base_url, case, timeout_s):
    """真实链路：建会话 → 发完整原文（SSE）→ 用同一套判据评新会话。"""
    import time
    t0 = time.time()
    conv = _http_json(opener, base_url + "/api/conversations",
                      {"title": "GoldenSet %s %s" % (case["id"], _utc()),
                       "intent": case["intent"]}, timeout=30)
    cid = int(conv.get("id") or conv.get("conversation_id") or 0)
    if not cid:
        raise RuntimeError("建会话失败：%s" % str(conv)[:200])
    c0 = _ro_conn()
    try:
        id_base = c0.execute("SELECT COALESCE(MAX(id),0) FROM llm_usage_stats").fetchone()[0]
    finally:
        c0.close()
    _headers = {"Content-Type": "application/json"}
    _headers.update(_eval_headers())
    req = urllib.request.Request(
        base_url + "/api/conversations/%d/chat/stream" % cid,
        data=json.dumps({"message": _case_input(case)}, ensure_ascii=False).encode("utf-8"),
        headers=_headers, method="POST")
    done = False
    # ★ 2026-11-17：429 退避重试。**复用同一个会话**（会话已建好、评分也按 cid），
    #    不要重新建会话 —— 那会污染 conv_map 的"每例一个新会话"判据。
    #    Retry-After 由服务端给出（core/rate_limit.py 的标准响应头），
    #    取不到时按 5/10/15 秒递增，上限 120 秒（不让退避本身变成卡死）。
    _retry = 0
    while True:
        try:
            with opener.open(req, timeout=timeout_s) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        ev = json.loads(line[6:])
                    except Exception:
                        continue
                    if ev.get("event") == "done" or ev.get("type") == "done":
                        done = True
                        break
            break
        except urllib.error.HTTPError as _he:
            if int(getattr(_he, "code", 0)) != 429 or _retry >= MAX_429_RETRY:
                raise
            _retry += 1
            try:
                _wait = max(1, min(120, int(_he.headers.get("Retry-After") or 0)
                                   or (5 * _retry)))
            except Exception:
                _wait = 5 * _retry
            time.sleep(_wait)
    c = _ro_conn()
    try:
        res = G.score_case(c, case, cid)
        tok = c.execute("SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) "
                        "FROM llm_usage_stats WHERE id>?", (id_base,)).fetchone()[0]
    finally:
        c.close()
    res["conv_id"] = cid
    res["latency_ms"] = int((time.time() - t0) * 1000)
    res["tokens"] = int(tok or 0)
    res["sse_done"] = done
    if not done:
        # ★ P1 前置修复：断流（没收到 done 终态帧）不得当作有效样本静默进基线。
        # done 帧是服务端唯一终态哨兵（routers/conversations.py:804-811）；
        # 缺失意味着生成器中途结束 / 异常被吞。此时评分既可能是「空会话」假红，
        # 也可能恰好命中历史残留 ⇒ 一律作废，并留可归因的失败判据。
        # 变异自证：live-preflight 的 M1 抽掉 done 帧，本分支必须把它判无效。
        res["passed"] = False
        res["score"] = 0.0
        res["checks"] = list(res.get("checks") or []) + [{
            "type": "sse_done_required", "passed": False,
            "evidence": "SSE 流未收到 done 终态帧（可能断流 / 异常被吞），本例作废",
        }]
        res["invalid_reason"] = "sse_done_missing"
    return res


def _write_partial(out_path: str, all_runs: list, conv_hist: dict, *,
                   variant: str, label: str, mode: str, dataset: dict,
                   total_cases: int) -> None:
    """每跑完一例就增量落盘 `<out>.partial`（2026-11-17 事故驱动）。

    为什么必须增量：原实现只在**全部**用例跑完后写一次 out —— 实测第 15 例撞 429
    后 main 抛栈退出，3 小时进度 + 15 个真实会话的评分**全部丢失**，
    连"跑到哪、哪些 conv 已换新"都无法追溯。partial 不是正式报告
    （不能拿去宣称基线），只是**可恢复的进度快照**。
    """
    if not out_path or mode != "live":
        return
    try:
        cmap = {}
        for cid, hist in conv_hist.items():
            picked = [x for x in hist if x[1]] or hist
            if picked:
                cmap[cid] = picked[-1][0]
        payload = {"phase": "running", "schema": RUN_SCHEMA + "-partial",
                   "variant": variant, "label": label, "mode": mode,
                   "dataset_id": dataset.get("dataset_id", ""),
                   "completed_cases": sum(len(x["cases"]) for x in all_runs),
                   "total_cases": int(total_cases), "conv_map": cmap,
                   "runs": all_runs, "generated_at_utc": _utc()}
        with open(str(out_path) + ".partial", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
    except Exception:
        pass        # 观测设施故障不得变成业务故障（与本仓门禁同一纪律）


def run_batch(dataset, variant, label, repeat, mode, cfg_overrides,
             opener=None, base_url="", timeout_s=900, out_path: str = ""):
    c = _ro_conn()
    conv_hist = {}  # case_id -> [(conv_id, passed), ...]；live 下据此生成 conv_map
    all_runs = []
    try:
        _total = repeat * len(dataset.get("cases") or [])
        for rep in range(1, repeat + 1):
            cases = []
            for case in dataset["cases"]:
                try:
                    if mode == "live":
                        r = _live_case(opener, base_url, case, timeout_s)
                        if r.get("conv_id"):
                            conv_hist.setdefault(case["id"], []).append(
                                (int(r["conv_id"]), bool(r.get("passed"))))
                    else:
                        r = G.score_case(c, case)
                        # offline 是确定性回放：不编延迟/token，也不假装是独立采样
                        r["latency_ms"] = None
                        r["tokens"] = None
                except Exception as _e:
                    # ★ live 单例失败**不得炸掉整批**：与 `_run_verify` 同纪律 ——
                    #   失败只是一种"不通过"，不是崩溃。写成同形错误行继续跑，
                    #   分数 0 / passed=False 如实计入，不编造 latency/token。
                    if mode != "live":
                        raise
                    r = {"case_id": case["id"], "score": 0.0, "passed": False,
                         "latency_ms": None, "tokens": None, "sse_done": False,
                         "invalid_reason": "live_error",
                         "error": "%s: %s" % (type(_e).__name__, str(_e)[:200]),
                         "checks": []}
                r["repeat_index"] = rep
                cases.append(r)
                _write_partial(out_path, all_runs + [{"repeat_index": rep, "cases": cases}],
                               conv_hist, variant=variant, label=label, mode=mode,
                               dataset=dataset, total_cases=_total)
            all_runs.append({"repeat_index": rep, "cases": cases})
    finally:
        c.close()
    _write_partial(out_path, all_runs, conv_hist,
                   variant=variant, label=label, mode=mode,
                   dataset=dataset, total_cases=_total)
    flat = [x for run in all_runs for x in run["cases"]]
    per_case = {}
    for case in dataset["cases"]:
        cs = [x for x in flat if x["case_id"] == case["id"]]
        lat = [x["latency_ms"] for x in cs if x["latency_ms"] is not None]
        tok = [x["tokens"] for x in cs if x["tokens"] is not None]
        per_case[case["id"]] = {
            "n": len(cs), "pass_count": sum(1 for x in cs if x["passed"]),
            "pass_rate": round(sum(1 for x in cs if x["passed"]) / max(len(cs), 1), 4),
            "mean_score": round(sum(x["score"] for x in cs) / max(len(cs), 1), 4),
            "mean_latency_ms": round(sum(lat) / len(lat), 1) if lat else None,
            "mean_tokens": round(sum(tok) / len(tok), 1) if tok else None,
        }
    # live 基线落库闭环：golden_e2e.py --conv-map 要的就是 case_id→conv_id。
    # 同例多轮时优先取「最后一次通过的」，全都没通过则取最后一次（如实反映）。
    conv_map = {}
    for cid, hist in conv_hist.items():
        picked = [x for x in hist if x[1]] or hist
        conv_map[cid] = picked[-1][0]
    return {
        "schema": RUN_SCHEMA, "variant": variant, "label": label, "mode": mode,
        "conv_map": conv_map,
        "dataset_id": dataset["dataset_id"], "dataset_version": dataset["version"],
        "repeat": repeat, "cfg_overrides": cfg_overrides, "generated_at_utc": _utc(),
        "runs": all_runs, "per_case": per_case,
        "summary": {
            "mean_score": round(sum(x["score"] for x in flat) / max(len(flat), 1), 4),
            "pass_rate": round(sum(1 for x in flat if x["passed"]) / max(len(flat), 1), 4),
            "mean_latency_ms": (round(sum(x["mean_latency_ms"] for x in per_case.values()
                                          if x["mean_latency_ms"] is not None) /
                                     max(sum(1 for x in per_case.values()
                                             if x["mean_latency_ms"] is not None), 1), 1)
                                if any(x["mean_latency_ms"] is not None for x in per_case.values())
                                else None),
            "total_tokens": sum(x["tokens"] for x in flat if x["tokens"] is not None) or None,
        },
        "limitations": G.LIMITATIONS + [
            "offline 模式的 repeat 是确定性复现，不构成独立采样；真实重跑请用 --mode live",
            "架构变量由调用方在两次 run 之间切换，本脚本不改 DB/代码/override",
        ],
    }


# ── compare ─────────────────────────────────────────────────────────────────
def _cfg_diff(a, b):
    """返回 A/B 之间不同的变量清单（点路径 → (A值,B值)）。"""
    ka, kb = a.get("cfg_overrides") or {}, b.get("cfg_overrides") or {}
    keys = sorted(set(ka) | set(kb))
    return {k: [ka.get(k), kb.get(k)] for k in keys if ka.get(k) != kb.get(k)}


def compare_runs(a, b, min_repeat=MIN_REPEAT, include_per_case=True):
    """纯函数：两份 run → 逐任务变化清单。include_per_case=False 是变异开关（只看平均分）。"""
    errors = []
    for k in ("schema", "dataset_id", "mode"):
        if a.get(k) != b.get(k):
            errors.append("%s 不一致（A=%r / B=%r）—— 跨数据集/跨模式比较没有意义" %
                          (k, a.get(k), b.get(k)))
    if int(a.get("repeat") or 0) < min_repeat or int(b.get("repeat") or 0) < min_repeat:
        errors.append("重跑次数不足：A=%s B=%s，要求各 ≥%d" %
                      (a.get("repeat"), b.get("repeat"), min_repeat))
    diff = _cfg_diff(a, b)
    if len(diff) > 1:
        errors.append("违反单变量纪律：A/B 之间变了 %d 个配置（%s）" %
                      (len(diff), list(diff)))
    if a.get("variant") == b.get("variant"):
        errors.append("A/B 的 variant 不能相同")
    if errors:
        return {"schema": CMP_SCHEMA, "verdict": "invalid", "errors": errors,
                "generated_at_utc": _utc()}

    def _flat(report):
        return {_cfg_key(x["case_id"], x["repeat_index"]): x
                for run in report["runs"] for x in run["cases"]}

    fa, fb = _flat(a), _flat(b)
    shared = sorted(set(fa) & set(fb))
    regressions, improvements, unchanged = [], [], 0
    for k in shared:
        pa, pb = bool(fa[k]["passed"]), bool(fb[k]["passed"])
        if pa and not pb:
            regressions.append({"case": k, "case_id": fa[k]["case_id"],
                                "repeat_index": fa[k]["repeat_index"],
                                "score_a": fa[k]["score"], "score_b": fb[k]["score"],
                                "reason": "原本成功 → 变更失败",
                                "failed_checks": [x["type"] for x in fb[k]["checks"]
                                                  if not x["passed"]]})
        elif (not pa) and pb:
            improvements.append({"case": k, "reason": "原本失败 → 变更成功"})
        else:
            unchanged += 1
    per_case = []
    for cid in sorted(set(x["case_id"] for x in fa.values())):
        xa, xb = a["per_case"].get(cid, {}), b["per_case"].get(cid, {})
        per_case.append({
            "case_id": cid,
            "pass_rate_a": xa.get("pass_rate"), "pass_rate_b": xb.get("pass_rate"),
            "pass_rate_delta": round((xb.get("pass_rate") or 0) - (xa.get("pass_rate") or 0), 4),
            "mean_score_a": xa.get("mean_score"), "mean_score_b": xb.get("mean_score"),
            "latency_a_ms": xa.get("mean_latency_ms"), "latency_b_ms": xb.get("mean_latency_ms"),
            "tokens_a": xa.get("mean_tokens"), "tokens_b": xb.get("mean_tokens"),
            "regression_count": sum(1 for r in regressions if r["case_id"] == cid),
        })
    measured = all(x.get("mean_latency_ms") is not None
                   for x in list(a["per_case"].values()) + list(b["per_case"].values()))
    out = {
        "schema": CMP_SCHEMA, "verdict": "ok", "errors": [],
        "dataset_id": a["dataset_id"], "mode": a["mode"],
        "variant_a": a["variant"], "variant_b": b["variant"],
        "label_a": a["label"], "label_b": b["label"],
        "cfg_diff": diff, "repeat": a["repeat"],
        "mean_score_a": a["summary"]["mean_score"], "mean_score_b": b["summary"]["mean_score"],
        "mean_score_delta": round(b["summary"]["mean_score"] - a["summary"]["mean_score"], 4),
        "pass_rate_a": a["summary"]["pass_rate"], "pass_rate_b": b["summary"]["pass_rate"],
        "latency_a_ms": a["summary"].get("mean_latency_ms"),
        "latency_b_ms": b["summary"].get("mean_latency_ms"),
        "tokens_a": a["summary"].get("total_tokens"), "tokens_b": b["summary"].get("total_tokens"),
        "latency_tokens_measured": measured,
        "compared_tasks": len(shared), "unchanged": unchanged,
        "improvements": improvements, "per_case": per_case,
        # ★ 文章最核心的判据：不看平均分，看「原本成功的任务现在失败了」的具体清单
        "regressions": regressions if include_per_case else [],
        "noise_note": "LLM 重排噪声地板约 recall@3/@5 ±0.05、MRR ±0.01；"
                      "本判据用逐任务成功→失败，不受平均分噪声影响",
        "limitations": G.LIMITATIONS + (
            [] if measured else ["offline 模式未测延迟/token，不得据此宣称成本收益"]),
        "generated_at_utc": _utc(),
    }
    if include_per_case and regressions:
        out["verdict"] = "blocked"
    return out


# ── self-test（确定性 + 变异自证，不依赖服务/LLM/网络）────────────────────────
def self_test():
    ok, fail = 0, 0

    def ck(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS", name)
        else:
            fail += 1
            print("  FAIL", name, detail)

    ds = G.load_dataset()
    types = {x["type"] for cs in ds["cases"] for x in cs["checks"]}
    ck("S1 数据集判据类型全部登记", types <= set(G.CHECKERS), str(types - set(G.CHECKERS)))
    ck("S2 三条链路都覆盖",
       {cs["chain"] for cs in ds["cases"]} ==
       {"single_agent", "orchestrated", "model_and_validate"})
    ck("S3 用例数与指纹", ds["case_count"] == 12 and len(ds["dataset_id"]) == 16)

    # S4 评分器有牙齿：不存在的会话必须 FAIL（防「判据恒真」的假绿）
    c = _ro_conn()
    try:
        bad = G.score_case(c, ds["cases"][0], 999999999)
        good = G.score_case(c, ds["cases"][0])
    finally:
        c.close()
    ck("S4 变异：空会话必须判失败", (not bad["passed"]) and good["passed"],
       "bad=%s good=%s" % (bad["passed"], good["passed"]))

    # S5/S6 构造两份只差一例的 run（B 的 G05 第 2 次失败）
    a = {"schema": RUN_SCHEMA, "variant": "A", "label": "合并前", "mode": "offline_replay",
         "dataset_id": ds["dataset_id"], "repeat": 3, "cfg_overrides": {"orchestration.merge_pair": False},
         "runs": [], "per_case": {}, "summary": {"mean_score": 1.0, "pass_rate": 1.0,
                                                  "mean_latency_ms": None, "total_tokens": None}}
    for rep in range(1, 4):
        a["runs"].append({"repeat_index": rep, "cases": [
            {"case_id": cs["id"], "repeat_index": rep, "passed": True, "score": 1.0,
             "checks": [], "latency_ms": None, "tokens": None} for cs in ds["cases"]]})
    for cs in ds["cases"]:
        a["per_case"][cs["id"]] = {"n": 3, "pass_count": 3, "pass_rate": 1.0,
                                   "mean_score": 1.0, "mean_latency_ms": None, "mean_tokens": None}
    b = copy.deepcopy(a)
    b["variant"] = "B"
    b["label"] = "合并后"
    b["cfg_overrides"] = {"orchestration.merge_pair": True}
    for run in b["runs"]:
        for x in run["cases"]:
            if x["case_id"] == "G05" and x["repeat_index"] == 2:
                x["passed"] = False
                x["score"] = 0.5
                x["checks"] = [{"type": "artifact_kind_present", "passed": False}]
    b["per_case"]["G05"]["pass_rate"] = 0.6667
    b["per_case"]["G05"]["pass_count"] = 2
    # 平均分几乎不变（文章说的「平均分掩盖回归」）
    b["summary"]["mean_score"] = 0.9861
    b["summary"]["pass_rate"] = 0.9722

    cmp_full = compare_runs(a, b)
    ck("S5 逐任务清单抓到 G05 回归",
       cmp_full["verdict"] == "blocked" and
       any(r["case_id"] == "G05" and r["repeat_index"] == 2 for r in cmp_full["regressions"]),
       str(cmp_full.get("regressions"))[:200])
    ck("S6 平均分变化很小也不得放行",
       abs(cmp_full["mean_score_delta"]) < 0.02 and cmp_full["verdict"] == "blocked",
       "delta=%s" % cmp_full.get("mean_score_delta"))
    # M1 变异：退化为「只看平均分」→ 回归清单消失（证明清单不是摆设）
    cmp_avg_only = compare_runs(a, b, include_per_case=False)
    ck("M1 变异：去掉逐任务清单后 verdict 变 ok",
       cmp_avg_only["verdict"] == "ok" and not cmp_avg_only["regressions"])
    # M2 变异：多变量 → 必须拒绝比较
    b2 = copy.deepcopy(b)
    b2["cfg_overrides"] = {"orchestration.merge_pair": True, "rag.hyde_enabled": False}
    ck("M2 变异：多变量必须判 invalid",
       compare_runs(a, b2)["verdict"] == "invalid")
    # M3 变异：重跑次数不足 → 必须拒绝
    a2 = copy.deepcopy(a)
    a2["repeat"] = 2
    ck("M3 变异：repeat<3 必须判 invalid",
       compare_runs(a2, b)["verdict"] == "invalid")
    # M4：A==B 时不得报回归（防「恒定报红」）
    sym = copy.deepcopy(a)
    sym["variant"] = "B"
    sym["label"] = "同配置"
    ck("M4 对称：A/B 相同必须无回归", compare_runs(a, sym)["verdict"] == "ok")
    print("SELF-TEST: %d PASS / %d FAIL" % (ok, fail))
    return 0 if fail == 0 else 1


# ── live-preflight（桩驱动；零服务 / 零 LLM / 零生产写入）────────────────────
def live_preflight():
    """确定性验证 live 路径代码本身，含 3 组变异自证。

    夹具是共享内存 SQLite（``file:...?mode=memory&cache=shared``），
    不落任何磁盘文件、不触碰 mbse.db；桩 opener 不发任何网络请求。
    ⚠️ 它**不**证明真实 LLM 能跑通，也不产出基线 —— 见方案 §13。
    """
    import urllib.request
    global _ro_conn, _case_input
    ok, fail = 0, 0

    def ck(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS", name)
        else:
            fail += 1
            print("  FAIL", name, detail)

    ds = G.load_dataset()
    case = ds["cases"][0]
    full_text = "【完整原文夹具】需求模型 v0.7 增量定义。" * 12
    src_cid = int(case["conv_id"])
    new_cid = 987654321
    mid = int(case["input_source"]["message_id"])

    uri = "file:p1_live_preflight?mode=memory&cache=shared"
    keep = sqlite3.connect(uri, uri=True)
    keep.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, content TEXT)")
    keep.execute("CREATE TABLE llm_usage_stats (id INTEGER PRIMARY KEY, "
                 "prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0)")
    keep.execute("INSERT INTO messages VALUES (?,?)", (mid, full_text))
    for i in (1, 2, 3):  # id 基线之前的 3 条历史调用，必须被排除
        keep.execute("INSERT INTO llm_usage_stats VALUES (?,?,?)", (i, 100, 50))
    keep.commit()

    def fake_ro():
        c = sqlite3.connect(uri, uri=True)
        c.row_factory = sqlite3.Row
        return c

    score_calls = []

    def fake_score(conn, cs, conv_id=None):
        score_calls.append((cs["id"], conv_id))
        return {"case_id": cs["id"], "chain": cs["chain"], "intent": cs["intent"],
                "conv_id": conv_id, "source_conv_id": cs["conv_id"],
                "passed": True, "score": 1.0, "checks": []}

    class _Resp:
        def __init__(self, payload=None, lines=None):
            self._payload, self._lines = payload, (lines or [])

        def read(self):
            return json.dumps(self._payload, ensure_ascii=False).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            return iter(self._lines)

    try:
        from core.sse import to_sse_frame
    except Exception:
        to_sse_frame = None

    def sse_lines(with_done=True):
        """用真实 core.sse.to_sse_frame 产出，保证夹具与线上帧格式同源。"""
        frames = [{"type": "token", "data": {"t": "噪声帧"}}]
        if with_done:
            frames.append({"type": "done", "data": {"ok": True}})
        out = []
        for f in frames:
            s = to_sse_frame(f) if to_sse_frame else (
                "event: %s\ndata: %s\n\n" % (f["type"], json.dumps(f, ensure_ascii=False)))
            for ln in s.split("\n"):
                out.append((ln + "\n").encode("utf-8"))
        return out

    class _FakeOpener:
        def __init__(self, with_done=True):
            self.calls = []
            self.with_done = with_done

        def open(self, req, timeout=None):
            body = req.data.decode("utf-8", "replace") if req.data else ""
            self.calls.append((req.full_url, req.get_method(), body, timeout))
            if req.full_url.endswith("/api/conversations"):
                return _Resp(payload={"id": new_cid})
            # 模拟服务在本次请求里新写 2 条 llm_usage_stats（id 大于基线 3）
            w = sqlite3.connect(uri, uri=True)
            for i in (4, 5):
                w.execute("INSERT OR REPLACE INTO llm_usage_stats VALUES (?,?,?)",
                          (i, 200, 300))
            w.commit()
            w.close()
            return _Resp(lines=sse_lines(self.with_done))

    orig_ro, orig_score, orig_input = _ro_conn, G.score_case, _case_input
    _ro_conn = fake_ro
    G.score_case = fake_score
    try:
        # L1 禁代理（§2 坑 17：本机 urllib 连 127.0.0.1 也会走代理 → 404）
        # ⚠️ 行为级判据，别写成「opener 里必须有一个 proxies={} 的 ProxyHandler」：
        #    CPython 的 build_opener 用 skip 机制排除默认 ProxyHandler，传进去的空
        #    ProxyHandler 实例并不会登记进 op.handlers（本机 Python 3.13 实测）。
        #    判「有没有能执行代理的 proxy_open 处理器」才对；且必须配负对照，
        #    否则默认 opener 也不挂代理时本判据会退化成恒真空转。
        op = _no_proxy_opener()
        default_op = urllib.request.build_opener()

        def has_proxy(o):
            return any(hasattr(h, "proxy_open") for h in o.handlers)

        # L1c 就是 L1 的变异对照：若 _no_proxy_opener 退化成裸 build_opener()，
        # 它会挂 ProxyHandler ⇒ L1 的 not has_proxy(...) 必然翻红。
        ck("L1c 变异对照：默认 opener 确实挂代理处理器", has_proxy(default_op),
           str([type(h).__name__ for h in default_op.handlers]))
        ck("L1 _no_proxy_opener 不含任何代理处理器", not has_proxy(op),
           str([type(h).__name__ for h in op.handlers]))
        ck("L1b 夹具用真实 core.sse.to_sse_frame", to_sse_frame is not None)

        fake = _FakeOpener(with_done=True)
        res = _live_case(fake, "http://127.0.0.1:8000", case, 30)
        ck("L2 建会话 URL / intent 正确",
           fake.calls[0][0].endswith("/api/conversations") and
           ('"intent": "%s"' % case["intent"]) in fake.calls[0][2],
           str(fake.calls[0])[:200])
        ck("L3 chat/stream 发的是完整原文而非 preview",
           ("/api/conversations/%d/chat/stream" % new_cid) in fake.calls[1][0] and
           full_text in fake.calls[1][2] and
           case["input_preview"] not in fake.calls[1][2],
           fake.calls[1][2][:160])
        ck("L4 done 终态帧被识别", res["sse_done"] is True)
        ck("L5 评分器收到新 conv_id（数据源切换）",
           bool(score_calls) and score_calls[-1] == (case["id"], new_cid)
           and new_cid != src_cid, str(score_calls))
        probe = sqlite3.connect(uri, uri=True)
        all_tok = probe.execute(
            "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) "
            "FROM llm_usage_stats").fetchone()[0]
        new_tok = probe.execute(
            "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) "
            "FROM llm_usage_stats WHERE id>3").fetchone()[0]
        probe.close()
        ck("L6 token 只算 id>基线 的新增调用",
           res["tokens"] == new_tok and new_tok > 0 and new_tok != all_tok,
           "res=%s new=%s all=%s" % (res["tokens"], new_tok, all_tok))
        ck("L7 正常路径对照：有 done 帧不得误判无效",
           res["passed"] is True and not res.get("invalid_reason"))

        # L8 live 整批必须产出完整 conv_map —— 否则「run --mode live →
        # golden_e2e --conv-map」这条补齐路径是断的，P1 前置仍落不了地。
        batch = run_batch(ds, "A", "preflight", 1, "live", {},
                          _FakeOpener(with_done=True),
                          "http://127.0.0.1:8000", 30)
        want = {c["id"] for c in ds["cases"]}
        cm = batch.get("conv_map") or {}
        ck("L8 live run 产出完整 conv_map（12 例全覆盖且都是新会话）",
           set(cm) == want and all(cm[c["id"]] == new_cid for c in ds["cases"])
           and cm.get(case["id"]) != src_cid, str(cm)[:200])
        ck("L8b --emit-conv-map 已接线（CLI 能写出 golden_e2e 要的 JSON）",
           "--emit-conv-map" in open(os.path.abspath(__file__), encoding="utf-8").read())

        # M1 变异：抽掉 done 帧 ⇒ 必须判无效（证明 _live_case 的修复有牙齿）
        bad = _live_case(_FakeOpener(with_done=False),
                         "http://127.0.0.1:8000", case, 30)
        ck("M1 变异：缺 done 帧必须判无效",
           bad["sse_done"] is False and bad["passed"] is False and
           bad.get("invalid_reason") == "sse_done_missing" and
           any(x.get("type") == "sse_done_required" and not x.get("passed")
               for x in bad["checks"]), str(bad)[:240])

        # M2 变异：_case_input 退化成 preview ⇒ L3 的判据必须能抓住
        _case_input = lambda cs: cs["input_preview"]
        fp = _FakeOpener(with_done=True)
        _live_case(fp, "http://127.0.0.1:8000", case, 30)
        ck("M2 变异：发 preview 会被 L3 判据抓住",
           full_text not in fp.calls[1][2] and
           case["input_preview"] in fp.calls[1][2], fp.calls[1][2][:160])

        # M3 变异（数值对照，不改代码）：若统计去掉 WHERE id>?，结果会变成 all_tok
        ck("M3 变异对照：id 基线口径若退化，L6 数值必然不同",
           all_tok != new_tok, "all=%s new=%s" % (all_tok, new_tok))
    finally:
        _ro_conn = orig_ro
        G.score_case = orig_score
        _case_input = orig_input
        keep.close()

    print("LIVE-PREFLIGHT: %d PASS / %d FAIL" % (ok, fail))
    return 0 if fail == 0 else 1


# ── CLI ─────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--variant", choices=["A", "B"], required=True)
    p.add_argument("--label", default="")
    p.add_argument("--repeat", type=int, default=MIN_REPEAT)
    p.add_argument("--mode", choices=["offline_replay", "live"], default="offline_replay")
    p.add_argument("--set", default=DEFAULT_SET)
    p.add_argument("--out", required=True)
    p.add_argument("--emit-conv-map", default="",
                   help="live 下把 case_id→conv_id 写成 JSON，供 golden_e2e --conv-map 消费")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    p.add_argument("--timeout", type=int, default=900)
    p.add_argument("--cfg", action="append", default=[],
                   help="GROUP.KEY=VALUE，只记入报告与内存，不落盘")
    q = sub.add_parser("compare")
    q.add_argument("--a", required=True)
    q.add_argument("--b", required=True)
    q.add_argument("--out", default="")
    q.add_argument("--min-repeat", type=int, default=MIN_REPEAT)
    q.add_argument("--allow-regressions", action="store_true")
    sub.add_parser("self-test")
    sub.add_parser("live-preflight")
    args = ap.parse_args()

    if args.cmd == "self-test":
        return self_test()
    if args.cmd == "live-preflight":
        return live_preflight()
    if args.cmd == "run":
        cfg = {}
        for item in args.cfg:
            if "=" not in item or "." not in item:
                print("!! --cfg 格式应为 GROUP.KEY=VALUE：%s" % item)
                return 2
            path, val = item.split("=", 1)
            sec, key = path.split(".", 1)
            try:
                val = json.loads(val)
            except Exception:
                pass
            cfg[path] = val
            # 只改本进程内存（仿 run_eval.py --cfg），绝不落盘
            try:
                from core import config as _c
                _c._CONFIG.setdefault(sec, {})[key] = val
            except Exception:
                pass
        ds = G.load_dataset(args.set)
        opener = _no_proxy_opener() if args.mode == "live" else None
        rep = run_batch(ds, args.variant, args.label or ("变体 %s" % args.variant),
                        args.repeat, args.mode, cfg, opener, args.base_url, args.timeout,
                        out_path=args.out)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
            f.write("\n")
        if args.emit_conv_map:
            if args.mode != "live":
                print("!! --emit-conv-map 只在 --mode live 下有意义（offline 没有新会话）")
                return 2
            os.makedirs(os.path.dirname(os.path.abspath(args.emit_conv_map)),
                        exist_ok=True)
            with open(args.emit_conv_map, "w", encoding="utf-8", newline="\n") as f:
                json.dump(rep["conv_map"], f, ensure_ascii=False, indent=1,
                          sort_keys=True)
                f.write("\n")
            print("WROTE conv_map %d 例 → %s" % (len(rep["conv_map"]),
                                                 args.emit_conv_map))
        print("RUN %s mode=%s repeat=%d dataset=%s mean_score=%s pass_rate=%s → %s" %
              (args.variant, args.mode, args.repeat, rep["dataset_id"],
               rep["summary"]["mean_score"], rep["summary"]["pass_rate"], args.out))
        return 0
    # compare
    with open(args.a, encoding="utf-8") as f:
        a = json.load(f)
    with open(args.b, encoding="utf-8") as f:
        b = json.load(f)
    out = compare_runs(a, b, min_repeat=args.min_repeat)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")
    print("COMPARE verdict=%s mean_score %.4f→%.4f  regressions=%d improvements=%d compared=%d" %
          (out["verdict"], out.get("mean_score_a", 0), out.get("mean_score_b", 0),
           len(out.get("regressions", [])), len(out.get("improvements", [])),
           out.get("compared_tasks", 0)))
    for r in out.get("regressions", []):
        print("  !! 回归 %s（第 %s 次）失败判据=%s" %
              (r["case"], r["repeat_index"], r["failed_checks"]))
    if out.get("errors"):
        print("  errors:", out["errors"])
    if out["verdict"] == "invalid":
        return 3
    if out["verdict"] == "blocked" and not args.allow_regressions:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
