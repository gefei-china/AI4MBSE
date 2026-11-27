# -*- coding: utf-8 -*-
"""静默兜底台账门禁 —— 2026-11-25（P0 治理的"不许变多"闸门）。

## 它卡的是什么
架构评估 P0：全仓 69% 的 `except Exception` 是静默吞掉。一次性改完不现实，
但**必须阻止它继续变多** —— 否则每加一个新功能，就又埋几个"失败但看起来正常"的坑。

判据只有一个方向：**当前静默数 ≤ 基线**。
  · 允许变少（治理有进展）；
  · 不允许变多（新增静默必须显式登记基线，登记动作本身就是一次人工复核）。
这与"门禁不看绝对值、只看是否劣化"的既有纪律一致（绝对值会随代码量自然变化，
硬卡绝对值会在正常开发中假红）。

## 双口径（不许只报一个数）
本仓已归档的教训：同一个量在多处各写一份，最后必然对不上。
架构评估报告的「1361 / 69%」没有公布判定口径与扫描范围，**无法与任何新统计直接比对**。
所以本门禁与扫描器一律同时输出：
  · `strict`（仅 `: pass`）—— 最无争议的一档；
  · `wide`（含 `return 默认值 / assign / continue`）—— 治理视角，
    因为 `except: return 0` 在指标层面与 `pass` 同样不可见。
**任何对外引用必须注明口径**，本文件不提供"唯一正确数字"这种幻觉。

## 变异自证（M 系列）
  M1  `except: pass`            → 必须判静默（宽+严都真）
  M2  `except: logger.warning`  → 必须判非静默
  M3  `except: return 0`        → 宽口径真 / 严格口径假（证明双口径不是同一个数）
  M4  `except: raise`           → 必须判非静默
  M5  扫描器在临时目录造的真文件 → 必须被抓到（证明 AST 路径真的在走）
"""
import json
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

import ast  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "tools"))
import scan_silent_except as S  # noqa: E402

PASS, FAIL, SKIPPED = [], [], []
BASELINE = os.path.join(ROOT, "tools", "silent_except_baseline.json")


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- " + str(detail)) if detail else ""), flush=True)


# ══ ① 判据单测（纯 AST，不依赖仓库现状）══════════════════════════════
print("[1] 静默判定口径", flush=True)


def _handler_of(src: str) -> ast.ExceptHandler:
    """从一段源码里取出第一个 ExceptHandler。"""
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, ast.ExceptHandler):
            return n
    raise AssertionError("源码里没有 except")


_h_pass = _handler_of("try:\n    x()\nexcept Exception:\n    pass\n")
check("M1 `except: pass` 宽/严两口径都判静默",
      S._is_silent(_h_pass)[0] is True and S._is_strict_silent(_h_pass) is True,
      "%s / %s" % (S._is_silent(_h_pass), S._is_strict_silent(_h_pass)))

_h_log = _handler_of("try:\n    x()\nexcept Exception as e:\n    logger.warning('x', e)\n")
check("M2 `except: logger.warning` 判为非静默（有可见动作）",
      S._is_silent(_h_log)[0] is False and S._is_strict_silent(_h_log) is False,
      "%s / %s" % (S._is_silent(_h_log), S._is_strict_silent(_h_log)))

_h_ret = _handler_of("try:\n    x()\nexcept Exception:\n    return 0\n")
check("M3 `except: return 0` 宽口径真 / 严格口径假（双口径确实是两个数）",
      S._is_silent(_h_ret)[0] is True and S._is_strict_silent(_h_ret) is False,
      "wide=%s strict=%s" % (S._is_silent(_h_ret), S._is_strict_silent(_h_ret)))

_h_raise = _handler_of("try:\n    x()\nexcept Exception:\n    raise\n")
check("M4 `except: raise` 判为非静默",
      S._is_silent(_h_raise)[0] is False and S._is_strict_silent(_h_raise) is False,
      "%s / %s" % (S._is_silent(_h_raise), S._is_strict_silent(_h_raise)))

# 降级台账调用也算可见动作 —— 这是本次 P0 的改造方向，必须被认可
_h_note = _handler_of(
    "try:\n    x()\nexcept Exception as e:\n    from core.degrade import note as n\n    n('a.b', e)\n")
