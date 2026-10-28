# -*- coding: utf-8 -*-
"""门禁：P0-5 障碍恢复（真实执行路径 + 真实失败）。

方案：docs/持续自主执行能力-最终方案设计-20261028.md §4.4 / §6 第 5 步。

为什么走 `_log_tool_call`（本门禁也据此验证）：
  `_exec_tool_call` 有 10+ 个失败出口（白名单/RBAC/HIL/超时/异常…），
  逐个补必然漏；而**所有出口都汇入 `_log_tool_call`** ⇒ 改这一处，一条不错。

真机判据（全部可证伪）：
  ① 开关关 ⇒ 失败结果**不带**障碍提示（行为不变）
  ② 开关开 + auth 类失败 ⇒ 追加「auth」类提示，且含「不要直接问用户」
  ③ 开关开 + not_found 类 ⇒ 提示换路径
  ④ ★ 权限类失败 ⇒ **不**追加（确实该找人，不能诱导模型绕过权限）
  ⑤ ok=True ⇒ 不追加（成功不该有恢复建议）
  ⑥ 平台独立回读：`result["obstacle"]` 结构可被程序消费

用法：
  python tools/verify/verify_obstacle_p05.py
退出码 0 = 全绿；1 = 有红项。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

RESULTS = []


def check(group, name, cond, detail=""):
    RESULTS.append((group, name, bool(cond), detail))


def main():
    print("=" * 74)
    print("门禁：P0-5 障碍恢复")
    print("=" * 74)

    from core import config as _cfg
    from agent.pipeline_parts.tools import ToolMixin

    orig = _cfg.as_bool

    def set_switch(on: bool):
        _cfg.as_bool = lambda sec, key, dft=None: (
            on if (sec, key) == ("agent", "obstacle_recovery_enabled")
            else orig(sec, key, dft))

    obj = ToolMixin()

    def log_and_get(result):
        """走真实的 _log_tool_call 收口（这正是生产路径的最后一站）。"""
        r = dict(result)          # 传副本，便于断言
        obj._log_tool_call("fake_tool", "builtin", {}, r,
                           {"intent": ""}, {"agent": ""}, 0, 0.0)
        return r

    try:
        # ══ A. 配置默认关 ══
        cfg_src = open(os.path.join(_ROOT, "core", "config.py"),
                       encoding="utf-8").read()
        check("A", "obstacle_recovery_enabled 配置项存在",
              '"obstacle_recovery_enabled"' in cfg_src)
        check("A", "★默认关（False）",
              '_as_bool("agent", "obstacle_recovery_enabled", False)' in
              open(os.path.join(_ROOT, "agent", "pipeline_parts", "tools.py"),
                   encoding="utf-8").read())

        # ══ B. 开关关 ⇒ 不追加（行为不变）══
        set_switch(False)
        r = log_and_get({"ok": False, "result": "401 Unauthorized 凭据失效"})
        check("B", "★开关关 ⇒ 结果不含障碍提示",
              "【障碍恢复建议】" not in str(r.get("result")),
              str(r.get("result"))[:120])
        check("B", "开关关 ⇒ 无 obstacle 字段", "obstacle" not in r)

        # ══ C. 开关开 + auth ⇒ 追加并明确"不要问用户" ══
        set_switch(True)
        r = log_and_get({"ok": False, "result": "401 Unauthorized 凭据失效"})
        txt = str(r.get("result") or "")
        check("C", "★auth 类失败追加障碍提示", "【障碍恢复建议】" in txt, txt[:150])
        check("C", "★提示含障碍类型 auth", "auth" in txt, txt[:150])
        check("C", "★提示含「不要直接问用户」", "不要" in txt and "问用户" in txt, txt[:200])
        check("C", "★提示含恢复动作（刷新凭据）", "刷新凭据" in txt, txt[:200])
        check("C", "obstacle 结构可被程序消费",
              isinstance(r.get("obstacle"), dict)
              and r["obstacle"].get("kind") == "auth"
              and r["obstacle"].get("ask_user_allowed") is False,
              str(r.get("obstacle"))[:150])
        check("C", "★原始失败信息未被丢弃",
              "401 Unauthorized" in txt, txt[:120])

        # ══ D. not_found ⇒ 换路径 ══
        r = log_and_get({"ok": False, "result": "404 not found 包不存在"})
        txt = str(r.get("result") or "")
        check("D", "★not_found 类提示换路径",
              "not_found" in txt and ("换路径" in txt or "list/search" in txt), txt[:200])
        check("D", "not_found 不允许问用户",
              r.get("obstacle", {}).get("ask_user_allowed") is False,
              str(r.get("obstacle"))[:120])

        # ══ E. ★ 权限类失败不追加（不能诱导绕过权限）══
        r = log_and_get({"ok": False, "permission_denied": True,
                         "result": "权限不足：当前用户没有 agent_tool:read 权限"})
        check("E", "★permission_denied 不追加障碍提示",
              "【障碍恢复建议】" not in str(r.get("result")),
              str(r.get("result"))[:150])
        r = log_and_get({"ok": False, "permission_error": True,
                         "result": "权限不足"})
        check("E", "★permission_error 不追加障碍提示",
              "【障碍恢复建议】" not in str(r.get("result")),
              str(r.get("result"))[:150])

        # ══ F. 成功不追加 ══
        r = log_and_get({"ok": True, "result": "查询成功：3 条"})
        check("F", "ok=True 不追加障碍提示",
              "【障碍恢复建议】" not in str(r.get("result")),
              str(r.get("result"))[:120])
        check("F", "ok=True 无 obstacle 字段", "obstacle" not in r)

        # ══ G. 未识别错误 ⇒ 保守当 transient（不误判 user_only）══
        r = log_and_get({"ok": False, "result": "某个从没见过的怪错误 xyzzy"})
        ob = r.get("obstacle") or {}
        check("G", "★未识别错误 ⇒ transient（不误判成要问人）",
              ob.get("kind") == "transient" and ob.get("ask_user_allowed") is False,
              str(ob)[:150])

        # ══ H. 变异自证 ══
        # H1 判据 c_judge：真实现上为 True，变异（不追加）上为 False
        def c_judge(res) -> bool:
            return ("【障碍恢复建议】" in str(res.get("result") or "")
                    and "不要" in str(res.get("result") or ""))

        real = log_and_get({"ok": False, "result": "401 Unauthorized"})
        bad = {"ok": False, "result": "401 Unauthorized"}   # 变异：不做任何追加
        check("H", "★变异自证：不追加提示 ⇒ auth 判据翻红",
              c_judge(real) and not c_judge(bad),
              f"真实现={c_judge(real)} 变异={c_judge(bad)}")

        # H2 变异：连权限类也追加 ⇒ E 组判据必须翻红
        #    （用"假设实现改坏"的方式验：直接构造一个带提示的权限失败）
        bad_perm = {"ok": False, "permission_denied": True,
                    "result": "权限不足【障碍恢复建议】auth 类：刷新凭据"}
        check("H", "★变异自证：权限失败也追加 ⇒ E 组判据翻红",
              ("【障碍恢复建议】" not in str(
                  log_and_get({"ok": False, "permission_denied": True,
                               "result": "权限不足"}).get("result")))
              and ("【障碍恢复建议】" in str(bad_perm.get("result"))),
              "变异实现未被拦住")

        # ══ I. 真实 _exec_tool_call 路径（不是只调 _log_tool_call）══
        #   调一个**必然失败**的工具，走完整 _exec_tool_call ⇒ 确认收口点真的覆盖到
        r2 = obj._exec_tool_call("definitely_not_a_real_tool_xyzzy", {})
        txt2 = str((r2 or {}).get("result") or "")
        check("I", "★真实 _exec_tool_call 失败路径也带障碍提示",
              "【障碍恢复建议】" in txt2, txt2[:200])
        check("I", "★真实路径 obstacle 结构存在",
              isinstance((r2 or {}).get("obstacle"), dict),
              str((r2 or {}).get("obstacle"))[:150])
    finally:
        _cfg.as_bool = orig

    # ── 输出 ──
    groups = {}
    for g, n, ok, d in RESULTS:
        groups.setdefault(g, []).append((n, ok, d))
    total = failed = 0
    for g in sorted(groups):
        print(f"\n【{g} 组】")
        for n, ok, d in groups[g]:
            total += 1
            if not ok:
                failed += 1
            line = f"  [{'PASS' if ok else 'FAIL'}] {n}"
            if d and not ok:
                line += f"   ← {d}"
            print(line)
    print("\n" + "-" * 74)
    print(f"合计 {total} 项，通过 {total - failed}，失败 {failed}")
    print("-" * 74)
    if failed:
        print("❌ 门禁未通过")
        return 1
    print("✅ 全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
