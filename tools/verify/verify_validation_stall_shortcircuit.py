# -*- coding: utf-8 -*-
"""门禁：校验停滞在"重复调用短路"分支也要短路（VALIDATION_STALL_IN_DUP_V1）——CI-OPTIONAL。

真机 conv 999999977（2026-11-20，米爸报"会话因校验工具短路一直完不成"）：
sysml_v2_validate 连跑 3 次，第 2/3 次被 dedup 短路（lat 91/97ms）却回喂同样的
"判据 block / 40 错"原文 ⇒ 模型继续修 → 继续短路 → 死循环。
根因：validation_stalled 只装在"真执行"分支之后，dedup 短路分支走不到。
修法：短路当下用 is_validation_block_result + validation_stalled 检测，命中改回喂停滞说明。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

PASS = FAIL = 0
FAILS = []


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print("  PASS ", name)
    else:
        FAIL += 1; FAILS.append(name); print("  FAIL ", name, extra)


def main():
    from agent.loop_guard import LoopGuard, is_validation_block_result as ib

    print("\n=== P 纯函数：block 回执识别 ===")
    block = "【SysML v2 校验】判据：block —— 存在硬错，必须修复后重新校验\nERROR 共 40 条"
    check("P1 block 回执 → True", ib("sysml_v2_validate", {"result": block}) is True)
    check("P2 pass 回执 → False",
          ib("sysml_v2_validate", {"result": "判据：pass —— 无词法语法错"}) is False)
    check("P3 非校验工具 → False", ib("file_read", {"result": block}) is False)
    check("P4 空结果 → False", ib("sysml_v2_validate", {}) is False)

    print("\n=== P5-P6 停滞计数（3 次同签名命中）===")
    lg = LoopGuard()
    flags = [lg.validation_stalled("sysml_v2_validate", {"result": block}) for _ in range(3)]
    check("P5 同 block 签名第 3 次命中停滞", flags == [False, False, True], str(flags))
    # ★ 2026-11-24：期望串去字面化 —— 2026-11-23（5c043e2）把 payload 文案从
    #   "不要再用同样参数重跑" 改成 "不要用同样参数重跑"，该断言随之断裂（字段一直在）。
    #   判据意图不变：payload 必须明确劝阻"同样参数重跑"，故用更短的稳定子串。
    check("P6 停滞 payload 含'不要用同样参数重跑'",
          "不要用同样参数重跑" in lg.validation_stall_payload()["result"])
    # ★ 2026-11-24（run 69 死锁实证）：pass / report（n_hard=0）**永不算停滞**，
    #   否则停滞 payload 会覆盖已经通过的校验回执 ⇒ 待办验收永远读不到 pass ⇒ 无限续跑。
    lg2 = LoopGuard()
    _pass = {"result": "【SysML v2 校验】判据：pass —— 通过 —— 无词法/语法错"}
    # ★ 2026-11-24（run 71 实证 8 次 pass 白烧 2 分钟）：pass **第 2 次**起短路，
    #   但必须是"已通过/勿重复"的文案，不能是"停滞/有错"那份（否则会让模型改好的代码）。
    _pf = [lg2.validation_stalled("sysml_v2_validate", _pass) for _ in range(3)]
    check("P7 pass 第 2 次起短路（已达标不再重复校验）", _pf == [False, True, True], str(_pf))
    check("P7b pass 短路文案是'已通过'而非'停滞/有错'",
          ("已通过" in lg2.validation_pass_repeat_payload()["result"])
          and ("不要再调用" in lg2.validation_pass_repeat_payload()["result"]))
    lg3 = LoopGuard()
    _report = {"result": "【SysML v2 校验】判据：report —— 有语义错但无硬错（不阻断） 语义:54"}
    check("P8 report(n_hard=0) 首次不判停滞（已达验收口径）",
          not lg3.validation_stalled("sysml_v2_validate", _report))
    lg4 = LoopGuard()
    for _ in range(3):
        lg4.validation_stalled("sysml_v2_validate", {"result": block})
    check("P9 停滞后再 pass ⇒ 计数复位（不是永久黑名单）",
          (not lg4.validation_stalled("sysml_v2_validate", _pass))
          and (not lg4.validation_stalled("sysml_v2_validate", {"result": block})))

    print("\n=== B 行为验证：短路路径必须真能跑，不能只靠源码字符串匹配 ===")
    # ★★ 2026-11-21 事故：上一版门禁只做 `"xxx" in source` 字符串匹配，
    #   而 stream.py 当时写的是 `_lg.is_validation_block_result(...)` ——
    #   该函数是**模块级**纯函数、不是 LoopGuard 方法 ⇒ 运行期 AttributeError
    #   掀翻整轮，门禁却全绿。教训：字符串出现 ≠ 调用可行，必须真跑一遍。
    import inspect as _inspect
    from agent import loop_guard as _lgmod
    check("B1 is_validation_block_result 是模块级函数（不是类方法）",
          _inspect.isfunction(_lgmod.is_validation_block_result)
          and not hasattr(LoopGuard, "is_validation_block_result"))
    # 复刻短路分支的真实调用组合：模块级判 block + 实例方法判停滞
    _g = LoopGuard()
    _ok = True
    for _ in range(3):
        _prev = block
        _hit = (_lgmod.is_validation_block_result("sysml_v2_validate", _prev)
                and _g.validation_stalled("sysml_v2_validate", {"result": _prev}))
    check("B2 模块级判 block + 实例判停滞：连跑 3 次不抛异常且第 3 次命中", _hit is True)
    # 反证：若按旧写法用实例调模块级函数，必须抛 AttributeError（证明 B1/B2 有牙齿）
    _raised = False
    try:
        LoopGuard().is_validation_block_result("sysml_v2_validate", {"result": block})
    except AttributeError:
        _raised = True
    check("B3 实例调用该函数确实抛 AttributeError（旧写法必炸，反证非空转）", _raised)

    print("\n=== S 源码接线（两条 ReAct 路径必须同源）===")
    s = open(os.path.join(ROOT, "agent/pipeline_parts/stream.py"), encoding="utf-8").read()
    e = open(os.path.join(ROOT, "agent/pipeline_parts/execute.py"), encoding="utf-8").read()
    check("S1 流式：短路分支用模块级别名判 block", "_is_validation_block(tname, _dup_prev)" in s)
    check("S2 流式：短路分支调 validation_stalled", "validation_stalled(tname, {\"result\": _dup_prev})" in s)
    def _code_has(src: str, needle: str) -> bool:
        """只在**非注释代码行**里找 needle（注释里作为反例提及不算）。
        2026-11-21：本门禁上一版用裸 `in s` 判"无旧写法"，结果被注释里的
        反例文字误判为红 —— 检查必须精准，否则要么假红要么假绿。"""
        for ln in src.splitlines():
            code = ln.split("#", 1)[0]
            if needle in code and not code.strip().startswith('#'):
                return True
        return False

    check("S3 流式：**无** 实例调用模块级函数的旧写法（仅查代码行）",
          not _code_has(s, "_lg.is_validation_block_result"))
    check("S4 流式：停滞时改回喂 validation_stall_payload",
          s.count("validation_stall_payload()") >= 2)
    check("S5 流式：收敛闸校验停滞时 break（不再推回校验）",
          "if _validate_stalled:" in s and "校验停滞收敛" in s)
    check("S6 流式：每轮重置 _validate_stalled", "_validate_stalled = False" in s)
    check("S7 流式：停滞标志在短路分支置位", "_validate_stalled = True" in s)
    check("S8 非流式：短路分支也用模块级别名判 block", "_is_validation_block(tname, _dup_prev)" in e)
    check("S9 非流式：短路分支调 validation_stalled", "validation_stalled(tname, {\"result\": _dup_prev})" in e)
    check("S10 非流式：**无** 实例调用旧写法（仅查代码行）",
          not _code_has(e, "_guard.is_validation_block_result"))

    print("\n=== M 变异自证：block 判据说成 pass ⇒ P1 必须翻红 ===")
    fp = os.path.join(ROOT, "agent/loop_guard.py")
    orig = open(fp, "rb").read()
    mutated = orig.decode("utf-8").replace("\r\n", "\n").replace(
        'return ("判据：block" in txt or "判据:block" in txt\n            or "存在硬错" in txt)',
        'return False').encode("utf-8")
    if mutated == orig:
        check("M0 变异锚点存在", False)
    else:
        try:
            open(fp, "wb").write(mutated)
            import importlib
            import agent.loop_guard as _m
            importlib.reload(_m)
            check("M1 关掉 block 识别后 P1 翻红",
                  _m.is_validation_block_result("sysml_v2_validate", {"result": block}) is False)
        finally:
            open(fp, "wb").write(orig)
            import importlib
            import agent.loop_guard as _m
            importlib.reload(_m)
            check("M2 还原后重新命中",
                  _m.is_validation_block_result("sysml_v2_validate", {"result": block}) is True)

    print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
    if FAILS: print("失败项：" + " / ".join(FAILS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