check("M5 接入 degrade.note 后判为非静默（治理动作真的能降数）",
      S._is_silent(_h_note)[0] is False,
      repr(S._is_silent(_h_note)))

# ══ ② 全仓扫描 + 双口径自洽 ═══════════════════════════════════════════
print("[2] 全仓扫描与口径自洽", flush=True)
res = S.scan()
check("S1 扫描产出结构完整",
      all(k in res for k in ("except_total", "silent_total", "strict_silent_total",
                             "by_dir", "items", "parse_fail")),
      list(res.keys()))
check("S2 有扫描到文件与 except",
      res["scanned_files"] > 100 and res["except_total"] > 100,
      "files=%s except=%s" % (res["scanned_files"], res["except_total"]))
check("S3 严格口径 ⊆ 宽口径（strict ≤ wide，否则判定逻辑自相矛盾）",
      0 <= res["strict_silent_total"] <= res["silent_total"],
      "strict=%s wide=%s" % (res["strict_silent_total"], res["silent_total"]))
_sum_dir = sum(v["silent"] for v in res["by_dir"].values())
check("S4 分目录合计 == 总数（没有条目落在统计之外）",
      _sum_dir == res["silent_total"],
      "sum_dir=%s total=%s" % (_sum_dir, res["silent_total"]))
check("S5 items 条数与 silent_total 一致",
      len(res["items"]) == res["silent_total"],
      "items=%s total=%s" % (len(res["items"]), res["silent_total"]))

# ══ ③ 基线比对：不许变多 ═════════════════════════════════════════════
print("[3] 基线比对（只卡劣化，不卡绝对值）", flush=True)
baseline = None
try:
    with open(BASELINE, "r", encoding="utf-8") as f:
        baseline = json.load(f)
except Exception:
    baseline = None

_plain = {"generated_at": res["generated_at"],
          "scanned_files": res["scanned_files"],
          "except_total": res["except_total"],
          "silent_total": res["silent_total"],
          "strict_silent_total": res["strict_silent_total"],
          "broad_except_total": res["broad_except_total"],
          "broad_silent_total": res["broad_silent_total"],
          "note": ("⚠️ 本基线由 tools/scan_silent_except.py 生成，"
                   "与《系统架构合理性评估-20261124.md》里的「1980/1361/69%」"
                   "**不可直接比对**：报告未公布扫描范围与判据实现。"
                   "本机实测 `except Exception` 基数更大（见 broad_except_total）。"
                   "后续所有引用一律以本文件为准；要更新基线必须跑一次门禁并人工确认。"),
          "by_dir": res["by_dir"],
          # ★ 2026-11-27：**逐文件账**。只比总数是不成立的：扫描面一变（新增分层目录/
          #   新模块），总数必然变化，而"变红"与"真的多写了静默兜底"在总数上看不出区别 ——
          #   实测 437 → 547 个文件使总数 2564 → 2648，红得毫无信息量。
          #   有了逐文件账，判定可以落到「**同一个文件**里有没有变多」，这才是
          #   "这次改动有没有增加静默兜底"的真判据。
          # ⚠️ 必须覆盖**扫描面全量**（含 0 条的文件）：只记"有静默的文件"会把
          #   一家老文件从 0→1 误判成"新增文件"（归因错通道）。
          "files": {_f: sum(1 for _x in res["items"] if _x["file"] == _f)
                    for _f in sorted(set(res.get("scanned") or []))},
          "strict_files": {_f: sum(1 for _x in res["items"]
                                   if _x["file"] == _f and _x.get("strict"))
                           for _f in sorted(set(res.get("scanned") or []))},
          # 新增文件允许的静默额度（默认 0 = 新文件不该带静默兜底；要放宽必须显式改这个数）
          "new_files_allow": 0}
# ★ 2026-11-27：补上 `--update-baseline`。此前 B3 的提示语写着"可执行 --update-baseline 固化"，
#   但**该开关根本没实现** —— 提示指向一个不存在的动作，比不提示更糟。
#   纪律：更新基线必须带 `--reason`（一段说明），且旧/新总数会一起写进 note，
#   让"这次放宽了多少"永远可追溯，而不是覆盖式静默改写。
_UPDATE = "--update-baseline" in sys.argv
_REASON = ""
if "--reason" in sys.argv:
    try:
        _REASON = sys.argv[sys.argv.index("--reason") + 1]
    except IndexError:
        _REASON = ""

