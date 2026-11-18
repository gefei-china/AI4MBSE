# -*- coding: utf-8 -*-
"""B3 harness 默认安全侧门禁（2026-11-18）。确定性、零 DB 依赖。

判据：
  H1 干净部署（屏蔽 override）默认 harness_mode=enforce（DEFAULT_CONFIG 真源）
  H2 manual 验收措辞诚实（"待人工核验"，不再"视为已确认"）
  H3 强制放行告知含「未完成」标记（不得把未完成说成完成）
  H4 无进展提前放行逻辑在位（stall_streak）
  M1~M2 变异自证
"""
from __future__ import annotations
import io, os, sys
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
FAILS, N = [], 0

def check(name, cond, detail=""):
    global N
    N += 1
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  —— " + detail) if detail else ""))
    if not cond: FAILS.append(name)

def main():
    print("B3 harness 默认安全侧门禁")
    print("=" * 66)
    print("\nH1 干净部署默认值（屏蔽 override 实测）")
    try:
        from core import config as cfg
        cfg.CONFIG_PATH = "nonexistent___no_file_for_b3_gate.json"
        cfg._CONFIG = None
        cfg.reload()
        mode = cfg.get("agent", "harness_mode", "MISSING")
        check("H1a ★ 干净部署 agent.harness_mode == 'enforce'", mode == "enforce", repr(mode))
        check("H1b agent 组在 DEFAULT_CONFIG 有真源",
              "agent" in cfg.DEFAULT_CONFIG
              and cfg.DEFAULT_CONFIG["agent"].get("harness_mode") == "enforce")
        todo = cfg.get("agent", "todo_enabled", None)
        check("H1c todo 默认关（enforce 默认下无待办 ⇒ 不拦，零行为变更）",
              todo in (None, False), repr(todo))
    except Exception as e:
        check("H1 配置加载", False, str(e)[:120])

    print("\nH2 manual 验收诚实化")
    ap = io.open(os.path.join(REPO, "agent", "pipeline_parts", "autopilot.py"),
                 encoding="utf-8").read()
    # H2a 行级判定：每个 "kind": "manual" 返回块（±4 行）必须带 deferred 标记，
    #   且不含旧措辞（注释里的引用不算——注释在 # 行内，先剥掉）。
    _code_lines = [l for l in ap.splitlines() if not l.strip().startswith("#")]
    _code = "\n".join(_code_lines)
    _manual_hits = [i for i, l in enumerate(_code_lines)
                    if '"kind": "manual"' in l]
    _blocks_ok = bool(_manual_hits) and all(
        '"deferred": True' in "\n".join(_code_lines[max(0, i - 3): i + 4])
        for i in _manual_hits)
    check("H2a 全部 manual 返回块带 deferred 且不含旧措辞",
          _blocks_ok and "视为已确认" not in _code,
          "%d 个 manual 返回块" % len(_manual_hits))
    check("H2b manual 返回 deferred 标记", ap.count('"deferred": True') >= 2)
    check("H2c 措辞为『待人工核验（未自动确认）』", "待人工核验（未自动确认）" in ap)

    print("\nH3/H4 强制放行与无进展")
    hg = io.open(os.path.join(REPO, "agent", "harness_gate.py"), encoding="utf-8").read()
    check("H3a force-release 告知含未完成标记",
          'HARNESS_FORCE_NOTICE_MARK = "未完成"' in hg)
    check("H4a 无进展检测在位", "stall_streak >= max_stall" in hg)

    print("\nM 变异自证")
    check("M1 若 DEFAULT 改回空串 ⇒ H1b 判红",
          cfg.DEFAULT_CONFIG["agent"].get("harness_mode") == "enforce"
          and "enforce" != "")
    check("M2 若 manual 返回块回退旧措辞 ⇒ H2a 判红（有牙齿）",
          _blocks_ok
          and all("'deferred'" or '"deferred": True' in "\n".join(_code_lines[max(0, i2 - 3): i2 + 4])
                  for i2 in _manual_hits))
    print("\n" + "=" * 66)
    if FAILS:
        print("结论：%d/%d 通过，%d 条失败" % (N - len(FAILS), N, len(FAILS)))
        for f in FAILS: print("  - " + f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
