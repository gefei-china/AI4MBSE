# -*- coding: utf-8 -*-
"""B1-7 门禁：负载感知分派（打分器接线 + 权重/匹配口径标定）。

**背景（对标取证）**：`services/assignment.py` 的 `rank_candidates`/`best_agent` 等
170 行**零调用点**，实测 `agent_tasks.assign_score` 与 `load_snapshot` **0/222 有值**
⇒ "这次为什么派给它"在数据上**无法回答**（文章工程要点③要的正是这个）。

**为什么不直接按原权重接线**（先量后调）：
  ① `SYNONYM_MAP` 只有 6 组，覆盖真库 76 个 capability 词条的 **9 个（12%）**；
     8 视图系列（`SysML v2 状态视图代码` 等）**全部未覆盖** ⇒ 那批任务
     `capability_match` **恒 0**，`0.6*capability_match` 整项塌缩。
  ② `embed_fn` 兜底在当前 embedding（bigram TF 降级，4096 维仅 3~12 非零位）下
     **无区分度**：实测「生成状态机视图」~「SysML v2 状态视图代码」= **0.236**，
     而与无关的「报告生成」**同为 0.236**，都低于 `EMBED_THRESHOLD=0.55`。
  ⇒ 原样接线 = 接一个"专长项恒 0"的残缺品。故先改匹配手段（字符 bigram 重合度），
    再定权重，最后接线。标定结果见下方 D 组。

**★ 2026-10-26 D/E 组重标定（为什么改，改前是什么）**：
  原 D 组用 6 条「8 视图任务」用例，期望 top-1 命中 8 个**视图生成 Agent**。但这 8 个 Agent
  已于 2026-10-14（commit 07ffea0「B4-4 下线视图 Agent 双轨」）按取证下线
  （视图能力已由 `sysml_view_generation_*` skill 承载；它们同时在抢 `view_expansion` 的路由）
  ⇒ 候选池里根本没有它们 ⇒ D1 恒 **0/6**、G1 变异自证同步失效（现行 0/6 vs 变异 0/6，
  **抓不住差异**）⇒ 门禁对着**已退役的架构**要绿。
  另一层约束：`capabilities` 受 `agent/registry.py` 的 `_CAPABILITIES_ENUM`（**23 词白名单**）
  硬约束，视图专属词（状态机/活动图/时序图/IBD）**不在枚举内**
  ⇒ 任何配置都做不到让「生成状态机视图」top-1 命中 `view_expansion`
  （实测最高只到 0.20，输给 `report_generation` 的 0.20）⇒ 这不是调参能解决的。
  ⇒ 改法：D 组换成**现行 active Agent 自己的能力词**构成的能力域用例（9 条，实测 9/9），
    并新增 D0（保护"视图 Agent 保持退役"这一取证决策）与 D1b（防"并列靠字母序"的假命中）；
    E 组锚点从已退役的「状态机视图生成」换成在池的 `requirement_structuring`，
    并新增 E0 —— **锚点不在池里就判红，不许静默跳过**（原 E 组被 `if _state:` 整组跳过，
    门禁却报"通过"：判据空转的症状是"少了几条断言"，不是"红了"）。

**本脚本守什么**：
  [A] 配置组存在且权重可覆盖（可回退）
  [B] 接线存在且**包在 try 里**（分派失败不阻断编排）
  [C] **不改派 planner 的选择**（弱信号不改强信号的决定）—— 这条最容易被"优化"掉
  [D] 标定效果：能力域任务 top-1 命中（现行 active 候选池实跑）+ 视图 Agent 保持退役
  [E] 区分度：真配对 > 错配对（防止权重调好后反而无区分度）
  [F] 留痕字段真的进 config（assign_score / load_snapshot / capability_match）
  [G] 变异自证：抽掉 bigram 匹配这一层 ⇒ D 组命中数必须下降

**只读**：真库 `mode=ro`；变异在内存副本做，**不写回源文件**。

**运行**：`.venv/Scripts/python.exe -X utf8 tools/verify/verify_load_aware_assign.py`
"""
import io
import os
import re
import sys
import json
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ASSIGN = os.path.join(ROOT, "services", "assignment.py")
INTEG = os.path.join(ROOT, "services", "assignment_integration.py")
STREAM = os.path.join(ROOT, "agent", "pipeline_parts", "stream.py")
CONFIG = os.path.join(ROOT, "core", "config.py")
PROD_DB = os.path.join(ROOT, "mbse.db")

_ok = 0
_fail = 0
_muts = []


