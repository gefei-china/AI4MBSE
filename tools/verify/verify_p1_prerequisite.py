# -*- coding: utf-8 -*-
"""P1 前置常驻门禁 —— 把方案 §6 的硬前置从文档文字变成机器判据。

原文（docs/单Agent循环对标评估与优化方案-20261115.md §6）：
  「P1 的任何合并动作**必须**等 P0 的 Golden Set 与 A/B 门禁跑出基线，
    否则违反文章最核心的纪律。」

本门禁守的是「前置机制是否健全 + 有没有被绕过」，**不是**「基线已经产出」：
  · 当前真库 live 基线数 = 0 ⇒ 输出 p1_release=blocked，这是如实状态，不判红；
  · 任何人宣称 p1_release=ready，必须指到一条**合格**的 live 基线（H 组）；
  · 「合格」= mode=live + 指纹未漂移 + 12 例全部 conv_id ≠ source_conv_id，
    即排除「标着 live、其实评的是历史会话」的伪造报告。

零真实 LLM / 零服务 / 零生产写入：所有变异都在内存字符串或 :memory: SQLite
里做，不落任何临时文件（Windows + tsbx.dll 下删除临时文件会挂死，见工程 §1r.2）。
干净 checkout 无 mbse.db 时，真库相关判据降级并显式打印 DEGRADED，不假绿。

用法：
  ./.venv/Scripts/python.exe -X utf8 tools/verify/verify_p1_prerequisite.py
"""
import ast
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools", "eval"))

PY = sys.executable
AB = os.path.join(ROOT, "tools", "eval", "run_arch_ab.py")
GE = os.path.join(ROOT, "tools", "eval", "golden_e2e.py")
DS = os.path.join(ROOT, "docs", "评测", "datasets", "golden_e2e_v0.json")
DECL_DIR = os.environ.get("MBSE_P1_DECL_DIR") or os.path.join(ROOT, "docs", "评测")
DB = os.environ.get("MBSE_DB_PATH") or os.path.join(ROOT, "mbse.db")
EVAL_KIND = "golden_e2e"
CHAINS = {"single_agent", "orchestrated", "model_and_validate"}

PASS, FAIL, DEGRADED = [], [], []


def rec(tag, cond, detail=""):
    (PASS if cond else FAIL).append(tag)
    print("  %s %-4s %s" % ("PASS" if cond else "FAIL", tag, detail))


def load_src(p):
    return open(p, "rb").read().decode("utf-8")


# ── 纯判据（门禁与变异共用同一处，防两份实现漂移）─────────────────────────
def live_guard_ok(src):
    """_live_case 必须有「断流判无效」守卫（P1-1 修的真实假绿）。"""
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_live_case"), None)
    if fn is None:
        return False
    d = ast.dump(fn)
    return all(x in d for x in ("invalid_reason", "sse_done_missing",
                                "sse_done_required"))


def anti_forge_ok(src):
    """golden_e2e.main 必须有 `if args.mode == "live":` 且内含 ≥3 个 return 2。"""
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
    if fn is None:
        return False
    for n in ast.walk(fn):
        if not isinstance(n, ast.If) or not isinstance(n.test, ast.Compare):
            continue
        lhs = n.test.left
        if not (isinstance(lhs, ast.Attribute) and lhs.attr == "mode"):
            continue
        if not any(isinstance(c, ast.Constant) and c.value == "live"
                   for c in n.test.comparators):
            continue
        rets = [x for x in ast.walk(n) if isinstance(x, ast.Return)
                and isinstance(x.value, ast.Constant) and x.value.value == 2]
        if len(rets) >= 3:
            return True
    return False


