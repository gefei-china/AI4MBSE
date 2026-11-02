"""最终正文的「中间版本代码收敛」（2026-11-02，米爸反馈）。

需求原话：「输出的代码块总是包括多份数据，视图也是，逻辑上只需要承载最后确定的代码，
不应该把中间环节的代码放在这里，如果需要在中间环节呈现，应该在其他部分展示」。

── 问题形态（真库实证，conv 9999 / msg 5689，32308 字）────────────────────
自动编排的各子任务交付物**各自带一份完整 SysML 模型代码**（t1 修正版 v1 / t2 修正版 v2 /
t3 修正版片段 v3），汇总（LLM 整合或拼接降级）原样带入最终正文 ⇒ 用户在最终输出里看到
3 份 ```sysml 代码块 + 3 段重复的诊断章节。中间版本在**子任务执行卡**里本就可见
（`_procS.subOutput` → `card.liveOutput`），最终正文重复粘贴只增加噪音。

── 修法 ────────────────────────────────────────────────────────────────
对最终正文做一次**呈现层收敛**：同一语言族内、判定为「同一交付物的演进版本」的
围栏代码块，只保留**最后一份**（= 最后确定的版本），早期版本替换为一行指引
（指向子任务执行卡与下文最终版）。**一字代码不删**——只是不再在正文重复展示。

判据（宁可漏判、不可误杀）：
  · 完全相同（空白归一化后）⇒ 重复，收敛；
  · SysML 族（sysml/kerml…）：**根 package 名相同** ⇒ 同一模型的迭代版本，收敛；
    （package 名是 SysML 交付物的身份标识；不同视图/不同模型不会同名）
  · 其它语言：相似度 ≥ 0.60（difflib）或一方被另一方包含 ⇒ 演进版本，收敛；
  · 仅一块、或判据不命中 ⇒ 原样保留，绝不误杀合法的不同代码块。

链式判定：t1≈t2、t2≈t3 但 t1 vs t3 相似度可能不足 ⇒ 从后往前走，
每个早期块与「保留块」或「相邻块」比较，相邻链可传递。

挂钩点（三处，见各自调用注释）：
  1. workflows/planner.py::_summarize_plan —— LLM 整合输出后 + 拼接降级后；
  2. agent/pipeline_parts/stream.py —— 流式路径收尾（reflection/质量门禁之后、
     流式推送与 _ensure_sysml_blocks 之前；reflection 可能整体重写正文，必须最后再收一次）；
  3. 非流式路径经 run_planner_plan → _summarize_plan 已覆盖，无需另挂。
"""
import re
from difflib import SequenceMatcher

