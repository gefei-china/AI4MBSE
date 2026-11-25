# -*- coding: utf-8 -*-
"""门禁：编排正文只保留最终产物，中间版本完整留在过程区（FINAL_ARTIFACT_ONLY_V1）。

真机 conv 999999952 的正文有 5 个围栏：2 个 SysML（174 行旧版 + 138 行最终版）、
2 棵包树、1 张写入确认单。本门锁：折叠规则纯函数 + 落库/前端两侧都接住 + 变异自证。
"""
# CI-OPTIONAL: 最终产物折叠：纯函数+静态源码门禁，含源码变异自证，暂不进 CI

import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source, read_stream_source_bytes  # noqa: E402  # noqa: E402
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
    print("门禁：最终正文只留最终产物（FINAL_ARTIFACT_ONLY_V1）")
    from agent.pipeline_parts.stream import _collapse_intermediate_artifacts as fold

    print("\n=== P 纯函数 ===")
    text = ("前\n```sysml\npackage OLD { }\n```\n中\n"
            "```\n1 A\n├── B\n└── C\n```\n"
            "```sysml\npackage FINAL { }\n```\n"
            "```json\n{ \"确认单\": \"写入V1\" }\n```\n后")
    out, saved = fold(text)
    check("P1 两个 sysml 只留最后一个",
          out.count("```sysml") == 1 and "package FINAL" in out and "package OLD" not in out,
          repr(out[:200]))
    check("P2 包树 / 确认单也移出正文",
          "├──" not in out and "确认单" not in out, repr(out[:300]))
    check("P3 被折叠的 3 段原文一字不丢进过程区",
          len(saved) == 3 and any("package OLD" in x["text"] for x in saved)
          and any("确认单" in x["text"] for x in saved), "saved=%d" % len(saved))
    check("P4 只有一个 sysml 时不动（零回归）",
          fold("a\n```sysml\nONLY\n```") == ("a\n```sysml\nONLY\n```", []))
    check("P5 无围栏时逐字节原样", fold("普通正文。") == ("普通正文。", []))

    print("\n=== S 源码契约 ===")
    s = read_stream_source()
    check("S1 流式推送前先折叠（不是 done 时才变）",
          s.index("_collapse_intermediate_artifacts(orch_content)") <
          s.index("for _chunk in iter_stream_chunks(orch_content)"))
    check("S2 _ensure_sysml_blocks 补回后再折一次（幂等）",
          s.count("_collapse_intermediate_artifacts(orch_content)") >= 2)
    check("S3 折叠原文落进 card_data.exec.process_artifacts",
          '"process_artifacts": _folded_artifacts' in s)
    js = open(os.path.join(ROOT, "static/js/mods/06-cards.js"), encoding="utf-8").read()
    check("S4 前端过程区渲染折叠块",
          "ex.process_artifacts" in js and "中间产物 " in js)

    print("\n=== M 变异自证：让最后一个 sysml 也不保留，P1 必须翻红 ===")
    fp = os.path.join(ROOT, "agent/pipeline_parts/stream.py")
    orig = read_stream_source_bytes()
    mutated = orig.decode("utf-8").replace(
        "fold = bool(_last_sysml >= 0 and i in sysml_idx and i != _last_sysml)",
        "fold = bool(i in sysml_idx)").encode("utf-8")
    if mutated == orig:
        check("M0 变异锚点存在", False)
    else:
        try:
            open(fp, "wb").write(mutated)
            import importlib
            import agent.pipeline_parts.stream as _st
            importlib.reload(_st)
            o2, _ = _st._collapse_intermediate_artifacts(text)
            check("M1 两个 sysml 都被折叠后 P1 判据翻红",
                  "```sysml" not in o2, repr(o2[:200]))
        finally:
            open(fp, "wb").write(orig)
            import importlib
            import agent.pipeline_parts.stream as _st
            importlib.reload(_st)
            o3, _ = _st._collapse_intermediate_artifacts(text)
            check("M2 还原后最终版本保留", "package FINAL" in o3)

    print("\n结果：%d PASS / %d FAIL" % (PASS, FAIL))
    if FAILS:
        print("失败项：" + " / ".join(FAILS))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
