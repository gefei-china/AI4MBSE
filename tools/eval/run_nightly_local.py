#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地 nightly 跑批器（2026-10-22）—— nightly 语料接线的落地方案。

## 为什么是"本地跑批"而不是把库搬上 GitHub

nightly-evals 作业此前因语料缺失而显式失败（有意设计，不恒绿）。
接线评估时把三个方案逐一证伪/证实：

| 方案 | 结论 |
|---|---|
| ③ 语料 md 入库重建 | **证伪**：实测 14 份语料全是用户上传（origin='upload'，含 14MB 的 OMG SysML v2 PDF），不在 git 里，仓库侧无 md 可重建 |
| ① artifact 导出库 | 可行但笨重：VACUUM 后仍 345MB（数据本身就这么大），且**生产库含用户会话/附件，出网即敏感数据外流**；免费档 artifact 配额 500MB 一次几乎占满 |
| ② 对象存储 + 加密快照 | 最稳健，但需要用户提供 COS 桶与 secrets，本轮不做 |
| **④ 本地跑批（本脚本）** | **本轮落地**：库不出机器、零新基础设施、报告 checked-in 后 PR 门禁自动消费 |

④不是妥协而是更优：生产指标自动化的要件是"定时跑 + 结果可消费"，
不必然是"在 GitHub 上跑"。本脚本一次跑完三类评测，落库
（前端趋势立即可见）+ 写 digest（人类记录）+ 可选 commit（PR 门禁
消费 checked-in 报告 —— 这正是 ci.yml 注释里预留的路径）。

## 跑什么（顺序即依赖）

  1. 生成质量评测（秒级，读生产 messages，不调外部服务）
  2. RAG 检索评测（分钟级，需 embedding + LLM 重排）
  3. 意图路由评测（干净库自建，不碰生产数据）
  每步后紧跟对应门禁；任一步失败不阻断后续（digest 里逐条记账）。

## 用法

  # 跑一遍（不 commit）
  .venv/Scripts/python.exe -X utf8 tools/eval/run_nightly_local.py

  # 跑完把 latest 报告提交（PR 门禁随后就能看到）
  .venv/Scripts/python.exe -X utf8 tools/eval/run_nightly_local.py --commit

  # 注册 Windows 计划任务（每日 03:17，错过开机补跑）
  .venv/Scripts/python.exe -X utf8 tools/eval/run_nightly_local.py --register

  # 卸掉计划任务
  .venv/Scripts/python.exe -X utf8 tools/eval/run_nightly_local.py --unregister

## 输出位置

  docs/评测/nightly/digest-YYYYMMDD-HHMM.json   每次跑批的完整 digest
  docs/评测/nightly/latest.json                 最近一次（含各门禁结论）
  docs/评测/nightly/latest-rag-report.json      供 verify_rag_eval_gate.py 消费
  docs/评测/nightly/latest-gen-report.json      供 verify_generation_quality_gate.py 消费
  eval_reports 表（--save 链）                   前端趋势数据源

⚠ 报告 committed-in 是**有意的数据外流边界**：digest 只含指标与聚合，
  不含答案正文/附件内容（生成质量报告的 per_answer 只含实体名与计数）。
  换库/换机器前先想清楚这报告能不能进 git。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NIGHTLY_DIR = os.path.join(ROOT, "docs", "评测", "nightly")
TMP_P21 = os.path.join(ROOT, "tmp", "p21")