# 围栏代码块：```lang\n ... ```（非贪婪；正文是模型/模板生成的 markdown，无嵌套围栏场景）
_FENCE_RE = re.compile(r"```([\w.+\-]*)[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.S)

# SysML/KernelML 语言标签归一族（大小写不敏感）
_SYSML_LANGS = {"sysml", "sysmlv2", "sysml_v2", "sysmlv1", "kerml", "kerml2", "kermlv2"}

# 早于该长度的小片段不参与收敛（一两行的片段重复无伤大雅，误杀风险 > 收益）
_MIN_BLOCK_CHARS = 400

# 其它语言的「演进版本」相似度阈值。
# ⚠️ 实测教训（本模块自测 case4）：不同 SysML 模型因**模板化样板**（同类 comment/part 写法）
# 相似度可以 >0.9 ⇒ SysML 族**绝不用相似度兜底**（签名不同就是不同交付物）；
# 其它语言样板化同样普遍，门槛取 0.75 高位，且要求同语言标签。

_NOTE = ("> ▦ 中间版本代码已收敛，此处不再重复展示：最终确定版见下文代码块；"
         "各中间版本完整内容可在上方对应子任务执行卡中查看。")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _lang_family(lang: str) -> str:
    l = (lang or "").strip().lower()
    if l in _SYSML_LANGS:
        return "sysml"
    return l or "plain"


def _sysml_signature(code: str) -> str:
    """SysML 交付物身份：首个 `package 'X'`；没有 package 则首个 `xxx def 'Y'`。
    同名 ⇒ 视为同一模型的迭代版本（判据来自需求方语义：正文只承载最后确定的代码）。"""
    m = re.search(r"\bpackage\s+'([^']+)'", code) or re.search(r'\bpackage\s+"([^"]+)"', code)
    if m:
        return "pkg:" + m.group(1)
    m = re.search(r"\b(?:part|requirement|action|attribute|port|interface|use\s+case|"
                  r"activity|state|constraint|item|connection)\s+def\s+'([^']+)'", code)
    if m:
        return "def:" + m.group(1)
    return ""


def _similar(a: str, b: str) -> float:
    # 长文本全量比对太贵：截前 2 万字符（版本比对足够）
    return SequenceMatcher(None, a[:20000], b[:20000]).ratio()


def _is_interim(earlier: str, later: str, family: str) -> bool:
    """earlier 是否为 later 的中间版本。判据刻意保守：宁可漏判，不可误杀。"""
    ne, nl = _norm(earlier), _norm(later)
    if not ne or not nl:
        return False
    if ne == nl:
        return True                      # 完全相同（空白归一化后）
    if len(ne) < _MIN_BLOCK_CHARS or len(nl) < _MIN_BLOCK_CHARS:
        return False
    if ne in nl or nl in ne:
        return True                      # 一方被另一方包含 = 子集/超集演进
    if family == "sysml":
        # SysML 族：签名（根 package/def）相同 ⇒ 同一模型的迭代版本。
        # ⚠️ 不做相似度兜底——不同模型的样板结构相似度可以极高（自测 case4 实证）。
        se, sl = _sysml_signature(earlier), _sysml_signature(later)
        return bool(se and sl and se == sl)
    if family == "plain":
        return False                     # 无标签块最保守：只认相同/包含
    return _similar(ne, nl) >= _SIM_OTHER


def dedupe_interim_code_blocks(text: str):
    """收敛最终正文中的中间版本代码块。返回 (new_text, removed_count)。

    设计约束：
      · 绝不抛异常（调用点都在编排收尾主链上，呈现层失败不能影响交付）；
      · 每个语言族独立处理；族内只保留最后一块，早期块按判据替换为指引行；
      · 替换不改变块外任何文本（章节标题/摘要原样保留 —— 需求只针对代码块）。
    """
    try:
        s = str(text or "")
        if "```" not in s:
            return s, 0
        blocks = list(_FENCE_RE.finditer(s))
        if len(blocks) < 2:
            return s, 0
        # 分语言族（保序）
        groups = {}
        for i, m in enumerate(blocks):
            groups.setdefault(_lang_family(m.group(1)), []).append(i)
        drop = {}   # block index -> note text
        for fam, idxs in groups.items():
            eligible = [i for i in idxs
                        if len(_norm(blocks[i].group(2))) >= _MIN_BLOCK_CHARS]
            if len(eligible) < 2:
                continue
            keep = eligible[-1]          # 最后一块 = 最后确定的版本
            for j in range(len(eligible) - 2, -1, -1):
                cur = eligible[j]
                nxt = eligible[j + 1]
                ref = blocks[keep].group(2)
                if _is_interim(blocks[cur].group(2), ref, fam) or \
                   _is_interim(blocks[cur].group(2), blocks[nxt].group(2), fam):
                    drop[cur] = _NOTE
        if not drop:
            return s, 0
        out, pos, n = [], 0, 0
        for i, m in enumerate(blocks):
            if i in drop:
                out.append(s[pos:m.start()])
                out.append(drop[i])
                pos = m.end()
                n += 1
        out.append(s[pos:])
        return "".join(out), n
    except Exception:
        return text, 0
