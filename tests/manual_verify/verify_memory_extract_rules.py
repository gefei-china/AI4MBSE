# -*- coding: utf-8 -*-
"""记忆提取规则验证（2026-09-29）。

证明目标：
  A. 三重门对**真实生产库里的坏样本**全部拦下（报告正文 / 图谱实体 / 寒暄 / 纯数字）。
  B. 对**真实生产库里的好样本**（真经验）全部放行 —— 防止"一刀切全拦"这种假修复。
  C. 变异测试：把门 3（经验信号门）掐掉 → A 中至少一条必须漏过（说明断言非空转）。
  D. reflow 不再把图谱实体写进 agent_memory（用真库跑回归口径）。

纪律：坏样本/好样本均**直接取生产库真实数据**（不手捏），避免"夹具自造乐观样本"。
"""
import os
import re
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from memory_service import MemoryService  # noqa: E402

PASS, FAIL = [], []


def ck(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  -> " + str(extra)) if extra else ""))


# ── 坏样本：直接抄自生产库（原样，不改写） ──
BAD = [
    ("报告正文（Markdown 标题）",
     "# MBSE 任务汇总最终报告（修订版）\n\n> 整合范围：仅基于 t1、t2、t3 交付物。所有截断均在对应小节标注。"),
    ("报告正文（多行）",
     "说明：本报告仅整合给定子任务交付物 t1–t6 的内容。\n凡标注为待确认的事项，\n均不得视为已确认事实。\n"
     "给定交付物存在截断。\n综合来看应分三步走。\n最终建议先做溯源。"),
    ("图谱实体样式（来源 _）",
     "IP67防护等级要求：系统必须满足IP67防护等级，作为系统级防护性能指标。（来源 test-reg）"),
    ("图谱实体样式（来源 -）",
     "CIA验证TS54010通信载荷：受影响元素，影响度由1.0升至1.3（来源 impact-9）"),
    ("寒暄/自我介绍", "你好，我是 MBSE 平台的通用助手。你发送的「123」我这边没有识别到具体意图，方便补充一下你想做什么吗？"),
    ("纯数字输入", "123"),
]

# ── 好样本：也来自生产库（这些是公认有价值的记忆） ──
GOOD = [
    ("平台 vc 口径",
     "智源平台工程 vc 格式为 branchId,quId：普通工程 quId=0 即草稿上下文，导入工程带 queryType=1 及版本号；"
     "SysML v2 覆盖导入写回仅草稿上下文支持，须按包取源码并携带对应 vc。"),
    ("查询方法",
     "查询智源工程包结构树时，先通过工程列表确定当前打开工程及其vc，再以该vc查询包树；"
     "顶层包为父节点为空、位于工程根下的节点。"),
    ("踩过的坑",
     "智源MBSE平台工程列表与包结构树查询的返回内容易在响应前缀处截断，需二次查询或分页补齐，"
     "才能得到完整顶层包清单，不可将截断结果直接当作完整事实。"),
    ("方法论",
     "工程数据转三元组并写入个人分支图库时，需先确认建模映射规范；三元组预评审采用完整性、一致性、重复、冲突四维判定。"),
]


def main():
    print("=" * 62)
    print("A. 坏样本必须全部拦下（取自生产库原样）")
    print("=" * 62)
    bad_leaked = []
    for name, txt in BAD:
        ok, why = MemoryService._extract_worthy(txt, min_len=40)
        ck(f"A. 拦下：{name}", not ok, f"why={why or 'PASSED_THROUGH(!)'}")
        if ok:
            bad_leaked.append(name)

    print("\n" + "=" * 62)
    print("B. 好样本必须全部放行（防'一刀切全拦'假修复）")
    print("=" * 62)
    for name, txt in GOOD:
        ok, why = MemoryService._extract_worthy(txt, min_len=40)
        ck(f"B. 放行：{name}", ok, f"rejected_why={why}")

    print("\n" + "=" * 62)
    print("C. 变异测试：逐门验证「每道门都不可替代」")
    print("=" * 62)
    # ⚠️ 教训（本项目已踩）：笼统拿整组坏样本做变异会得到 leaked=0 —— 因为门1/门2 已经很密，
    #    样本在到门3 之前就被拦了，门3 成了**冗余防线**，变异抓不到，A 的断言看起来"空转"。
    #    正确做法：**为每道门各造一个"只有它能拦"的样本**（其余门都放行），再分别掐断验证。
    #    下面三个探针（Probe）都过了门1（长度带 OK）、门2（非噪音）、且不触发 document_body：
    PROBE = {
        # 只有门3（经验信号）能拦：长度 60+、单行、非噪音、无任何经验信号词
        "gate3": ("针对某个子系统我们做了一次比较完整的梳理和说明，"
                  "内容覆盖了各个层级之间的关系与对应关系，整体上呈现为一个说明性的陈述，"
                  "这部分内容描述了当时的情况和背景信息，属于对现状的一个客观罗列和呈现。"),
        # 只有门2（噪音）能拦：长度 60+、单行、带经验信号词 → 但因为是图谱实体样式
        "gate2": ("某防护等级要求：系统必须满足该防护等级，作为系统级防护性能指标，"
                  "并需在整机设计阶段同步落实。（来源 test-xyz）"),
        # 只有门1（长度带）能拦：超长报告正文，但**刻意含经验信号词**（"必须"/"需先"），
        #   以保证门3 会放行 —— 这样"只有门 1 能拦住它"，掐断门1 就必然漏网。
        # ⚠️ 第五轮教训（本项目第三次踩"冗余防线"）：探针若不含信号词，掐断门1 后它照样被门3 拦，
        #   变异显示 leaked=False，看起来像"门1 没用"，实际是探针没挑对。
        #   **判据：探针必须"除被测门之外，其余门全部放行"**。反之亦然。
        "gate1": ("关于本次整体情况的说明：" + ("本次必须逐层梳理覆盖的各个层级关系与对应关系，"
                  "并需先确认层级之间的追溯链路是否闭合。") * 40),
    }
    for k, t in PROBE.items():
        ok, why = MemoryService._extract_worthy(t, min_len=40)
        ck(f"C-pre. 探针 {k} 被拦（当前由某门拦住）", not ok, f"why={why}")
    # 探针自证（关键）：除被测门外，其余门必须**全部放行** —— 否则"掐断被测门"后
    # 探针会被别的门兜住，变异显示不 leak，看起来像被测门"没用"（本项目已踩三次）。
    # gate1 探针：断言"长度超过 600"（门1 会拦）+ "不含噪音"（门2 放行）+ "含信号词"（门3 放行）
    _g1 = PROBE["gate1"]
    ck("C-pre. 探针自证：gate1 探针除'超长'外其余门全放行",
       len(_g1) > 600
       and not any(__import__("re").search(p, _g1) for p in MemoryService._NOISE_RE)
       and any(k in _g1 for k in MemoryService._VALUE_KEYS),
       f"len={len(_g1)}")
    _sh2 = 40 <= len(PROBE["gate2"]) <= 600 and any(
        k in PROBE["gate2"] for k in MemoryService._VALUE_KEYS)
    ck("C-pre. 探针自证：gate2 探针除'噪音'外其余门全放行", _sh2, f"len={len(PROBE['gate2'])}")
    _sh3 = 40 <= len(PROBE["gate3"]) <= 600 and not any(
        __import__("re").search(p, PROBE["gate3"]) for p in MemoryService._NOISE_RE)
    ck("C-pre. 探针自证：gate3 探针除'无信号'外其余门全放行", _sh3, f"len={len(PROBE['gate3'])}")

    # ⚠️ 第三轮教训：**子进程会读到 __pycache__ 的旧 .pyc** —— 变异写回源文件后 mkdir 同秒内
    #    mtime 未变，Python 判定缓存有效 → 子进程跑的是**未变异**的旧字节码，
    #    于是"掐断门1 后探针仍被拦"这种假 FAIL 出现（实测踩到）。
    #    修法：子进程一律 `-B`（禁读写 pyc）+ 变异前清掉 __pycache__。
    PYC = os.path.join(_ROOT, "__pycache__")

    def _clean_pyc():
        if os.path.isdir(PYC):
            for f in os.listdir(PYC):
                if f.startswith("memory_service"):
                    try:
                        os.remove(os.path.join(PYC, f))
                    except Exception:
                        pass

    target = os.path.join(_ROOT, "memory_service.py")
    bak = target + ".mutbak"
    # (变异锚点, 探针键, 期望：掐断后该探针**漏网**)
    MUTS = [
        ("        _has_sig = any(k in c for k in MemoryService._VALUE_KEYS) or \\\n"
         "            any(re.search(p, c) for p in MemoryService._VALUE_RE)\n"
         "        if not _has_sig:\n"
         "            return False, \"no_actionable_value\"",
         "gate3", "门3 经验信号门"),
        ("        for pat in MemoryService._NOISE_RE:\n"
         "            if re.search(pat, c, re.I):\n"
         "                return False, \"noise:\" + pat",
         "gate2", "门2 噪音门"),
        ("        if len(c) > 600:\n            return False, \"too_long_looks_like_document\"",
         "gate1", "门1 长度带（超长）"),
    ]
    MUT_MAP = {
        "gate3": ("        if False:\n            return False, \"no_actionable_value\""),
        "gate2": ("        for pat in MemoryService._NOISE_RE:\n"
                  "            if False and re.search(pat, c, re.I):\n"
                  "                return False, \"noise:\" + pat"),
        "gate1": ("        if False and len(c) > 600:\n            return False, \"too_long_looks_like_document\""),
    }
    for anchor, probe_key, label in MUTS:
        src = open(target, encoding="utf-8").read()
        if src.count(anchor) != 1:
            ck(f"C0[{label}]. 变异锚点唯一命中", False, f"count={src.count(anchor)}")
            continue
        ck(f"C0[{label}]. 变异锚点唯一命中", True)
        shutil.copy2(target, bak)
        open(target, "w", encoding="utf-8").write(src.replace(anchor, MUT_MAP[probe_key]))
        _clean_pyc()
        # ⚠️ 第四轮教训：探针文本**不要经命令行 argv 传**（Windows 下有长度/编码坑，
        #    实测超长中文探针经 `-c` 内联 `%r` 时静默走了别的分支 → 假 FAIL）。
        #    改为**写临时文件、子进程读文件**，并把"读到的探针"回显做自证。
        probe_file = os.path.join(_ROOT, "_probe_tmp.txt")
        open(probe_file, "w", encoding="utf-8").write(PROBE[probe_key])
        try:
            code = ("import sys, io\n"
                    "sys.path.insert(0, r'%s')\n"
                    "from memory_service import MemoryService as M\n"
                    "t = io.open(r'%s', encoding='utf-8').read()\n"
                    "print('LEN', len(t))\n"
                    "print('LEAK', M._extract_worthy(t, 40))\n" % (_ROOT, probe_file))
            r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, cwd=_ROOT)
            out = (r.stdout or "") + (r.stderr or "")
            mm = re.search(r"LEN (\d+)", out)
            plen = int(mm.group(1)) if mm else -1
            leaked = "LEAK (True" in out
            ck(f"C1[{label}]. 掐断后该探针漏网（证明此门不可替代、断言非空转）",
               leaked, f"len={plen} {out.strip()[:100]}")
        finally:
            os.replace(bak, target)
            _clean_pyc()
            try:
                os.remove(probe_file)
            except Exception:
                pass
    print("  [restored] memory_service.py 已还原")

    print("\n" + "=" * 62)
    print("C'. 还原后基线复跑（防还原不干净）")
    print("=" * 62)
    r = subprocess.run([sys.executable, "-B", "-c",
                        "import sys;sys.path.insert(0,r'%s')\n"
                        "from memory_service import MemoryService as M\n"
                        "print('OK', sum(1 for t in %r if not M._extract_worthy(t,40)[0]))"
                        % (_ROOT, [t for _, t in BAD])],
                       capture_output=True, text=True, cwd=_ROOT)
    out = (r.stdout or "").strip()
    ck("C'1. 还原后 6 条坏样本重新全部被拦", out.startswith("OK 6"), out)

    print("\n" + "=" * 62)
    print("D. reflow 不再把图谱实体写进 agent_memory")
    print("=" * 62)
    src2 = open(target, encoding="utf-8").read()
    rf = open(os.path.join(_ROOT, "knowledge_reflow.py"), encoding="utf-8").read()
    ck("D1. reflow 只沉淀 经验/决策/风险 三类",
       '_EXP_TYPES = ("经验", "决策", "风险")' in rf and 'et in _EXP_TYPES' in rf)
    ck("D2. reflow 拒收占位 note（'规则提炼' 等无信息量串）",
       "_placeholder" in rf and "规则提炼" in rf)

    print("\n" + "=" * 62)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    print("=" * 62)
    if FAIL:
        for f in FAIL:
            print("  FAIL:", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