def valid_live_report(detail, dataset_id, expect_n=12):
    """合格 live 基线：mode=live + 指纹一致 + 例数一致 + 每例 conv_id 都换了。"""
    if not isinstance(detail, dict):
        return False, "detail 不是 dict"
    if detail.get("mode") != "live":
        return False, "mode=%s" % detail.get("mode")
    if detail.get("dataset_id") != dataset_id:
        return False, "指纹漂移"
    rows = detail.get("rows") or []
    if len(rows) != expect_n:
        return False, "例数=%d" % len(rows)
    same = [str(r.get("case_id")) for r in rows
            if str(r.get("conv_id")) == str(r.get("source_conv_id"))]
    if same:
        return False, "评的仍是历史会话: %s" % ",".join(same[:4])
    return True, "ok"


def collect_live(conn, dataset_id, expect_n=12):
    """返回 ([(id, valid, why)], err)。只读。"""
    out = []
    try:
        rows = conn.execute(
            "SELECT id, detail FROM eval_reports WHERE eval_kind=? AND is_golden=1 "
            "ORDER BY id", (EVAL_KIND,)).fetchall()
    except Exception as e:
        return out, "查库失败: %s" % e
    for r in rows:
        try:
            d = json.loads(r[1] or "{}")
        except Exception:
            continue
        if d.get("mode") != "live":
            continue
        ok, why = valid_live_report(d, dataset_id, expect_n)
        out.append((int(r[0]), ok, why))
    return out, ""


def release_check(decls, valid_ids):
    """任何 p1_release=ready 声明必须指到一条有效 live 基线。返回违规列表。"""
    bad = []
    for d in decls:
        if str(d.get("p1_release", "")).lower() != "ready":
            continue
        try:
            rid = int(d.get("baseline_report_id") or 0)
        except Exception:
            rid = 0
        if rid <= 0 or rid not in valid_ids:
            bad.append("声明 %s 标 ready，但 baseline_report_id=%s 不是有效 live 基线"
                       % (d.get("name", "?"), rid or "-"))
    return bad


def scan_decls(d):
    out = []
    for p in sorted(glob.glob(os.path.join(d, "*p1*release*.json"))):
        try:
            j = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if isinstance(j, dict):
            j.setdefault("name", os.path.basename(p))
            out.append(j)
    return out