#: (步骤名, 命令, 产出报告路径[None=不产出报告], 对应门禁[None=无])
STEPS = [
    # ── 评测集重建（2026-10-23）─────────────────────────────────
    # 为什么本地跑批器也要重建：评测集是**语料的派生物**——语料改了、
    # 负样本集扩了（2026-10-23 neg 8→30），旧集就会漂移（run_eval 的
    # 指纹门禁会判红）或用旧 neg 集打分。此前本地跑批器不重建，靠人
    # 手动跑 build_evalset.py ⇒ "改了语料但分数没变"的静默漂移风险。
    # 与 CI nightly-evals 作业同序（那边也是先 build 再评）。
    ("构建评测集",
     [sys.executable, "-X", "utf8", "tools/eval/build_evalset.py"],
     None, None),
    # ── 断链③ 语义层（2026-10-22 晚）：生成质量 + LLM 忠实度评判 ──────────
    # --llm-judge 让语义层只在 nightly 跑（模型输出有温度、依赖服务可用性，
    #   不是确定性断言——与"确定性进 PR 阻断、模型相关 nightly 跑"同构）。
    #   实测基线：忠实率 0.53~0.67（15 条，deepseek-v4-flash，边界案例
    #   ±1/15 波动）；服务不可用时三态记账全记"判不了"，门禁判 SKIP 不红。
    ("生成质量评测",
     [sys.executable, "-X", "utf8", "tools/eval/eval_generation_quality.py",
      "--llm-judge", "--save", "--out", os.path.join(TMP_P21, "eval_generation_quality.json")],
     os.path.join(TMP_P21, "eval_generation_quality.json"),
     [sys.executable, "-X", "utf8", "tools/verify/verify_generation_quality_gate.py",
      "--report", os.path.join(TMP_P21, "eval_generation_quality.json")]),
    # ── 断链②（2026-10-22）：Trace 一致性评测 ─────────────────────
    # 消费 trace2dataset 产出的生产人工裁决样本（228 条：strong 55 /
    # weak 173），度量"当前路由 ↔ 人工裁决"一致率。--no-llm 是**刻意**
    # 的：含 LLM 口径实测 0.881~0.929 随服务可用性波动（E3b 纪律：
    # 进门禁的必须可复现），nightly 要的是确定性结论；LLM 层波动由
    # run_ci_eval（nightly CI 段）另行监控。耗时约 12s（55 strong +
    # 50 weak 抽样），排在轻量的生成质量之后、重量的检索评测之前。
    # --baseline 指向**上一轮**的 latest 快照（本步在快照覆盖之前跑，
    # 顺序即正确性）：weak 回归稳定率 = 同一批生产查询本轮与上轮路由
    # 是否一致（诊断用不判定；口径不符自动跳过并写明原因）。
    ("Trace 一致性评测",
     [sys.executable, "-X", "utf8", "tools/eval/eval_trace_consistency.py",
      "--no-llm", "--baseline",
      os.path.join(NIGHTLY_DIR, "latest-trace-report.json"),
      "--save", "--out", os.path.join(TMP_P21, "eval_trace_consistency.json")],
     os.path.join(TMP_P21, "eval_trace_consistency.json"),
     [sys.executable, "-X", "utf8", "tools/verify/verify_trace_quality_gate.py",
      "--report", os.path.join(TMP_P21, "eval_trace_consistency.json")]),
    ("RAG 检索评测",
     [sys.executable, "-X", "utf8", "tools/eval/run_eval.py",
      "--tag", "nightly", "--control",
      "--out", os.path.join(TMP_P21, "eval_report_ci.json")],
     os.path.join(TMP_P21, "eval_report_ci.json"),
     [sys.executable, "-X", "utf8", "tools/verify/verify_rag_eval_gate.py",
      "--report", os.path.join(TMP_P21, "eval_report_ci.json")]),
    ("意图路由评测",
     [sys.executable, "-X", "utf8", "tools/eval/run_ci_eval.py", "--report"],
     None, None),
]

TASK_NAME = "MBSE-Eval-Nightly"


def _run(cmd, timeout=1800):
    """跑一条命令，返回 (exit_code, 输出尾部)。"""
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        return p.returncode, out[-4000:], time.time() - t0
    except subprocess.TimeoutExpired:
        return 124, "超时（%ds）" % timeout, time.time() - t0
    except Exception as e:  # noqa: BLE001 —— 跑批器自己不能崩，任何一步的失败都要记账
        return 127, "%s: %s" % (type(e).__name__, e), time.time() - t0


