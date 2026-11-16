"""自检：任务计划 prompt 的**字段奇偶性**（三处模板必须字段集合恒等）。

口径：裸跑；不启服务、不调 LLM、不写任何库。纯源码静态分析 + 纯函数判据。

为什么需要这个门禁（2026-11-09 实测教训）：
    团队模式的 planner 模板（`agent/pipeline_parts/stream.py` 团队分支）少列了
    `expected_output` / `tools` / `skills` 三个字段 ⇒ `TaskQueue.create_plan` 直接读顶层
    `expected_output` 落库 ⇒ 那条路径的子任务**永久没有验收标准**，且**不报任何错**。
    实测：普通编排 run 990207/990208 填写率 100%，团队模式 run 990209~990216 连续 8 个
    run 全为 0%。定位成本极高（要翻库做分组统计才发现），而修复只需补 3 行字符串。
    本文件把"发现成本"从"翻库统计"降到"跑一条门禁"。

断言分三类（与 `verify_plan_guard.py` 同范式）：
1. **正向**：三处模板都必须存在，且字段集合恒等；
2. **必需字段**：`expected_output` 必须在模板里、且每处都有"每个任务都要写"的硬要求句；
3. **变异自证**：把判据改成"允许缺字段"，必须求值为 False（防假绿）。

⚠️ 锚点数量变化即报错：新增第四处建计划的 prompt 时，必须显式加进来 ——
   否则它就是"没人检查的那一处"，同族漂移会从那里重新长出来。
"""

from __future__ import annotations

import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 三处已知建计划 prompt 的落点（相对路径, 人类可读名）
TEMPLATE_SITES = [
    ("agent/pipeline_parts/stream.py", "流式主路径（普通编排）"),
    ("agent/pipeline_parts/stream.py", "流式主路径（团队模式）"),
    ("workflows/planner.py", "非流式 / Flow 节点"),
]

#: 任务对象必须声明的顶层字段（`required` 是 config 的子字段，单独断言）
REQUIRED_FIELDS = {
    "key", "title", "agent", "deps", "task_type",
    "context", "expected_output", "config", "tools", "skills",
}

#: 模板锚点：键值都是字面量 "t1"，可排除 `"key": it.get(...)` 这类代码构造。
#: `\{{1,2}` 兼容 `workflows/planner.py` 里为 `.format()` 转义成 `{{"tasks"` 的写法。
ANCHOR_RE = re.compile(r'"tasks"\s*:\s*\[\{{1,2}"key"\s*:\s*"t1"')

#: 硬要求句（三处必须一致出现）
HARD_REQ = "每个任务都要写 expected_output"

#: 顶层字段提取（排除 config 的子字段）
FIELD_RE = re.compile(r'"([a-z_]+)"\s*:')
SUB_FIELDS = {"required", "tasks"}

PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % name)
        return True
    FAIL += 1
    FAILED.append(name)
    print("  [FAIL] %s%s" % (name, ("  —— " + detail) if detail else ""))
    return False


def extract_template(src: str, start: int) -> str:
    """从锚点处向后截取一个 JSON 示例（直到 `]}` 或 `]}}`）。

    模板内 `deps` 用 `[]` 且其后跟 `,`，因此 `]}` 只可能出现在示例结尾。
    """
    tail = src[start:]
    end = tail.find("]}")
    if end < 0:
        return tail[:600]
    return tail[: end + 2]


def fields_of(chunk: str) -> set[str]:
    return {f for f in FIELD_RE.findall(chunk) if f not in SUB_FIELDS}


def detect_templates(path: str) -> list[tuple[int, str]]:
    full = os.path.join(REPO, path)
    src = io.open(full, encoding="utf-8").read()
    return [(m.start(), extract_template(src, m.start())) for m in ANCHOR_RE.finditer(src)]