def run_sub(args, timeout=240):
    """跑子脚本，解析 `XXX: n PASS / m FAIL` 结论行。返回 ((n,m), err)。"""
    try:
        r = subprocess.run([PY, "-X", "utf8"] + args, cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "TIMEOUT"
    out = (r.stdout or "") + (r.stderr or "")
    m = re.search(r"(SELF-TEST|LIVE-PREFLIGHT): (\d+) PASS / (\d+) FAIL", out)
    if not m:
        return None, "无结论行 rc=%s tail=%s" % (r.returncode, out.strip()[-200:])
    return (int(m.group(2)), int(m.group(3))), ""


def main():
    print("=" * 78)
    print("P1 前置门禁（方案 §6 硬前置：P0 基线 → P1 合并）")
    print("=" * 78)

    # ── P 组：前置机制健全 ────────────────────────────────────────────────
    ds = json.load(open(DS, encoding="utf-8"))
    dsid = ds.get("dataset_id") or ""
    cases = ds.get("cases") or []
    rec("P1", len(dsid) == 16, "数据集指纹 %s" % dsid)
    rec("P2", len(cases) == 12 and {c.get("chain") for c in cases} == CHAINS,
        "%d 例 / %s" % (len(cases), sorted({c.get("chain") for c in cases})))

    absrc, gesrc = load_src(AB), load_src(GE)
    rec("P3", live_guard_ok(absrc), "_live_case 断流必须判无效")
    rec("P4", anti_forge_ok(gesrc), "golden_e2e 拒绝伪造 live 报告")
    rec("P5", 'report["mode"]' in gesrc and "detail" in gesrc,
        "save_report 把 mode 写进 doc_name/detail 可查落点")

    n, err = run_sub([AB, "live-preflight"])
    rec("P6", n is not None and n[1] == 0,
        err or "live-preflight PASS=%d FAIL=%d（桩，零服务零 LLM）" % (n or (0, 0)))

    has_db = os.path.isfile(DB) and os.path.getsize(DB) > 0
    if has_db:
        n2, err2 = run_sub([AB, "self-test"])
        rec("P7", n2 is not None and n2[1] == 0,
            err2 or "self-test PASS=%d FAIL=%d" % (n2 or (0, 0)))
    else:
        DEGRADED.append("P7 self-test 需真库，干净 checkout 跳过")
        print("  SKIP P7 self-test（无 mbse.db）")

    # ── S 组：当前 live 基线状态（如实报告，不判红）──────────────────────
    live = []
    if has_db:
        conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
        try:
            live, err3 = collect_live(conn, dsid, len(cases))
        finally:
            conn.close()
        if err3:
            DEGRADED.append("S 组: " + err3)
    else:
        DEGRADED.append("S 组 需真库，干净 checkout 跳过")
    valid_ids = {i for i, ok, _ in live if ok}
    print("  INFO S1 live 基线: 总 %d / 合格 %d%s" % (
        len(live), len(valid_ids),
        "" if not live else " " + str([(i, ok, w) for i, ok, w in live][:4])))
    p1_release = "ready" if valid_ids else "blocked"
    reasons = []
    if not valid_ids:
        reasons.append("真库无合格 live 基线（mode=live 且 12 例 conv_id 全部换新）")
        reasons.append("补齐路径：起服务 → run_arch_ab.py run --mode live --repeat 3 "
                       "→ golden_e2e.py --mode live --conv-map <map>.json --save")

    # ── H 组：防绕过 ────────────────────────────────────────────────────
    decls = scan_decls(DECL_DIR)
    bad = release_check(decls, valid_ids)
    rec("H1", not bad, "扫描 %d 份 p1_release 声明；%s" % (
        len(decls), "; ".join(bad) if bad else "无违规"))

    # ── M 组：变异自证（全零 IO，不落临时文件）──────────────────────────
    rec("M1", bool(release_check(
        [{"name": "fake", "p1_release": "ready", "baseline_report_id": 999999}], set())),
        "变异：假 ready 声明必须判红")
    rec("M2", not live_guard_ok(absrc.replace("sse_done_missing", "zzz_removed")),
        "变异：删掉断流守卫必须判红")
    rec("M3", not anti_forge_ok(gesrc.replace('if args.mode == "live":', "if False:")),
        "变异：去掉 live 防伪造分支必须判红")
    mem = sqlite3.connect(":memory:")
    mem.execute("CREATE TABLE eval_reports (id INTEGER PRIMARY KEY, detail TEXT, "
                "eval_kind TEXT, is_golden INTEGER)")
    fake = {"mode": "live", "dataset_id": dsid, "rows": [
        {"case_id": c["id"], "conv_id": c["conv_id"], "source_conv_id": c["conv_id"]}
        for c in cases]}
    mem.execute("INSERT INTO eval_reports VALUES (1,?,?,1)",
                (json.dumps(fake, ensure_ascii=False), EVAL_KIND))
    got, _ = collect_live(mem, dsid, len(cases))
    mem.close()
    rec("M4", len(got) == 1 and got[0][1] is False,
        "变异：标着 live 但用历史会话的报告不算数 %s" % str(got))

    # ── 结论 ────────────────────────────────────────────────────────────
    print("-" * 78)
    print("PASS %d / FAIL %d | DEGRADED %d | p1_release=%s | 合格 live 基线 %d 条 | %s"
          % (len(PASS), len(FAIL), len(DEGRADED), p1_release, len(valid_ids),
             datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")))
    for d in DEGRADED:
        print("  DEGRADED:", d)
    if p1_release != "ready":
        print("  P1 未放行原因：")
        for r in reasons:
            print("    -", r)
    if FAIL:
        print("  FAIL:", ", ".join(FAIL))
    print("=" * 78)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