def check(name, cond, detail=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print("  [PASS] %s" % name)
    else:
        _fail += 1
        print("  [FAIL] %s  %s" % (name, detail))


sys.path.insert(0, ROOT)
from services import assignment as A  # noqa: E402
from services.assignment_integration import _assign_load_aware  # noqa: E402

a_src = io.open(ASSIGN, encoding="utf-8").read()
i_src = io.open(INTEG, encoding="utf-8").read()
s_src = io.open(STREAM, encoding="utf-8").read()
c_src = io.open(CONFIG, encoding="utf-8").read()

print("=== A. 配置组存在且可回退 ===")
# ⚠️ 定位要**锚在变量赋值处**，不能用 `c_src.split("CONFIG_SCHEMA")` 切段 ——
#    文件**顶部的模块 docstring 里就出现了 "CONFIG_SCHEMA"**（讲三层配置来源），
#    裸 split 会从那个 docstring 位置切，把 DEFAULT_CONFIG（35~987 行）整段丢掉
#    ⇒ 后面所有"默认值存在吗"的断言全部空转。
#    （这是本门禁第一版的真实 bug：4 条 A3 全红，而产品代码是对的。）
# 正确做法：按 `DEFAULT_CONFIG = {` 与 `CONFIG_SCHEMA = {` 两个**赋值点**切段。
_def_start = c_src.index("DEFAULT_CONFIG = {")
_sch_start = c_src.index("CONFIG_SCHEMA = {")
check("A0b CONFIG_SCHEMA 赋值点在 DEFAULT_CONFIG 之后", _def_start < _sch_start)
_def_part = c_src[_def_start:_sch_start]
_sch_seg = c_src[_sch_start:]
check("A1 DEFAULT_CONFIG 有 agent_assignment",
      re.search(r'"agent_assignment":\s*\{', _def_part) is not None)
check("A2 CONFIG_SCHEMA 有该组",
      re.search(r'"agent_assignment":\s*\{', _sch_seg) is not None)
_m = re.search(r'"agent_assignment":\s*\{', _def_part)
check("A0 DEFAULT_CONFIG 段定位到 agent_assignment 组", _m is not None)
_def_seg = _def_part[_m.end():_m.end() + 1200] if _m else ""
for k in ("enabled", "weight_capability", "weight_load", "weight_history"):
    check("A3.%s 在 DEFAULT_CONFIG 段有默认值" % k, '"%s"' % k in _def_seg,
          "未在 agent_assignment 组里找到 %s" % k)
check("A3b CONFIG_SCHEMA 段登记了该组（REST 元信息用）",
      re.search(r'"agent_assignment":\s*\{', _sch_seg) is not None)
check("A4 权重之和为 1.0（可整体缩放而不改相对关系）",
      abs(A.WEIGHT_CAPABILITY + A.WEIGHT_LOAD + A.WEIGHT_HISTORY - 1.0) < 1e-9,
      "sum=%.3f" % (A.WEIGHT_CAPABILITY + A.WEIGHT_LOAD + A.WEIGHT_HISTORY))
check("A5 成功率权重已从 0.1 调高（实测区分度最明显）",
      A.WEIGHT_HISTORY >= 0.2, "got=%.2f" % A.WEIGHT_HISTORY)
check("A6 权重走配置读取（_weights 函数存在）", "def _weights(" in a_src)

print("\n=== B. 接线存在且被 try 包住 ===")
check("B1 stream.py 引入了接线模块",
      "from services.assignment_integration import _assign_load_aware" in s_src)
check("B2 接线调用包在 try 里（失败不阻断编排）",
      re.search(r"try:\s*\n\s*from services\.assignment_integration import _assign_load_aware"
                r"\s*\n\s*plan = _assign_load_aware\(plan\)\s*\n\s*except Exception",
                s_src) is not None)
check("B3 总开关存在（关=完全退回改动前）",
      'agent_assignment", "enabled"' in s_src)
check("B4 接线在 planner 产出计划之后、落库之前",
      s_src.index("_assign_load_aware(plan)") > s_src.index('plan = [t for t in ((data or {}).get("tasks")')
      and s_src.index("_assign_load_aware(plan)") < s_src.index("TaskQueue.create_plan(conn, run_id, plan"),
      "接线位置不对（在解析前或落库后都失去意义）")

print("\n=== C. 不改派 planner 的选择（核心纪律）===")
check("C1 模块明写了『不改派』立场", "不推翻" in i_src and "不是\"决策者\"" in i_src)
# 真跑：给的 plan 里 agent 名不在候选池/或不在库中时，必须原样保留
_plan = [{"key": "t1", "title": "生成状态机视图", "agent": "状态机视图生成", "deps": []},
         {"key": "t2", "title": "不存在的活", "agent": "根本不存在Agent", "deps": []}]
_out = _assign_load_aware(_plan)
check("C2 未知 Agent 名原样保留（不静默换掉，掩盖数据不一致）",
      _out[1]["agent"] == "根本不存在Agent", _out[1]["agent"])
check("C3 已知任务也不被改派（只留痕）",
      _out[0]["agent"] == "状态机视图生成", _out[0]["agent"])
check("C4 返回新对象不改原 plan", _plan[0].get("config") is None,
      "原地改了入参 ⇒ 编排层若重试会用脏 plan")
check("C5 源码里没有直接改写 agent 名的赋值",
      not re.search(r'\["agent"\]\s*=\s*(?!str\(t)', i_src.split("def _assign_load_aware")[1][:2500]
                    if "def _assign_load_aware" in i_src else ""),
      "检测到改派赋值")

print("\n=== D. 标定效果（现行 active 候选池实跑）===")
# ★ 2026-10-26 重标定（根因见文件头「为什么 D 组要重标」）：
#   原 6 条用例锚在 8 个**视图生成 Agent**上，而它们已于 2026-10-14
#   （commit 07ffea0「B4-4 下线视图 Agent 双轨」）按取证下线 ⇒ 候选池里根本没有它们
#   ⇒ D1 恒 0/6、G1 变异自证同步失效（现行 0/6 vs 变异 0/6，抓不住差异）。
#   现改为「在现行词表下，各能力域任务 top-1 命中对应 Agent」+「视图域交给意图路由」。
_RETIRED_VIEW_AGENTS = ("需求视图生成", "结构视图生成", "交互视图（IBD）生成", "用例视图生成",
                        "活动图生成", "状态机视图生成", "参数视图生成", "顺序视图（时序图）生成")
if os.path.exists(PROD_DB):
    _c = sqlite3.connect("file:%s?mode=ro" % PROD_DB, uri=True)
    _c.row_factory = sqlite3.Row
    _ags = [(r["name"], json.loads(r["capabilities"] or "[]"))
            for r in _c.execute("SELECT name, capabilities FROM agents WHERE status='active'")]
    _c.close()
    _active_names = {n for n, _ in _ags}
    print("  候选 Agent: %d 个" % len(_ags))
    # D0 保护退役决策：视图生成 Agent 不得回到 active 池
    #   （它们会抢 view_expansion 的路由 —— 2026-10-14 取证：视图能力已由
    #    sysml_view_generation_* skill 承载，Agent 侧只做单轨）
    _still_active = [n for n in _RETIRED_VIEW_AGENTS if n in _active_names]
    check("D0 视图生成 Agent 仍处退役态（不在 active 池）",
          not _still_active, "又 active 了：%s" % _still_active)

    # D1 标定：各能力域任务 top-1 命中对应 Agent
    #   ⚠️ 用例必须是**改述**（不是照抄能力词），否则命中由「词表层」给出 1.0/0.8，
    #      bigram 层就不承重 ⇒ G 组变异自证会**空转**（变异体照样全中）。
    #      D1c 专门守这条，防止后人"简化"用例时把自证悄悄弄废。
    cases = [("把需求里的条目逐条理清", "requirement_structuring"),
             ("读懂这段自然语言并澄清其中的歧义", "requirement_analysis"),
             ("把结论装订成一份可交付的稿子并导出为文档", "report_generation"),
             ("检查各部分描述是否前后一致并给出合理性评价", "review"),
             ("核对模型中的错误并按优先级修好", "model_validation_repair"),
             ("给出几种候选方案并逐一比较取舍", "多方案生成"),
             ("讲清两种方法论在概念上的差别与适用路线", "knowledge_qa"),
             ("沿依赖关系逐层遍历并产出影响图", "impact")]
    _hit = 0
    _margins = []
    for _t, _want in cases:
        _sc = sorted(((A.capability_match(_caps, _t), _n) for _n, _caps in _ags), reverse=True)
        _ok_top = _sc and _sc[0][1] == _want
        _hit += 1 if _ok_top else 0
        _m = (_sc[0][0] - _sc[1][0]) if len(_sc) > 1 else _sc[0][0]
        _margins.append((_m, _ok_top, _t))
        print("    %-24s → %-24s %.2f margin=%.2f %s"
              % (_t[:22], _sc[0][1] if _sc else "-", _sc[0][0] if _sc else 0, _m,
                 "✓" if _ok_top else "✗"))
    check("D1 能力域任务 top-1 命中 ≥ 7/8", _hit >= 7, "只中 %d/%d" % (_hit, len(cases)))
    # D1b 防「并列靠字母序」的假命中：命中还不够，得**明显领先**才算真的分得开
    _clear = sum(1 for _m, _ok, _t in _margins if _ok and _m >= 0.1)
    _ties = [t for m, ok, t in _margins if ok and m <= 0]
    check("D1b 命中项领先幅度 ≥0.1 的至少 7 个，且**无并列**（并列即靠字母序取胜）",
          _clear >= 7 and not _ties,
          "清晰 %d 个，并列 %s" % (_clear, _ties))
    # D1c 标定用例**不触发词表层**（层①给 1.0/0.8）——否则 bigram 层不承重，
    #      G 组变异自证会空转（变异体照样全中，自证等于没做）
    _wl_bad = []
    for _t, _want in cases:
        for _n, _caps in _ags:
            for _cap in _caps:
                _norm = A.normalize_capability(_cap)
                for _v in [_norm] + list(A.SYNONYM_MAP.get(_norm, [])):
                    if _v and _v in _t:
                        _wl_bad.append("%s:『%s』含触发串『%s』" % (_t[:12], _n, _v))
    check("D1c 标定用例不触发词表层（bigram 必须承重，否则变异自证空转）",
          not _wl_bad, "；".join(_wl_bad[:3]))
    # D1d 已知天花板（如实记录，不判红）：视图类词不在 23 词枚举内
    #   ⇒ 打分器对视图任务**不区分**（实测 top-1 是 report_generation 0.20，view_expansion 0.00）
    #   ⇒ 视图任务的正确去向由**意图路由**负责（view_expansion 的 intent_keywords 含
    #     生成活动图/生成用例视图/生成状态机/生成时序图/生成IBD/生成参数视图），
    #     负载感知分派只留痕、不改派（C 组）⇒ 这里如实标注，不假装自己能分。
    _v_sc = sorted(((A.capability_match(_c2, "生成状态机视图"), _n2) for _n2, _c2 in _ags),
                   reverse=True)
    print("  [INFO] 视图任务「生成状态机视图」打分 top-1=%s(%.2f) —— 视图域由意图路由负责"
          % (_v_sc[0][1] if _v_sc else "-", _v_sc[0][0] if _v_sc else 0))

    # D2 原词表覆盖率（本条守护"为什么不能只用词表"这个前提）
    _allcaps = set()
    for _n, _caps in _ags:
        _allcaps.update(_caps)
    _cov = set(A.SYNONYM_MAP) | {v for vs in A.SYNONYM_MAP.values() for v in vs}
    check("D2 词表覆盖率确实低（前提成立，解释为何新增 bigram 匹配）",
          len(_allcaps & _cov) < len(_allcaps) * 0.3,
          "覆盖率已变高（%d/%d）⇒ docstring 的论证需更新" % (len(_allcaps & _cov), len(_allcaps)))
else:
    print("  [SKIP] 未找到真库")

print("\n=== E. 区分度（真配对 vs 错配对）===")
# ★ 2026-10-26 换锚点：原锚点写死「状态机视图生成」，而它 2026-10-14 已退役
#   ⇒ `_state` 为空 ⇒ 整个 E 组被 `if _state:` **静默跳过**，门禁却报"通过"
#   （判据空转的典型：症状是"少了几条断言"，不是"红了"）。故换现行 active 锚点，
#   并加 E0 守卫：锚点 Agent 不在池里 ⇒ 直接判红，不许静默跳过。
_E_ANCHOR = "requirement_structuring"
_state = [c for n, c in _ags if n == _E_ANCHOR]
check("E0 锚点 Agent 在 active 池内（防空转：锚点不存在时整组须判红而非跳过）",
      bool(_state), "锚点 %s 不在候选池 ⇒ E 组会静默空转" % _E_ANCHOR)
if _state:
    _pos = A.bigram_overlap(_state[0], "把需求里的条目逐条理清")
    _neg = A.bigram_overlap(_state[0], "把这张幻灯片排版调好看")
    print("  %s ~「把需求里的条目逐条理清」= %.3f｜~「把这张幻灯片排版调好看」= %.3f"
          % (_E_ANCHOR, _pos, _neg))
    check("E1 真配对高于错配对", _pos > _neg, "pos=%.3f neg=%.3f" % (_pos, _neg))
    check("E2 真配对过门槛（能被 capability_match 采用）",
          A.capability_match(_state, "把需求里的条目逐条理清") > 0)
    check("E3 错配对被门槛滤掉（不误判为有关）",
          A.bigram_overlap(_state, "把这张幻灯片排版调好看") < A.BIGRAM_MIN_OVERLAP,
          "错配对 %.3f 过了门槛 %.3f" % (_neg, A.BIGRAM_MIN_OVERLAP))
check("E4 噪声门槛已设（0< x <1）", 0 < A.BIGRAM_MIN_OVERLAP < 1)

print("\n=== F. 留痕字段真的进 config ===")
_p2 = [{"key": "t1", "title": "需求条目化与规格化", "agent": "requirement_structuring", "deps": []}]
_o2 = _assign_load_aware(_p2)
_cfg = _o2[0].get("config") or {}
print("  config: %s" % {k: v for k, v in _cfg.items() if k != "load_snapshot"})
check("F1 config 写入 assign_score", "assign_score" in _cfg)
check("F2 config 写入 load_snapshot（含 running/max_concurrency/success_rate）",
      isinstance(_cfg.get("load_snapshot"), dict)
      and {"running", "max_concurrency", "success_rate"} <= set(_cfg["load_snapshot"]))
check("F3 config 写入 capability_match（可解释性）", "capability_match" in _cfg)
check("F4 原 config 若为 JSON 字符串也被正确合并",
      _assign_load_aware([{"key": "t1", "title": "需求条目化", "agent": "requirement_structuring",
                           "deps": [], "config": '{"a":1}'}])[0]["config"].get("a") == 1)

print("\n=== G. 变异自证（抽掉 bigram 匹配这一层）===")
# ⚠️ 曾试过"把 weight_capability 置 0"，但 D1 断言的是 **`capability_match()` 的排序**，
#    而 `capability_match` **不读权重**（权重只在 `_score` 里用）⇒ 改权重完全动不了 D1，
#    命中数当然不变。**变异必须打在 D 组真正依赖的那段逻辑上。**
# 本次变异：让 `capability_match` 跳过第 ② 层 bigram 匹配（回到"只有词表"）
#   —— 那正是 2026-10-13 之前的形态（词表覆盖 12% ⇒ 8 视图全 0）。
_big = "    _ov = bigram_overlap(caps, task_text)"
if _big not in a_src:
    print("  [VACUOUS] 锚点不在源码里，自证中止")
else:
    _mut = a_src.replace(_big, "    _ov = 0.0  # MUTATION: 跳过 bigram 匹配（回到只有词表）")
    _ns = {"__name__": "_mut_assign"}
    try:
        exec(compile(_mut, "<mutant>", "exec"), _ns)
        _mA = _ns
        _hits = 0
        _zeros = 0
        for _t, _want in cases:
            _sc = sorted(((_mA["capability_match"](_caps, _t), _n) for _n, _caps in _ags), reverse=True)
            if _sc and _sc[0][0] == 0:
                _zeros += 1
            if _sc and _sc[0][1] == _want:
                _hits += 1
        print("  变异体下能力域 top-1 命中: %d/%d（其中 %d 例匹配度直接归 0）"
              % (_hits, len(cases), _zeros))
        check("G1 变异体命中率显著低于现行实现（bigram 匹配确实在承重）",
              _hits < _hit, "变异体 %d/%d vs 现行 %d/%d —— D1 抓不住变异，自证无效"
              % (_hits, len(cases), _hit, len(cases)))
        check("G2 变异体确实出现匹配度归 0 的用例（词表覆盖不到的直接塌成 0）",
              _zeros > 0, "无一例归 0 ⇒ 变异没生效或词表已够用（需重新评估）")
        _muts.append(("M1", "capability_match 跳过 bigram 层（回到只有 6 组词表）"))
        print("  [SELF-PROOF] M1 抽掉 bigram 匹配 —— 能力域 top-1 命中率下降，"
              "证明该层是主要区分来源（改前它们全靠词表、恒为 0）")
    except Exception as e:
        check("G1 变异体执行成功", False, str(e))

print("\n" + "=" * 60)
print("常态断言：%d 条，%d 通过 / %d 失败" % (_ok + _fail, _ok, _fail))
print("变异自证：%d 组" % len(_muts))
for mid, desc in _muts:
    print("  - %s %s" % (mid, desc))
print("结论：%s" % ("门禁通过" if _fail == 0 and _muts else "门禁未通过"))
sys.exit(0 if (_fail == 0 and _muts) else 1)