def main() -> int:
    print("=" * 70)
    print("[1] 锚点数：三处建计划 prompt 必须都在（新增第四处必须显式登记）")
    print("=" * 70)

    per_file: dict[str, list[tuple[int, str]]] = {}
    for path, _label in TEMPLATE_SITES:
        if path not in per_file:
            per_file[path] = detect_templates(path)

    total = sum(len(v) for v in per_file.values())
    for path, hits in per_file.items():
        print("    %s → %d 处" % (path, len(hits)))
    check("1.1 锚点总数 = 3（= 已登记的三处）", total == 3,
          "实测 %d 处；若新增了建计划 prompt，请在 TEMPLATE_SITES 登记并同步断言" % total)

    templates: list[tuple[str, str]] = []
    counters: dict[str, int] = {}
    for path, label in TEMPLATE_SITES:
        hits = per_file.get(path) or []
        i = counters.get(path, 0)
        counters[path] = i + 1
        if i >= len(hits):
            check("1.2 %s 存在模板" % label, False, "%s 中未找到模板" % path)
            continue
        pos, chunk = hits[i]
        line_no = io.open(os.path.join(REPO, path), encoding="utf-8").read()[:pos].count("\n") + 1
        templates.append((label, chunk))
        print("    · %s（%s:%d）" % (label, path, line_no))

    print()
    print("=" * 70)
    print("[2] 字段奇偶性：三处模板的顶层字段集合必须恒等")
    print("=" * 70)

    sets = [(label, fields_of(chunk)) for label, chunk in templates]
    for label, fs in sets:
        print("    %-26s %d 个字段" % (label, len(fs)))

    base_label, base = sets[0] if sets else ("", set())
    for label, fs in sets:
        diff_missing = sorted(base - fs)
        diff_extra = sorted(fs - base)
        check("2.x %s 与「%s」字段集合一致" % (label, base_label),
              fs == base,
              "缺少 %s / 多出 %s" % (diff_missing, diff_extra))

    print()
    print("=" * 70)
    print("[3] 必需字段：每处都必须声明完成标准与工具/技能")
    print("=" * 70)

    for label, chunk in templates:
        fs = fields_of(chunk)
        for f in ("expected_output", "tools", "skills", "context", "config", "deps"):
            check("3.x %s 含字段 %s" % (label, f), f in fs)
        check("3.y %s 的 config 声明 required" % label, '"required"' in chunk)

    print()
    print("=" * 70)
    print("[4] 硬要求句：三处都必须明确要求 LLM 产出 expected_output")
    print("=" * 70)

    for path in dict.fromkeys(p for p, _ in TEMPLATE_SITES):
        src = io.open(os.path.join(REPO, path), encoding="utf-8").read()
        # 每处锚点前后各 1600 字符窗口内应能读到硬要求句
        near = 0
        for m in ANCHOR_RE.finditer(src):
            window = src[max(0, m.start() - 200): m.start() + 1600]
            if HARD_REQ in window:
                near += 1
        expect = len(per_file.get(path) or [])
        check("4.x %s：%d/%d 处模板附近有硬要求句" % (path, near, expect),
              expect > 0 and near >= expect)

    print()
    print("=" * 70)
    print("[5] 变异自证：判据必须能翻红（防「假绿」）")
    print("=" * 70)

    # 变异 1：把团队模式模板的 expected_output 抹掉，奇偶性判据必须 False
    mutated = "\n".join(
        line for line in (templates[1][1].splitlines() if len(templates) > 1 else [])
        if "expected_output" not in line)
    parity_mutated = fields_of(mutated) == base
    check("5.1 抹掉 expected_output 后，奇偶性判据应翻红",
          parity_mutated is False,
          "判据仍是 True ⇒ 它根本没在读字段（假绿）")

    # 变异 2：必需字段判据在空模板上必须 False
    check("5.2 空模板的必需字段判据应翻红",
          ("expected_output" in fields_of("")) is False)

    # 变异 3：锚点正则不得误抓代码构造（`"key": it.get(...)`）
    planner_src = io.open(os.path.join(REPO, "workflows/planner.py"), encoding="utf-8").read()
    false_hits = [m for m in ANCHOR_RE.finditer(planner_src)
                  if "it.get" in planner_src[m.start(): m.start() + 60]]
    check("5.3 锚点正则不误抓代码构造的 dict", len(false_hits) == 0,
          "命中 %d 处，说明正则过宽" % len(false_hits))

    print()
    print("=" * 70)
    print("汇总：PASS = %d / FAIL = %d" % (PASS, FAIL))
    if FAILED:
        print("失败项：")
        for f in FAILED:
            print("  -", f)
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