if baseline is None:
    # 首次跑：写基线并**显式提示**（不能静默创建 —— 静默创建等于给了自己一个
    # "随便多少都算合规"的起点，这正是本项目反对的那种做法）
    with open(BASELINE, "w", encoding="utf-8") as f:
        json.dump(_plain, f, ensure_ascii=False, indent=1)
    check("B0 基线不存在 → 已创建并打印（首次运行）", True,
          "写入 %s：宽=%s 严=%s" % (BASELINE, res["silent_total"],
                                    res["strict_silent_total"]))
else:
    _bw = int(baseline.get("silent_total") or 0)
    _bs = int(baseline.get("strict_silent_total") or 0)
    _bf = baseline.get("files")
    _bsf = baseline.get("strict_files") or {}
    if not isinstance(_bf, dict) or not _bf:
        # 老基线（只有总数）：**保留**总数判据，但把"口径不可比"说清楚，
        # 不让它看起来像"这次改动把它弄红了"。
        check("B1 宽口径未劣化（silent_total ≤ 基线）",
              res["silent_total"] <= _bw,
              "now=%s baseline=%s；⚠️ 该基线**无逐文件账**，总数比对在扫描面变化时不可比"
              "（现扫描 %d 文件）" % (res["silent_total"], _bw, res["scanned_files"]))
        check("B2 严格口径未劣化（strict_silent_total ≤ 基线）",
              res["strict_silent_total"] <= _bs,
              "now=%s baseline=%s" % (res["strict_silent_total"], _bs))
    else:
        # ── 逐文件同口径比对（真判据）──
        # ⚠️ 必须以**扫描面全量**为键（含 0 条的文件），不能只从 items 建。
        #   只从 items 建的话：所有"0 条静默"的文件都不在 now_f 里 ⇒
        #   ① B1 漏判"0→N"（新写的静默落在原本干净的文件上，那是最常见的情形）；
        #   ② B4 把这几百个 0 条文件全部误报成"消失"（实测报了 200 个）。
        now_f = {f: 0 for f in (res.get("scanned") or [])}
        for _x in res["items"]:
            now_f[_x["file"]] = now_f.get(_x["file"], 0) + 1
        now_sf = {f: 0 for f in (res.get("scanned") or [])}
        for _x in res["items"]:
            if _x.get("strict"):
                now_sf[_x["file"]] = now_sf.get(_x["file"], 0) + 1
        common = set(_bf) & set(now_f)
        worse = sorted("%s %s→%s" % (f, _bf[f], now_f.get(f, 0))
                       for f in common if now_f.get(f, 0) > int(_bf[f] or 0))
        worse_s = sorted("%s %s→%s" % (f, _bsf.get(f, 0), now_sf.get(f, 0))
                         for f in common if now_sf.get(f, 0) > int(_bsf.get(f, 0) or 0))
        check("B1 共同文件集内宽口径无劣化（逐文件同口径）", not worse, worse[:6])
        check("B2 共同文件集内严格口径无劣化（逐文件同口径）", not worse_s, worse_s[:6])
        # 新增文件：不该带静默兜底；要放宽必须显式改 new_files_allow（额度必须记账）
        new_files = sorted(set(now_f) - set(_bf))
        new_silent = sum(now_f[f] for f in new_files)
        allow = int(baseline.get("new_files_allow") or 0)
        check("B3 新增文件的静默数 ≤ 记账额度（new_files_allow）",
              new_silent <= allow,
              "新增文件 %d 个、静默 %d 条 > 额度 %d：%s"
              % (len(new_files), new_silent, allow,
                 ["%s(%d)" % (f, now_f[f]) for f in new_files if now_f.get(f)][:6]))
        # 消失的文件（被删/搬迁）单独报：静默"变少"可能只是范围缩小（本仓记过的同族坑）
        gone = sorted(set(_bf) - set(now_f))
        if gone:
            check("B4 基线里的文件未静默消失（防「少扫=变好」）", False,
                  "消失 %d 个：%s" % (len(gone), gone[:6]))
        check("B5 总账（信息项：允许随扫描面变化，不作为劣化判据）", True,
              "now %d/%d（基线 %d/%d，扫描面 %d 文件）"
              % (res["silent_total"], res["strict_silent_total"], _bw, _bs,
                 res["scanned_files"]))