def run_batch(do_commit=False):
    os.makedirs(NIGHTLY_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M")
    digest = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "host": os.environ.get("COMPUTERNAME", ""),
              "steps": [], "gates": []}

    print("=" * 74)
    print("本地 nightly 跑批（%s）" % stamp)
    print("=" * 74)

    for name, cmd, report, gate in STEPS:
        print("\n── %s ──" % name)
        rc, out, secs = _run(cmd)
        digest["steps"].append({"step": name, "cmd": " ".join(cmd[1:4]),
                                "exit": rc, "secs": round(secs, 1),
                                "tail": out[-1500:]})
        print("  exit=%d  耗时 %.1fs" % (rc, secs))
        if rc != 0:
            print("  ✗ 失败（尾部输出见 digest）；后续步骤继续")
        if gate:
            grc, gout, gsecs = _run(gate)
            verdict = "PASS" if grc == 0 else "FAIL"
            digest["gates"].append({"gate": os.path.basename(gate[3]),
                                    "exit": grc, "secs": round(gsecs, 1)})
            print("  门禁 %s（exit=%d, %.1fs）" % (verdict, grc, gsecs))

    # ── latest 快照（供 PR 门禁消费的 checked-in 报告）──────────────────
    latest = {"updated_at": digest["started_at"],
              "gates": digest["gates"],
              "steps": [{"step": s["step"], "exit": s["exit"]} for s in digest["steps"]]}
    copied = []
    for src, dst_name in ((os.path.join(TMP_P21, "eval_generation_quality.json"),
                           "latest-gen-report.json"),
                          (os.path.join(TMP_P21, "eval_trace_consistency.json"),
                           "latest-trace-report.json"),
                          (os.path.join(TMP_P21, "eval_report_ci.json"),
                           "latest-rag-report.json")):
        if os.path.exists(src):
            dst = os.path.join(NIGHTLY_DIR, dst_name)
            shutil.copyfile(src, dst)
            copied.append(dst_name)
    with io.open(os.path.join(NIGHTLY_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)
    digest_path = os.path.join(NIGHTLY_DIR, "digest-%s.json" % stamp)
    with io.open(digest_path, "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=1)

    print("\n" + "=" * 74)
    n_fail = sum(1 for s in digest["steps"] if s["exit"] != 0)
    n_gfail = sum(1 for g in digest["gates"] if g["exit"] != 0)
    print("跑批完成：步骤失败 %d，门禁失败 %d" % (n_fail, n_gfail))
    print("digest: %s" % os.path.relpath(digest_path, ROOT).replace("\\", "/"))
    print("latest 报告: %s" % ", ".join(copied))

    if do_commit:
        _commit(latest)

    ok = n_fail == 0 and n_gfail == 0
    print("结论：%s" % ("全部通过" if ok else "存在失败项，见 digest"))
    _record_failure_marker(digest, ok, digest_path)
    _notify(digest, ok)
    return 0 if ok else 1


def _record_failure_marker(digest, ok, digest_path):
    """失败可见化（O1 主通道）：失败时写红旗标记，成功时清掉。

    ⚠ 刻意写进 docs/评测/nightly/（--commit 时随 git 推走）而不是 tmp/：
    这个目录是 PR 门禁与人共同消费的地方，失败红旗出现在这里 = 打开目录
    就看见，不依赖任何人想起来看日志或退出码。
    """
    marker = os.path.join(NIGHTLY_DIR, "last-failure.json")
    if ok:
        if os.path.exists(marker):
            os.remove(marker)
            print("  已清除上次失败标记（last-failure.json）")
        return
    bad_steps = [s for s in digest["steps"] if s["exit"] != 0]
    bad_gates = [g for g in digest["gates"] if g["exit"] != 0]
    payload = {
        "failed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": digest.get("host", ""),
        "failed_steps": [{"step": s["step"], "exit": s["exit"],
                          "tail": s.get("tail", "")[-600:]} for s in bad_steps],
        "failed_gates": bad_gates,
        "digest": os.path.relpath(digest_path, ROOT).replace("\\", "/"),
    }
    with io.open(marker, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("  ⚠ 失败标记已写：%s（成功跑批时自动清除）"
          % os.path.relpath(marker, ROOT).replace("\\", "/"))


def _notify(digest, ok):
    """可选 webhook 副通道（O1）：设了 EVAL_NIGHTLY_NOTIFY_URL 才发。

    复用 core.alert_evaluator.notify_alert_webhook（A2A 事件结构 + 可选
    HMAC 签名共用同一份实现，不另写）。默认不配 = 零外部依赖，失败信息
    由 last-failure.json + digest + 退出码三条确定性通道承载。
    """
    if ok:
        return
    url = os.environ.get("EVAL_NIGHTLY_NOTIFY_URL", "").strip()
    if not url:
        return
    try:
        sys.path.insert(0, ROOT)
        from core.alert_evaluator import notify_alert_webhook
        bad = [s["step"] for s in digest["steps"] if s["exit"] != 0] + \
              [g["gate"] for g in digest["gates"] if g["exit"] != 0]
        # 借用同一份 A2A 信封实现；失败项清单放不进固定结构，写进规则名。
        rule = {"id": 0, "name": ("nightly 评测跑批失败: %s"
                                  % ", ".join(bad))[:120],
                "level": "warning", "secret": "", "notify_url": url}
        sent = notify_alert_webhook(rule, "nightly_failed", len(bad), 0, ">")
        print("  webhook 通知：%s" % ("已送达" if sent else "投递失败（不影响退出码）"))
    except Exception as e:
        print("  webhook 通知异常（忽略）：%s" % e)


def _marker_in_git():
    """失败标记是否已被 git 跟踪（成功后工作区消失，但历史里还在，要显式
    `git add` 删除才能让远端也变绿 —— git rm 语义由 add -A 类提交自然完成，
    这里只需保证路径进 git add 清单以记录删除）。"""
    rc, out, _ = _run(["git", "ls-files", "--error-unmatch",
                       "docs/评测/nightly/last-failure.json"], timeout=30)
    return rc == 0


def _commit(latest):
    """把 latest 报告提交进 git（PR 门禁消费的就是它们）。"""
    paths = ["docs/评测/nightly/latest.json",
             "docs/评测/nightly/latest-gen-report.json",
             "docs/评测/nightly/latest-trace-report.json",
             "docs/评测/nightly/latest-rag-report.json"]
    # O2：版本化评测集（datasets/ 随 git 可 review；缺失则跳过）
    for rel in ("docs/评测/datasets/evalset.json",
                "docs/评测/datasets/trace_evalset.json",
                "docs/评测/datasets/manifest.json"):
        if os.path.exists(os.path.join(ROOT, rel)):
            paths.append(rel)
    marker = os.path.join(ROOT, "docs/评测/nightly/last-failure.json")
    if os.path.exists(marker) or _marker_in_git():
        paths.append("docs/评测/nightly/last-failure.json")
    existing = [p for p in paths if os.path.exists(os.path.join(ROOT, p))]
    if not existing:
        print("  （无可提交的 latest 报告，跳过 commit）")
        return
    stamp = time.strftime("%Y-%m-%d %H:%M")
    msg = ("chore(eval): nightly 跑批快照 %s\n\n"
           "本地跑批器自动提交；PR 门禁消费这些 checked-in 报告。\n"
           "门禁结论：%s" % (stamp, json.dumps(latest["gates"], ensure_ascii=False)))
    rc, out, _ = _run(["git", "add"] + existing, timeout=120)
    if rc != 0:
        print("  git add 失败：%s" % out[-300:])
        return
    rc, out, _ = _run(["git", "commit", "-m", msg], timeout=120)
    if rc != 0:
        print("  git commit 失败（可能无变更）：%s" % out[-300:])
        return
    rc, out, _ = _run(["git", "push"], timeout=300)
    print("  已提交并推送 latest 报告（%s）" % stamp if rc == 0
          else "  已提交但 push 失败：%s" % out[-300:])


def register_task():
    """注册 Windows 计划任务（每日 03:17，错过开机补跑）。

    ⚠ 这是对用户机器的系统级变更，**只在显式 --register 时执行**。
    """
    py = sys.executable
    script = os.path.join(ROOT, "tools", "eval", "run_nightly_local.py")
    tr = '"%s" -X utf8 "%s" --commit' % (py, script)
    cmd = ["schtasks", "/create", "/tn", TASK_NAME, "/tr", tr,
           "/sc", "daily", "/st", "03:17", "/f"]
    print("注册计划任务：%s" % " ".join(cmd))
    rc, out, _ = _run(cmd, timeout=120)
    print(out[-1500:])
    if rc == 0:
        print("✓ 已注册。管理入口：任务计划程序 → %s" % TASK_NAME)
        print("  错过开机补跑由任务的'条件→如果任务失败，按需重启'控制；")
        print("  本任务默认错过不补跑（评测晚几小时跑没有意义，不如次日整点跑）。")
    return rc


def unregister_task():
    rc, out, _ = _run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"], timeout=120)
    print(out[-800:])
    return rc


def main():
    ap = argparse.ArgumentParser(description="本地 nightly 跑批器")
    ap.add_argument("--commit", action="store_true",
                    help="跑完把 latest 报告提交并推送（PR 门禁消费）")
    ap.add_argument("--register", action="store_true",
                    help="注册 Windows 计划任务（每日 03:17）后退出")
    ap.add_argument("--unregister", action="store_true",
                    help="卸掉 Windows 计划任务后退出")
    args = ap.parse_args()

    if args.register:
        return register_task()
    if args.unregister:
        return unregister_task()
    return run_batch(do_commit=args.commit)


if __name__ == "__main__":
    sys.exit(main())
