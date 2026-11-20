# -*- coding: utf-8 -*-
"""门禁：正文里的祈使式确认必须自动成卡（PROSE_CONFIRM_CARD_V1）。

用户原话（2026-11-20）：「AI 输出中有项用户询问确认的描述，结果并未出现澄清卡片，
这个问题屡次出现，无法彻底解决吗？」

根因：模型只在**正文**写"请你确认"，不调 `ask_user` ⇒ 后端无 clarify_ask 事件、
pending_clarify 为空 ⇒ 界面无卡、用户无处作答。
判据：
  P 纯函数：祈使式命中 / 陈述式不命中 / 已有卡不重复 / 只扫尾部
  S 源码：主路径接线（正文定稿后探测、dry_run 不触发、done 带 clarify 标记）
  M 变异：把探测短路成 None ⇒ P 的"应命中"用例必须翻红
"""
# CI-OPTIONAL: 散文式确认自动成卡：纯函数 + 源码接线 + 变异自证，暂不进 CI

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
PASS = FAIL = 0
FAILS = []


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  FAIL  %s  << %s" % (name, detail))


def main():
    print("门禁：散文式确认自动成卡（PROSE_CONFIRM_CARD_V1）")
    from agent.pipeline_parts.stream import detect_prose_confirm as det

    print("\n=== P 纯函数 ===")
    r = det("模型与校验已完成。\n\n以上为写入确认单，请你确认后执行。")
    check("P1 祈使式确认命中并取出问题", bool(r) and "确认" in r.get("question", ""), str(r))
    check("P2 选项可用（继续/改动/不继续）+ 允许自定义",
          bool(r) and len(r["options"]) == 3 and r["allow_custom"] is True, str(r))
    check("P3 陈述式「待确认项」清单不误触发",
          det("结论如下。\n\n## 缺口与待确认项\n1. 阈值未定\n2. 父容器未确认") is None)
    check("P4 「无需确认」不误触发", det("已全部完成，无需确认。") is None)
    check("P5 已有卡片时不重复弹", det("请你确认", has_card=True) is None)
    check("P6 只扫尾部：正文中段提及不触发",
          det("请你确认这点很重要。\n" + "正文" * 800 + "\n完毕。") is None)
    check("P7 尾部命中（长正文）仍然有效",
          bool(det("正文" * 600 + "\n最后请你确认是否继续。")))
    check("P8 空输入不崩", det("") is None and det(None) is None)
    # 2026-11-20 误报修复：模型在解释自己的确认规则时不该成卡
    check("P9 规则元说明「等你确认后才执行」不误触发",
          det("我是系统管理助手。等你确认后才执行写操作。以上是我的工作方式。") is None)
    check("P10 规则元说明「需要你确认…技术故障不会拿来问你」不误触发",
          det("需要你确认\n\n技术故障（工具报错、超时、授权失效、数据取不到）不会拿来问你怎么办——我会自行降级或跳过，") is None)
    check("P11 真交互请求「请你确认后执行」仍命中（防排除过宽）",
          bool(det("模型与校验已完成。\n\n以上为写入确认单，请你确认后执行。")))

    print("\n=== S 源码接线 ===")
    s = open(os.path.join(ROOT, "agent/pipeline_parts/stream.py"), encoding="utf-8").read()
    check("S1 主路径探测存在且是纯函数", "def detect_prose_confirm(" in s)
    check("S2 子任务（dry_run）不自己弹卡 —— 交给编排层统一",
          "if not dry_run:\n                    _prose_confirm_q = detect_prose_confirm(" in s)
    check("S3 发 clarify_ask 事件（前端据此建卡）",
          '"source": "prose_confirm"' in s)
    check("S4 落 pending_clarify（否则答了也不续跑）",
          "self._persist_clarify(" in s and "已生成确认卡" in s)
    check("S5 done 带 msg_type='clarify'（前端保留正文 + 插卡）",
          '**({"clarify_asked": True, "msg_type": "clarify",' in s)
    check("S6 卡片消息 content 不写正文（防消息重复）",
          'content="⏸ 正文里提出了确认请求，已生成确认卡：%s"' in s)

    print("\n=== M 变异自证：探测恒 None，P1 必须翻红 ===")
    fp = os.path.join(ROOT, "agent/pipeline_parts/stream.py")
    orig = open(fp, "rb").read()
    # ★ 2026-11-20：锚点必须**换行无关**（本机 autocrlf=true ⇒ CRLF，CI 是 LF）。
    #   归一成 LF 再匹配；还原用 `orig` 原始字节，行尾逐字节不变。
    mutated = orig.decode("utf-8").replace("\r\n", "\n").replace(
        "    s = str(text or \"\")\n    if has_card or not s.strip():",
        "    s = str(text or \"\")\n    if True:").encode("utf-8")
    if mutated == orig:
        check("M0 变异锚点存在", False)
    else:
        try:
            open(fp, "wb").write(mutated)
            import importlib
            import agent.pipeline_parts.stream as _st
            importlib.reload(_st)
            check("M1 探测关掉后 P1 用例失败（断言非空转）",
                  _st.detect_prose_confirm("请你确认后执行。") is None)
        finally:
            open(fp, "wb").write(orig)
            import importlib
            import agent.pipeline_parts.stream as _st
            importlib.reload(_st)
            check("M2 还原后重新命中",
                  bool(_st.detect_prose_confirm("请你确认后执行。")))

    print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：" + " / ".join(FAILS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