if _UPDATE:
    if not _REASON:
        check("B6 更新基线必须带 --reason（放宽多少要可追溯）", False,
              "用法：--update-baseline --reason '……'")
    else:
        _old_note = baseline.get("note", "") if baseline else ""
        _plain["note"] = (
            "%s\n\n── 基线更新 %s ──\n原因：%s\n"
            "旧：silent=%s strict=%s 扫描面=%s 文件\n"
            "新：silent=%s strict=%s 扫描面=%s 文件\n"
            "⚠️ 本次更新**吸收了未被逐条归因的差额**：宽 %+d / 严 %+d（含扫描面扩大的影响）。"
            "自本条起判定改为**逐文件同口径**（B1/B2），因此该差额不会掩盖后续新增。"
            % (_old_note, res["generated_at"], _REASON,
               (baseline or {}).get("silent_total"), (baseline or {}).get("strict_silent_total"),
               (baseline or {}).get("scanned_files"),
               res["silent_total"], res["strict_silent_total"], res["scanned_files"],
               res["silent_total"] - int((baseline or {}).get("silent_total") or 0),
               res["strict_silent_total"] - int((baseline or {}).get("strict_silent_total") or 0)))
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(_plain, f, ensure_ascii=False, indent=1)
        check("B6 基线已更新（含新旧总数与原因，差额可追溯）", True,
              "写 %s（宽 %d / 严 %d）" % (BASELINE, res["silent_total"],
                                       res["strict_silent_total"]))

# ══ ④ E2E：临时目录造真文件，断言扫描器真的走 AST ════════════════════
print("[4] E2E（临时目录真文件）", flush=True)
_tmp = tempfile.mkdtemp(prefix="dg_scan_")
try:
    with open(os.path.join(_tmp, "fake_mod.py"), "w", encoding="utf-8") as f:
        f.write("def query_entities(conn):\n"
                "    try:\n"
                "        return conn.execute('select 1').fetchall()\n"
                "    except Exception:\n"
                "        pass\n"
                "\n"
                "def write_row(conn):\n"
                "    try:\n"
                "        conn.execute('insert 1')\n"
                "    except Exception as e:\n"
                "        logger.warning('x', e)\n")
    _r = S.scan(_tmp)
    check("E1 临时目录里 `except: pass` 被抓到（AST 路径真的在走）",
          _r["silent_total"] == 1 and _r["strict_silent_total"] == 1,
          "wide=%s strict=%s" % (_r["silent_total"], _r["strict_silent_total"]))
    _it = (_r["items"] or [{}])[0]
    check("E2 抓到的是 query_entities 那处，且被标记为疑似主流程",
          _it.get("func") == "query_entities" and _it.get("suspect_main") is True,
          repr(_it))
finally:
    try:
        for _f in os.listdir(_tmp):
            os.unlink(os.path.join(_tmp, _f))
        os.rmdir(_tmp)
    except OSError:
        pass

# ══ ⑤ 已接入台账的模块必须被认可为非静默 ═════════════════════════════
print("[5] 本次 P0 改造点的静态自证", flush=True)
_res_by_file = {}
for _x in res["items"]:
    _res_by_file.setdefault(_x["file"], []).append(_x)


def _silent_at(rel: str, func: str) -> bool:
    return any(x.get("func") == func for x in _res_by_file.get(rel, []))


check("P1 metrics_core 的指标兜底已不再静默（已接 degrade）",
      not any(x.get("func") in ("alias_coverage", "dedup_state", "release_branches")
              for x in _res_by_file.get("services/metrics_core.py", [])),
      [x.get("func") for x in _res_by_file.get("services/metrics_core.py", [])][:8])
check("P2 ingest_gate.gate_mode 已不再静默（门禁模式读取失败可见）",
      not _silent_at("core/domain/ingest_gate.py", "gate_mode"),
      [x.get("func") for x in _res_by_file.get("core/domain/ingest_gate.py", [])][:8])

print("\n=== %d PASS / %d FAIL / %d SKIP ===" % (len(PASS), len(FAIL), len(SKIPPED)), flush=True)
if SKIPPED:
    print("SKIP 项（未验证，不等于通过）：%s" % ", ".join(SKIPPED), flush=True)
if FAIL:
    print("FAIL: %s" % ", ".join(FAIL), flush=True)
sys.exit(1 if FAIL else 0)
